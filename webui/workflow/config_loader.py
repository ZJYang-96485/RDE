from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.json"

_DEFAULT_CONFIG = {
    "serial": {
        "baud_rate": 115200,
        "ports": {
            # Keep these fallbacks aligned with config.json. They are only
            # used when a key is absent from that file; they must never
            # silently redirect a controller to an older port assignment.
            "rde": "COM6",
            "rotation": "COM3",
            "linear": "COM4",
            "horizontal": "COM8",
            "vertical": "COM5"
        },
        "hardware": {
            "mock_serial": False
        },
        "timeouts": {
            "rde_s": 1.0,
            "axis_s": 0.4,
            "rotation_s": 0.4,
            "rotation_ack_s": 10.0,
            "write_s": 1.0,
            "startup_delay_s": 2.0
        }
    },
    "rde": {
        "rpm_min": 30,
        "rpm_max": 12000,
        "stop_rpm": 20
    },
    "motion": {
        "safe_z": 0,
        "max_axis_command": 300000,
        "axis_limits": {
            "linear": [-100000, 100000],
            "horizontal": [-300000, 300000],
            "vertical": [-300000, 300000]
        },
        "axis_mapping": {
            "x": "horizontal",
            "y": "vertical",
            "z": "linear"
        },
        "default_positions": {
            "sample_z": 50000,
            "home": {
                "linear": 0,
                "horizontal": 0,
                "vertical": 0
            }
        }
    },
    "rotation": {
        "home_command": "0",
        "ccw_command": "1"
    },
    "rinse": {
        "enabled": True,
        "position": {
            "x": 120000,
            "y": 60000,
            "z": 50000
        },
        "rpm": 1000,
        "duration_s": 10,
        "rotation_command": "",
        "return_to_safe_z_after": True
    },
    "automation": {
        "max_repetitions": 100,
        "max_samples": 100,
        "max_protocol_steps": 100,
        "poll_interval_s": 0.1
    },
    "paths": {
        "protocols_dir": "protocols",
        "run_plans_dir": "run_plans",
        "output_dir": "output/runs",
        "legacy_recipes_dir": "recipes"
    },
    "gamry": {
        "mode": "mock",
        "worker_python": "",
        "worker_script": "gamry_worker/worker.py",
        "real_worker_python": "",
        "real_worker_script": "",
        "real_worker_command": [],
        "real_timeout_s": 7200,
        "probe_timeout_s": 15,
        "instrument_index": 0,
        "instrument_label": "",
        "default_file_extension": ".DTA",
        "live_plot": {
            "enabled": True,
            "poll_interval_ms": 500,
            "mock_time_scale": 0.05,
            "max_browser_points": 5000,
        },
        "ru_preparation": {
            "ru_retry_count": 5,
            "compensation_fraction": 1.0,
            "ru_repeatability_limit": 0.05,
            "ru_min_ohm": 0.01,
            "ru_max_ohm": 100000.0,
            "ocp_stabilization_s": 5.0,
            "ocp_stabilization_timeout_s": 30.0,
            "ocp_sample_interval_s": 0.25,
            "ocp_stability_window": 5,
            "ocp_stability_limit_v": 0.005,
            "ocp_abs_limit_v": 2.5,
            "ru_frequency_hz": 100000.0,
            "ru_ac_voltage_v": 0.005,
            "ru_estimated_z_ohm": 100.0,
            "ru_settle_s": 0.50,
            "ru_speed": 1,
            "ru_readz_passes": 30,
            "ru_vch_range_headroom_factor": 5.0,
            "continue_without_ir_on_ru_failure": True,
            "fixed_current_range_a": 0.003,
            "electrode_channel": "primary",
            "require_single_instrument": True
        }
    }
}

_config_cache: dict[str, Any] | None = None


class ConfigError(RuntimeError):
    pass


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)

    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def read_config_file() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ConfigError("config.json must contain a JSON object.")

    return payload


def validate_serial_config(config: dict[str, Any]) -> None:
    serial = config.get("serial")

    if not isinstance(serial, dict):
        raise ConfigError("serial config must be an object.")

    ports = serial.get("ports")

    if not isinstance(ports, dict):
        raise ConfigError("serial.ports must be an object.")

    for name in ["rde", "rotation", "linear", "horizontal", "vertical"]:
        port = str(ports.get(name, "")).strip()

        if not port:
            raise ConfigError(f"serial.ports.{name} cannot be empty.")

    baud_rate = int(serial.get("baud_rate", 0))

    if baud_rate <= 0:
        raise ConfigError("serial.baud_rate must be > 0.")

    timeouts = serial.get("timeouts")

    if not isinstance(timeouts, dict):
        raise ConfigError("serial.timeouts must be an object.")

    for name in ["rde_s", "axis_s", "rotation_s", "rotation_ack_s", "write_s", "startup_delay_s"]:
        value = float(timeouts.get(name, 0))

        if value < 0:
            raise ConfigError(f"serial.timeouts.{name} cannot be negative.")

    if float(timeouts.get("rotation_ack_s", 0)) <= 0:
        raise ConfigError("serial.timeouts.rotation_ack_s must be > 0.")


def validate_rde_config(config: dict[str, Any]) -> None:
    rde = config.get("rde")

    if not isinstance(rde, dict):
        raise ConfigError("rde config must be an object.")

    rpm_min = int(rde.get("rpm_min", 0))
    rpm_max = int(rde.get("rpm_max", 0))
    stop_rpm = int(rde.get("stop_rpm", 0))

    if rpm_min <= 0:
        raise ConfigError("rde.rpm_min must be > 0.")

    if rpm_max <= rpm_min:
        raise ConfigError("rde.rpm_max must be greater than rde.rpm_min.")

    if stop_rpm < 0:
        raise ConfigError("rde.stop_rpm cannot be negative.")


def validate_axis_limits(axis_limits: dict[str, Any]) -> None:
    for axis in ["linear", "horizontal", "vertical"]:
        limits = axis_limits.get(axis)

        if not isinstance(limits, list) or len(limits) != 2:
            raise ConfigError(f"motion.axis_limits.{axis} must be a list with two values.")

        low = int(limits[0])
        high = int(limits[1])

        if low >= high:
            raise ConfigError(f"motion.axis_limits.{axis} lower limit must be less than upper limit.")


def validate_motion_config(config: dict[str, Any]) -> None:
    motion = config.get("motion")

    if not isinstance(motion, dict):
        raise ConfigError("motion config must be an object.")

    max_axis_command = int(motion.get("max_axis_command", 0))

    if max_axis_command <= 0:
        raise ConfigError("motion.max_axis_command must be > 0.")

    axis_limits = motion.get("axis_limits")

    if not isinstance(axis_limits, dict):
        raise ConfigError("motion.axis_limits must be an object.")

    validate_axis_limits(axis_limits)

    axis_mapping = motion.get("axis_mapping")

    if not isinstance(axis_mapping, dict):
        raise ConfigError("motion.axis_mapping must be an object.")

    valid_internal_axes = {"linear", "horizontal", "vertical"}

    for user_axis in ["x", "y", "z"]:
        internal_axis = str(axis_mapping.get(user_axis, "")).strip()

        if internal_axis not in valid_internal_axes:
            raise ConfigError(f"motion.axis_mapping.{user_axis} must map to linear, horizontal, or vertical.")

    default_positions = motion.get("default_positions")

    if not isinstance(default_positions, dict):
        raise ConfigError("motion.default_positions must be an object.")

    home = default_positions.get("home")

    if not isinstance(home, dict):
        raise ConfigError("motion.default_positions.home must be an object.")

    for axis in ["linear", "horizontal", "vertical"]:
        int(home.get(axis, 0))


def validate_rinse_config(config: dict[str, Any]) -> None:
    rinse = config.get("rinse")

    if not isinstance(rinse, dict):
        raise ConfigError("rinse config must be an object.")

    position = rinse.get("position", {})

    if not isinstance(position, dict):
        raise ConfigError("rinse.position must be an object.")

    for axis in ["x", "y", "z"]:
        int(position.get(axis, 0))

    rpm = int(rinse.get("rpm", 0))
    duration_s = float(rinse.get("duration_s", 0))

    if bool(rinse.get("enabled", False)):
        rde = config["rde"]

        if rpm < int(rde["rpm_min"]) or rpm > int(rde["rpm_max"]):
            raise ConfigError("rinse.rpm must be within the configured RDE RPM range.")

        if duration_s <= 0:
            raise ConfigError("rinse.duration_s must be > 0 when rinse is enabled.")


def validate_automation_config(config: dict[str, Any]) -> None:
    automation = config.get("automation")

    if not isinstance(automation, dict):
        raise ConfigError("automation config must be an object.")

    for key in ["max_repetitions", "max_samples", "max_protocol_steps"]:
        value = int(automation.get(key, 0))

        if value <= 0:
            raise ConfigError(f"automation.{key} must be > 0.")

    poll_interval_s = float(automation.get("poll_interval_s", 0))

    if poll_interval_s <= 0:
        raise ConfigError("automation.poll_interval_s must be > 0.")


def validate_paths_config(config: dict[str, Any]) -> None:
    paths = config.get("paths")

    if not isinstance(paths, dict):
        raise ConfigError("paths config must be an object.")

    for key in ["protocols_dir", "run_plans_dir", "output_dir", "legacy_recipes_dir"]:
        value = str(paths.get(key, "")).strip()

        if not value:
            raise ConfigError(f"paths.{key} cannot be empty.")


def validate_gamry_config(config: dict[str, Any]) -> None:
    gamry = config.get("gamry")

    if not isinstance(gamry, dict):
        raise ConfigError("gamry config must be an object.")

    mode = str(gamry.get("mode", "mock")).strip().lower()

    if mode not in {"mock", "real", "toolkitpy", "gamry"}:
        raise ConfigError("gamry.mode must be mock, real, toolkitpy, or gamry.")

    extension = str(gamry.get("default_file_extension", ".DTA")).strip()

    if not extension:
        raise ConfigError("gamry.default_file_extension cannot be empty.")

    real_worker_command = gamry.get("real_worker_command", [])

    if not isinstance(real_worker_command, (list, str)):
        raise ConfigError("gamry.real_worker_command must be a list or string.")

    real_timeout_s = float(gamry.get("real_timeout_s", 7200))

    if real_timeout_s <= 0:
        raise ConfigError("gamry.real_timeout_s must be > 0.")

    ru = gamry.get("ru_preparation")
    if not isinstance(ru, dict):
        raise ConfigError("gamry.ru_preparation must be an object.")
    if int(ru.get("ru_retry_count", 0)) < 3:
        raise ConfigError("gamry.ru_preparation.ru_retry_count must be at least 3.")
    fraction = float(ru.get("compensation_fraction", 0))
    if fraction <= 0 or fraction > 1:
        raise ConfigError(
            "gamry.ru_preparation.compensation_fraction must be greater than 0 and at most 1."
        )
    if float(ru.get("ru_repeatability_limit", 0)) <= 0:
        raise ConfigError("gamry.ru_preparation.ru_repeatability_limit must be > 0.")
    minimum = float(ru.get("ru_min_ohm", 0))
    maximum = float(ru.get("ru_max_ohm", 0))
    if minimum <= 0 or maximum <= minimum:
        raise ConfigError("gamry.ru_preparation Ru limits must be positive and ordered.")
    for key in (
        "ocp_stabilization_s",
        "ocp_stabilization_timeout_s",
        "ocp_sample_interval_s",
        "ocp_stability_limit_v",
        "ocp_abs_limit_v",
        "ru_frequency_hz",
        "ru_ac_voltage_v",
        "ru_estimated_z_ohm",
        "fixed_current_range_a",
    ):
        if float(ru.get(key, 0)) <= 0:
            raise ConfigError(f"gamry.ru_preparation.{key} must be > 0.")
    if int(ru.get("ocp_stability_window", 0)) < 2:
        raise ConfigError("gamry.ru_preparation.ocp_stability_window must be at least 2.")
    if int(ru.get("ru_readz_passes", 0)) < 10:
        raise ConfigError("gamry.ru_preparation.ru_readz_passes must be at least 10.")
    headroom = float(ru.get("ru_vch_range_headroom_factor", 0))
    if headroom < 1 or headroom > 10:
        raise ConfigError(
            "gamry.ru_preparation.ru_vch_range_headroom_factor must be between 1 and 10."
        )


def validate_config(config: dict[str, Any]) -> None:
    validate_serial_config(config)
    validate_rde_config(config)
    validate_motion_config(config)
    validate_rinse_config(config)
    validate_automation_config(config)
    validate_paths_config(config)
    validate_gamry_config(config)


def load_config(refresh: bool = False) -> dict[str, Any]:
    global _config_cache

    if _config_cache is not None and not refresh:
        return copy.deepcopy(_config_cache)

    user_config = read_config_file()
    config = deep_merge(_DEFAULT_CONFIG, user_config)
    validate_config(config)

    _config_cache = config

    return copy.deepcopy(config)


def reload_config() -> dict[str, Any]:
    return load_config(refresh=True)


def set_gamry_mode(mode: str) -> dict[str, Any]:
    """Validate the configured Gamry mode without rewriting config.json."""
    normalized = str(mode or "").strip().lower()

    if normalized not in {"mock", "real", "toolkitpy", "gamry"}:
        raise ConfigError("gamry.mode must be mock, real, toolkitpy, or gamry.")

    config = reload_config()
    configured = str(config["gamry"]["mode"]).strip().lower()
    if normalized != configured:
        raise ConfigError(
            "Gamry backend is fixed by config.json and cannot be changed from the web UI. "
            f"Configured mode: {configured}."
        )

    return config


def get_baud_rate() -> int:
    return int(load_config()["serial"]["baud_rate"])


def get_serial_port(name: str) -> str:
    ports = load_config()["serial"]["ports"]
    key = str(name).strip()

    if key not in ports:
        raise ConfigError(f"unknown serial port name: {name}")

    return str(ports[key]).strip()


def get_timeout(name: str) -> float:
    timeouts = load_config()["serial"]["timeouts"]
    key = str(name).strip()

    if key not in timeouts:
        raise ConfigError(f"unknown timeout name: {name}")

    return float(timeouts[key])


def get_rde_limits() -> dict[str, int]:
    rde = load_config()["rde"]

    return {
        "rpm_min": int(rde["rpm_min"]),
        "rpm_max": int(rde["rpm_max"]),
        "stop_rpm": int(rde["stop_rpm"])
    }


def get_motion_config() -> dict[str, Any]:
    return load_config()["motion"]


def get_axis_mapping() -> dict[str, str]:
    mapping = get_motion_config()["axis_mapping"]

    return {
        "x": str(mapping["x"]),
        "y": str(mapping["y"]),
        "z": str(mapping["z"])
    }


def user_axis_to_internal_axis(user_axis: str) -> str:
    key = str(user_axis).strip().lower()
    mapping = get_axis_mapping()

    if key not in mapping:
        raise ConfigError(f"unknown user axis: {user_axis}")

    return mapping[key]


def get_internal_axis_limit(internal_axis: str) -> tuple[int, int]:
    axis = str(internal_axis).strip().lower()
    limits = get_motion_config()["axis_limits"]

    if axis not in limits:
        raise ConfigError(f"unknown internal axis: {internal_axis}")

    return int(limits[axis][0]), int(limits[axis][1])


def get_user_axis_limit(user_axis: str) -> tuple[int, int]:
    internal_axis = user_axis_to_internal_axis(user_axis)
    return get_internal_axis_limit(internal_axis)


def get_safe_z() -> int:
    return int(get_motion_config()["safe_z"])


def get_max_axis_command() -> int:
    return int(get_motion_config()["max_axis_command"])


def get_path(name: str) -> Path:
    paths = load_config()["paths"]
    key = str(name).strip()

    if key not in paths:
        raise ConfigError(f"unknown path name: {name}")

    path = Path(str(paths[key]))

    if not path.is_absolute():
        path = BASE_DIR / path

    path.mkdir(parents=True, exist_ok=True)

    return path


def get_gamry_config() -> dict[str, Any]:
    return load_config()["gamry"]


def get_live_plot_config() -> dict[str, Any]:
    live_plot = get_gamry_config().get("live_plot", {})
    return {
        "enabled": bool(live_plot.get("enabled", True)),
        "poll_interval_ms": int(live_plot.get("poll_interval_ms", 500)),
        "mock_time_scale": float(live_plot.get("mock_time_scale", 0.05)),
        "max_browser_points": int(live_plot.get("max_browser_points", 5000)),
    }
