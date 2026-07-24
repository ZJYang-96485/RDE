from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_path, get_rotation_config, load_config
from workflow.rinse_arm_paths import validate_rinse_arm_settings
from workflow.rinse_paths import validate_rinse_settings
from workflow.safety import validate_axis_command, validate_rpm

MAX_RUN_NAME_LENGTH = 80

ATOMIC_ACTIONS = {
    "move_x",
    "move_z",
    "move_xz_parallel",
    "rotation",
    "rinse",
    "rinse_arm_oscillation",
    "set_rpm",
    "wait",
    "echem",
    "rpm_echem",
    "stop_rpm",
    "gamry_cell_on",
    "gamry_cell_off",
}


class RunPlanError(ValueError):
    pass


def run_plans_dir() -> Path:
    path = get_path("run_plans_dir")
    path.mkdir(parents=True, exist_ok=True)
    return path


def automation_limits() -> tuple[int, int]:
    config = load_config()
    automation = config.get("automation", {})
    max_groups = int(automation.get("max_samples", automation.get("max_groups", 100)))
    max_steps = int(automation.get("max_run_steps", 1000))
    return max_groups, max_steps


def normalize_run_name(raw_name: Any) -> str:
    name = str(raw_name or "").strip()

    if not name:
        raise RunPlanError("run plan name cannot be empty.")

    if len(name) > MAX_RUN_NAME_LENGTH:
        raise RunPlanError(f"run plan name cannot be longer than {MAX_RUN_NAME_LENGTH} characters.")

    name = re.sub(r"[^A-Za-z0-9 _.-]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")

    if not name:
        raise RunPlanError("run plan name must contain at least one letter or number.")

    return name


def run_plan_file_name(raw_name: Any) -> str:
    return normalize_run_name(raw_name).replace(" ", "_") + ".json"


def run_plan_path_for_name(raw_name: Any) -> Path:
    return run_plans_dir() / run_plan_file_name(raw_name)


def optional_string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def parse_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RunPlanError(f"{field_name} must be an integer.") from exc


def parse_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RunPlanError(f"{field_name} must be a number.") from exc


def parse_nonnegative_float(value: Any, field_name: str) -> float:
    result = parse_float(value, field_name)
    if result < 0:
        raise RunPlanError(f"{field_name} cannot be negative.")
    return result


def validate_atomic_step(raw_step: dict[str, Any], group_label: str, index: int) -> dict[str, Any]:
    if not isinstance(raw_step, dict):
        raise RunPlanError(f"{group_label}: step {index} must be an object.")

    action = optional_string(raw_step.get("action") or raw_step.get("type")).lower()
    name = optional_string(
        raw_step.get("name") or raw_step.get("label"),
        f"Step {index}",
    )

    if action not in ATOMIC_ACTIONS:
        raise RunPlanError(f"{group_label}: step {index} has unsupported action '{action}'.")

    step: dict[str, Any] = {
        "name": name or f"Step {index}",
        "action": action,
        "enabled": bool(raw_step.get("enabled", True)),
    }

    if action in {"move_x", "move_z"}:
        # Signed relative steps, matching Motor Control exactly.
        raw_steps = raw_step.get("steps", raw_step.get("position", 0))
        step["steps"] = parse_int(raw_steps, f"{group_label}/{name}: steps")
        if step["steps"] == 0:
            raise RunPlanError(f"{group_label}/{name}: steps cannot be 0.")

        try:
            validate_axis_command(step["steps"])
        except Exception as exc:
            raise RunPlanError(f"{group_label}/{name}: {exc}") from exc

    elif action == "move_xz_parallel":
        step["x_steps"] = parse_int(
            raw_step.get("x_steps", 0),
            f"{group_label}/{name}: x_steps",
        )
        step["z_steps"] = parse_int(
            raw_step.get("z_steps", 0),
            f"{group_label}/{name}: z_steps",
        )

        if step["x_steps"] == 0 and step["z_steps"] == 0:
            raise RunPlanError(
                f"{group_label}/{name}: x_steps and z_steps cannot both be 0."
            )

        try:
            if step["x_steps"] != 0:
                validate_axis_command(step["x_steps"])
            if step["z_steps"] != 0:
                validate_axis_command(step["z_steps"])
        except Exception as exc:
            raise RunPlanError(f"{group_label}/{name}: {exc}") from exc

    elif action == "rotation":
        command = optional_string(raw_step.get("command"))
        if not command:
            raise RunPlanError(f"{group_label}/{name}: rotation command cannot be empty.")
        step["command"] = command

    elif action == "rinse_arm_oscillation":
        rotation = get_rotation_config()
        defaults = rotation.get("rinse_oscillation", {})
        enabled_value = raw_step.get(
            "oscillation_enabled",
            defaults.get("enabled", False),
        )
        if not isinstance(enabled_value, bool):
            raise RunPlanError(
                f"{group_label}/{name}: oscillation_enabled must be true or false."
            )
        oscillation_enabled = enabled_value
        try:
            settings = validate_rinse_arm_settings(
                amplitude_deg=raw_step.get(
                    "amplitude_deg",
                    defaults.get("amplitude_deg", 5.0),
                ),
                cycles=raw_step.get("cycles", defaults.get("cycles", 3)),
                pause_between_moves_s=raw_step.get(
                    "pause_between_moves_s",
                    defaults.get("pause_between_moves_s", 0.2),
                ),
                return_to_start=raw_step.get(
                    "return_to_start",
                    defaults.get("return_to_start", True),
                ),
                motor_full_steps_per_rev=int(rotation["motor_full_steps_per_rev"]),
                microstep=int(rotation["microstep"]),
                max_relative_steps=int(rotation["max_relative_steps"]),
            )
        except (TypeError, ValueError) as exc:
            raise RunPlanError(f"{group_label}/{name}: {exc}") from exc

        step["oscillation_enabled"] = oscillation_enabled
        step.update(settings)

    elif action == "rinse":
        try:
            rinse_settings = validate_rinse_settings(
                cycles=raw_step.get("cycles", 8),
                diamond=raw_step.get(
                    "diamond",
                    {
                        "x_radius_steps": 5000,
                        "z_radius_steps": 7000,
                    },
                ),
                arm_oscillation=raw_step.get(
                    "arm_oscillation",
                    {
                        "enabled": True,
                        "amplitude_deg": 2.0,
                        "pause_between_moves_s": 0.1,
                        "mode": "continuous_until_diamond_complete",
                        "stop_policy": "finish_closed_cycle",
                    },
                ),
                disk_rotation=raw_step.get(
                    "disk_rotation",
                    {
                        "enabled": True,
                        "rpm": 300,
                        "settle_s": 1.0,
                        "mode": "continuous_for_entire_rinse_step",
                        "stop_after": True,
                        "immersed_rotation_confirmed": False,
                    },
                ),
                inter_cycle_pause_s=raw_step.get(
                    "inter_cycle_pause_s",
                    0.0,
                ),
                cycle_timeout_s=raw_step.get("cycle_timeout_s", 30.0),
                require_closed_paths=raw_step.get(
                    "require_closed_paths",
                    True,
                ),
            )
        except (TypeError, ValueError) as exc:
            raise RunPlanError(f"{group_label}/{name}: {exc}") from exc
        step.update(rinse_settings)

    elif action == "set_rpm":
        rpm = parse_int(raw_step.get("rpm", 0), f"{group_label}/{name}: rpm")
        if rpm < 0:
            raise RunPlanError(f"{group_label}/{name}: rpm cannot be negative.")

        if rpm > 0:
            try:
                validate_rpm(rpm)
            except Exception as exc:
                raise RunPlanError(f"{group_label}/{name}: {exc}") from exc

        step["rpm"] = rpm

    elif action == "wait":
        step["duration_s"] = parse_nonnegative_float(
            raw_step.get("duration_s", 0),
            f"{group_label}/{name}: duration_s",
        )

    elif action == "echem":
        protocol = optional_string(raw_step.get("protocol"))
        if not protocol:
            raise RunPlanError(f"{group_label}/{name}: protocol cannot be empty.")
        step["protocol"] = protocol

    elif action == "rpm_echem":
        rpm = parse_int(raw_step.get("rpm", 1600), f"{group_label}/{name}: rpm")
        if rpm <= 0:
            raise RunPlanError(f"{group_label}/{name}: rpm must be > 0.")

        try:
            validate_rpm(rpm)
        except Exception as exc:
            raise RunPlanError(f"{group_label}/{name}: {exc}") from exc

        protocol = optional_string(raw_step.get("protocol"))
        if not protocol:
            raise RunPlanError(f"{group_label}/{name}: protocol cannot be empty.")

        step["rpm"] = rpm
        step["protocol"] = protocol
        step["rpm_settle_s"] = parse_nonnegative_float(
            raw_step.get("rpm_settle_s", 0),
            f"{group_label}/{name}: rpm_settle_s",
        )
        step["stop_rpm_after"] = bool(raw_step.get("stop_rpm_after", True))

    elif action == "gamry_cell_on":
        raw_duration = raw_step.get("duration_s")
        if raw_duration is None or raw_duration == "" or raw_duration == 0 or raw_duration == "0":
            step["duration_s"] = None
        else:
            duration_s = parse_float(
                raw_duration,
                f"{group_label}/{name}: duration_s",
            )
            if not math.isfinite(duration_s) or duration_s <= 0:
                raise RunPlanError(
                    f"{group_label}/{name}: duration_s must be greater than 0, blank, or null."
                )
            step["duration_s"] = duration_s

    return step


def validate_group(raw_group: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw_group, dict):
        raise RunPlanError(f"group {index} must be an object.")

    label = optional_string(raw_group.get("label") or raw_group.get("name"), f"Group {index}")
    group_id = optional_string(raw_group.get("group_id"), f"group_{index:03d}")
    raw_steps = raw_group.get("steps", [])

    if not isinstance(raw_steps, list):
        raise RunPlanError(f"{label}: steps must be a list.")

    steps = [validate_atomic_step(step, label, step_index) for step_index, step in enumerate(raw_steps, start=1)]

    return {
        "group_id": group_id,
        "label": label,
        "enabled": bool(raw_group.get("enabled", True)),
        "steps": steps,
    }


def validate_grouped_payload(payload: dict[str, Any]) -> dict[str, Any]:
    run_name = normalize_run_name(payload.get("run_name") or payload.get("name"))
    display_name = optional_string(payload.get("display_name"), run_name)
    description = optional_string(payload.get("description"), "")
    repetitions = parse_int(payload.get("repetitions", 1), "repetitions")

    if repetitions < 1 or repetitions > 100:
        raise RunPlanError("repetitions must be between 1 and 100.")

    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list):
        raise RunPlanError("groups must be a list.")

    max_groups, max_steps = automation_limits()
    if len(raw_groups) > max_groups:
        raise RunPlanError(f"run plan cannot contain more than {max_groups} groups.")

    groups = [validate_group(group, index) for index, group in enumerate(raw_groups, start=1)]
    total_steps = sum(len(group["steps"]) for group in groups)

    if total_steps > max_steps:
        raise RunPlanError(f"run plan cannot contain more than {max_steps} atomic steps.")

    return {
        "schema_version": 2,
        "run_name": run_name,
        "display_name": display_name,
        "description": description,
        "repetitions": repetitions,
        "groups": groups,
        "saved_at": payload.get("saved_at"),
    }


def validate_sample(raw_sample: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw_sample, dict):
        raise RunPlanError(f"sample {index} must be an object.")

    label = optional_string(raw_sample.get("label") or raw_sample.get("sample_id"), f"Sample {index}")
    if bool(raw_sample.get("rinse_after", False)):
        raise RunPlanError(
            f"{label}: rinse_after is no longer supported. "
            "Build the rinse sequence from explicit motion, rotation, RPM, and wait steps."
        )

    position = raw_sample.get("position", {})
    if not isinstance(position, dict):
        raise RunPlanError(f"{label}: position must be an object.")

    return {
        "sample_id": optional_string(raw_sample.get("sample_id"), f"sample_{index:03d}"),
        "label": label,
        "enabled": bool(raw_sample.get("enabled", True)),
        "position": {
            "x": parse_int(position.get("x", 0), f"{label}: position.x"),
            "y": parse_int(position.get("y", 0), f"{label}: position.y"),
            "z": parse_int(position.get("z", 0), f"{label}: position.z"),
        },
        "rpm": parse_int(raw_sample.get("rpm", 0), f"{label}: rpm"),
        "stabilization_s": parse_nonnegative_float(raw_sample.get("stabilization_s", 0), f"{label}: stabilization_s"),
        "protocol": optional_string(raw_sample.get("protocol"), "ocp_only"),
        "rotation_command": optional_string(raw_sample.get("rotation_command"), ""),
        "post_echem_wait_s": parse_nonnegative_float(raw_sample.get("post_echem_wait_s", 0), f"{label}: post_echem_wait_s"),
    }


def validate_legacy_sample_payload(payload: dict[str, Any]) -> dict[str, Any]:
    run_name = normalize_run_name(payload.get("run_name") or payload.get("name"))
    repetitions = parse_int(payload.get("repetitions", 1), "repetitions")

    if repetitions < 1 or repetitions > 100:
        raise RunPlanError("repetitions must be between 1 and 100.")

    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list):
        raise RunPlanError("samples must be a list.")

    max_groups, _ = automation_limits()
    if len(raw_samples) > max_groups:
        raise RunPlanError(f"run plan cannot contain more than {max_groups} samples.")

    samples = [validate_sample(sample, index) for index, sample in enumerate(raw_samples, start=1)]

    return {
        "schema_version": 1,
        "run_name": run_name,
        "display_name": optional_string(payload.get("display_name"), run_name),
        "description": optional_string(payload.get("description"), ""),
        "repetitions": repetitions,
        "samples": samples,
        "saved_at": payload.get("saved_at"),
    }


def validate_run_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RunPlanError("run plan payload must be an object.")

    if "groups" in payload or int(payload.get("schema_version", 1) or 1) >= 2:
        return validate_grouped_payload(payload)

    return validate_legacy_sample_payload(payload)


def load_run_plan(name: str) -> dict[str, Any]:
    path = run_plan_path_for_name(name)
    if not path.exists():
        raise RunPlanError(f"run plan '{name}' does not exist.")

    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)

    return validate_run_plan_payload(payload)


def save_run_plan(payload: dict[str, Any], *, overwrite: bool = True) -> dict[str, Any]:
    plan = validate_run_plan_payload(payload)
    plan["saved_at"] = datetime.now(timezone.utc).isoformat()
    path = run_plan_path_for_name(plan["run_name"])
    if path.exists() and not overwrite:
        raise RunPlanError(
            f"run plan '{plan['run_name']}' already exists; choose a unique name for a new plan."
        )

    with path.open("w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
        f.write("\n")

    group_count = len(plan.get("groups", []))
    sample_count = len(plan.get("samples", []))
    step_count = sum(len(group.get("steps", [])) for group in plan.get("groups", []))

    return {
        "ok": True,
        "run_name": plan["run_name"],
        "display_name": plan["display_name"],
        "group_count": group_count,
        "sample_count": sample_count or group_count,
        "step_count": step_count,
        "path": str(path),
        "saved_at": plan["saved_at"],
    }


def create_blank_sample_run_plan() -> dict[str, Any]:
    return {
        "id": None,
        "name": "",
        "run_name": "",
        "display_name": "",
        "description": "",
        "repetitions": 1,
        "groups": [],
        "steps": [],
        "editor_mode": "create",
        "is_dirty": False,
    }


def delete_run_plan(name: str) -> dict[str, Any]:
    run_name = normalize_run_name(name)
    path = run_plan_path_for_name(run_name)

    if not path.exists():
        raise RunPlanError(f"run plan '{run_name}' does not exist.")

    path.unlink()
    return {"ok": True, "run_name": run_name}


def list_run_plans() -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []

    for path in sorted(run_plans_dir().glob("*.json")):
        try:
            with path.open("r", encoding="utf-8-sig") as f:
                payload = json.load(f)
            plan = validate_run_plan_payload(payload)

            group_count = len(plan.get("groups", []))
            sample_count = len(plan.get("samples", []))
            step_count = sum(len(group.get("steps", [])) for group in plan.get("groups", []))

            plans.append(
                {
                    "run_name": plan["run_name"],
                    "display_name": plan["display_name"],
                    "description": plan["description"],
                    "repetitions": plan["repetitions"],
                    "schema_version": plan.get("schema_version", 1),
                    "group_count": group_count,
                    "sample_count": sample_count or group_count,
                    "step_count": step_count,
                    "saved_at": plan.get("saved_at"),
                    "file": path.name,
                }
            )
        except Exception:
            continue

    return plans


def default_run_plan_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_name": "default",
        "display_name": "Default",
        "description": "Blank grouped run plan.",
        "repetitions": 1,
        "groups": [
            {
                "group_id": "group_001",
                "label": "Sample 1",
                "enabled": True,
                "steps": [],
            }
        ],
    }
