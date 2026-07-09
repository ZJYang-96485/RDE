from __future__ import annotations

from typing import Any

from workflow.config_loader import (
    get_internal_axis_limit,
    get_max_axis_command,
    get_rde_limits,
    get_safe_z,
    get_user_axis_limit,
    user_axis_to_internal_axis,
)
from workflow.state import get_axis_position, get_axis_positions


class SafetyError(ValueError):
    pass


def validate_rpm(rpm: int) -> int:
    rpm = int(rpm)
    limits = get_rde_limits()

    rpm_min = int(limits["rpm_min"])
    rpm_max = int(limits["rpm_max"])

    if rpm < rpm_min or rpm > rpm_max:
        raise SafetyError(f"rpm must be between {rpm_min} and {rpm_max}.")

    return rpm


def validate_duration_seconds(duration_seconds: int | float) -> float:
    duration = float(duration_seconds)

    if duration <= 0:
        raise SafetyError("duration_seconds must be > 0.")

    return duration


def validate_axis_command(steps: int) -> int:
    steps = int(steps)

    if steps == 0:
        raise SafetyError("axis command cannot be 0.")

    max_command = get_max_axis_command()

    if abs(steps) > max_command:
        raise SafetyError(f"axis command cannot exceed ±{max_command} steps.")

    return steps


def validate_axis_position(internal_axis: str, position: int) -> int:
    axis = str(internal_axis).strip().lower()
    position = int(position)

    low, high = get_internal_axis_limit(axis)

    if position < low or position > high:
        raise SafetyError(f"{axis} position must be between {low} and {high}.")

    return position


def validate_axis_move(internal_axis: str, steps: int) -> int:
    axis = str(internal_axis).strip().lower()
    steps = validate_axis_command(steps)

    current_position = get_axis_position(axis)
    target_position = int(current_position) + int(steps)

    validate_axis_position(axis, target_position)
    return steps


def validate_user_axis_position(user_axis: str, position: int) -> int:
    axis = str(user_axis).strip().lower()
    position = int(position)

    low, high = get_user_axis_limit(axis)

    if position < low or position > high:
        raise SafetyError(f"{axis} position must be between {low} and {high}.")

    return position


def validate_xyz_position(x: int, y: int, z: int) -> dict[str, int]:
    x = validate_user_axis_position("x", int(x))
    y = validate_user_axis_position("y", int(y))
    z = validate_user_axis_position("z", int(z))

    return {
        "x": x,
        "y": y,
        "z": z,
    }


def user_xyz_to_internal_positions(x: int, y: int, z: int) -> dict[str, int]:
    validate_xyz_position(x, y, z)

    values = {
        "x": int(x),
        "y": int(y),
        "z": int(z),
    }

    converted = {}

    for user_axis, value in values.items():
        internal_axis = user_axis_to_internal_axis(user_axis)
        converted[internal_axis] = value

    return converted


def axis_ack_timeout_seconds(steps: int) -> float:
    """
    Estimate the longest expected movement time and add a generous margin.

    This intentionally uses the slower Nano/X-axis pulse timing (2000 us base)
    so the same timeout is safe for both X and Z controllers.

    Firmware speed schedule:
      <= 100 steps:   multiplier 1
      <= 1000:        multiplier 2
      <= 10000:       multiplier 5
      > 10000:        multiplier 10

    Each step has one HIGH and one LOW pulse delay, so movement time is:
      steps * 2 * pulse_us
    """
    steps_abs = abs(int(steps))

    if steps_abs <= 100:
        multiplier = 1
    elif steps_abs <= 1000:
        multiplier = 2
    elif steps_abs <= 10000:
        multiplier = 5
    else:
        multiplier = 10

    base_pulse_us = 2000
    min_pulse_us = 50
    pulse_us = max(min_pulse_us, int(base_pulse_us / multiplier))

    estimated_motion_s = steps_abs * (2.0 * pulse_us) / 1_000_000.0

    # 50% motion margin + 8 s for serial startup, scheduling, and ACK handling.
    timeout_s = estimated_motion_s * 1.5 + 8.0

    return max(10.0, min(600.0, timeout_s))


def safe_z_delta() -> int:
    safe_z = get_safe_z()
    current_z = get_axis_position("linear")
    return int(safe_z) - int(current_z)


def needs_safe_z_move() -> bool:
    return safe_z_delta() != 0


def home_axis_delta(internal_axis: str) -> int:
    axis = str(internal_axis).strip().lower()
    current = get_axis_position(axis)
    return -int(current)


def home_deltas() -> dict[str, int]:
    positions = get_axis_positions()

    return {
        "linear": -int(positions["linear"]),
        "horizontal": -int(positions["horizontal"]),
        "vertical": -int(positions["vertical"]),
    }


def validate_sample_motion_fields(sample: dict[str, Any]) -> dict[str, int]:
    if not isinstance(sample, dict):
        raise SafetyError("sample must be an object.")

    position = sample.get("position", {})

    if not isinstance(position, dict):
        raise SafetyError("sample.position must be an object.")

    x = int(position.get("x", 0))
    y = int(position.get("y", 0))
    z = int(position.get("z", 0))

    return validate_xyz_position(x, y, z)
