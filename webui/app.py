from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

try:
    import serial
except ImportError:
    serial = None


COM_PORT = "COM10"
ROTATION_COM_PORT = "COM7"
LINEAR_COM_PORT = "COM9"
HORIZONTAL_COM_PORT = "COM4"
VERTICAL_COM_PORT = "COM5"

BAUD_RATE = 115200
RPM_MIN = 30
RPM_MAX = 12000
STOP_RPM = 20

SAFE_Z = 0
MAX_AXIS_COMMAND = 300000

AXIS_LIMITS = {
    "linear": (-100000, 100000),
    "horizontal": (-300000, 300000),
    "vertical": (-300000, 300000),
}

RECIPES_DIR = Path(__file__).with_name("recipes")
LEGACY_RECIPE_PATH = Path(__file__).with_name("recipe_default.json")
DEFAULT_RECIPE_NAME = "default"
MAX_RECIPE_NAME_LENGTH = 80
MAX_RECIPE_STEPS = 100

app = Flask(__name__)

state_lock = threading.Lock()
state = {
    "running": False,
    "target_rpm": None,
    "duration_seconds": None,
    "started_at": None,
    "ends_at": None,
    "last_error": None,
}

serial_conn = None
rotation_serial_conn = None
linear_serial_conn = None
horizontal_serial_conn = None
vertical_serial_conn = None

stop_timer = None

rotation_lock = threading.Lock()
linear_lock = threading.Lock()
horizontal_lock = threading.Lock()
vertical_lock = threading.Lock()

axis_position_lock = threading.Lock()
axis_positions = {
    "linear": 0,
    "horizontal": 0,
    "vertical": 0,
}

automation_lock = threading.Lock()
automation_state = {
    "running": False,
    "current_step": None,
    "last_error": None,
}

_NO_AUTOMATION_ERROR_UPDATE = object()
automation_abort_event = threading.Event()


class AutomationAbortRequested(Exception):
    pass


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def ensure_serial_connection() -> None:
    global serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    if serial_conn and serial_conn.is_open:
        return

    serial_conn = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    time.sleep(2)


def send_rpm(rpm: int) -> None:
    ensure_serial_connection()
    serial_conn.write(f"{int(rpm)}\n".encode("ascii"))
    serial_conn.flush()


def ensure_rotation_serial_connection() -> None:
    global rotation_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    if rotation_serial_conn and rotation_serial_conn.is_open:
        return

    rotation_serial_conn = serial.Serial(
        ROTATION_COM_PORT,
        BAUD_RATE,
        timeout=0.4,
        write_timeout=1,
    )
    time.sleep(2.0)
    rotation_serial_conn.reset_input_buffer()
    rotation_serial_conn.reset_output_buffer()


def send_rotation_command(value: int) -> str | None:
    return send_rotation_text(str(int(value)))


def send_rotation_text(command: str) -> str | None:
    global rotation_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    payload = f"{command}\n".encode("ascii")
    ack = None

    with rotation_lock:
        try:
            ensure_rotation_serial_connection()
            rotation_serial_conn.write(payload)
            rotation_serial_conn.flush()
        except Exception:
            try:
                if rotation_serial_conn and rotation_serial_conn.is_open:
                    rotation_serial_conn.close()
            finally:
                rotation_serial_conn = None

            ensure_rotation_serial_connection()
            rotation_serial_conn.write(payload)
            rotation_serial_conn.flush()

        for _ in range(4):
            line = rotation_serial_conn.readline().decode("utf-8", errors="replace").strip()
            if line:
                ack = line
                break

    return ack


def axis_ack_timeout_seconds(command: str) -> float:
    try:
        steps_abs = abs(int(command))
    except ValueError:
        return 5.0

    if steps_abs <= 100:
        mult = 1
    elif steps_abs <= 1000:
        mult = 2
    elif steps_abs <= 10000:
        mult = 5
    else:
        mult = 10

    pulse_us = max(50, int(800 / mult))
    per_step_seconds = (pulse_us * 2) / 1_000_000.0
    estimate = (steps_abs * per_step_seconds) + 3.0
    return max(3.0, min(120.0, estimate))


def wait_for_axis_ack(
    conn,
    timeout_seconds: float,
    com_port: str,
    abort_event: threading.Event | None = None,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_line = None

    while time.monotonic() < deadline:
        if abort_event is not None and abort_event.is_set():
            raise AutomationAbortRequested("Abort requested during axis movement.")

        line = conn.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue

        last_line = line
        if line.startswith("ACK"):
            return line
        if line.startswith("ERR"):
            raise RuntimeError(f"{com_port} reported error: {line}")

    detail = f" Last line from board: {last_line}" if last_line else ""
    raise TimeoutError(f"Timeout waiting for ACK from {com_port}.{detail}")


def ensure_linear_serial_connection() -> None:
    global linear_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    if linear_serial_conn and linear_serial_conn.is_open:
        return

    linear_serial_conn = serial.Serial(
        LINEAR_COM_PORT,
        BAUD_RATE,
        timeout=0.4,
        write_timeout=1,
    )
    time.sleep(2.0)
    linear_serial_conn.reset_input_buffer()
    linear_serial_conn.reset_output_buffer()


def send_linear_text(command: str, abort_event: threading.Event | None = None) -> str | None:
    global linear_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    payload = f"{command}\n".encode("ascii")
    ack_timeout_seconds = axis_ack_timeout_seconds(command)

    with linear_lock:
        try:
            ensure_linear_serial_connection()
            linear_serial_conn.write(payload)
            linear_serial_conn.flush()
        except Exception:
            try:
                if linear_serial_conn and linear_serial_conn.is_open:
                    linear_serial_conn.close()
            finally:
                linear_serial_conn = None

            ensure_linear_serial_connection()
            linear_serial_conn.write(payload)
            linear_serial_conn.flush()

        ack = wait_for_axis_ack(
            linear_serial_conn,
            ack_timeout_seconds,
            LINEAR_COM_PORT,
            abort_event=abort_event,
        )

    return ack


def ensure_horizontal_serial_connection() -> None:
    global horizontal_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    if horizontal_serial_conn and horizontal_serial_conn.is_open:
        return

    horizontal_serial_conn = serial.Serial(
        HORIZONTAL_COM_PORT,
        BAUD_RATE,
        timeout=0.4,
        write_timeout=1,
    )
    time.sleep(2.0)
    horizontal_serial_conn.reset_input_buffer()
    horizontal_serial_conn.reset_output_buffer()


def send_horizontal_text(command: str, abort_event: threading.Event | None = None) -> str | None:
    global horizontal_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    payload = f"{command}\n".encode("ascii")
    ack_timeout_seconds = axis_ack_timeout_seconds(command)

    with horizontal_lock:
        try:
            ensure_horizontal_serial_connection()
            horizontal_serial_conn.write(payload)
            horizontal_serial_conn.flush()
        except Exception:
            try:
                if horizontal_serial_conn and horizontal_serial_conn.is_open:
                    horizontal_serial_conn.close()
            finally:
                horizontal_serial_conn = None

            ensure_horizontal_serial_connection()
            horizontal_serial_conn.write(payload)
            horizontal_serial_conn.flush()

        ack = wait_for_axis_ack(
            horizontal_serial_conn,
            ack_timeout_seconds,
            HORIZONTAL_COM_PORT,
            abort_event=abort_event,
        )

    return ack


def ensure_vertical_serial_connection() -> None:
    global vertical_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    if vertical_serial_conn and vertical_serial_conn.is_open:
        return

    vertical_serial_conn = serial.Serial(
        VERTICAL_COM_PORT,
        BAUD_RATE,
        timeout=0.4,
        write_timeout=1,
    )
    time.sleep(2.0)
    vertical_serial_conn.reset_input_buffer()
    vertical_serial_conn.reset_output_buffer()


def send_vertical_text(command: str, abort_event: threading.Event | None = None) -> str | None:
    global vertical_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    payload = f"{command}\n".encode("ascii")
    ack_timeout_seconds = axis_ack_timeout_seconds(command)

    with vertical_lock:
        try:
            ensure_vertical_serial_connection()
            vertical_serial_conn.write(payload)
            vertical_serial_conn.flush()
        except Exception:
            try:
                if vertical_serial_conn and vertical_serial_conn.is_open:
                    vertical_serial_conn.close()
            finally:
                vertical_serial_conn = None

            ensure_vertical_serial_connection()
            vertical_serial_conn.write(payload)
            vertical_serial_conn.flush()

        ack = wait_for_axis_ack(
            vertical_serial_conn,
            ack_timeout_seconds,
            VERTICAL_COM_PORT,
            abort_event=abort_event,
        )

    return ack


def validate_axis_move(axis: str, steps: int) -> None:
    axis_min, axis_max = AXIS_LIMITS[axis]
    with axis_position_lock:
        current = axis_positions[axis]
    new_position = current + int(steps)

    if new_position < axis_min or new_position > axis_max:
        raise ValueError(
            f"{axis} move exceeds range: current={current}, command={steps}, "
            f"new={new_position}, allowed=[{axis_min}, {axis_max}]"
        )

    if abs(int(steps)) > MAX_AXIS_COMMAND:
        raise ValueError(f"{axis} command exceeds maximum step command {MAX_AXIS_COMMAND}.")


def move_linear_steps(steps: int, abort_event: threading.Event | None = None) -> str | None:
    validate_axis_move("linear", steps)
    ack = send_linear_text(str(int(steps)), abort_event=abort_event)
    with axis_position_lock:
        axis_positions["linear"] += int(steps)
    return ack


def move_horizontal_steps(steps: int, abort_event: threading.Event | None = None) -> str | None:
    validate_axis_move("horizontal", steps)
    ack = send_horizontal_text(str(int(steps)), abort_event=abort_event)
    with axis_position_lock:
        axis_positions["horizontal"] += int(steps)
    return ack


def move_vertical_steps(steps: int, abort_event: threading.Event | None = None) -> str | None:
    validate_axis_move("vertical", steps)
    ack = send_vertical_text(str(int(steps)), abort_event=abort_event)
    with axis_position_lock:
        axis_positions["vertical"] += int(steps)
    return ack


def set_automation_state(
    *,
    running: bool | None = None,
    step: str | None = None,
    error: str | None | object = _NO_AUTOMATION_ERROR_UPDATE,
) -> None:
    with automation_lock:
        if running is not None:
            automation_state["running"] = running
        if step is not None:
            automation_state["current_step"] = step
        if error is not _NO_AUTOMATION_ERROR_UPDATE:
            automation_state["last_error"] = error


def set_stopped(error: str | None = None) -> None:
    state["running"] = False
    state["target_rpm"] = None
    state["duration_seconds"] = None
    state["started_at"] = None
    state["ends_at"] = None
    state["last_error"] = error


def run_rpm_for_duration(rpm: int, duration_seconds: int, abort_event: threading.Event | None = None) -> None:
    with state_lock:
        send_rpm(rpm)
        now = datetime.now(timezone.utc)
        state["running"] = True
        state["target_rpm"] = rpm
        state["duration_seconds"] = duration_seconds
        state["started_at"] = now
        state["ends_at"] = now + timedelta(seconds=duration_seconds)
        state["last_error"] = None

    deadline = time.monotonic() + duration_seconds
    aborted = False

    while time.monotonic() < deadline:
        if abort_event is not None and abort_event.is_set():
            aborted = True
            break
        time.sleep(0.1)

    with state_lock:
        send_rpm(STOP_RPM)
        set_stopped("Automation aborted." if aborted else None)

    if aborted:
        raise AutomationAbortRequested("Abort requested during RPM run.")


def sleep_interruptible(seconds: float, abort_event: threading.Event | None = None) -> None:
    if seconds <= 0:
        return

    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        if abort_event is not None and abort_event.is_set():
            raise AutomationAbortRequested("Abort requested during delay.")
        time.sleep(0.1)


def move_to_xyz(x: int, vertical: int, z: int, abort_event: threading.Event | None = None) -> None:
    with axis_position_lock:
        current_x = axis_positions["horizontal"]
        current_y = axis_positions["vertical"]
        current_z = axis_positions["linear"]

    if current_z != SAFE_Z:
        move_linear_steps(SAFE_Z - current_z, abort_event=abort_event)

    x_delta = int(x) - current_x
    y_delta = int(vertical) - current_y

    if x_delta != 0:
        move_horizontal_steps(x_delta, abort_event=abort_event)

    if y_delta != 0:
        move_vertical_steps(y_delta, abort_event=abort_event)

    with axis_position_lock:
        current_z = axis_positions["linear"]

    z_delta = int(z) - current_z

    if z_delta != 0:
        move_linear_steps(z_delta, abort_event=abort_event)


def home_axes_internal() -> dict:
    with axis_position_lock:
        linear_offset = axis_positions["linear"]
        horizontal_offset = axis_positions["horizontal"]
        vertical_offset = axis_positions["vertical"]

    linear_home_cmd = -linear_offset
    horizontal_home_cmd = -horizontal_offset
    vertical_home_cmd = -vertical_offset

    if linear_home_cmd != 0:
        send_linear_text(str(linear_home_cmd))

    rotation_ack = send_rotation_command(0)

    if horizontal_home_cmd != 0:
        send_horizontal_text(str(horizontal_home_cmd))

    if vertical_home_cmd != 0:
        send_vertical_text(str(vertical_home_cmd))

    with axis_position_lock:
        axis_positions["linear"] = 0
        axis_positions["horizontal"] = 0
        axis_positions["vertical"] = 0

    return {
        "linear_command": linear_home_cmd,
        "horizontal_command": horizontal_home_cmd,
        "vertical_command": vertical_home_cmd,
        "rotation_command": 0,
        "rotation_ack": rotation_ack,
    }


def horizontal_offset_for_sample(sample_index: int) -> int:
    if sample_index == 1:
        return -80000
    if sample_index == 2:
        return 0
    if sample_index == 3:
        return 80000
    raise ValueError(f"Invalid sample index: {sample_index}")


def parse_int_field(data: dict[str, Any], names: tuple[str, ...], default: int | None = None) -> int:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return int(data[name])
    if default is None:
        raise ValueError(f"missing integer field: {names[0]}")
    return default


def parse_recipe_steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = payload.get("steps")

    if raw_steps is None:
        raw_samples = payload.get("samples")
        if isinstance(raw_samples, list):
            raw_steps = []
            for idx, sample in enumerate(raw_samples, start=1):
                if not isinstance(sample, dict) or not bool(sample.get("enabled", False)):
                    continue
                rpm = parse_int_field(sample, ("rpm",))
                duration_seconds = parse_int_field(sample, ("duration_seconds", "seconds"))
                raw_steps.append(
                    {
                        "name": f"Sample {idx}",
                        "enabled": True,
                        "x": horizontal_offset_for_sample(idx),
                        "vertical": 0,
                        "z": 50000,
                        "rpm": rpm,
                        "duration_seconds": duration_seconds,
                        "rotation_command": "",
                    }
                )

    if not isinstance(raw_steps, list):
        raise ValueError("steps must be a list.")

    parsed_steps = []

    for idx, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"step {idx} must be an object.")

        enabled = bool(raw_step.get("enabled", True))
        if not enabled:
            continue

        name = str(raw_step.get("name") or f"Step {idx}").strip()
        x = parse_int_field(raw_step, ("x", "horizontal"), default=0)
        vertical = parse_int_field(raw_step, ("vertical", "y"), default=0)
        z = parse_int_field(raw_step, ("z", "linear"), default=0)
        rpm = parse_int_field(raw_step, ("rpm",), default=0)
        duration_seconds = parse_int_field(raw_step, ("duration_seconds", "seconds", "time"), default=0)
        rotation_command = str(raw_step.get("rotation_command", "") or "").strip()

        if rpm != 0 and (rpm < RPM_MIN or rpm > RPM_MAX):
            raise ValueError(f"{name}: rpm must be 0 or between {RPM_MIN} and {RPM_MAX}.")

        if duration_seconds < 0:
            raise ValueError(f"{name}: duration_seconds cannot be negative.")

        if rpm != 0 and duration_seconds <= 0:
            raise ValueError(f"{name}: duration_seconds must be > 0 when rpm is nonzero.")

        for axis, value in (("horizontal", x), ("vertical", vertical), ("linear", z)):
            axis_min, axis_max = AXIS_LIMITS[axis]
            if value < axis_min or value > axis_max:
                raise ValueError(f"{name}: {axis} position {value} is outside [{axis_min}, {axis_max}].")

        parsed_steps.append(
            {
                "name": name,
                "x": x,
                "vertical": vertical,
                "z": z,
                "rpm": rpm,
                "duration_seconds": duration_seconds,
                "rotation_command": rotation_command,
            }
        )

    if not parsed_steps:
        raise ValueError("select at least one enabled step.")

    return parsed_steps


def normalize_recipe_steps_for_storage(raw_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        raise ValueError("steps must be a list.")

    if len(raw_steps) > MAX_RECIPE_STEPS:
        raise ValueError(f"recipe cannot contain more than {MAX_RECIPE_STEPS} steps.")

    normalized_steps = []

    for idx, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"step {idx} must be an object.")

        enabled = bool(raw_step.get("enabled", True))
        name = str(raw_step.get("name") or f"Step {idx}").strip()
        if not name:
            name = f"Step {idx}"

        try:
            x = parse_int_field(raw_step, ("x", "horizontal"), default=0)
            vertical = parse_int_field(raw_step, ("vertical", "y"), default=0)
            z = parse_int_field(raw_step, ("z", "linear"), default=0)
            rpm = parse_int_field(raw_step, ("rpm",), default=0)
            duration_seconds = parse_int_field(raw_step, ("duration_seconds", "seconds", "time"), default=0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: invalid numeric field: {exc}") from exc

        rotation_command = str(raw_step.get("rotation_command", "") or "").strip()

        if rpm != 0 and (rpm < RPM_MIN or rpm > RPM_MAX):
            raise ValueError(f"{name}: rpm must be 0 or between {RPM_MIN} and {RPM_MAX}.")

        if duration_seconds < 0:
            raise ValueError(f"{name}: duration_seconds cannot be negative.")

        for axis, value in (("horizontal", x), ("vertical", vertical), ("linear", z)):
            axis_min, axis_max = AXIS_LIMITS[axis]
            if value < axis_min or value > axis_max:
                raise ValueError(f"{name}: {axis} position {value} is outside [{axis_min}, {axis_max}].")

        normalized_steps.append(
            {
                "name": name,
                "enabled": enabled,
                "x": x,
                "vertical": vertical,
                "z": z,
                "rpm": rpm,
                "duration_seconds": duration_seconds,
                "rotation_command": rotation_command,
            }
        )

    return normalized_steps


def default_recipe_payload() -> dict[str, Any]:
    return {
        "repetitions": 1,
        "steps": [
            {
                "name": "Sample 1",
                "enabled": True,
                "x": 0,
                "vertical": 0,
                "z": 50000,
                "rpm": 1000,
                "duration_seconds": 10,
                "rotation_command": "",
            },
            {
                "name": "Sample 2",
                "enabled": True,
                "x": 80000,
                "vertical": 0,
                "z": 50000,
                "rpm": 1000,
                "duration_seconds": 10,
                "rotation_command": "",
            },
            {
                "name": "DI Water",
                "enabled": False,
                "x": 120000,
                "vertical": 60000,
                "z": 50000,
                "rpm": 0,
                "duration_seconds": 5,
                "rotation_command": "",
            },
        ],
    }


def automation_worker(recipe_steps: list[dict[str, Any]], repetitions: int) -> None:
    try:
        for repetition in range(1, repetitions + 1):
            for idx, recipe_step in enumerate(recipe_steps, start=1):
                name = recipe_step["name"]
                set_automation_state(
                    step=f"Rep {repetition}/{repetitions} - Move to {name} ({idx}/{len(recipe_steps)})"
                )

                move_to_xyz(
                    recipe_step["x"],
                    recipe_step["vertical"],
                    recipe_step["z"],
                    abort_event=automation_abort_event,
                )

                rotation_command = recipe_step["rotation_command"]
                if rotation_command:
                    set_automation_state(step=f"Rep {repetition}/{repetitions} - Rotate at {name}")
                    send_rotation_text(rotation_command)

                rpm = recipe_step["rpm"]
                duration_seconds = recipe_step["duration_seconds"]

                if rpm != 0 and duration_seconds > 0:
                    set_automation_state(
                        step=f"Rep {repetition}/{repetitions} - {name}: {rpm} RPM for {duration_seconds}s"
                    )
                    run_rpm_for_duration(rpm, duration_seconds, abort_event=automation_abort_event)
                elif duration_seconds > 0:
                    set_automation_state(
                        step=f"Rep {repetition}/{repetitions} - {name}: wait {duration_seconds}s"
                    )
                    sleep_interruptible(duration_seconds, abort_event=automation_abort_event)

        set_automation_state(step="Moving to safe Z")
        with axis_position_lock:
            current_z = axis_positions["linear"]

        if current_z != SAFE_Z:
            move_linear_steps(SAFE_Z - current_z, abort_event=automation_abort_event)

        set_automation_state(step="Homing axes")
        home_axes_internal()
        set_automation_state(running=False, step="Automation complete", error=None)
    except AutomationAbortRequested:
        try:
            set_automation_state(step="Abort requested: stopping motor")
            with state_lock:
                send_rpm(STOP_RPM)
                set_stopped(None)
            set_automation_state(step="Abort requested: homing axes")
            home_axes_internal()
            set_automation_state(running=False, step="Automation aborted and homed", error=None)
        except Exception as exc:
            with state_lock:
                set_stopped(str(exc))
            set_automation_state(running=False, step="Automation abort failed", error=str(exc))
    except Exception as exc:
        with state_lock:
            set_stopped(str(exc))
        set_automation_state(running=False, step="Automation failed", error=str(exc))
    finally:
        automation_abort_event.clear()


def auto_stop() -> None:
    global stop_timer
    with state_lock:
        if not state["running"]:
            return
        try:
            send_rpm(STOP_RPM)
            set_stopped(None)
        except Exception as exc:
            set_stopped(str(exc))
        finally:
            stop_timer = None


@app.get("/")
def index():
    return render_template(
        "index.html",
        rpm_min=RPM_MIN,
        rpm_max=RPM_MAX,
        stop_rpm=STOP_RPM,
        com_port=COM_PORT,
        rotation_com_port=ROTATION_COM_PORT,
        linear_com_port=LINEAR_COM_PORT,
        horizontal_com_port=HORIZONTAL_COM_PORT,
        vertical_com_port=VERTICAL_COM_PORT,
    )


@app.get("/api/status")
def status():
    with state_lock:
        with axis_position_lock:
            linear_pos = axis_positions["linear"]
            horizontal_pos = axis_positions["horizontal"]
            vertical_pos = axis_positions["vertical"]
        with automation_lock:
            automation_running = automation_state["running"]
            automation_step = automation_state["current_step"]
            automation_error = automation_state["last_error"]
        return jsonify(
            {
                "running": state["running"],
                "target_rpm": state["target_rpm"],
                "duration_seconds": state["duration_seconds"],
                "started_at": _iso_utc(state["started_at"]),
                "ends_at": _iso_utc(state["ends_at"]),
                "last_error": state["last_error"],
                "com_port": COM_PORT,
                "rotation_com_port": ROTATION_COM_PORT,
                "linear_com_port": LINEAR_COM_PORT,
                "horizontal_com_port": HORIZONTAL_COM_PORT,
                "vertical_com_port": VERTICAL_COM_PORT,
                "linear_position": linear_pos,
                "horizontal_position": horizontal_pos,
                "vertical_position": vertical_pos,
                "axis_limits": AXIS_LIMITS,
                "safe_z": SAFE_Z,
                "automation_running": automation_running,
                "automation_step": automation_step,
                "automation_error": automation_error,
                "stop_rpm": STOP_RPM,
            }
        )


@app.post("/api/start")
def start():
    global stop_timer

    payload = request.get_json(silent=True) or {}

    try:
        rpm = int(payload.get("rpm"))
        duration = int(payload.get("duration_seconds"))
    except (TypeError, ValueError):
        return jsonify({"error": "rpm and duration_seconds must be integers."}), 400

    if rpm < RPM_MIN or rpm > RPM_MAX:
        return jsonify({"error": f"rpm must be between {RPM_MIN} and {RPM_MAX}."}), 400

    if duration <= 0:
        return jsonify({"error": "duration_seconds must be > 0."}), 400

    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; wait until it finishes."}), 409

    with state_lock:
        if state["running"]:
            return jsonify({"error": "Motor is already running."}), 409

        try:
            send_rpm(rpm)
        except Exception as exc:
            set_stopped(str(exc))
            return jsonify({"error": f"Unable to send rpm to {COM_PORT}: {exc}"}), 500

        now = datetime.now(timezone.utc)
        state["running"] = True
        state["target_rpm"] = rpm
        state["duration_seconds"] = duration
        state["started_at"] = now
        state["ends_at"] = now + timedelta(seconds=duration)
        state["last_error"] = None

        stop_timer = threading.Timer(duration, auto_stop)
        stop_timer.daemon = True
        stop_timer.start()

        return jsonify({"ok": True})


@app.post("/api/stop")
def stop():
    global stop_timer

    with state_lock:
        if stop_timer is not None:
            stop_timer.cancel()
            stop_timer = None

        try:
            send_rpm(STOP_RPM)
            set_stopped(None)
        except Exception as exc:
            set_stopped(str(exc))
            return jsonify({"error": f"Unable to send stop rpm to {COM_PORT}: {exc}"}), 500

    return jsonify({"ok": True, "stop_rpm": STOP_RPM})


@app.post("/api/rotation/ccw")
def rotation_ccw():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual rotation commands are disabled."}), 409

    try:
        ack = send_rotation_command(1)
    except Exception as exc:
        return jsonify({"error": f"Unable to send 1 to {ROTATION_COM_PORT}: {exc}"}), 500

    return jsonify({"ok": True, "value": 1, "com_port": ROTATION_COM_PORT, "ack": ack})


@app.post("/api/rotation/home")
def rotation_home():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual rotation commands are disabled."}), 409

    try:
        ack = send_rotation_command(0)
    except Exception as exc:
        return jsonify({"error": f"Unable to send 0 to {ROTATION_COM_PORT}: {exc}"}), 500

    return jsonify({"ok": True, "value": 0, "com_port": ROTATION_COM_PORT, "ack": ack})


@app.post("/api/rotation/send")
def rotation_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual rotation commands are disabled."}), 409

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()

    if not command:
        return jsonify({"error": "command must be a non-empty string."}), 400

    try:
        ack = send_rotation_text(command)
    except Exception as exc:
        return jsonify({"error": f"Unable to send '{command}' to {ROTATION_COM_PORT}: {exc}"}), 500

    return jsonify({"ok": True, "command": command, "com_port": ROTATION_COM_PORT, "ack": ack})


def parse_axis_command_request(axis_name: str) -> int | tuple[Any, int]:
    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()

    try:
        steps = int(command)
    except ValueError:
        return jsonify({"error": "command must be an integer."}), 400

    if steps == 0:
        return jsonify({"error": "command cannot be 0."}), 400

    try:
        validate_axis_move(axis_name, steps)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return steps


@app.post("/api/linear/send")
def linear_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual linear commands are disabled."}), 409

    steps_or_error = parse_axis_command_request("linear")
    if not isinstance(steps_or_error, int):
        return steps_or_error

    try:
        ack = move_linear_steps(steps_or_error)
    except Exception as exc:
        return jsonify({"error": f"Unable to send '{steps_or_error}' to {LINEAR_COM_PORT}: {exc}"}), 500

    return jsonify({"ok": True, "command": str(steps_or_error), "com_port": LINEAR_COM_PORT, "ack": ack})


@app.post("/api/horizontal/send")
def horizontal_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual horizontal commands are disabled."}), 409

    steps_or_error = parse_axis_command_request("horizontal")
    if not isinstance(steps_or_error, int):
        return steps_or_error

    try:
        ack = move_horizontal_steps(steps_or_error)
    except Exception as exc:
        return jsonify({"error": f"Unable to send '{steps_or_error}' to {HORIZONTAL_COM_PORT}: {exc}"}), 500

    return jsonify({"ok": True, "command": str(steps_or_error), "com_port": HORIZONTAL_COM_PORT, "ack": ack})


@app.post("/api/vertical/send")
def vertical_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual vertical commands are disabled."}), 409

    steps_or_error = parse_axis_command_request("vertical")
    if not isinstance(steps_or_error, int):
        return steps_or_error

    try:
        ack = move_vertical_steps(steps_or_error)
    except Exception as exc:
        return jsonify({"error": f"Unable to send '{steps_or_error}' to {VERTICAL_COM_PORT}: {exc}"}), 500

    return jsonify({"ok": True, "command": str(steps_or_error), "com_port": VERTICAL_COM_PORT, "ack": ack})


@app.post("/api/axes/home")
def axes_home():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; home is disabled."}), 409

    try:
        result = home_axes_internal()
    except Exception as exc:
        return jsonify({"error": f"Unable to return axes to home position: {exc}"}), 500

    return jsonify(
        {
            "ok": True,
            "linear_command": result["linear_command"],
            "horizontal_command": result["horizontal_command"],
            "vertical_command": result["vertical_command"],
            "linear_position": 0,
            "horizontal_position": 0,
            "vertical_position": 0,
            "linear_com_port": LINEAR_COM_PORT,
            "rotation_com_port": ROTATION_COM_PORT,
            "horizontal_com_port": HORIZONTAL_COM_PORT,
            "vertical_com_port": VERTICAL_COM_PORT,
            "rotation_command": result["rotation_command"],
            "rotation_ack": result["rotation_ack"],
        }
    )


def normalize_recipe_name(raw_name: Any) -> str:
    name = str(raw_name or "").strip()
    if not name:
        raise ValueError("recipe name cannot be empty.")
    if len(name) > MAX_RECIPE_NAME_LENGTH:
        raise ValueError(f"recipe name cannot be longer than {MAX_RECIPE_NAME_LENGTH} characters.")
    name = re.sub(r"[^A-Za-z0-9 _.-]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if not name:
        raise ValueError("recipe name must contain at least one letter or number.")
    return name


def recipe_path_for_name(raw_name: Any) -> Path:
    name = normalize_recipe_name(raw_name)
    return RECIPES_DIR / f"{name}.json"


def recipe_payload_from_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("recipe file is invalid.")

    name = normalize_recipe_name(data.get("name", path.stem))

    try:
        repetitions = int(data.get("repetitions", 1))
    except (TypeError, ValueError):
        repetitions = 1

    repetitions = min(100, max(1, repetitions))
    steps = normalize_recipe_steps_for_storage(data.get("steps", []))

    return {
        "name": name,
        "repetitions": repetitions,
        "steps": steps,
        "saved_at": data.get("saved_at"),
    }


def write_recipe_payload(name: str, repetitions: int, steps: list[dict[str, Any]]) -> Path:
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    path = recipe_path_for_name(name)
    data = {
        "name": normalize_recipe_name(name),
        "repetitions": repetitions,
        "steps": steps,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


def list_saved_recipes() -> list[dict[str, Any]]:
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    recipes = []

    for path in sorted(RECIPES_DIR.glob("*.json")):
        try:
            data = recipe_payload_from_file(path)
            recipes.append(
                {
                    "name": data["name"],
                    "repetitions": data["repetitions"],
                    "step_count": len(data["steps"]),
                    "saved_at": data.get("saved_at"),
                }
            )
        except Exception:
            continue

    if not recipes and LEGACY_RECIPE_PATH.exists():
        try:
            data = recipe_payload_from_file(LEGACY_RECIPE_PATH)
            data["name"] = DEFAULT_RECIPE_NAME
            write_recipe_payload(data["name"], data["repetitions"], data["steps"])
            recipes.append(
                {
                    "name": data["name"],
                    "repetitions": data["repetitions"],
                    "step_count": len(data["steps"]),
                    "saved_at": data.get("saved_at"),
                }
            )
        except Exception:
            pass

    return recipes


@app.get("/api/recipes")
def list_recipes():
    return jsonify({"ok": True, "recipes": list_saved_recipes()})


@app.get("/api/recipe")
def load_recipe():
    raw_name = request.args.get("name", DEFAULT_RECIPE_NAME)

    try:
        name = normalize_recipe_name(raw_name)
        path = recipe_path_for_name(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not path.exists():
        if name == DEFAULT_RECIPE_NAME and LEGACY_RECIPE_PATH.exists():
            path = LEGACY_RECIPE_PATH
        else:
            payload = default_recipe_payload()
            return jsonify({"ok": True, "name": name, **payload})

    try:
        data = recipe_payload_from_file(path)
    except Exception as exc:
        return jsonify({"error": f"Unable to load recipe: {exc}"}), 500

    data["name"] = name
    return jsonify({"ok": True, **data})


@app.post("/api/recipe")
def save_recipe():
    payload = request.get_json(silent=True) or {}

    try:
        name = normalize_recipe_name(payload.get("name", DEFAULT_RECIPE_NAME))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        repetitions = int(payload.get("repetitions", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "repetitions must be an integer."}), 400

    if repetitions < 1 or repetitions > 100:
        return jsonify({"error": "repetitions must be between 1 and 100."}), 400

    try:
        steps = normalize_recipe_steps_for_storage(payload.get("steps"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        write_recipe_payload(name, repetitions, steps)
    except Exception as exc:
        return jsonify({"error": f"Unable to save recipe: {exc}"}), 500

    return jsonify({"ok": True, "name": name, "count": len(steps), "repetitions": repetitions})


@app.delete("/api/recipe")
def delete_recipe():
    raw_name = request.args.get("name", "")

    try:
        name = normalize_recipe_name(raw_name)
        path = recipe_path_for_name(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not path.exists():
        return jsonify({"error": f"Recipe '{name}' does not exist."}), 404

    try:
        path.unlink()
    except Exception as exc:
        return jsonify({"error": f"Unable to delete recipe: {exc}"}), 500

    return jsonify({"ok": True, "name": name})


@app.get("/api/automation/status")
def automation_status():
    with automation_lock:
        return jsonify(
            {
                "running": automation_state["running"],
                "current_step": automation_state["current_step"],
                "last_error": automation_state["last_error"],
            }
        )


@app.post("/api/automation/start")
def automation_start():
    payload = request.get_json(silent=True) or {}

    try:
        repetitions = int(payload.get("repetitions", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "repetitions must be an integer."}), 400

    if repetitions < 1 or repetitions > 100:
        return jsonify({"error": "repetitions must be between 1 and 100."}), 400

    try:
        recipe_steps = parse_recipe_steps(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    with state_lock:
        if state["running"]:
            return jsonify({"error": "motor is currently running; stop it before automation."}), 409

    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is already running."}), 409
        automation_state["running"] = True
        automation_state["current_step"] = "Queued"
        automation_state["last_error"] = None
        automation_abort_event.clear()

    worker = threading.Thread(
        target=automation_worker,
        args=(recipe_steps, repetitions),
        daemon=True,
    )
    worker.start()

    return jsonify(
        {
            "ok": True,
            "selected_steps": [step["name"] for step in recipe_steps],
            "repetitions": repetitions,
        }
    )


@app.post("/api/automation/abort-home")
def automation_abort_home():
    with automation_lock:
        if not automation_state["running"]:
            return jsonify({"error": "automation is not running."}), 409
        automation_state["current_step"] = "Abort requested; waiting for safe stop"
        automation_state["last_error"] = None

    automation_abort_event.set()
    return jsonify({"ok": True, "message": "Abort requested. System will stop and go home."})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
