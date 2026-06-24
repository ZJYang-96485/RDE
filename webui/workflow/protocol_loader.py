from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_path, load_config

MAX_PROTOCOL_NAME_LENGTH = 80

ALLOWED_TECHNIQUES = {
    "ocp",
    "cv",
    "lsv",
    "ca",
    "ca_staircase",
    "eis",
}


class ProtocolError(ValueError):
    pass


def protocols_dir() -> Path:
    path = get_path("protocols_dir")
    path.mkdir(parents=True, exist_ok=True)
    return path


def max_protocol_steps() -> int:
    return int(load_config()["automation"]["max_protocol_steps"])


def normalize_protocol_name(raw_name: Any) -> str:
    name = str(raw_name or "").strip()

    if not name:
        raise ProtocolError("protocol name cannot be empty.")

    if len(name) > MAX_PROTOCOL_NAME_LENGTH:
        raise ProtocolError(f"protocol name cannot be longer than {MAX_PROTOCOL_NAME_LENGTH} characters.")

    name = re.sub(r"[^A-Za-z0-9 _.-]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")

    if not name:
        raise ProtocolError("protocol name must contain at least one letter or number.")

    return name


def protocol_file_name(raw_name: Any) -> str:
    name = normalize_protocol_name(raw_name)
    return name.replace(" ", "_") + ".json"


def protocol_path_for_name(raw_name: Any) -> Path:
    return protocols_dir() / protocol_file_name(raw_name)


def parse_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{field_name} must be a number.") from exc


def parse_positive_float(value: Any, field_name: str) -> float:
    result = parse_float(value, field_name)

    if result <= 0:
        raise ProtocolError(f"{field_name} must be > 0.")

    return result


def parse_nonnegative_float(value: Any, field_name: str) -> float:
    result = parse_float(value, field_name)

    if result < 0:
        raise ProtocolError(f"{field_name} cannot be negative.")

    return result


def parse_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{field_name} must be an integer.") from exc


def parse_positive_int(value: Any, field_name: str) -> int:
    result = parse_int(value, field_name)

    if result <= 0:
        raise ProtocolError(f"{field_name} must be > 0.")

    return result


def optional_string(value: Any, default: str = "") -> str:
    if value is None:
        return default

    return str(value).strip()


def validate_common_step_fields(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    name = optional_string(raw_step.get("name"), f"step_{index}")
    technique = optional_string(raw_step.get("technique")).lower()

    if not name:
        name = f"step_{index}"

    if technique not in ALLOWED_TECHNIQUES:
        raise ProtocolError(f"{name}: unsupported technique '{technique}'.")

    return {
        "name": name,
        "technique": technique,
        "enabled": bool(raw_step.get("enabled", True)),
        "output": optional_string(raw_step.get("output"), f"{name}.DTA"),
    }


def validate_ocp_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    step = validate_common_step_fields(raw_step, index)
    step["duration_s"] = parse_positive_float(raw_step.get("duration_s", 60), f"{step['name']}: duration_s")
    step["sample_period_s"] = parse_positive_float(raw_step.get("sample_period_s", 0.5), f"{step['name']}: sample_period_s")
    step["stability_mv_s"] = parse_nonnegative_float(raw_step.get("stability_mv_s", 0), f"{step['name']}: stability_mv_s")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    return step


def validate_cv_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    step = validate_common_step_fields(raw_step, index)
    step["initial_voltage_v"] = parse_float(raw_step.get("initial_voltage_v", 0), f"{step['name']}: initial_voltage_v")
    step["first_vertex_v"] = parse_float(raw_step.get("first_vertex_v", 1), f"{step['name']}: first_vertex_v")
    step["second_vertex_v"] = parse_float(raw_step.get("second_vertex_v", -1), f"{step['name']}: second_vertex_v")
    step["final_voltage_v"] = parse_float(raw_step.get("final_voltage_v", 0), f"{step['name']}: final_voltage_v")
    step["scan_rate_v_s"] = parse_positive_float(raw_step.get("scan_rate_v_s", 0.05), f"{step['name']}: scan_rate_v_s")
    step["cycles"] = parse_positive_int(raw_step.get("cycles", 1), f"{step['name']}: cycles")
    step["sample_period_s"] = parse_positive_float(raw_step.get("sample_period_s", 0.01), f"{step['name']}: sample_period_s")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    return step


def validate_lsv_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    step = validate_common_step_fields(raw_step, index)
    step["start_voltage_v"] = parse_float(raw_step.get("start_voltage_v", 0.2), f"{step['name']}: start_voltage_v")
    step["end_voltage_v"] = parse_float(raw_step.get("end_voltage_v", -0.8), f"{step['name']}: end_voltage_v")
    step["scan_rate_v_s"] = parse_positive_float(raw_step.get("scan_rate_v_s", 0.01), f"{step['name']}: scan_rate_v_s")
    step["sample_period_s"] = parse_positive_float(raw_step.get("sample_period_s", 0.1), f"{step['name']}: sample_period_s")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    return step


def validate_ca_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    step = validate_common_step_fields(raw_step, index)
    step["voltage_v"] = parse_float(raw_step.get("voltage_v", 0), f"{step['name']}: voltage_v")
    step["duration_s"] = parse_positive_float(raw_step.get("duration_s", 300), f"{step['name']}: duration_s")
    step["sample_period_s"] = parse_positive_float(raw_step.get("sample_period_s", 1), f"{step['name']}: sample_period_s")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    return step


def validate_ca_staircase_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    step = validate_common_step_fields(raw_step, index)
    step.pop("output", None)
    step["output_prefix"] = optional_string(raw_step.get("output_prefix"), step["name"])
    step["start_voltage_v"] = parse_float(raw_step.get("start_voltage_v", -0.1), f"{step['name']}: start_voltage_v")
    step["step_voltage_v"] = parse_float(raw_step.get("step_voltage_v", -0.1), f"{step['name']}: step_voltage_v")
    step["step_count"] = parse_positive_int(raw_step.get("step_count", 1), f"{step['name']}: step_count")
    step["pre_step_time_s"] = parse_nonnegative_float(raw_step.get("pre_step_time_s", 0), f"{step['name']}: pre_step_time_s")
    step["step_time_s"] = parse_positive_float(raw_step.get("step_time_s", 300), f"{step['name']}: step_time_s")
    step["second_step_time_s"] = parse_nonnegative_float(raw_step.get("second_step_time_s", 0), f"{step['name']}: second_step_time_s")
    step["sample_period_s"] = parse_positive_float(raw_step.get("sample_period_s", 1), f"{step['name']}: sample_period_s")
    step["current_limit_ma_cm2"] = parse_positive_float(raw_step.get("current_limit_ma_cm2", 300), f"{step['name']}: current_limit_ma_cm2")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    step["current_range_mode"] = optional_string(raw_step.get("current_range_mode"), "auto")
    step["sampling_mode"] = optional_string(raw_step.get("sampling_mode"), "fast")
    step["max_current_ma"] = parse_positive_float(raw_step.get("max_current_ma", 100), f"{step['name']}: max_current_ma")
    step["equilibration_time_s"] = parse_nonnegative_float(raw_step.get("equilibration_time_s", 0), f"{step['name']}: equilibration_time_s")
    step["ir_compensation"] = optional_string(raw_step.get("ir_compensation"), "none")
    step["pf_correction_ohm"] = parse_nonnegative_float(raw_step.get("pf_correction_ohm", 0), f"{step['name']}: pf_correction_ohm")
    return step


def validate_eis_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    step = validate_common_step_fields(raw_step, index)
    step["dc_voltage_v"] = parse_float(raw_step.get("dc_voltage_v", 0), f"{step['name']}: dc_voltage_v")
    step["dc_voltage_reference"] = optional_string(raw_step.get("dc_voltage_reference"), "open_circuit")
    step["initial_frequency_hz"] = parse_positive_float(raw_step.get("initial_frequency_hz", 100000), f"{step['name']}: initial_frequency_hz")
    step["final_frequency_hz"] = parse_positive_float(raw_step.get("final_frequency_hz", 0.1), f"{step['name']}: final_frequency_hz")
    step["points_per_decade"] = parse_positive_int(raw_step.get("points_per_decade", 10), f"{step['name']}: points_per_decade")
    step["ac_voltage_mv_rms"] = parse_positive_float(raw_step.get("ac_voltage_mv_rms", 10), f"{step['name']}: ac_voltage_mv_rms")
    step["estimated_z_ohm"] = parse_positive_float(raw_step.get("estimated_z_ohm", 100), f"{step['name']}: estimated_z_ohm")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    step["speed"] = optional_string(raw_step.get("speed"), "normal")
    step["thd"] = bool(raw_step.get("thd", False))
    step["drift_correction"] = bool(raw_step.get("drift_correction", False))

    if step["initial_frequency_hz"] <= step["final_frequency_hz"]:
        raise ProtocolError(f"{step['name']}: initial_frequency_hz must be greater than final_frequency_hz.")

    return step


def validate_protocol_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw_step, dict):
        raise ProtocolError(f"step {index} must be an object.")

    technique = optional_string(raw_step.get("technique")).lower()

    validators = {
        "ocp": validate_ocp_step,
        "cv": validate_cv_step,
        "lsv": validate_lsv_step,
        "ca": validate_ca_step,
        "ca_staircase": validate_ca_staircase_step,
        "eis": validate_eis_step,
    }

    if technique not in validators:
        raise ProtocolError(f"step {index}: unsupported technique '{technique}'.")

    return validators[technique](raw_step, index)


def validate_protocol_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("protocol payload must be an object.")

    protocol_name = normalize_protocol_name(payload.get("protocol_name") or payload.get("name"))
    display_name = optional_string(payload.get("display_name"), protocol_name)
    description = optional_string(payload.get("description"), "")
    source = payload.get("source", {})

    if source is None:
        source = {}

    if not isinstance(source, dict):
        raise ProtocolError("source must be an object.")

    raw_steps = payload.get("steps")

    if not isinstance(raw_steps, list):
        raise ProtocolError("steps must be a list.")

    if not raw_steps:
        raise ProtocolError("protocol must contain at least one step.")

    max_steps = max_protocol_steps()

    if len(raw_steps) > max_steps:
        raise ProtocolError(f"protocol cannot contain more than {max_steps} steps.")

    steps = [validate_protocol_step(raw_step, index) for index, raw_step in enumerate(raw_steps, start=1)]

    return {
        "protocol_name": protocol_name,
        "display_name": display_name,
        "description": description,
        "source": source,
        "steps": steps,
        "saved_at": payload.get("saved_at"),
    }


def load_protocol(name: str) -> dict[str, Any]:
    path = protocol_path_for_name(name)

    if not path.exists():
        raise ProtocolError(f"protocol '{name}' does not exist.")

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    return validate_protocol_payload(payload)


def save_protocol(payload: dict[str, Any]) -> dict[str, Any]:
    protocol = validate_protocol_payload(payload)
    protocol["saved_at"] = datetime.now(timezone.utc).isoformat()

    path = protocol_path_for_name(protocol["protocol_name"])

    with path.open("w", encoding="utf-8") as f:
        json.dump(protocol, f, indent=2)
        f.write("\n")

    return {
        "ok": True,
        "protocol_name": protocol["protocol_name"],
        "display_name": protocol["display_name"],
        "step_count": len(protocol["steps"]),
        "path": str(path),
        "saved_at": protocol["saved_at"],
    }


def delete_protocol(name: str) -> dict[str, Any]:
    protocol_name = normalize_protocol_name(name)
    path = protocol_path_for_name(protocol_name)

    if not path.exists():
        raise ProtocolError(f"protocol '{protocol_name}' does not exist.")

    path.unlink()

    return {
        "ok": True,
        "protocol_name": protocol_name,
    }


def list_protocols() -> list[dict[str, Any]]:
    protocols = []

    for path in sorted(protocols_dir().glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)

            protocol = validate_protocol_payload(payload)
            protocols.append(
                {
                    "protocol_name": protocol["protocol_name"],
                    "display_name": protocol["display_name"],
                    "description": protocol["description"],
                    "step_count": len(protocol["steps"]),
                    "saved_at": protocol.get("saved_at"),
                    "file": path.name,
                }
            )
        except Exception:
            continue

    return protocols


def default_protocol_payload() -> dict[str, Any]:
    return {
        "protocol_name": "ocp_only",
        "display_name": "OCP Only",
        "description": "Simple open-circuit potential test.",
        "source": {
            "type": "default"
        },
        "steps": [
            {
                "name": "ocp",
                "technique": "ocp",
                "output": "OCP.DTA",
                "duration_s": 60,
                "sample_period_s": 0.5,
                "stability_mv_s": 0,
                "area_cm2": 1
            }
        ]
    }