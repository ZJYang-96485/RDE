from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from hardware.rotation_controller import (
    RotationController,
    RotationMoveInterrupted,
    get_rotation_controller,
)
from workflow.data_manager import append_log, register_action_result, utc_timestamp
from workflow.rinse_arm_paths import build_symmetric_arm_oscillation
from workflow.state import check_abort


def execute_rinse_arm_oscillation(
    *,
    run_dir: str | Path,
    label: str,
    amplitude_deg: float,
    amplitude_steps: int,
    cycles: int,
    pause_between_moves_s: float,
    controller: RotationController | None = None,
    pause_fn: Callable[[float], None] | None = None,
    abort_check_fn: Callable[[str], None] = check_abort,
    record_fn: Callable[[str | Path, dict[str, Any]], dict[str, Any]] = register_action_result,
    log_fn: Callable[[str | Path, str], None] = append_log,
) -> dict[str, Any]:
    arm = controller or get_rotation_controller()
    pause = pause_fn or (lambda _seconds: None)
    start_state = arm.expected_relative_state()
    segment_records: list[dict[str, Any]] = []

    result: dict[str, Any] = {
        "action": "rinse_arm_oscillation",
        "label": str(label),
        "status": "running",
        "started_at": utc_timestamp(),
        "completed_at": None,
        "amplitude_deg": float(amplitude_deg),
        "amplitude_deg_requested": float(amplitude_deg),
        "amplitude_steps": int(amplitude_steps),
        "cycles": int(cycles),
        "cycles_requested": int(cycles),
        "cycles_completed": 0,
        "segments_completed": 0,
        "pause_between_moves_s": float(pause_between_moves_s),
        "return_to_start": True,
        "path_pattern": ["+A", "-2A", "+A"],
        "starting_expected_offset_steps": int(start_state["expected_offset_steps"]),
        "starting_expected_arm_offset_steps": int(start_state["expected_offset_steps"]),
        "ending_expected_offset_steps": None,
        "angle_confidence": str(start_state["angle_confidence"]),
        "automatic_recovery_attempted": False,
        "segments": segment_records,
    }

    if start_state["angle_confidence"] != "tracked":
        result.update(
            {
                "status": "failed",
                "completed_at": utc_timestamp(),
                "error": (
                    "Rotation-arm angle is uncertain from an earlier interrupted or "
                    "unacknowledged movement; no rinse-arm commands were sent."
                ),
            }
        )
        record_fn(run_dir, result)
        raise RuntimeError(result["error"])

    segments = build_symmetric_arm_oscillation(
        int(amplitude_steps),
        int(cycles),
        max_relative_steps=arm.max_relative_steps(),
    )
    log_fn(
        run_dir,
        (
            f"{label}: rinse-arm oscillation starting; "
            f"amplitude={amplitude_deg:g} deg ({amplitude_steps} steps), "
            f"cycles={cycles}, pattern=+A,-2A,+A, net commanded movement=0."
        ),
    )

    try:
        for segment_index, segment in enumerate(segments):
            abort_check_fn("Abort requested before rinse-arm movement.")
            live_state = arm.expected_relative_state()
            if live_state["angle_confidence"] != "tracked":
                raise RuntimeError(
                    "Rotation-arm angle became uncertain; no further "
                    "oscillation command was sent."
                )
            move_result = arm.relative_steps(segment.relative_steps)
            record = {
                **asdict(segment),
                "requested_angle_deg": float(move_result.requested_angle_deg),
                "executed_steps": int(move_result.executed_steps),
                "executed_angle_deg": float(move_result.executed_angle_deg),
                "ack_status": str(move_result.status),
                "ack": str(move_result.raw_response),
                "angle_confidence": str(move_result.angle_confidence),
                "completed_at": utc_timestamp(),
            }
            segment_records.append(record)
            current_state = arm.expected_relative_state()
            expected_absolute_offset = (
                int(start_state["expected_offset_steps"])
                + int(segment.expected_offset_after_segment)
            )
            if int(current_state["expected_offset_steps"]) != expected_absolute_offset:
                raise RuntimeError(
                    "Rinse-arm expected offset did not match the acknowledged segment."
                )
            if (
                segment.segment_index == 3
                and int(current_state["expected_offset_steps"])
                != int(start_state["expected_offset_steps"])
            ):
                raise RuntimeError(
                    f"Rinse-arm cycle {segment.cycle_index} did not return "
                    "to its tracked starting offset."
                )
            log_fn(
                run_dir,
                (
                    f"{label}: rinse-arm cycle {segment.cycle_index}/{cycles}, "
                    f"segment {segment.segment_index}/3, requested={segment.relative_steps} "
                    f"steps ({segment.direction}), ack={move_result.raw_response}."
                ),
            )
            if (
                float(pause_between_moves_s) > 0
                and segment_index < len(segments) - 1
            ):
                pause(float(pause_between_moves_s))

        end_state = arm.expected_relative_state()
        expected_end = int(start_state["expected_offset_steps"])
        if int(end_state["expected_offset_steps"]) != expected_end:
            raise RuntimeError(
                "Rinse-arm expected offset did not return to its package start."
            )
        if end_state["angle_confidence"] != "tracked":
            raise RuntimeError("Rinse-arm angle confidence became uncertain.")

        result.update(
            {
                "status": "completed",
                "completed_at": utc_timestamp(),
                "ending_expected_offset_steps": int(end_state["expected_offset_steps"]),
                "final_expected_offset_steps": int(end_state["expected_offset_steps"]),
                "angle_confidence": str(end_state["angle_confidence"]),
                "net_commanded_steps": 0,
                "net_relative_steps": 0,
                "cycles_completed": int(cycles),
                "segments_completed": len(segment_records),
            }
        )
        record_fn(run_dir, result)
        log_fn(
            run_dir,
            (
                f"{label}: rinse-arm oscillation completed; "
                "net commanded movement=0 and expected offset returned to package start."
            ),
        )
        return result

    except Exception as exc:
        mark_uncertain = getattr(arm, "mark_angle_uncertain", None)
        if callable(mark_uncertain):
            mark_uncertain()
        end_state = arm.expected_relative_state()
        if isinstance(exc, RotationMoveInterrupted):
            partial = exc.result
            failed_segment = segments[len(segment_records)]
            segment_records.append(
                {
                    **asdict(failed_segment),
                    "requested_angle_deg": float(partial.requested_angle_deg),
                    "executed_steps": int(partial.executed_steps),
                    "executed_angle_deg": float(partial.executed_angle_deg),
                    "ack_status": str(partial.status),
                    "ack": str(partial.raw_response),
                    "angle_confidence": "uncertain",
                    "completed_at": utc_timestamp(),
                }
            )
        result.update(
            {
                "status": "failed",
                "completed_at": utc_timestamp(),
                "ending_expected_offset_steps": int(end_state["expected_offset_steps"]),
                "angle_confidence": "uncertain",
                "error": str(exc),
            }
        )
        completed_records = [
            item
            for item in segment_records
            if item.get("ack_status") == "completed"
        ]
        result["cycles_completed"] = len(completed_records) // 3
        result["segments_completed"] = len(completed_records)
        failed_index = len(completed_records)
        if failed_index < len(segments):
            failed_segment = segments[failed_index]
            result["failed_cycle"] = failed_segment.cycle_index
            result["failed_segment"] = failed_segment.segment_index
            result["requested_segment_steps"] = failed_segment.relative_steps
        if isinstance(exc, RotationMoveInterrupted):
            result["executed_segment_steps"] = int(exc.result.executed_steps)
            result["interrupted"] = True
        record_fn(run_dir, result)
        log_fn(
            run_dir,
            (
                f"{label}: rinse-arm oscillation stopped after "
                f"{len(segment_records)}/{len(segments)} segment records: {exc}. "
                "Angle is uncertain; no automatic reverse or home was attempted."
            ),
        )
        raise
