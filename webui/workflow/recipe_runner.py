from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from analysis.ca_charge import (
    ANALYSIS_TYPE as CA_CHARGE_ANALYSIS_TYPE,
    ANALYSIS_VERSION as CA_CHARGE_ANALYSIS_VERSION,
    cumulative_charge_enabled,
    run_ca_charge_analysis,
)
from hardware.gamry_client import run_gamry_step
from hardware.gamry_cell_client import gamry_cell_off, gamry_cell_on
from hardware.motion_controller import move_horizontal_steps, move_linear_steps, move_to_xyz, move_xz_steps_parallel
from hardware.rde_controller import send_rpm, stop_rde
from hardware.rinse_controller import run_rinse_cycle
from hardware.rotation_controller import send_rotation_text
from gamry_worker.live_writer import fail_live_stream
from workflow.data_manager import (
    append_log,
    create_run_workspace,
    create_sample_workspace,
    mark_run_aborted,
    mark_run_complete,
    mark_run_failed,
    prepare_protocol_outputs,
    register_trial_analysis_result,
    register_trial_result,
    save_protocol_snapshot,
)
from workflow.protocol_loader import load_protocol
from workflow.levich_runner import run_levich_rpm_sweep_ca
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
    return {int(item["step_index"]): item for item in protocol_outputs}


def run_requested_post_acquisition_analyses(
    *,
    run_dir: Path,
    step: dict[str, Any],
    outputs: list[str],
) -> list[dict[str, Any]]:
    """Run enabled analyses after acquisition without changing trial success."""

    if not cumulative_charge_enabled(step):
        return []

    records: list[dict[str, Any]] = []
    for output in outputs:
        raw_dta = Path(output)
        if not raw_dta.is_file():
            append_log(
                run_dir,
                f"CA cumulative-charge analysis skipped because DTA is missing: {raw_dta}.",
            )
            continue
        try:
            analysis = run_ca_charge_analysis(raw_dta)
            record = register_trial_analysis_result(
                run_dir,
                raw_dta=raw_dta,
                analysis_type=analysis["analysis_type"],
                analysis_version=analysis["analysis_version"],
                label="CA Cumulative Charge",
                technique="ca",
                status="complete",
                analysis_artifacts=analysis["artifacts"],
                summary=analysis["summary"],
            )
            records.append(record)
            final_charge = analysis["summary"]["result"]["final_signed_charge_c"]
            append_log(
                run_dir,
                f"CA cumulative-charge analysis complete for {raw_dta.name}: "
                f"{final_charge:.12g} C (recomputed from DTA).",
            )
        except Exception as exc:
            error_text = str(exc)
            try:
                record = register_trial_analysis_result(
                    run_dir,
                    raw_dta=raw_dta,
                    analysis_type=CA_CHARGE_ANALYSIS_TYPE,
                    analysis_version=CA_CHARGE_ANALYSIS_VERSION,
                    label="CA Cumulative Charge",
                    technique="ca",
                    status="failed",
                    error=error_text,
                )
                records.append(record)
            except Exception as registration_exc:
                append_log(
                    run_dir,
                    "Unable to register the failed CA charge analysis: "
                    f"{registration_exc}.",
                )
            append_log(
                run_dir,
                f"CA cumulative-charge analysis failed for {raw_dta.name}: "
                f"{error_text}. Acquisition remains completed.",
            )
    return records


def sample_label(sample: dict[str, Any], sample_index: int) -> str:
    return str(sample.get("label") or sample.get("sample_id") or f"Sample {sample_index}")


def safe_name(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return text or fallback


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
    filename_prefix: str | None = None,
) -> None:
    label = sample_label(sample, sample_index)
    protocol_snapshot = save_protocol_snapshot(run_dir, protocol)
    append_log(run_dir, f"{label}: protocol snapshot saved at {protocol_snapshot}.")

    protocol_outputs = prepare_protocol_outputs(
        run_dir=run_dir,
        sample_dir=sample_dir,
        sample_index=sample_index,
        protocol=protocol,
        filename_prefix=filename_prefix,
    )
    outputs_by_step = protocol_outputs_by_step(protocol_outputs)

    for step_index, step in enumerate(protocol.get("steps", []), start=1):
        if not bool(step.get("enabled", True)):
            append_log(run_dir, f"{label}: skipping disabled EChem step {step_index}.")
            continue

        check_abort("Abort requested before EChem step.")
        step_name = str(step.get("name") or f"Step {step_index}")
        technique = str(step.get("technique") or "echem").lower()

        set_automation_state(step=f"{label} - EChem {step_index}: {technique} / {step_name}")
        append_log(run_dir, f"{label}: starting EChem step {step_index}: {technique} / {step_name}.")

        if technique == "wait":
            duration_s = float(step["duration_s"])
            sleep_interruptible(
                duration_s,
                "Abort requested during EChem protocol wait.",
            )
            append_log(
                run_dir,
                f"{label}: finished EChem step {step_index}: waited {duration_s:g} s.",
            )
            continue

        output_record = outputs_by_step.get(step_index)
        if output_record is None:
            raise RecipeRunnerError(f"No output path prepared for step {step_index}: {step_name}")

        protocol_name = str(
            protocol.get("protocol_name")
            or protocol.get("display_name")
            or "protocol"
        )
        trial_id = (
            f"{safe_name(Path(sample_dir).name, f'sample-{sample_index}')}-step-{step_index}"
            + (f"-{filename_prefix}" if filename_prefix else "")
        )
        output_record["trial_id"] = trial_id
        trial_step = dict(step)
        trial_step["_trial_id"] = trial_id
        trial_step["_trial_index"] = step_index
        try:
            if technique == "levich_rpm_sweep_ca":
                if len(output_record["outputs"]) != 1:
                    raise RecipeRunnerError("Levich RPM sweep requires exactly one continuous CA DTA output.")
                result = run_levich_rpm_sweep_ca(
                    step=trial_step,
                    raw_dta=output_record["outputs"][0],
                    run_dir=run_dir,
                    sample_id=sample.get("sample_id"),
                    sample_label=label,
                    protocol_name=protocol_name,
                    sleep_fn=sleep_interruptible,
                    check_abort_fn=check_abort,
                )
                worker_result = result.get("gamry", {}) if isinstance(result, dict) else {}
            else:
                result = run_gamry_step(
                    step=trial_step,
                    outputs=output_record["outputs"],
                    run_dir=run_dir,
                    sample_id=sample.get("sample_id"),
                    sample_label=label,
                    protocol_name=protocol_name,
                )
                worker_result = result
        except Exception as exc:
            from gamry_worker.trial_preparation import default_trial_metadata, utc_now
            from workflow.config_loader import get_gamry_config

            error_result = getattr(exc, "result", None)
            saved_metadata = error_result.get("trial_metadata") if isinstance(error_result, dict) else None
            metadata = (
                dict(saved_metadata)
                if isinstance(saved_metadata, dict)
                else default_trial_metadata(get_gamry_config().get("ru_preparation", {}))
            )
            metadata.update(
                {
                    "trial_status": "failed",
                    "skip_reason": str(exc),
                    "completed_at": utc_now(),
                }
            )
            register_trial_result(run_dir, output_record, metadata)
            raise

        trial_metadata = worker_result.get("trial_metadata", {}) if isinstance(worker_result, dict) else {}
        if get_abort_event().is_set() and trial_metadata:
            trial_metadata = dict(trial_metadata)
            trial_metadata["trial_status"] = "aborted"
            trial_metadata["skip_reason"] = "Automation abort requested."
        trial_status = str(trial_metadata.get("trial_status", "")).lower()
        analysis_records: list[dict[str, Any]] = []
        if trial_status not in {"skipped", "failed", "aborted"} and not get_abort_event().is_set():
            analysis_records = run_requested_post_acquisition_analyses(
                run_dir=run_dir,
                step=trial_step,
                outputs=list(output_record["outputs"]),
            )
            if analysis_records:
                worker_result = dict(worker_result)
                worker_result["analysis_results"] = analysis_records
                trial_metadata = dict(trial_metadata)
                trial_metadata["analysis_results"] = [
                    {
                        "analysis_type": item.get("analysis_type"),
                        "analysis_version": item.get("analysis_version"),
                        "analysis_status": item.get("analysis_status"),
                        "raw_dta": item.get("raw_dta"),
                    }
                    for item in analysis_records
                ]
        if trial_metadata:
            register_trial_result(run_dir, output_record, trial_metadata, worker_result)
        if str(trial_metadata.get("trial_status", "")).lower() == "skipped":
            append_log(
                run_dir,
                f"{label}: bypassed EChem step {step_index}; {trial_metadata.get('skip_reason')}. Continuing.",
            )
            continue

        # The worker subprocess may finish its current acquisition call after
        # the UI sends an abort. Reflect that user-visible outcome in the
        # temporary stream before the shared abort check unwinds the plan.
        if get_abort_event().is_set():
            live_dir = Path(run_dir) / "_system" / "live"
            if (live_dir / "status.json").exists():
                fail_live_stream(
                    live_dir,
                    "Automation abort requested.",
                    status="aborted",
                )
            check_abort("Abort requested after EChem step.")

        append_log(run_dir, f"{label}: finished EChem step {step_index}: {result}.")


def run_rinse_after_sample(run_dir: Path, label: str) -> None:
    set_automation_state(step=f"Rinse after {label}")
    append_log(run_dir, f"{label}: rinse started.")
    rinse_result = run_rinse_cycle()
    append_log(run_dir, f"{label}: rinse finished: {rinse_result}.")


def run_group_echem_action(
    *,
    run_dir: Path,
    group_dir: Path,
    group_index: int,
    step_index: int,
    label: str,
    step_name: str,
    protocol_name: str,
    synthetic_sample: dict[str, Any],
) -> None:
    protocol = load_protocol(protocol_name)
    append_log(run_dir, f"{label}: loaded EChem protocol {protocol_name}.")

    echem_sample = dict(synthetic_sample)
    echem_sample["sample_id"] = (
        f"{synthetic_sample['sample_id']}_step_{step_index:03d}"
    )
    echem_sample["label"] = f"{label} / {step_name}"

    # DTA files go directly into the group folder. The atomic step number is
    # prefixed to each filename, so repeated protocols never overwrite files.
    run_protocol_for_sample(
        run_dir=run_dir,
        sample_dir=group_dir,
        sample_index=group_index,
        sample=echem_sample,
        protocol=protocol,
        filename_prefix=f"{step_index:03d}",
    )


# ---------------------------------------------------------------------------
# Legacy sample-based execution (kept for existing run-plan JSON files)
# ---------------------------------------------------------------------------

def run_sample(
    run_dir: Path,
    sample: dict[str, Any],
    sample_index: int,
    repetition: int,
    repetitions: int,
) -> None:
    label = sample_label(sample, sample_index)
    position = sample["position"]

    set_automation_state(step=f"Rep {repetition}/{repetitions} - Move to {label}")
    append_log(run_dir, f"Rep {repetition}/{repetitions}: moving to {label}.")

    sample_dir = create_sample_workspace(
        run_dir,
        sample,
        sample_index,
        repetition=repetition,
        repetitions=repetitions,
    )

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
        set_automation_state(step=f"Rep {repetition}/{repetitions} - Stabilize {label} for {stabilization_s}s")
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
        set_automation_state(step=f"Rep {repetition}/{repetitions} - Post-EChem wait for {label}")
        append_log(run_dir, f"{label}: post-EChem wait {post_echem_wait_s}s.")
        sleep_interruptible(post_echem_wait_s, "Abort requested during post-EChem wait.")

    stop_sample_rpm(run_dir, label)

    if bool(sample.get("rinse_after", False)):
        run_rinse_after_sample(run_dir, label)


# ---------------------------------------------------------------------------
# New grouped, atomic-step execution
# ---------------------------------------------------------------------------

def group_workspace_sample(group: dict[str, Any], group_index: int) -> dict[str, Any]:
    return {
        "sample_id": safe_name(group.get("group_id"), f"group_{group_index:03d}"),
        "label": str(group.get("label") or f"Group {group_index}"),
        "enabled": bool(group.get("enabled", True)),
        "position": {"x": 0, "y": 0, "z": 0},
        "rpm": 0,
        "stabilization_s": 0,
        "protocol": "ocp_only",
        "rotation_command": "",
        "post_echem_wait_s": 0,
        "rinse_after": False,
    }


def run_group(
    run_dir: Path,
    group: dict[str, Any],
    group_index: int,
    repetition: int,
    repetitions: int,
    position_state: dict[str, int],
) -> None:
    label = str(group.get("label") or f"Group {group_index}")
    synthetic_sample = group_workspace_sample(group, group_index)
    group_dir = create_sample_workspace(
        run_dir,
        synthetic_sample,
        group_index,
        repetition=repetition,
        repetitions=repetitions,
    )
    rde_is_running = False

    append_log(run_dir, f"Rep {repetition}/{repetitions}: starting group '{label}'.")

    try:
        for step_index, step in enumerate(group.get("steps", []), start=1):
            if not bool(step.get("enabled", True)):
                append_log(run_dir, f"{label}: skipping disabled atomic step {step_index}.")
                continue

            check_abort("Abort requested before atomic run-plan step.")
            action = str(step.get("action") or "").strip().lower()
            step_name = str(step.get("name") or f"Step {step_index}")
            state_label = f"Rep {repetition}/{repetitions} - {label} - Step {step_index}: {step_name}"
            set_automation_state(step=state_label)
            append_log(run_dir, f"{label}: starting atomic step {step_index}: {action} / {step_name}.")

            if action == "move_x":
                steps = int(step["steps"])
                ack = move_horizontal_steps(
                    steps,
                    abort_event=get_abort_event(),
                )
                position_state["x"] += steps
                append_log(
                    run_dir,
                    f"{label}: X relative move={steps}, tracked X={position_state['x']}, ack={ack}.",
                )

            elif action == "move_z":
                steps = int(step["steps"])
                ack = move_linear_steps(
                    steps,
                    abort_event=get_abort_event(),
                )
                position_state["z"] += steps
                append_log(
                    run_dir,
                    f"{label}: Z relative move={steps}, tracked Z={position_state['z']}, ack={ack}.",
                )

            elif action == "move_xz_parallel":
                x_steps = int(step["x_steps"])
                z_steps = int(step["z_steps"])

                check_abort(
                    "Abort requested before concurrent X/Z movement."
                )

                result = move_xz_steps_parallel(
                    x_steps=x_steps,
                    z_steps=z_steps,
                    abort_event=get_abort_event(),
                )

                position_state["x"] += x_steps
                position_state["z"] += z_steps

                append_log(
                    run_dir,
                    (
                        f"{label}: concurrent X/Z move completed. "
                        f"X={x_steps}, Z={z_steps}, "
                        f"tracked X={position_state['x']}, "
                        f"tracked Z={position_state['z']}, "
                        f"x_ack={result['x_ack']}, "
                        f"z_ack={result['z_ack']}."
                    ),
                )

            elif action == "rotation":
                ack = send_rotation_text(str(step["command"]))
                append_log(run_dir, f"{label}: rotation ack={ack}.")

            elif action == "set_rpm":
                rpm = int(step["rpm"])
                if rpm <= 0:
                    stop_rde(None)
                    rde_is_running = False
                    append_log(run_dir, f"{label}: RPM set to stop because value was {rpm}.")
                else:
                    send_rpm(rpm)
                    rde_is_running = True
                    append_log(run_dir, f"{label}: RDE set to {rpm} RPM.")

            elif action == "wait":
                duration_s = float(step["duration_s"])
                append_log(run_dir, f"{label}: waiting {duration_s}s.")
                sleep_interruptible(duration_s, "Abort requested during grouped wait.")

            elif action == "echem":
                run_group_echem_action(
                    run_dir=run_dir,
                    group_dir=group_dir,
                    group_index=group_index,
                    step_index=step_index,
                    label=label,
                    step_name=step_name,
                    protocol_name=str(step["protocol"]),
                    synthetic_sample=synthetic_sample,
                )

            elif action == "rpm_echem":
                rpm = int(step["rpm"])
                protocol_name = str(step["protocol"])
                rpm_settle_s = float(step.get("rpm_settle_s", 0))
                stop_after = bool(step.get("stop_rpm_after", True))

                # This action is reached only after every earlier atomic step
                # has finished. Therefore, when it follows Move Z, the RDE
                # cannot begin spinning until the Z movement ACK is received.
                check_abort("Abort requested before concurrent RPM + EChem.")
                send_rpm(rpm)
                rde_is_running = True
                append_log(
                    run_dir,
                    f"{label}: concurrent RPM + EChem started at {rpm} RPM.",
                )

                try:
                    if rpm_settle_s > 0:
                        sleep_interruptible(
                            rpm_settle_s,
                            "Abort requested during RPM stabilization.",
                        )

                    run_group_echem_action(
                        run_dir=run_dir,
                        group_dir=group_dir,
                        group_index=group_index,
                        step_index=step_index,
                        label=label,
                        step_name=step_name,
                        protocol_name=protocol_name,
                        synthetic_sample=synthetic_sample,
                    )
                finally:
                    if stop_after:
                        stop_rde(None)
                        rde_is_running = False
                        append_log(
                            run_dir,
                            f"{label}: RDE stopped after concurrent RPM + EChem.",
                        )

            elif action == "stop_rpm":
                stop_rde(None)
                rde_is_running = False
                append_log(run_dir, f"{label}: RDE stopped.")

            elif action == "rinse":
                run_rinse_after_sample(run_dir, label)

            elif action == "gamry_cell_on":
                duration_s = step.get("duration_s")
                result = gamry_cell_on(
                    None if duration_s is None else float(duration_s)
                )
                append_log(
                    run_dir,
                    f"{label}: Gamry cell ON result: {result.get('last_result', result)}",
                )

            elif action == "gamry_cell_off":
                result = gamry_cell_off()
                append_log(
                    run_dir,
                    f"{label}: Gamry cell OFF result: {result.get('last_result', result)}",
                )

            else:
                raise RecipeRunnerError(f"Unsupported grouped action: {action}")

            append_log(run_dir, f"{label}: finished atomic step {step_index}: {action} / {step_name}.")

    finally:
        # A group is a logical safety boundary. RPM is never left running merely
        # because the final stop step was accidentally omitted.
        if rde_is_running:
            try:
                stop_rde(None)
                append_log(run_dir, f"{label}: safety stop at group end.")
            except Exception as exc:
                append_log(run_dir, f"{label}: safety stop at group end failed: {exc}.")
                raise


def execute_plan_body(run_plan: dict[str, Any], run_dir: Path) -> None:
    repetitions = int(run_plan.get("repetitions", 1))

    if "groups" in run_plan:
        groups = [group for group in run_plan["groups"] if bool(group.get("enabled", True))]
        if not groups:
            raise RecipeRunnerError("run plan has no enabled groups.")

        append_log(run_dir, f"Grouped automation started. Repetitions={repetitions}, enabled groups={len(groups)}.")
        append_log(
            run_dir,
            "Automatic homing is disabled. Grouped X/Z commands are signed relative steps, identical to Motor Control.",
        )
        position_state = {"x": 0, "z": 0}

        for repetition in range(1, repetitions + 1):
            append_log(run_dir, f"Starting repetition {repetition}/{repetitions}.")
            for group_index, group in enumerate(groups, start=1):
                check_abort("Abort requested before group.")
                run_group(
                    run_dir=run_dir,
                    group=group,
                    group_index=group_index,
                    repetition=repetition,
                    repetitions=repetitions,
                    position_state=position_state,
                )

    else:
        samples = [sample for sample in run_plan["samples"] if bool(sample.get("enabled", True))]
        if not samples:
            raise RecipeRunnerError("run plan has no enabled samples.")

        append_log(run_dir, f"Automation started. Repetitions={repetitions}, enabled samples={len(samples)}.")

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

        append_log(
            run_dir,
            "Automation finished. Automatic homing is disabled; axes remain at their final tracked positions.",
        )


def force_gamry_cell_off_for_cleanup(run_dir: Path, context: str) -> dict[str, Any] | None:
    try:
        result = gamry_cell_off()
        append_log(
            run_dir,
            f"Gamry cell OFF cleanup succeeded ({context}): {result.get('last_result', result)}",
        )
        return result
    except Exception as exc:
        append_log(run_dir, f"Gamry cell OFF cleanup failed ({context}): {exc}.")
        return None


def run_plan_payload(run_plan: dict[str, Any]) -> dict[str, Any]:
    run_plan = validate_run_plan_payload(run_plan)
    workspace = create_run_workspace(run_plan)
    run_dir = workspace["run_dir"]
    start_automation(run_dir=str(run_dir))

    try:
        execute_plan_body(run_plan, run_dir)
        cleanup_result = force_gamry_cell_off_for_cleanup(run_dir, "normal completion")
        if cleanup_result is None:
            raise RecipeRunnerError(
                "Automation steps finished, but the final Gamry Cell OFF cleanup failed."
            )
        mark_run_complete(run_dir)
        finish_automation("Automation complete")
        append_log(run_dir, "Automation complete.")

        return {"ok": True, "run_dir": str(run_dir), "message": "Automation complete."}

    except AutomationAbortRequested:
        append_log(run_dir, "Automation abort requested.")

        try:
            stop_rde("Automation aborted.")
        except Exception as exc:
            append_log(run_dir, f"RDE stop during abort failed: {exc}.")

        force_gamry_cell_off_for_cleanup(run_dir, "automation abort")

        clear_abort()
        mark_run_aborted(run_dir)
        finish_automation("Automation aborted; axes left in place")
        append_log(
            run_dir,
            "Automation aborted. RDE stopped; automatic homing is disabled and axes were left in place.",
        )

        return {
            "ok": False,
            "aborted": True,
            "run_dir": str(run_dir),
            "message": "Automation aborted. RDE stopped; axes left in place.",
        }

    except Exception as exc:
        append_log(run_dir, f"Automation failed: {exc}.")
        try:
            stop_rde(str(exc))
        except Exception as stop_exc:
            append_log(run_dir, f"RDE stop after failure failed: {stop_exc}.")

        force_gamry_cell_off_for_cleanup(run_dir, "automation failure")

        clear_abort()
        mark_run_failed(run_dir, str(exc))
        fail_automation(str(exc), "Automation failed")
        raise


def run_saved_plan(name: str) -> dict[str, Any]:
    return run_plan_payload(load_run_plan(name))


def run_plan_payload_background(run_plan: dict[str, Any]) -> threading.Thread:
    if not reserve_automation():
        raise RecipeRunnerError("automation is already running.")

    clear_abort()

    def target() -> None:
        try:
            run_plan_payload(run_plan)
        except Exception as exc:
            if automation_is_running():
                fail_automation(str(exc), "Automation failed")
            raise

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def run_saved_plan_background(name: str) -> threading.Thread:
    return run_plan_payload_background(load_run_plan(name))


def abort_automation() -> None:
    request_abort()
