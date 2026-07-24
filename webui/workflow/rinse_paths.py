from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from workflow.config_loader import get_rde_limits, get_rotation_config
from workflow.rinse_arm_paths import validate_rinse_arm_settings
from workflow.safety import validate_axis_command, validate_rpm


MIN_RINSE_CYCLES = 1
MAX_RINSE_CYCLES = 20


@dataclass(frozen=True)
class DiamondSegment:
    cycle_index: int
    segment_index: int
    x_steps: int
    z_steps: int
    expected_x_offset_after_segment: int
    expected_z_offset_after_segment: int


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    try:
        parsed = int(value)
        if float(value) != float(parsed):
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc
    return parsed


def _nonnegative_float(value: Any, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a finite number.") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field_name} must be finite and cannot be negative.")
    return parsed


def _required_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be true or false.")
    return value


def build_diamond_cycle(
    x_radius_steps: int,
    z_radius_steps: int,
    *,
    cycle_index: int = 1,
) -> list[DiamondSegment]:
    x_radius = _integer(x_radius_steps, "diamond.x_radius_steps")
    z_radius = _integer(z_radius_steps, "diamond.z_radius_steps")
    cycle = _integer(cycle_index, "cycle_index")

    if x_radius <= 0:
        raise ValueError("diamond.x_radius_steps must be a positive integer.")
    if z_radius <= 0:
        raise ValueError("diamond.z_radius_steps must be a positive integer.")
    if cycle <= 0:
        raise ValueError("cycle_index must be a positive integer.")

    commands = (
        (x_radius, -z_radius),
        (-2 * x_radius, 0),
        (0, 2 * z_radius),
        (2 * x_radius, 0),
        (-x_radius, -z_radius),
    )
    x_offset = 0
    z_offset = 0
    segments: list[DiamondSegment] = []

    for segment_index, (x_steps, z_steps) in enumerate(commands, start=1):
        if x_steps:
            validate_axis_command(x_steps)
        if z_steps:
            validate_axis_command(z_steps)
        x_offset += x_steps
        z_offset += z_steps
        segments.append(
            DiamondSegment(
                cycle_index=cycle,
                segment_index=segment_index,
                x_steps=x_steps,
                z_steps=z_steps,
                expected_x_offset_after_segment=x_offset,
                expected_z_offset_after_segment=z_offset,
            )
        )

    if x_offset != 0 or z_offset != 0:
        raise AssertionError("diamond cycle did not form a closed X/Z path.")

    return segments


def validate_rinse_settings(
    *,
    cycles: Any,
    diamond: Any,
    arm_oscillation: Any,
    disk_rotation: Any,
    inter_cycle_pause_s: Any,
    cycle_timeout_s: Any,
    require_closed_paths: Any,
) -> dict[str, Any]:
    cycle_count = _integer(cycles, "cycles")
    if not MIN_RINSE_CYCLES <= cycle_count <= MAX_RINSE_CYCLES:
        raise ValueError(
            f"cycles must be between {MIN_RINSE_CYCLES} and {MAX_RINSE_CYCLES}."
        )

    if not isinstance(diamond, dict):
        raise ValueError("diamond must be an object.")
    x_radius = _integer(
        diamond.get("x_radius_steps", 5000),
        "diamond.x_radius_steps",
    )
    z_radius = _integer(
        diamond.get("z_radius_steps", 7000),
        "diamond.z_radius_steps",
    )
    diamond_path = build_diamond_cycle(x_radius, z_radius)

    if not isinstance(arm_oscillation, dict):
        raise ValueError("arm_oscillation must be an object.")
    arm_enabled = _required_bool(
        arm_oscillation.get("enabled", True),
        "arm_oscillation.enabled",
    )
    if "cycles" in arm_oscillation:
        raise ValueError(
            "arm_oscillation.cycles is not allowed; the arm must run "
            "continuously until all diamond cycles complete."
        )
    if (
        "return_to_start" in arm_oscillation
        and arm_oscillation["return_to_start"] is not True
    ):
        raise ValueError(
            "arm_oscillation.return_to_start must be true for a closed path."
        )
    arm_mode = str(
        arm_oscillation.get("mode", "continuous_until_diamond_complete")
    ).strip()
    if arm_mode != "continuous_until_diamond_complete":
        raise ValueError(
            "arm_oscillation.mode must be 'continuous_until_diamond_complete'."
        )
    arm_stop_policy = str(
        arm_oscillation.get("stop_policy", "finish_closed_cycle")
    ).strip()
    if arm_stop_policy != "finish_closed_cycle":
        raise ValueError(
            "arm_oscillation.stop_policy must be 'finish_closed_cycle'."
        )

    rotation = get_rotation_config()
    arm_settings = validate_rinse_arm_settings(
        amplitude_deg=arm_oscillation.get("amplitude_deg", 2.0),
        cycles=1,
        pause_between_moves_s=arm_oscillation.get(
            "pause_between_moves_s",
            0.1,
        ),
        return_to_start=True,
        motor_full_steps_per_rev=int(rotation["motor_full_steps_per_rev"]),
        microstep=int(rotation["microstep"]),
        max_relative_steps=int(rotation["max_relative_steps"]),
    )

    if not isinstance(disk_rotation, dict):
        raise ValueError("disk_rotation must be an object.")
    disk_enabled = _required_bool(
        disk_rotation.get("enabled", True),
        "disk_rotation.enabled",
    )
    rpm = _integer(disk_rotation.get("rpm", 300), "disk_rotation.rpm")
    settle_s = _nonnegative_float(
        disk_rotation.get("settle_s", 1.0),
        "disk_rotation.settle_s",
    )
    disk_mode = str(
        disk_rotation.get("mode", "continuous_for_entire_rinse_step")
    ).strip()
    if disk_mode != "continuous_for_entire_rinse_step":
        raise ValueError(
            "disk_rotation.mode must be 'continuous_for_entire_rinse_step'."
        )
    stop_after = _required_bool(
        disk_rotation.get("stop_after", True),
        "disk_rotation.stop_after",
    )
    if not stop_after:
        raise ValueError("disk_rotation.stop_after must be true.")
    immersed_confirmed = _required_bool(
        disk_rotation.get("immersed_rotation_confirmed", False),
        "disk_rotation.immersed_rotation_confirmed",
    )

    if disk_enabled:
        validate_rpm(rpm)
        limits = get_rde_limits()
        rinse_rpm_max = int(limits.get("rinse_rpm_max", min(300, limits["rpm_max"])))
        if rpm > rinse_rpm_max:
            raise ValueError(
                f"disk_rotation.rpm cannot exceed the rinse-specific "
                f"limit of {rinse_rpm_max} RPM."
            )
        if not immersed_confirmed:
            raise ValueError(
                "disk_rotation.immersed_rotation_confirmed must be true "
                "when immersed disk rotation is enabled."
            )
    elif rpm < 0:
        raise ValueError("disk_rotation.rpm cannot be negative.")

    inter_cycle_pause = _nonnegative_float(
        inter_cycle_pause_s,
        "inter_cycle_pause_s",
    )
    cycle_timeout = _nonnegative_float(cycle_timeout_s, "cycle_timeout_s")
    if cycle_timeout <= 0:
        raise ValueError("cycle_timeout_s must be greater than 0.")
    closed_paths = _required_bool(require_closed_paths, "require_closed_paths")
    if not closed_paths:
        raise ValueError("require_closed_paths must be true.")

    if (
        diamond_path[-1].expected_x_offset_after_segment != 0
        or diamond_path[-1].expected_z_offset_after_segment != 0
    ):
        raise ValueError("diamond path must be closed.")

    return {
        "cycles": cycle_count,
        "diamond": {
            "x_radius_steps": x_radius,
            "z_radius_steps": z_radius,
        },
        "arm_oscillation": {
            "enabled": arm_enabled,
            "amplitude_deg": float(arm_settings["amplitude_deg"]),
            "amplitude_steps": int(arm_settings["amplitude_steps"]),
            "pause_between_moves_s": float(
                arm_settings["pause_between_moves_s"]
            ),
            "mode": arm_mode,
            "stop_policy": arm_stop_policy,
        },
        "disk_rotation": {
            "enabled": disk_enabled,
            "rpm": rpm,
            "settle_s": settle_s,
            "mode": disk_mode,
            "stop_after": True,
            "immersed_rotation_confirmed": immersed_confirmed,
        },
        "inter_cycle_pause_s": inter_cycle_pause,
        "cycle_timeout_s": cycle_timeout,
        "require_closed_paths": True,
    }
