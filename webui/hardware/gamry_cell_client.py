"""Flask-side client for the isolated Gamry cell-control worker.

ToolkitPy must never be imported here: the main web process is 64-bit while
the Gamry runtime is a separate configured 32-bit Python installation.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hardware.serial_base import available_serial_ports
from workflow.config_loader import get_gamry_config, load_config


class GamryCellClientError(RuntimeError):
    pass


WEBUI_ROOT = Path(__file__).resolve().parents[1]
GAMRY_CELL_STATE_PATH = WEBUI_ROOT / "output" / "gamry_cell_state.json"
DEFAULT_GAMRY_PYTHON = Path(
    r"C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe"
)
CELL_WORKER_PATH = WEBUI_ROOT / "gamry_worker" / "cell_control.py"

_command_lock = threading.Lock()
_state_lock = threading.RLock()

REQUIRED_STATION_PORTS = ("rde", "rotation", "linear", "horizontal")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict[str, Any]:
    return {
        "known_state": "unknown",
        "actual_state": "unknown",
        "instrument": None,
        "last_command": None,
        "last_result": "No Gamry cell command has been recorded.",
        "last_error": None,
        "updated_at": None,
    }


def read_gamry_cell_state() -> dict[str, Any]:
    with _state_lock:
        if not GAMRY_CELL_STATE_PATH.is_file():
            return default_state()

        try:
            payload = json.loads(GAMRY_CELL_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default_state()

        if not isinstance(payload, dict):
            return default_state()

        state = default_state()
        state.update(payload)
        return state


def write_gamry_cell_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = default_state()
    state.update(payload)
    state["updated_at"] = str(state.get("updated_at") or utc_now())

    with _state_lock:
        GAMRY_CELL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = GAMRY_CELL_STATE_PATH.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(GAMRY_CELL_STATE_PATH)

    return state


def configured_worker_python(config: dict[str, Any]) -> str:
    configured = str(config.get("worker_python", "") or "").strip()
    if configured:
        return configured
    if DEFAULT_GAMRY_PYTHON.is_file():
        return str(DEFAULT_GAMRY_PYTHON)
    return sys.executable


def parse_worker_json(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        raise GamryCellClientError("Gamry cell worker returned no JSON output.")

    candidates = [text]
    candidates.extend(line.strip() for line in reversed(text.splitlines()) if line.strip())

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise GamryCellClientError(
        "Gamry cell worker returned invalid JSON: " + text[-1000:]
    )


def validate_duration(duration_s: float | None) -> float | None:
    if duration_s is None or duration_s == "":
        return None

    try:
        duration = float(duration_s)
    except (TypeError, ValueError) as exc:
        raise GamryCellClientError("duration_s must be a positive number or null.") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise GamryCellClientError("duration_s must be greater than 0.")
    return duration


def missing_configured_station_ports() -> list[str]:
    """Return configured station ports that Windows does not currently expose."""
    configured = load_config()["serial"]["ports"]
    detected = {port.upper() for port in available_serial_ports()}
    missing: list[str] = []

    for name in REQUIRED_STATION_PORTS:
        port = str(configured[name]).strip()
        if port.upper() not in detected:
            missing.append(f"{name}={port}")

    return missing


def command_name(state: str, duration_s: float | None) -> str:
    if state == "on" and duration_s is not None:
        formatted = format(duration_s, "g")
        return f"on_{formatted}s"
    if state == "on":
        return "on_until_off"
    return state


def state_response(
    state: dict[str, Any],
    *,
    mode: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = dict(state)
    response.update({"ok": True, "mode": mode})
    if result is not None:
        response["command_result"] = result
    return response


def run_mock_command(
    state: str,
    duration_s: float | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    # Never make a mock response look like a hardware-confirmed instrument.
    instrument = "mock-potentiostat"

    if state == "on" and duration_s is not None:
        live_plot = config.get("live_plot", {})
        scale = float(live_plot.get("mock_time_scale", 0.05)) if isinstance(live_plot, dict) else 0.05
        time.sleep(max(0.0, duration_s * max(0.0, scale)))
        final_state = "off"
        message = f"Mock cell was ON for {duration_s:.1f} s and then OFF."
    elif state == "on":
        final_state = "on"
        message = "Mock cell was turned ON until a later OFF command."
    elif state == "off":
        final_state = "off"
        message = "Mock cell was turned OFF."
    else:
        final_state = read_gamry_cell_state().get("known_state", "unknown")
        message = "Mock Gamry cell status returned software-known state."

    return {
        "ok": True,
        "instrument": instrument,
        "requested_state": state,
        "duration_s": duration_s,
        "final_state": final_state,
        "actual_state": "unknown",
        "message": message,
        "time": utc_now(),
        "simulated": True,
    }


def run_real_command(
    state: str,
    duration_s: float | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    worker_python = configured_worker_python(config)
    python_exists = Path(worker_python).is_file() or shutil.which(worker_python) is not None
    if not python_exists:
        raise GamryCellClientError(f"Gamry Python runtime does not exist: {worker_python}")
    if not CELL_WORKER_PATH.is_file():
        raise GamryCellClientError(f"Gamry cell worker does not exist: {CELL_WORKER_PATH}")

    command = [worker_python, str(CELL_WORKER_PATH), "--state", state]
    if duration_s is not None:
        command.extend(["--duration", format(duration_s, "g")])

    instrument = str(config.get("instrument_label", "") or "").strip()
    if instrument:
        command.extend(["--instrument", instrument])

    timeout_s = duration_s + 20.0 if state == "on" and duration_s is not None else 15.0

    try:
        completed = subprocess.run(
            command,
            cwd=str(WEBUI_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise GamryCellClientError(
            f"Gamry cell {state} command timed out after {timeout_s:g} seconds."
        ) from exc
    except Exception as exc:
        raise GamryCellClientError(f"Unable to start Gamry cell worker: {exc}") from exc

    result = parse_worker_json(completed.stdout)
    if completed.returncode != 0 or not bool(result.get("ok", False)):
        error = result.get("error") or completed.stderr.strip() or "Gamry cell worker failed."
        raise GamryCellClientError(str(error))
    return result


def execute_command(state: str, duration_s: float | None = None) -> dict[str, Any]:
    duration = validate_duration(duration_s) if state == "on" else None
    if state not in {"status", "on", "off"}:
        raise GamryCellClientError(f"Unsupported Gamry cell command: {state}")

    command = command_name(state, duration)

    with _command_lock:
        previous = read_gamry_cell_state()
        config = get_gamry_config()
        mode = str(config.get("mode", "mock") or "mock").strip().lower()

        try:
            if state == "on" and mode != "mock":
                missing_ports = missing_configured_station_ports()
                if missing_ports:
                    raise GamryCellClientError(
                        "Gamry Cell ON is blocked because configured station ports "
                        f"are unavailable: {', '.join(missing_ports)}. "
                        "No port was remapped and no Gamry command was sent."
                    )

            if mode == "mock":
                result = run_mock_command(state, duration, config)
            else:
                result = run_real_command(state, duration, config)

            actual_state = str(result.get("actual_state", "unknown") or "unknown").lower()
            final_state = str(result.get("final_state", "unknown") or "unknown").lower()

            if state == "status":
                known_state = (
                    actual_state if actual_state in {"on", "off"} else previous["known_state"]
                )
            else:
                known_state = final_state if final_state in {"on", "off"} else "unknown"

            stored = write_gamry_cell_state(
                {
                    "known_state": known_state,
                    "actual_state": actual_state,
                    "instrument": result.get("instrument") or previous.get("instrument"),
                    "last_command": command,
                    "last_result": str(result.get("message") or "Command completed."),
                    "last_error": None,
                    "updated_at": str(result.get("time") or utc_now()),
                }
            )
            return state_response(stored, mode=mode, result=result)

        except Exception as exc:
            error = exc if isinstance(exc, GamryCellClientError) else GamryCellClientError(str(exc))
            write_gamry_cell_state(
                {
                    **previous,
                    "known_state": "unknown",
                    "actual_state": "unknown",
                    "last_command": command,
                    "last_result": "Command failed.",
                    "last_error": str(error),
                    "updated_at": utc_now(),
                }
            )
            raise error


def gamry_cell_status() -> dict[str, Any]:
    return execute_command("status")


def gamry_cell_on(duration_s: float | None = None) -> dict[str, Any]:
    return execute_command("on", duration_s)


def gamry_cell_off() -> dict[str, Any]:
    return execute_command("off")
