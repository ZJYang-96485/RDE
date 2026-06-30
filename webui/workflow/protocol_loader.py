from __future__ import annotations

import json
import re
from decimal import Decimal
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


def first_present(raw: dict[str, Any], names: list[str], default: Any) -> Any:
    for name in names:
        if name in raw:
            return raw[name]

    return default


def decimal_value(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ProtocolError(f"{field_name} must be a number.") from exc


def decimal_to_float(value: Decimal) -> float:
    return float(value)


def format_voltage_token(value: Any) -> str:
    voltage = decimal_value(value, "voltage")
    sign = "p"

    if voltage < 0:
        sign = "m"
        voltage = -voltage
    elif voltage == 0:
        return "0p0V"

    text = format(voltage.normalize(), "f")

    if "." not in text:
        text = text + ".0"

    return f"{sign}{text.replace('.', 'p')}V"


def format_template(template: str, *, label: str, voltage: Decimal, index: int) -> str:
    return template.format(
        label=label,
        index=index,
        voltage=decimal_to_float(voltage),
        voltage_token=format_voltage_token(voltage),
    )


def generate_decimal_range(
    *,
    start: Any,
    end: Any,
    step: Any,
    field_name: str,
) -> list[Decimal]:
    start_d = decimal_value(start, f"{field_name}: start_voltage_v")
    end_d = decimal_value(end, f"{field_name}: end_voltage_v")
    step_d = decimal_value(step, f"{field_name}: step_voltage_v")

    if step_d == 0:
        raise ProtocolError(f"{field_name}: step_voltage_v cannot be 0.")

    if end_d > start_d and step_d < 0:
        raise ProtocolError(f"{field_name}: step_voltage_v must be positive when end_voltage_v is greater than start_voltage_v.")

    if end_d < start_d and step_d > 0:
        raise ProtocolError(f"{field_name}: step_voltage_v must be negative when end_voltage_v is less than start_voltage_v.")

    values: list[Decimal] = []
    current = start_d
    guard = 0

    while True:
        if step_d > 0 and current > end_d:
            break

        if step_d < 0 and current < end_d:
            break

        values.append(current)
        current += step_d
        guard += 1

        if guard > 5000:
            raise ProtocolError(f"{field_name}: generated too many CA sequence steps.")

    if not values:
        raise ProtocolError(f"{field_name}: voltage range produced no steps.")

    return values


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

    initial_voltage = parse_float(
        first_present(raw_step, ["initial_voltage_v", "start_voltage_v", "e_initial_v"], 0),
        f"{step['name']}: initial_voltage_v",
    )
    apex1_voltage = parse_float(
        first_present(raw_step, ["apex1_voltage_v", "first_vertex_v", "vertex1_voltage_v", "upper_voltage_v"], 1),
        f"{step['name']}: apex1_voltage_v",
    )
    apex2_voltage = parse_float(
        first_present(raw_step, ["apex2_voltage_v", "second_vertex_v", "vertex2_voltage_v", "lower_voltage_v"], -1),
        f"{step['name']}: apex2_voltage_v",
    )
    final_voltage = parse_float(
        first_present(raw_step, ["final_voltage_v", "end_voltage_v", "e_final_v"], 0),
        f"{step['name']}: final_voltage_v",
    )

    scan_rate = parse_positive_float(raw_step.get("scan_rate_v_s", raw_step.get("scan_rate", 0.05)), f"{step['name']}: scan_rate_v_s")
    sample_period = parse_positive_float(raw_step.get("sample_period_s", 0.01), f"{step['name']}: sample_period_s")
    step_size = parse_positive_float(raw_step.get("step_size_v", scan_rate * sample_period), f"{step['name']}: step_size_v")

    step["initial_voltage_v"] = initial_voltage
    step["first_vertex_v"] = apex1_voltage
    step["second_vertex_v"] = apex2_voltage
    step["apex1_voltage_v"] = apex1_voltage
    step["apex2_voltage_v"] = apex2_voltage
    step["final_voltage_v"] = final_voltage
    step["scan_rate_v_s"] = scan_rate
    step["cycles"] = parse_positive_int(raw_step.get("cycles", 1), f"{step['name']}: cycles")
    step["sample_period_s"] = sample_period
    step["step_size_v"] = step_size
    step["precharge_s"] = parse_nonnegative_float(raw_step.get("precharge_s", 1), f"{step['name']}: precharge_s")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    return step

def validate_lsv_step(raw_step: dict[str, Any], index: int) -> dict[str, Any]:
    step = validate_common_step_fields(raw_step, index)

    start_voltage = parse_float(
        first_present(raw_step, ["start_voltage_v", "initial_voltage_v", "from_voltage_v", "e_initial_v"], 0.2),
        f"{step['name']}: start_voltage_v",
    )
    end_voltage = parse_float(
        first_present(raw_step, ["end_voltage_v", "final_voltage_v", "to_voltage_v", "e_final_v"], -0.8),
        f"{step['name']}: end_voltage_v",
    )

    scan_rate = parse_positive_float(raw_step.get("scan_rate_v_s", raw_step.get("scan_rate", 0.01)), f"{step['name']}: scan_rate_v_s")
    sample_period = parse_positive_float(raw_step.get("sample_period_s", 0.1), f"{step['name']}: sample_period_s")
    step_size = parse_positive_float(raw_step.get("step_size_v", scan_rate * sample_period), f"{step['name']}: step_size_v")

    step["start_voltage_v"] = start_voltage
    step["end_voltage_v"] = end_voltage
    step["initial_voltage_v"] = start_voltage
    step["final_voltage_v"] = end_voltage
    step["scan_rate_v_s"] = scan_rate
    step["sample_period_s"] = sample_period
    step["step_size_v"] = step_size
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
    step["step_time_s"] = parse_positive_float(raw_step.get("step_time_s", raw_step.get("duration_s", 300)), f"{step['name']}: step_time_s")
    step["duration_s"] = step["step_time_s"]
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

    initial_frequency = parse_positive_float(
        first_present(raw_step, ["initial_frequency_hz", "initial_freq_hz", "start_freq_hz", "initial_freq"], 100000),
        f"{step['name']}: initial_frequency_hz",
    )
    final_frequency = parse_positive_float(
        first_present(raw_step, ["final_frequency_hz", "final_freq_hz", "end_freq_hz", "final_freq"], 0.1),
        f"{step['name']}: final_frequency_hz",
    )

    if "ac_voltage_v" in raw_step:
        ac_voltage_v = parse_positive_float(raw_step.get("ac_voltage_v"), f"{step['name']}: ac_voltage_v")
    else:
        ac_voltage_v = parse_positive_float(raw_step.get("ac_voltage_mv_rms", 10), f"{step['name']}: ac_voltage_mv_rms") / 1000.0

    step["dc_voltage_v"] = parse_float(raw_step.get("dc_voltage_v", raw_step.get("bias_voltage_v", 0)), f"{step['name']}: dc_voltage_v")
    step["dc_voltage_reference"] = optional_string(raw_step.get("dc_voltage_reference"), "open_circuit")
    step["initial_frequency_hz"] = initial_frequency
    step["final_frequency_hz"] = final_frequency
    step["initial_freq_hz"] = initial_frequency
    step["final_freq_hz"] = final_frequency
    step["points_per_decade"] = parse_positive_int(raw_step.get("points_per_decade", 10), f"{step['name']}: points_per_decade")
    step["ac_voltage_mv_rms"] = ac_voltage_v * 1000.0
    step["ac_voltage_v"] = ac_voltage_v
    step["estimated_z_ohm"] = parse_positive_float(raw_step.get("estimated_z_ohm", raw_step.get("estimated_z", 100)), f"{step['name']}: estimated_z_ohm")
    step["area_cm2"] = parse_positive_float(raw_step.get("area_cm2", 1), f"{step['name']}: area_cm2")
    step["speed"] = optional_string(raw_step.get("speed"), "normal")
    step["thd"] = bool(raw_step.get("thd", False))
    step["drift_correction"] = bool(raw_step.get("drift_correction", False))
    step["settle_s"] = parse_nonnegative_float(raw_step.get("settle_s", 1), f"{step['name']}: settle_s")

    if step["initial_frequency_hz"] <= step["final_frequency_hz"]:
        raise ProtocolError(f"{step['name']}: initial_frequency_hz must be greater than final_frequency_hz.")

    return step


def expand_ca_range_step(raw_step: dict[str, Any], index: int) -> list[dict[str, Any]]:
    label = optional_string(
        raw_step.get("direction_label")
        or raw_step.get("label")
        or raw_step.get("name"),
        f"ca_range_{index}",
    )

    voltages = generate_decimal_range(
        start=raw_step.get("start_voltage_v", -0.1),
        end=raw_step.get("end_voltage_v", -1.6),
        step=raw_step.get("step_voltage_v", -0.1),
        field_name=label,
    )

    output_prefix = optional_string(raw_step.get("output_prefix"), f"CA_{label}")
    output_template = optional_string(raw_step.get("output_template"), "")
    name_template = optional_string(raw_step.get("step_name_template"), f"{label}_{{voltage_token}}")

    expanded_steps: list[dict[str, Any]] = []

    for local_index, voltage in enumerate(voltages, start=1):
        voltage_token = format_voltage_token(voltage)

        if output_template:
            output = format_template(
                output_template,
                label=label,
                voltage=voltage,
                index=local_index,
            )
        else:
            output = f"{output_prefix}_{voltage_token}.DTA"

        name = format_template(
            name_template,
            label=label,
            voltage=voltage,
            index=local_index,
        )

        expanded_steps.append(
            {
                "name": name,
                "technique": "ca",
                "enabled": bool(raw_step.get("enabled", True)),
                "output": output,
                "voltage_v": decimal_to_float(voltage),
                "duration_s": raw_step.get("duration_s", raw_step.get("step_time_s", 300)),
                "sample_period_s": raw_step.get("sample_period_s", raw_step.get("sample_time_s", 1)),
                "area_cm2": raw_step.get("area_cm2", 1),
                "expected_max_v": raw_step.get("expected_max_v", max(1.0, abs(decimal_to_float(voltage)))),
            }
        )

    return expanded_steps


def expand_protocol_steps(raw_steps: list[Any]) -> list[dict[str, Any]]:
    expanded_steps: list[dict[str, Any]] = []

    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ProtocolError(f"step {index} must be an object.")

        technique = optional_string(raw_step.get("technique")).lower()
        step_type = optional_string(raw_step.get("type")).lower()

        if technique in {"ca_range", "ca_sequence"} or step_type in {"ca_range", "ca_sequence"}:
            expanded_steps.extend(expand_ca_range_step(raw_step, index))
            continue

        expanded_steps.append(raw_step)

    return expanded_steps

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

    expanded_steps = expand_protocol_steps(raw_steps)
    max_steps = max_protocol_steps()

    if len(expanded_steps) > max_steps:
        raise ProtocolError(f"protocol cannot contain more than {max_steps} steps after expansion.")

    steps = [validate_protocol_step(raw_step, index) for index, raw_step in enumerate(expanded_steps, start=1)]

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