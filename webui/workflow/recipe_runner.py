from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from hardware.gamry_client import run_gamry_step
from hardware.motion_controller import home_axes_internal, move_to_xyz
from hardware.rde_controller import send_rpm, stop_rde
from hardware.rinse_controller import run_rinse_cycle
from hardware.rotation_controller import send_rotation_text
from workflow.data_manager import (
    append_log,
    create_run_workspace,
    create_sample_workspace,
    mark_run_aborted,
    mark_run_complete,
    mark_run_failed,
    prepare_protocol_outputs,
    save_protocol_snapshot,
)
from workflow.protocol_loader import load_protocol
from workflow.run_plan_loader import load_run_plan, validate_run_plan_payload
from workflow.state import (
    AutomationAbortRequested,
    automation_is_running,
    check_abort,
    clear_abort,
    fail_automation,
    finish_automation,
    get_abort_event,
    request_abort,
    reserve_automation,
    start_automation,
    set_automation_state,
)


class RecipeRunnerError(RuntimeError):
    pass


def sleep_interruptible(seconds: float, message: str = "Abort requested during wait.") -> None:
    seconds = float(seconds)

    if seconds <= 0:
        return

    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        check_abort(message)
        time.sleep(0.1)


def protocol_outputs_by_step(protocol_outputs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {
        int(item["step_index"]): item
        for item in protocol_outputs
    }


def sample_label(sample: dict[str, Any], sample_index: int) -> str:
    return str(sample.get("label") or sample.get("sample_id") or f"Sample {sample_index}")


def set_sample_rpm(run_dir: Path, sample: dict[str, Any], label: str) -> None:
    rpm = int(sample.get("rpm", 0))

    if rpm <= 0:
        append_log(run_dir, f"{label}: RDE spin skipped because rpm <= 0.")
        return

    set_automation_state(step=f"Set RDE RPM to {rpm} for {label}")
    append_log(run_dir, f"{label}: setting RDE RPM to {rpm}.")
    send_rpm(rpm)


def stop_sample_rpm(run_dir: Path, label: str) -> None:
    set_automation_state(step=f"Stop RDE after {label}")

    try:
        stop_rde(None)
        append_log(run_dir, f"{label}: RDE stopped.")
    except Exception as exc:
        append_log(run_dir, f"{label}: RDE stop failed: {exc}")
        raise


def run_protocol_for_sample(
    run_dir: Path,
    sample_dir: Path,
    sample_index: int,
    sample: dict[str, Any],
    protocol: dict[str, Any],
) -> None:
    label = sample_label(sample, sample_index)
    protocol_snapshot = save_protocol_snapshot(run_dir, protocol)
    append_log(run_dir, f"{label}: protocol snapshot saved at {protocol_snapshot}.")

    protocol_outputs = prepare_protocol_outputs(
        run_dir=run_dir,
        sample_dir=sample_dir,
        sample_index=sample_index,
        protocol=protocol,
    )

    outputs_by_step = protocol_outputs_by_step(protocol_outputs)

    for step_index, step in enumerate(protocol.get("steps", []), start=1):
        if not bool(step.get("enabled", True)):
            append_log(run_dir, f"{label}: skipping disabled EChem step {step_index}.")
            continue

        check_abort("Abort requested before EChem step.")

        step_name = str(step.get("name") or f"Step {step_index}")
        technique = str(step.get("technique") or "echem")
        output_record = outputs_by_step.get(step_index)

        if output_record is None:
            raise RecipeRunnerError(f"No output path prepared for step {step_index}: {step_name}")

        outputs = output_record["outputs"]

        set_automation_state(
            step=f"{label} - EChem {step_index}: {technique} / {step_name}"
        )
        append_log(run_dir, f"{label}: starting EChem step {step_index}: {technique} / {step_name}.")

        result = run_gamry_step(
            step=step,
            outputs=outputs,
            run_dir=run_dir,
            sample_id=sample.get("sample_id"),
        )

        append_log(run_dir, f"{label}: finished EChem step {step_index}: {result}.")


def run_rinse_after_sample(run_dir: Path, label: str) -> None:
    set_automation_state(step=f"Rinse after {label}")
    append_log(run_dir, f"{label}: rinse started.")

    rinse_result = run_rinse_cycle()

    append_log(run_dir, f"{label}: rinse finished: {rinse_result}.")


def run_sample(
    run_dir: Path,
    sample: dict[str, Any],
    sample_index: int,
    repetition: int,
    repetitions: int,
) -> None:
    label = sample_label(sample, sample_index)
    position = sample["position"]

    set_automation_state(
        step=f"Rep {repetition}/{repetitions} - Move to {label}"
    )
    append_log(run_dir, f"Rep {repetition}/{repetitions}: moving to {label}.")

    sample_dir = create_sample_workspace(run_dir, sample, sample_index)

    move_to_xyz(
        x=int(position["x"]),
        y=int(position["y"]),
        z=int(position["z"]),
        abort_event=get_abort_event(),
    )

    rotation_command = str(sample.get("rotation_command", "") or "").strip()

    if rotation_command:
        set_automation_state(step=f"Rep {repetition}/{repetitions} - Rotation command for {label}")
        rotation_ack = send_rotation_text(rotation_command)
        append_log(run_dir, f"{label}: rotation command {rotation_command}, ack={rotation_ack}.")

    set_sample_rpm(run_dir, sample, label)

    stabilization_s = float(sample.get("stabilization_s", 0))

    if stabilization_s > 0:
        set_automation_state(
            step=f"Rep {repetition}/{repetitions} - Stabilize {label} for {stabilization_s}s"
        )
        append_log(run_dir, f"{label}: stabilization wait {stabilization_s}s.")
        sleep_interruptible(stabilization_s, "Abort requested during sample stabilization.")

    protocol_name = str(sample.get("protocol", "ocp_only") or "ocp_only")
    protocol = load_protocol(protocol_name)

    append_log(run_dir, f"{label}: loaded protocol {protocol_name}.")

    run_protocol_for_sample(
        run_dir=run_dir,
        sample_dir=sample_dir,
        sample_index=sample_index,
        sample=sample,
        protocol=protocol,
    )

    post_echem_wait_s = float(sample.get("post_echem_wait_s", 0))

    if post_echem_wait_s > 0:
        set_automation_state(
            step=f"Rep {repetition}/{repetitions} - Post-EChem wait for {label}"
        )
        append_log(run_dir, f"{label}: post-EChem wait {post_echem_wait_s}s.")
        sleep_interruptible(post_echem_wait_s, "Abort requested during post-EChem wait.")

    stop_sample_rpm(run_dir, label)

    if bool(sample.get("rinse_after", False)):
        run_rinse_after_sample(run_dir, label)


def run_plan_payload(run_plan: dict[str, Any]) -> dict[str, Any]:
    run_plan = validate_run_plan_payload(run_plan)
    workspace = create_run_workspace(run_plan)
    run_dir = workspace["run_dir"]

    start_automation(run_dir=str(run_dir))

    try:
        repetitions = int(run_plan.get("repetitions", 1))
        samples = [
            sample
            for sample in run_plan["samples"]
            if bool(sample.get("enabled", True))
        ]

        append_log(run_dir, f"Automation started. Repetitions={repetitions}, enabled samples={len(samples)}.")

        if not samples:
            raise RecipeRunnerError("run plan has no enabled samples.")

        for repetition in range(1, repetitions + 1):
            append_log(run_dir, f"Starting repetition {repetition}/{repetitions}.")

            for sample_index, sample in enumerate(samples, start=1):
                check_abort("Abort requested before sample.")
                run_sample(
                    run_dir=run_dir,
                    sample=sample,
                    sample_index=sample_index,
                    repetition=repetition,
                    repetitions=repetitions,
                )

        set_automation_state(step="Moving to home")
        append_log(run_dir, "Automation finished. Moving to home.")
        home_axes_internal(abort_event=get_abort_event())

        mark_run_complete(run_dir)
        finish_automation("Automation complete")

        append_log(run_dir, "Automation complete.")

        return {
            "ok": True,
            "run_dir": str(run_dir),
            "message": "Automation complete."
        }

    except AutomationAbortRequested:
        append_log(run_dir, "Automation abort requested.")

        try:
            stop_rde("Automation aborted.")
        except Exception as exc:
            append_log(run_dir, f"RDE stop during abort failed: {exc}.")

        try:
            clear_abort()
            set_automation_state(step="Abort cleanup: moving home")
            home_axes_internal(abort_event=get_abort_event())
            mark_run_aborted(run_dir)
            finish_automation("Automation aborted and homed")
            append_log(run_dir, "Automation aborted and homed.")
        except Exception as exc:
            mark_run_failed(run_dir, str(exc))
            fail_automation(str(exc), "Automation abort cleanup failed")
            append_log(run_dir, f"Automation abort cleanup failed: {exc}.")
            raise RecipeRunnerError(f"Abort cleanup failed: {exc}") from exc

        return {
            "ok": False,
            "aborted": True,
            "run_dir": str(run_dir),
            "message": "Automation aborted and homed."
        }

    except Exception as exc:
        append_log(run_dir, f"Automation failed: {exc}.")

        try:
            stop_rde(str(exc))
        except Exception as stop_exc:
            append_log(run_dir, f"RDE stop after failure failed: {stop_exc}.")

        mark_run_failed(run_dir, str(exc))
        fail_automation(str(exc), "Automation failed")

        raise


def run_saved_plan(name: str) -> dict[str, Any]:
    run_plan = load_run_plan(name)
    return run_plan_payload(run_plan)


def run_plan_payload_background(run_plan: dict[str, Any]) -> threading.Thread:
    clear_abort()

    if not reserve_automation():
        raise RecipeRunnerError("automation is already running.")

    def target() -> None:
        try:
            run_plan_payload(run_plan)
        except Exception as exc:
            if automation_is_running():
                fail_automation(str(exc), "Automation failed")
            raise

    thread = threading.Thread(
        target=target,
        daemon=True,
    )
    thread.start()

    return thread


def run_saved_plan_background(name: str) -> threading.Thread:
    run_plan = load_run_plan(name)
    return run_plan_payload_background(run_plan)


def abort_automation() -> None:
    request_abort()
