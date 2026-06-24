from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_path, load_config
from workflow.protocol_loader import normalize_protocol_name
from workflow.safety import validate_xyz_position, validate_rpm


MAX_RUN_NAME_LENGTH = 80


class RunPlanError(ValueError):
    pass


def run_plans_dir() -> Path:
    path = get_path("run_plans_dir")
    path.mkdir(parents=True, exist_ok=True)
    return path


def max_repetitions() -> int:
    return int(load_config()["automation"]["max_repetitions"])


def max_samples() -> int:
    return int(load_config()["automation"]["max_samples"])


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
    name = normalize_run_name(raw_name)
    return name.replace(" ", "_") + ".json"


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


def parse_bool(value: Any) -> bool:
    return bool(value)


def default_sample_z() -> int:
    config = load_config()
    return int(config["motion"]["default_positions"].get("sample_z", 0))


def validate_repetitions(value: Any) -> int:
    repetitions = parse_int(value, "repetitions")

    if repetitions <= 0:
        raise RunPlanError("repetitions must be > 0.")

    limit = max_repetitions()

    if repetitions > limit:
        raise RunPlanError(f"repetitions cannot exceed {limit}.")

    return repetitions


def validate_rpm_or_zero(value: Any, field_name: str = "rpm") -> int:
    rpm = parse_int(value, field_name)

    if rpm < 0:
        raise RunPlanError(f"{field_name} cannot be negative.")

    if rpm == 0:
        return 0

    try:
        validate_rpm(rpm)
    except Exception as exc:
        raise RunPlanError(str(exc)) from exc

    return rpm


def normalize_protocol_reference(value: Any) -> str:
    protocol = optional_string(value, "ocp_only")

    if not protocol:
        protocol = "ocp_only"

    try:
        return normalize_protocol_name(protocol)
    except Exception as exc:
        raise RunPlanError(f"invalid protocol name: {exc}") from exc


def validate_position(raw_position: Any, sample_label: str) -> dict[str, int]:
    if raw_position is None:
        raw_position = {}

    if not isinstance(raw_position, dict):
        raise RunPlanError(f"{sample_label}: position must be an object.")

    x = parse_int(raw_position.get("x", 0), f"{sample_label}: position.x")
    y = parse_int(raw_position.get("y", 0), f"{sample_label}: position.y")
    z = parse_int(raw_position.get("z", default_sample_z()), f"{sample_label}: position.z")

    try:
        validate_xyz_position(x, y, z)
    except Exception as exc:
        raise RunPlanError(f"{sample_label}: {exc}") from exc

    return {
        "x": x,
        "y": y,
        "z": z
    }


def validate_sample(raw_sample: Any, sample_index: int) -> dict[str, Any]:
    if not isinstance(raw_sample, dict):
        raise RunPlanError(f"sample {sample_index} must be an object.")

    sample_id = optional_string(raw_sample.get("sample_id"), f"sample_{sample_index:03d}")
    label = optional_string(raw_sample.get("label"), f"Sample {sample_index}")

    if not sample_id:
        sample_id = f"sample_{sample_index:03d}"

    if not label:
        label = sample_id

    sample_label = f"sample {sample_index} ({label})"

    position = validate_position(raw_sample.get("position", {}), sample_label)

    rpm = validate_rpm_or_zero(raw_sample.get("rpm", 0), f"{sample_label}: rpm")
    stabilization_s = parse_float(raw_sample.get("stabilization_s", 0), f"{sample_label}: stabilization_s")
    post_echem_wait_s = parse_float(raw_sample.get("post_echem_wait_s", 0), f"{sample_label}: post_echem_wait_s")

    if stabilization_s < 0:
        raise RunPlanError(f"{sample_label}: stabilization_s cannot be negative.")

    if post_echem_wait_s < 0:
        raise RunPlanError(f"{sample_label}: post_echem_wait_s cannot be negative.")

    protocol = normalize_protocol_reference(raw_sample.get("protocol", "ocp_only"))

    return {
        "sample_id": sample_id,
        "label": label,
        "enabled": parse_bool(raw_sample.get("enabled", True)),
        "position": position,
        "rpm": rpm,
        "stabilization_s": stabilization_s,
        "protocol": protocol,
        "rotation_command": optional_string(raw_sample.get("rotation_command"), ""),
        "post_echem_wait_s": post_echem_wait_s,
        "rinse_after": parse_bool(raw_sample.get("rinse_after", False)),
    }


def validate_run_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RunPlanError("run plan payload must be an object.")

    run_name = normalize_run_name(payload.get("run_name") or payload.get("name"))
    display_name = optional_string(payload.get("display_name"), run_name)
    description = optional_string(payload.get("description"), "")
    repetitions = validate_repetitions(payload.get("repetitions", 1))

    raw_samples = payload.get("samples")

    if raw_samples is None:
        raw_samples = payload.get("steps")

    if not isinstance(raw_samples, list):
        raise RunPlanError("samples must be a list.")

    if not raw_samples:
        raise RunPlanError("run plan must contain at least one sample.")

    sample_limit = max_samples()

    if len(raw_samples) > sample_limit:
        raise RunPlanError(f"run plan cannot contain more than {sample_limit} samples.")

    samples = [
        validate_sample(raw_sample, sample_index)
        for sample_index, raw_sample in enumerate(raw_samples, start=1)
    ]

    return {
        "run_name": run_name,
        "display_name": display_name,
        "description": description,
        "repetitions": repetitions,
        "samples": samples,
        "saved_at": payload.get("saved_at"),
    }


def load_run_plan(name: str) -> dict[str, Any]:
    path = run_plan_path_for_name(name)

    if not path.exists():
        raise RunPlanError(f"run plan '{name}' does not exist.")

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    return validate_run_plan_payload(payload)


def save_run_plan(payload: dict[str, Any]) -> dict[str, Any]:
    run_plan = validate_run_plan_payload(payload)
    run_plan["saved_at"] = datetime.now(timezone.utc).isoformat()

    path = run_plan_path_for_name(run_plan["run_name"])

    with path.open("w", encoding="utf-8") as f:
        json.dump(run_plan, f, indent=2)
        f.write("\n")

    return {
        "ok": True,
        "run_name": run_plan["run_name"],
        "display_name": run_plan["display_name"],
        "sample_count": len(run_plan["samples"]),
        "path": str(path),
        "saved_at": run_plan["saved_at"],
    }


def delete_run_plan(name: str) -> dict[str, Any]:
    run_name = normalize_run_name(name)
    path = run_plan_path_for_name(run_name)

    if not path.exists():
        raise RunPlanError(f"run plan '{run_name}' does not exist.")

    path.unlink()

    return {
        "ok": True,
        "run_name": run_name,
    }


def list_run_plans() -> list[dict[str, Any]]:
    run_plans = []

    for path in sorted(run_plans_dir().glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)

            run_plan = validate_run_plan_payload(payload)

            run_plans.append(
                {
                    "run_name": run_plan["run_name"],
                    "display_name": run_plan["display_name"],
                    "description": run_plan["description"],
                    "repetitions": run_plan["repetitions"],
                    "sample_count": len(run_plan["samples"]),
                    "enabled_sample_count": len(
                        [
                            sample
                            for sample in run_plan["samples"]
                            if bool(sample.get("enabled", True))
                        ]
                    ),
                    "saved_at": run_plan.get("saved_at"),
                    "file": path.name,
                }
            )
        except Exception:
            continue

    return run_plans


def default_run_plan_payload() -> dict[str, Any]:
    return {
        "run_name": "single_sample_test",
        "display_name": "Single Sample Test",
        "description": "Move to one sample position, run one selected EChem protocol, then rinse.",
        "repetitions": 1,
        "samples": [
            {
                "sample_id": "sample_001",
                "label": "Sample 1",
                "enabled": True,
                "position": {
                    "x": 0,
                    "y": 0,
                    "z": default_sample_z()
                },
                "rpm": 1600,
                "stabilization_s": 10,
                "protocol": "ca_steps_backward",
                "rotation_command": "",
                "post_echem_wait_s": 0,
                "rinse_after": True
            }
        ]
    }