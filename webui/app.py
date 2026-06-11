from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request

try:
    import serial
except ImportError:  # pragma: no cover
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
    # Allow board reset/bootloader to settle after opening serial.
    time.sleep(2)


def send_rpm(rpm: int) -> None:
    ensure_serial_connection()
    serial_conn.write(f"{int(rpm)}\\n".encode("ascii"))
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
    # Give Nano time to finish reset once when the port is first opened.
    time.sleep(2.0)
    rotation_serial_conn.reset_input_buffer()
    rotation_serial_conn.reset_output_buffer()


def send_rotation_command(value: int) -> str | None:
    global rotation_serial_conn

    if serial is None:
        raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

    payload = f"{int(value)}\\n".encode("ascii")
    ack = None

    # Keep COM7 open across button presses to avoid resetting Nano every click.
    with rotation_lock:
        try:
            ensure_rotation_serial_connection()
            rotation_serial_conn.write(payload)
            rotation_serial_conn.flush()
        except Exception:
            # One reconnect attempt for stale/broken serial handles.
            try:
                if rotation_serial_conn and rotation_serial_conn.is_open:
                    rotation_serial_conn.close()
            finally:
                rotation_serial_conn = None

            ensure_rotation_serial_connection()
            rotation_serial_conn.write(payload)
            rotation_serial_conn.flush()

        # Best-effort read one non-empty line for diagnostics.
        for _ in range(4):
            line = rotation_serial_conn.readline().decode("utf-8", errors="replace").strip()
            if line:
                ack = line
                break

    return ack


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


def wait_for_axis_ack(conn, timeout_seconds: float, com_port: str, abort_event: threading.Event | None = None) -> str:
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
    ack = None
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
        ack = wait_for_axis_ack(linear_serial_conn, ack_timeout_seconds, LINEAR_COM_PORT, abort_event=abort_event)

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
    ack = None
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
        ack = wait_for_axis_ack(horizontal_serial_conn, ack_timeout_seconds, HORIZONTAL_COM_PORT, abort_event=abort_event)

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


def move_linear_steps(steps: int, abort_event: threading.Event | None = None) -> str | None:
    ack = send_linear_text(str(int(steps)), abort_event=abort_event)
    with axis_position_lock:
        axis_positions["linear"] += int(steps)
    return ack


def move_horizontal_steps(steps: int, abort_event: threading.Event | None = None) -> str | None:
    ack = send_horizontal_text(str(int(steps)), abort_event=abort_event)
    with axis_position_lock:
        axis_positions["horizontal"] += int(steps)
    return ack

def move_vertical_steps(steps: int, abort_event: threading.Event | None = None) -> str | None:
    ack = send_vertical_text(str(int(steps)), abort_event=abort_event)
    with axis_position_lock:
        axis_positions["vertical"] += int(steps)
    return ack


def set_automation_state(
    *, running: bool | None = None, step: str | None = None, error: str | None | object = _NO_AUTOMATION_ERROR_UPDATE
) -> None:
    with automation_lock:
        if running is not None:
            automation_state["running"] = running
        if step is not None:
            automation_state["current_step"] = step
        if error is not _NO_AUTOMATION_ERROR_UPDATE:
            automation_state["last_error"] = error


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
    # From home (0): sample 1 is -80000, sample 2 is 0, sample 3 is +80000.
    if sample_index == 1:
        return -80000
    if sample_index == 2:
        return 0
    if sample_index == 3:
        return 80000
    raise ValueError(f"Invalid sample index: {sample_index}")


def automation_worker(samples: list[dict], spin_down_seconds: int, repetitions: int) -> None:
    try:
        first_sample_index = samples[0]["sample_index"]

        for repetition in range(1, repetitions + 1):
            set_automation_state(step=f"Preparing repetition {repetition}/{repetitions}")
            current_horizontal_offset = horizontal_offset_for_sample(first_sample_index)
            if current_horizontal_offset != 0:
                move_horizontal_steps(current_horizontal_offset, abort_event=automation_abort_event)
            sleep_interruptible(5, abort_event=automation_abort_event)
            move_linear_steps(50000, abort_event=automation_abort_event)

            for i, sample in enumerate(samples):
                sample_num = sample["sample_index"]
                rpm = sample["rpm"]
                duration = sample["duration_seconds"]

                set_automation_state(
                    step=f"Rep {repetition}/{repetitions} - Sample {sample_num}: {rpm} RPM for {duration}s"
                )
                run_rpm_for_duration(rpm, duration, abort_event=automation_abort_event)
                if spin_down_seconds > 0:
                    set_automation_state(step=f"Rep {repetition}/{repetitions} - Spin-down ({spin_down_seconds}s)")
                    sleep_interruptible(spin_down_seconds, abort_event=automation_abort_event)

                if i < len(samples) - 1:
                    next_sample_index = samples[i + 1]["sample_index"]
                    set_automation_state(step=f"Rep {repetition}/{repetitions} - Transition to sample {next_sample_index}")
                    move_linear_steps(-50000, abort_event=automation_abort_event)
                    sleep_interruptible(5, abort_event=automation_abort_event)
                    next_horizontal_offset = horizontal_offset_for_sample(next_sample_index)
                    horizontal_delta = next_horizontal_offset - current_horizontal_offset
                    if horizontal_delta != 0:
                        move_horizontal_steps(horizontal_delta, abort_event=automation_abort_event)
                    current_horizontal_offset = next_horizontal_offset
                    sleep_interruptible(5, abort_event=automation_abort_event)
                    move_linear_steps(50000, abort_event=automation_abort_event)

            set_automation_state(step=f"Rep {repetition}/{repetitions} - Final linear return")
            move_linear_steps(-50000, abort_event=automation_abort_event)

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
        except Exception as exc:  # pragma: no cover
            with state_lock:
                set_stopped(str(exc))
            set_automation_state(running=False, step="Automation abort failed", error=str(exc))
    except Exception as exc:  # pragma: no cover
        with state_lock:
            set_stopped(str(exc))
        set_automation_state(running=False, step="Automation failed", error=str(exc))
    finally:
        automation_abort_event.clear()


def set_stopped(error: str | None = None) -> None:
    state["running"] = False
    state["target_rpm"] = None
    state["duration_seconds"] = None
    state["started_at"] = None
    state["ends_at"] = None
    state["last_error"] = error


def auto_stop() -> None:
    global stop_timer
    with state_lock:
        if not state["running"]:
            return
        try:
            send_rpm(STOP_RPM)
            set_stopped(None)
        except Exception as exc:  # pragma: no cover
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
        return (
            jsonify(
                {
                    "error": (
                        f"Unable to send 1 to {ROTATION_COM_PORT}: {exc}. "
                        f"Close Arduino Serial Monitor/Plotter for {ROTATION_COM_PORT} and try again."
                    )
                }
            ),
            500,
        )

    return jsonify({"ok": True, "value": 1, "com_port": ROTATION_COM_PORT, "ack": ack})


@app.post("/api/rotation/home")
def rotation_home():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual rotation commands are disabled."}), 409

    try:
        ack = send_rotation_command(0)
    except Exception as exc:
        return (
            jsonify(
                {
                    "error": (
                        f"Unable to send 0 to {ROTATION_COM_PORT}: {exc}. "
                        f"Close Arduino Serial Monitor/Plotter for {ROTATION_COM_PORT} and try again."
                    )
                }
            ),
            500,
        )

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
        return (
            jsonify(
                {
                    "error": (
                        f"Unable to send '{command}' to {ROTATION_COM_PORT}: {exc}. "
                        f"Close Arduino Serial Monitor/Plotter for {ROTATION_COM_PORT} and try again."
                    )
                }
            ),
            500,
        )

    return jsonify({"ok": True, "command": command, "com_port": ROTATION_COM_PORT, "ack": ack})


@app.post("/api/linear/send")
def linear_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual linear commands are disabled."}), 409

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()

    try:
        steps = int(command)
    except ValueError:
        return jsonify({"error": "command must be an integer."}), 400

    if steps == 0:
        return jsonify({"error": "command cannot be 0."}), 400

    if abs(steps) > 100000:
        return jsonify({"error": "absolute command must be between 1 and 100000."}), 400

    try:
        ack = move_linear_steps(steps)
    except Exception as exc:
        return (
            jsonify(
                {
                    "error": (
                        f"Unable to send '{command}' to {LINEAR_COM_PORT}: {exc}. "
                        f"Close Arduino Serial Monitor/Plotter for {LINEAR_COM_PORT} and try again."
                    )
                }
            ),
            500,
        )

    return jsonify({"ok": True, "command": command, "com_port": LINEAR_COM_PORT, "ack": ack})


@app.post("/api/horizontal/send")
def horizontal_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual horizontal commands are disabled."}), 409

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()

    try:
        steps = int(command)
    except ValueError:
        return jsonify({"error": "command must be an integer."}), 400

    if steps == 0:
        return jsonify({"error": "command cannot be 0."}), 400

    if abs(steps) > 100000:
        return jsonify({"error": "absolute command must be between 1 and 100000."}), 400

    try:
        ack = move_horizontal_steps(steps)
    except Exception as exc:
        return (
            jsonify(
                {
                    "error": (
                        f"Unable to send '{command}' to {HORIZONTAL_COM_PORT}: {exc}. "
                        f"Close Arduino Serial Monitor/Plotter for {HORIZONTAL_COM_PORT} and try again."
                    )
                }
            ),
            500,
        )

    return jsonify({"ok": True, "command": command, "com_port": HORIZONTAL_COM_PORT, "ack": ack})

@app.post("/api/vertical/send")
def vertical_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual vertical commands are disabled."}), 409

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()

    try:
        steps = int(command)
    except ValueError:
        return jsonify({"error": "command must be an integer."}), 400

    if steps == 0:
        return jsonify({"error": "command cannot be 0."}), 400

    if abs(steps) > 100000:
        return jsonify({"error": "absolute command must be between 1 and 100000."}), 400

    try:
        ack = move_vertical_steps(steps)
    except Exception as exc:
        return (
            jsonify(
                {
                    "error": (
                        f"Unable to send '{command}' to {VERTICAL_COM_PORT}: {exc}. "
                        f"Close Arduino Serial Monitor/Plotter for {VERTICAL_COM_PORT} and try again."
                    )
                }
            ),
            500,
        )

    return jsonify({"ok": True, "command": command, "com_port": VERTICAL_COM_PORT, "ack": ack})

@app.post("/api/axes/home")
def axes_home():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; home is disabled."}), 409

    try:
        result = home_axes_internal()
    except Exception as exc:
        return (
            jsonify(
                {
                    "error": (
                        "Unable to return axes to home position: "
                        f"{exc}. Close Arduino Serial Monitor/Plotter for {LINEAR_COM_PORT} and "
                        f"{ROTATION_COM_PORT} and {HORIZONTAL_COM_PORT} and try again."
                    )
                }
            ),
            500,
        )

    return jsonify(
        {
            "ok": True,
            "linear_command": result["linear_command"],
            "horizontal_command": result["horizontal_command"],
            "linear_position": 0,
            "horizontal_position": 0,
            "linear_com_port": LINEAR_COM_PORT,
            "rotation_com_port": ROTATION_COM_PORT,
            "horizontal_com_port": HORIZONTAL_COM_PORT,
            "rotation_command": result["rotation_command"],
            "rotation_ack": result["rotation_ack"],
            "vertical_command": result["vertical_command"],
            "vertical_position": 0,
            "vertical_com_port": VERTICAL_COM_PORT,
        }
    )


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
    samples = payload.get("samples")
    spin_down_seconds = 5
    repetitions_raw = payload.get("repetitions", 1)

    try:
        repetitions = int(repetitions_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "repetitions must be an integer."}), 400

    if repetitions < 1 or repetitions > 100:
        return jsonify({"error": "repetitions must be between 1 and 100."}), 400

    if not isinstance(samples, list) or len(samples) != 3:
        return jsonify({"error": "samples must be a list of exactly 3 blocks."}), 400

    parsed_samples = []
    for idx, sample in enumerate(samples, start=1):
        if not isinstance(sample, dict):
            return jsonify({"error": f"sample {idx} must be an object."}), 400

        enabled = bool(sample.get("enabled", False))
        if not enabled:
            continue

        try:
            rpm = int(sample.get("rpm"))
            duration_seconds = int(sample.get("duration_seconds"))
        except (TypeError, ValueError):
            return jsonify({"error": f"sample {idx}: rpm and duration_seconds must be integers."}), 400

        if rpm < RPM_MIN or rpm > RPM_MAX:
            return jsonify({"error": f"sample {idx}: rpm must be between {RPM_MIN} and {RPM_MAX}."}), 400

        if duration_seconds <= 0:
            return jsonify({"error": f"sample {idx}: duration_seconds must be > 0."}), 400

        parsed_samples.append(
            {
                "sample_index": idx,
                "rpm": rpm,
                "duration_seconds": duration_seconds,
            }
        )

    if not parsed_samples:
        return jsonify({"error": "select at least one sample block."}), 400

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
        args=(parsed_samples, spin_down_seconds, repetitions),
        daemon=True,
    )
    worker.start()
    return jsonify(
        {
            "ok": True,
            "selected_samples": [s["sample_index"] for s in parsed_samples],
            "spin_down_seconds": spin_down_seconds,
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
