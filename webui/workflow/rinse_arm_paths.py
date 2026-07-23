from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from hardware.rotation_controller import angle_to_steps, degrees_per_step


@dataclass(frozen=True)
class ArmOscillationSegment:
    cycle_index: int
    segment_index: int
    label: str
    relative_steps: int
    direction: str
    expected_offset_after_segment: int


def validate_rinse_arm_settings(
    *,
    amplitude_deg: Any,
    cycles: Any,
    pause_between_moves_s: Any,
    return_to_start: Any,
    motor_full_steps_per_rev: int,
    microstep: int,
    max_relative_steps: int,
) -> dict[str, int | float | bool]:
    try:
        amplitude = float(amplitude_deg)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("amplitude_deg must be a finite number.") from exc
    if not math.isfinite(amplitude) or amplitude <= 0:
        raise ValueError("amplitude_deg must be a finite number greater than 0.")

    if isinstance(cycles, bool):
        raise ValueError("cycles must be a positive integer.")
    try:
        cycle_count = int(cycles)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("cycles must be a positive integer.") from exc
    try:
        cycles_are_integral = float(cycles) == float(cycle_count)
    except (TypeError, ValueError, OverflowError):
        cycles_are_integral = False
    if cycle_count <= 0 or not cycles_are_integral:
        raise ValueError("cycles must be a positive integer.")

    try:
        pause_s = float(pause_between_moves_s)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("pause_between_moves_s must be a finite number.") from exc
    if not math.isfinite(pause_s) or pause_s < 0:
        raise ValueError("pause_between_moves_s must be finite and cannot be negative.")

    if return_to_start is not True:
        raise ValueError("return_to_start must remain enabled for rinse arm oscillation.")

    maximum = int(max_relative_steps)
    if maximum <= 0:
        raise ValueError("max_relative_steps must be greater than 0.")

    amplitude_steps = abs(
        angle_to_steps(
            amplitude,
            motor_full_steps_per_rev=int(motor_full_steps_per_rev),
            microstep=int(microstep),
        )
    )
    if amplitude_steps == 0:
        raise ValueError("amplitude_deg rounds to zero motor steps.")
    if 2 * amplitude_steps > maximum:
        step_angle = degrees_per_step(
            int(motor_full_steps_per_rev),
            int(microstep),
        )
        max_amplitude = (maximum // 2) * step_angle
        raise ValueError(
            "amplitude_deg is too large for the symmetric +A, -2A, +A path; "
            f"the maximum converted amplitude is {maximum // 2} steps "
            f"({max_amplitude:g} degrees of commanded movement)."
        )

    return {
        "amplitude_deg": amplitude,
        "amplitude_steps": amplitude_steps,
        "cycles": cycle_count,
        "pause_between_moves_s": pause_s,
        "return_to_start": True,
        "degrees_per_step": degrees_per_step(
            int(motor_full_steps_per_rev),
            int(microstep),
        ),
    }


def build_symmetric_arm_oscillation(
    amplitude_steps: int,
    cycles: int,
    *,
    max_relative_steps: int | None = None,
) -> list[ArmOscillationSegment]:
    if isinstance(amplitude_steps, bool) or not isinstance(amplitude_steps, int):
        raise ValueError("amplitude_steps must be a positive integer.")
    if amplitude_steps <= 0:
        raise ValueError("amplitude_steps must be a positive integer.")
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= 0:
        raise ValueError("cycles must be a positive integer.")
    if max_relative_steps is not None:
        maximum = int(max_relative_steps)
        if maximum <= 0:
            raise ValueError("max_relative_steps must be greater than 0.")
        if 2 * amplitude_steps > maximum:
            raise ValueError(
                f"symmetric center crossing requires {-2 * amplitude_steps} steps, "
                f"which exceeds +/-{maximum}."
            )

    segments: list[ArmOscillationSegment] = []
    offset = 0
    cycle_commands = (amplitude_steps, -2 * amplitude_steps, amplitude_steps)

    for cycle in range(1, cycles + 1):
        cycle_start = offset
        for segment_index, command in enumerate(cycle_commands, start=1):
            offset += command
            segments.append(
                ArmOscillationSegment(
                    cycle_index=cycle,
                    segment_index=segment_index,
                    label=(
                        f"Cycle {cycle}, segment {segment_index}: "
                        f"{'CCW' if command > 0 else 'CW'} "
                        f"{command:+d} steps"
                    ),
                    relative_steps=command,
                    direction="CCW" if command > 0 else "CW",
                    expected_offset_after_segment=offset,
                )
            )
        if offset != cycle_start:
            raise AssertionError("symmetric rinse-arm cycle did not return to its start.")

    if offset != 0:
        raise AssertionError("rinse-arm path did not have zero net commanded movement.")
    return segments
