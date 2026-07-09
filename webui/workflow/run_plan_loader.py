from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_path, load_config

MAX_RUN_NAME_LENGTH = 80

ATOMIC_ACTIONS = {
    "move_x",
    "move_z",
    "rotation",
    "set_rpm",
    "wait",
    "echem",
    "stop_rpm",
    "rinse",
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
    name = optional_string(raw_step.get("name"), f"Step {index}")

    if action not in ATOMIC_ACTIONS:
        raise RunPlanError(f"{group_label}: step {index} has unsupported action '{action}'.")

    step: dict[str, Any] = {
        "name": name or f"Step {index}",
        "action": action,
        "enabled": bool(raw_step.get("enabled", True)),
    }

    if action in {"move_x", "move_z"}:
        step["position"] = parse_int(raw_step.get("position", 0), f"{group_label}/{name}: position")

    elif action == "rotation":
        command = optional_string(raw_step.get("command"))
        if not command:
            raise RunPlanError(f"{group_label}/{name}: rotation command cannot be empty.")
        step["command"] = command

    elif action == "set_rpm":
        rpm = parse_int(raw_step.get("rpm", 0), f"{group_label}/{name}: rpm")
        if rpm < 0:
            raise RunPlanError(f"{group_label}/{name}: rpm cannot be negative.")
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
        "rinse_after": bool(raw_sample.get("rinse_after", False)),
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


def save_run_plan(payload: dict[str, Any]) -> dict[str, Any]:
    plan = validate_run_plan_payload(payload)
    plan["saved_at"] = datetime.now(timezone.utc).isoformat()
    path = run_plan_path_for_name(plan["run_name"])

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
