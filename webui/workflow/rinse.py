from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from hardware.motion_controller import emergency_stop_motion, move_xz_steps_parallel
from hardware.rde_controller import send_rpm, stop_rde
from hardware.rotation_controller import (
    RotationController,
    emergency_stop_rotation,
    get_rotation_controller,
)
from workflow.data_manager import append_log, register_action_result, utc_timestamp
from workflow.rinse_arm_paths import build_symmetric_arm_oscillation
from workflow.rinse_paths import build_diamond_cycle
from workflow.state import (
    AutomationAbortRequested,
    get_abort_event,
    get_axis_position_confidence,
    mark_axis_positions_uncertain,
)


class RinseExecutionError(RuntimeError):
    pass


class _CombinedCancellationEvent:
    """Event-like view used by motion code for local failure and user abort."""

    def __init__(
        self,
        local_event: threading.Event,
        external_event: threading.Event,
    ) -> None:
        self.local_event = local_event
        self.external_event = external_event

    def is_set(self) -> bool:
        return self.local_event.is_set() or self.external_event.is_set()

    def set(self) -> None:
        self.local_event.set()


def _raise_if_cancelled(
    cancellation_event: threading.Event,
    external_abort_event: threading.Event,
    message: str,
) -> None:
    if external_abort_event.is_set():
        raise AutomationAbortRequested(message)
    if cancellation_event.is_set():
        raise RinseExecutionError("Rinse worker cancelled after a component failure.")


def _sleep_interruptible(
    seconds: float,
    *,
    cancellation_event: threading.Event,
    external_abort_event: threading.Event,
    sleep_fn: Callable[[float], None],
) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        _raise_if_cancelled(
            cancellation_event,
            external_abort_event,
            "Abort requested during the packaged rinse step.",
        )
        interval = min(0.05, remaining)
        sleep_fn(interval)
        remaining -= interval


def _sleep_at_closed_arm_boundary(
    seconds: float,
    *,
    stop_after_closed_cycle_event: threading.Event,
    cancellation_event: threading.Event,
    external_abort_event: threading.Event,
    sleep_fn: Callable[[float], None],
) -> bool:
    """Pause between closed cycles and report a normal stop request."""

    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if stop_after_closed_cycle_event.is_set():
            return True
        _raise_if_cancelled(
            cancellation_event,
            external_abort_event,
            "Abort requested between closed rinse-arm cycles.",
        )
        interval = min(0.05, remaining)
        sleep_fn(interval)
        remaining -= interval
    return stop_after_closed_cycle_event.is_set()


def _run_all_diamond_cycles(
    *,
    settings: dict[str, Any],
    position_state: dict[str, int],
    position_lock: threading.Lock,
    cancellation_event: threading.Event,
    external_abort_event: threading.Event,
    move_fn: Callable[..., dict[str, str | None]],
    sleep_fn: Callable[[float], None],
    log_fn: Callable[[str | Path, str], None],
    run_dir: str | Path,
    label: str,
    progress: dict[str, Any],
) -> dict[str, Any]:
    cycles = int(settings["cycles"])
    x_radius = int(settings["diamond"]["x_radius_steps"])
    z_radius = int(settings["diamond"]["z_radius_steps"])
    cycle_timeout_s = float(settings["cycle_timeout_s"])
    inter_cycle_pause_s = float(settings["inter_cycle_pause_s"])
    combined_event = _CombinedCancellationEvent(
        cancellation_event,
        external_abort_event,
    )

    with position_lock:
        worker_start_x = int(position_state["x"])
        worker_start_z = int(position_state["z"])

    progress["diamond_started"] = True
    for cycle_index in range(1, cycles + 1):
        cycle_started_at = time.monotonic()
        with position_lock:
            cycle_start_x = int(position_state["x"])
            cycle_start_z = int(position_state["z"])

        for segment in build_diamond_cycle(
            x_radius,
            z_radius,
            cycle_index=cycle_index,
        ):
            _raise_if_cancelled(
                cancellation_event,
                external_abort_event,
                "Abort requested before an X/Z rinse segment.",
            )
            move_result = move_fn(
                x_steps=int(segment.x_steps),
                z_steps=int(segment.z_steps),
                abort_event=combined_event,
            )
            with position_lock:
                position_state["x"] += int(segment.x_steps)
                position_state["z"] += int(segment.z_steps)
                current_x = int(position_state["x"])
                current_z = int(position_state["z"])

            expected_x = cycle_start_x + int(
                segment.expected_x_offset_after_segment
            )
            expected_z = cycle_start_z + int(
                segment.expected_z_offset_after_segment
            )
            if current_x != expected_x or current_z != expected_z:
                raise RinseExecutionError(
                    "Tracked X/Z position did not match the acknowledged "
                    "diamond segment."
                )

            progress["diamond_segments"].append(
                {
                    **asdict(segment),
                    "x_ack": move_result.get("x_ack"),
                    "z_ack": move_result.get("z_ack"),
                    "tracked_x_steps": current_x,
                    "tracked_z_steps": current_z,
                    "completed_at": utc_timestamp(),
                }
            )
            log_fn(
                run_dir,
                (
                    f"{label}: diamond cycle {cycle_index}/{cycles}, "
                    f"segment {segment.segment_index}/5 acknowledged; "
                    f"X={segment.x_steps:+d}, Z={segment.z_steps:+d}."
                ),
            )
            if time.monotonic() - cycle_started_at > cycle_timeout_s:
                raise RinseExecutionError(
                    f"Diamond cycle {cycle_index} exceeded its "
                    f"{cycle_timeout_s:g}s timeout."
                )

        with position_lock:
            final_cycle_x = int(position_state["x"])
            final_cycle_z = int(position_state["z"])
        if final_cycle_x != cycle_start_x or final_cycle_z != cycle_start_z:
            raise RinseExecutionError(
                f"Diamond cycle {cycle_index} did not close at its tracked start."
            )

        progress["diamond_cycles_completed"] = cycle_index
        log_fn(
            run_dir,
            (
                f"{label}: diamond cycle {cycle_index}/{cycles} closed at "
                f"X={final_cycle_x}, Z={final_cycle_z}."
            ),
        )
        if cycle_index < cycles and inter_cycle_pause_s > 0:
            _sleep_interruptible(
                inter_cycle_pause_s,
                cancellation_event=cancellation_event,
                external_abort_event=external_abort_event,
                sleep_fn=sleep_fn,
            )

    progress["diamond_finished"] = True
    return {
        "cycles_completed": cycles,
        "starting_x_steps": worker_start_x,
        "starting_z_steps": worker_start_z,
        "ending_x_steps": final_cycle_x,
        "ending_z_steps": final_cycle_z,
    }


def _run_continuous_arm_oscillation(
    *,
    settings: dict[str, Any],
    controller: RotationController,
    starting_arm_offset_steps: int,
    stop_after_closed_cycle_event: threading.Event,
    arm_started_event: threading.Event,
    cancellation_event: threading.Event,
    external_abort_event: threading.Event,
    sleep_fn: Callable[[float], None],
    log_fn: Callable[[str | Path, str], None],
    run_dir: str | Path,
    label: str,
    progress: dict[str, Any],
) -> dict[str, Any]:
    arm = settings["arm_oscillation"]
    if not bool(arm["enabled"]):
        arm_started_event.set()
        progress["arm_finished"] = True
        return {
            "cycles_completed": 0,
            "starting_offset_steps": starting_arm_offset_steps,
            "ending_offset_steps": starting_arm_offset_steps,
        }

    segments = build_symmetric_arm_oscillation(
        int(arm["amplitude_steps"]),
        1,
        max_relative_steps=controller.max_relative_steps(),
    )
    pause_s = float(arm["pause_between_moves_s"])
    cycle_index = 0
    progress["arm_worker_started"] = True

    while True:
        cycle_index += 1
        for segment in segments:
            _raise_if_cancelled(
                cancellation_event,
                external_abort_event,
                "Abort requested before a rinse-arm segment.",
            )
            live_state = controller.expected_relative_state()
            if live_state["angle_confidence"] != "tracked":
                raise RinseExecutionError(
                    "Rotation-arm angle confidence became uncertain."
                )

            move_result = controller.relative_steps(int(segment.relative_steps))
            current_state = controller.expected_relative_state()
            expected_offset = (
                int(starting_arm_offset_steps)
                + int(segment.expected_offset_after_segment)
            )
            if int(current_state["expected_offset_steps"]) != expected_offset:
                raise RinseExecutionError(
                    "Tracked arm offset did not match the acknowledged "
                    "oscillation segment."
                )

            progress["arm_segments"].append(
                {
                    "cycle_index": cycle_index,
                    "segment_index": int(segment.segment_index),
                    "relative_steps": int(segment.relative_steps),
                    "expected_offset_after_segment": int(
                        segment.expected_offset_after_segment
                    ),
                    "executed_steps": int(move_result.executed_steps),
                    "ack_status": str(move_result.status),
                    "ack": str(move_result.raw_response),
                    "completed_at": utc_timestamp(),
                }
            )
            if not arm_started_event.is_set():
                arm_started_event.set()
            log_fn(
                run_dir,
                (
                    f"{label}: continuous arm cycle {cycle_index}, "
                    f"segment {segment.segment_index}/3 acknowledged; "
                    f"steps={segment.relative_steps:+d}."
                ),
            )

            if segment.segment_index < 3 and pause_s > 0:
                _sleep_interruptible(
                    pause_s,
                    cancellation_event=cancellation_event,
                    external_abort_event=external_abort_event,
                    sleep_fn=sleep_fn,
                )

        closed_state = controller.expected_relative_state()
        if int(closed_state["expected_offset_steps"]) != int(
            starting_arm_offset_steps
        ):
            raise RinseExecutionError(
                f"Rinse-arm cycle {cycle_index} did not close at its tracked start."
            )
        progress["arm_oscillation_cycles_completed"] = cycle_index

        # Normal stop is deliberately inspected only after +A,-2A,+A has
        # closed. Failure cancellation is checked before every segment above.
        if stop_after_closed_cycle_event.is_set():
            break

        if pause_s > 0:
            stop_requested = _sleep_at_closed_arm_boundary(
                pause_s,
                stop_after_closed_cycle_event=stop_after_closed_cycle_event,
                cancellation_event=cancellation_event,
                external_abort_event=external_abort_event,
                sleep_fn=sleep_fn,
            )
            if stop_requested:
                break

    progress["arm_finished"] = True
    return {
        "cycles_completed": cycle_index,
        "starting_offset_steps": starting_arm_offset_steps,
        "ending_offset_steps": int(
            controller.expected_relative_state()["expected_offset_steps"]
        ),
    }


def execute_rinse(
    *,
    run_dir: str | Path,
    label: str,
    settings: dict[str, Any],
    position_state: dict[str, int],
    controller: RotationController | None = None,
    move_fn: Callable[..., dict[str, str | None]] | None = None,
    send_rpm_fn: Callable[[int], str] | None = None,
    stop_rde_fn: Callable[[str | None], None] | None = None,
    emergency_stop_motion_fn: Callable[[], Any] | None = None,
    emergency_stop_rotation_fn: Callable[[], Any] | None = None,
    external_abort_event: threading.Event | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    record_fn: Callable[[str | Path, dict[str, Any]], dict[str, Any]] = register_action_result,
    log_fn: Callable[[str | Path, str], None] = append_log,
) -> dict[str, Any]:
    """Execute one packaged rinse with continuous RPM and arm concurrency."""

    arm_controller = controller or get_rotation_controller()
    move = move_fn or move_xz_steps_parallel
    start_rpm = send_rpm_fn or send_rpm
    stop_disk = stop_rde_fn or stop_rde
    stop_motion = emergency_stop_motion_fn or emergency_stop_motion
    stop_rotation = emergency_stop_rotation_fn or emergency_stop_rotation
    outside_abort = external_abort_event or get_abort_event()

    cancellation_event = threading.Event()
    stop_after_closed_cycle_event = threading.Event()
    arm_started_event = threading.Event()
    position_lock = threading.Lock()
    progress: dict[str, Any] = {
        "diamond_started": False,
        "diamond_finished": False,
        "diamond_cycles_completed": 0,
        "diamond_segments": [],
        "arm_worker_started": False,
        "arm_finished": False,
        "arm_oscillation_cycles_completed": 0,
        "arm_segments": [],
    }

    with position_lock:
        rinse_start_x = int(position_state["x"])
        rinse_start_z = int(position_state["z"])
    arm_start_state = arm_controller.expected_relative_state()
    rinse_start_arm = int(arm_start_state["expected_offset_steps"])
    x_start_confidence = get_axis_position_confidence("horizontal")
    z_start_confidence = get_axis_position_confidence("linear")

    result: dict[str, Any] = {
        "action": "rinse",
        "label": str(label),
        "status": "running",
        "started_at": utc_timestamp(),
        "completed_at": None,
        "cycles_requested": int(settings["cycles"]),
        "diamond_cycles_completed": 0,
        "arm_oscillation_cycles_completed": 0,
        "rpm": int(settings["disk_rotation"]["rpm"]),
        "rpm_started_once": False,
        "rpm_stopped_once": False,
        "rinse_start_x_steps": rinse_start_x,
        "rinse_start_z_steps": rinse_start_z,
        "rinse_start_arm_offset_steps": rinse_start_arm,
        "rinse_start_x_confidence": x_start_confidence,
        "rinse_start_z_confidence": z_start_confidence,
        "rinse_start_arm_confidence": str(arm_start_state["angle_confidence"]),
        "final_net_x_steps": None,
        "final_net_z_steps": None,
        "final_net_arm_steps": None,
        "final_rpm": None,
        "disk_angular_origin_claimed": False,
        "automatic_recovery_attempted": False,
        "homing_attempted": False,
        "legacy_rotation_zero_command_sent": False,
        "diamond_segments": progress["diamond_segments"],
        "arm_segments": progress["arm_segments"],
    }

    if arm_start_state["angle_confidence"] != "tracked":
        result.update(
            {
                "status": "failed",
                "completed_at": utc_timestamp(),
                "error": (
                    "Rotation-arm angle is uncertain; packaged rinse was not started."
                ),
            }
        )
        record_fn(run_dir, result)
        raise RinseExecutionError(result["error"])
    if x_start_confidence != "tracked" or z_start_confidence != "tracked":
        result.update(
            {
                "status": "failed",
                "completed_at": utc_timestamp(),
                "error": (
                    "X/Z tracked-position confidence is uncertain; "
                    "packaged rinse was not started."
                ),
            }
        )
        record_fn(run_dir, result)
        raise RinseExecutionError(result["error"])

    disk_enabled = bool(settings["disk_rotation"]["enabled"])
    arm_enabled = bool(settings["arm_oscillation"]["enabled"])
    rpm_started = False
    rpm_start_attempted = False
    rpm_stopped = False
    futures: list[Future[Any]] = []
    executor = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="packaged-rinse",
    )

    try:
        log_fn(
            run_dir,
            (
                f"{label}: packaged rinse starting; cycles={settings['cycles']}, "
                f"diamond radii X={settings['diamond']['x_radius_steps']}, "
                f"Z={settings['diamond']['z_radius_steps']}, "
                "arm mode=continuous closed cycles, "
                "RPM ownership=entire rinse step."
            ),
        )
        _raise_if_cancelled(
            cancellation_event,
            outside_abort,
            "Abort requested before the packaged rinse step.",
        )
        if disk_enabled:
            rpm_start_attempted = True
            start_rpm(int(settings["disk_rotation"]["rpm"]))
            rpm_started = True
            result["rpm_started_once"] = True
            log_fn(
                run_dir,
                (
                    f"{label}: RDE started once at "
                    f"{settings['disk_rotation']['rpm']} RPM."
                ),
            )
            settle_s = float(settings["disk_rotation"]["settle_s"])
            if settle_s > 0:
                _sleep_interruptible(
                    settle_s,
                    cancellation_event=cancellation_event,
                    external_abort_event=outside_abort,
                    sleep_fn=sleep_fn,
                )

        arm_future = executor.submit(
            _run_continuous_arm_oscillation,
            settings=settings,
            controller=arm_controller,
            starting_arm_offset_steps=rinse_start_arm,
            stop_after_closed_cycle_event=stop_after_closed_cycle_event,
            arm_started_event=arm_started_event,
            cancellation_event=cancellation_event,
            external_abort_event=outside_abort,
            sleep_fn=sleep_fn,
            log_fn=log_fn,
            run_dir=run_dir,
            label=label,
            progress=progress,
        )
        futures.append(arm_future)

        # Do not begin diamond cycle 1 until the arm worker has actually
        # acknowledged its first segment (or declared itself disabled).
        while not arm_started_event.wait(0.01):
            if arm_future.done():
                arm_future.result()
            _raise_if_cancelled(
                cancellation_event,
                outside_abort,
                "Abort requested while starting the rinse-arm worker.",
            )

        diamond_future = executor.submit(
            _run_all_diamond_cycles,
            settings=settings,
            position_state=position_state,
            position_lock=position_lock,
            cancellation_event=cancellation_event,
            external_abort_event=outside_abort,
            move_fn=move,
            sleep_fn=sleep_fn,
            log_fn=log_fn,
            run_dir=run_dir,
            label=label,
            progress=progress,
        )
        futures.append(diamond_future)

        wait_targets = (
            (arm_future, diamond_future)
            if arm_enabled
            else (diamond_future,)
        )
        while True:
            done, _pending = wait(
                wait_targets,
                timeout=0.05,
                return_when=FIRST_COMPLETED,
            )
            if arm_enabled and arm_future in done:
                # Before the normal stop signal, the continuous arm worker
                # must never finish; result() preserves its real exception.
                arm_future.result()
                raise RinseExecutionError(
                    "Continuous rinse-arm worker stopped before diamond completion."
                )
            if diamond_future in done:
                diamond_result = diamond_future.result()
                break
            _raise_if_cancelled(
                cancellation_event,
                outside_abort,
                "Abort requested during the packaged rinse step.",
            )

        stop_after_closed_cycle_event.set()
        arm_result = arm_future.result()

        with position_lock:
            final_x = int(position_state["x"])
            final_z = int(position_state["z"])
        final_arm_state = arm_controller.expected_relative_state()
        final_arm = int(final_arm_state["expected_offset_steps"])

        if final_x != rinse_start_x or final_z != rinse_start_z:
            raise RinseExecutionError(
                "Packaged rinse ended away from its tracked X/Z starting position."
            )
        if (
            final_arm != rinse_start_arm
            or final_arm_state["angle_confidence"] != "tracked"
        ):
            raise RinseExecutionError(
                "Packaged rinse arm did not return to its tracked starting angle."
            )
        if int(diamond_result["cycles_completed"]) != int(settings["cycles"]):
            raise RinseExecutionError(
                "Diamond worker completed fewer cycles than requested."
            )

        if disk_enabled:
            stop_disk(None)
            rpm_stopped = True
            result["rpm_stopped_once"] = True
            log_fn(
                run_dir,
                (
                    f"{label}: RDE stopped once after all diamond cycles and "
                    "the final closed arm cycle."
                ),
            )

        result.update(
            {
                "status": "completed",
                "completed_at": utc_timestamp(),
                "diamond_cycles_completed": int(
                    progress["diamond_cycles_completed"]
                ),
                "arm_oscillation_cycles_completed": int(
                    arm_result["cycles_completed"]
                ),
                "final_net_x_steps": final_x - rinse_start_x,
                "final_net_z_steps": final_z - rinse_start_z,
                "final_net_arm_steps": final_arm - rinse_start_arm,
                "final_rpm": 0,
                "final_expected_x_steps": final_x,
                "final_expected_z_steps": final_z,
                "final_expected_arm_offset_steps": final_arm,
                "x_position_confidence": "tracked",
                "z_position_confidence": "tracked",
                "arm_angle_confidence": "tracked",
            }
        )
        record_fn(run_dir, result)
        log_fn(
            run_dir,
            (
                f"{label}: packaged rinse completed; diamond cycles="
                f"{result['diamond_cycles_completed']}, arm closed cycles="
                f"{result['arm_oscillation_cycles_completed']}, "
                "net X/Z/arm=0, final RPM=0."
            ),
        )
        return result

    except Exception as exc:
        cancellation_event.set()

        cleanup_errors: list[str] = []
        if rpm_start_attempted and not rpm_stopped:
            try:
                stop_disk(str(exc))
                rpm_stopped = True
                result["rpm_stopped_once"] = True
                result["final_rpm"] = 0
            except Exception as stop_exc:
                cleanup_errors.append(f"RDE stop failed: {stop_exc}")

        try:
            stop_motion()
        except Exception as stop_exc:
            cleanup_errors.append(f"X/Z emergency stop failed: {stop_exc}")
        try:
            stop_rotation()
        except Exception as stop_exc:
            cleanup_errors.append(f"arm emergency stop failed: {stop_exc}")

        # Freeze worker-owned progress before persisting the failure record.
        # Emergency stops above release any command currently waiting for ACK.
        for future in futures:
            try:
                future.result()
            except Exception:
                pass

        if progress["diamond_started"] and not progress["diamond_finished"]:
            mark_axis_positions_uncertain(("horizontal", "linear"))
        if progress["arm_worker_started"] and not progress["arm_finished"]:
            mark_uncertain = getattr(arm_controller, "mark_angle_uncertain", None)
            if callable(mark_uncertain):
                mark_uncertain(
                    "Packaged rinse failed while the arm worker was active."
                )

        result.update(
            {
                "status": "failed",
                "completed_at": utc_timestamp(),
                "diamond_cycles_completed": int(
                    progress["diamond_cycles_completed"]
                ),
                "arm_oscillation_cycles_completed": int(
                    progress["arm_oscillation_cycles_completed"]
                ),
                "x_position_confidence": (
                    "uncertain"
                    if progress["diamond_started"]
                    and not progress["diamond_finished"]
                    else "tracked"
                ),
                "z_position_confidence": (
                    "uncertain"
                    if progress["diamond_started"]
                    and not progress["diamond_finished"]
                    else "tracked"
                ),
                "arm_angle_confidence": (
                    "uncertain"
                    if progress["arm_worker_started"]
                    and not progress["arm_finished"]
                    else str(
                        arm_controller.expected_relative_state()[
                            "angle_confidence"
                        ]
                    )
                ),
                "error": str(exc),
            }
        )
        with position_lock:
            if progress["diamond_finished"]:
                result["final_net_x_steps"] = (
                    int(position_state["x"]) - rinse_start_x
                )
                result["final_net_z_steps"] = (
                    int(position_state["z"]) - rinse_start_z
                )
            else:
                result["final_net_x_steps"] = None
                result["final_net_z_steps"] = None
        if result["arm_angle_confidence"] == "tracked":
            result["final_net_arm_steps"] = (
                int(
                    arm_controller.expected_relative_state()[
                        "expected_offset_steps"
                    ]
                )
                - rinse_start_arm
            )
        else:
            result["final_net_arm_steps"] = None
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
        record_fn(run_dir, result)
        log_fn(
            run_dir,
            (
                f"{label}: packaged rinse failed and was cancelled without "
                f"automatic return or homing: {exc}."
            ),
        )
        raise

    finally:
        cancellation_event.set()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True)
