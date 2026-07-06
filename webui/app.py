
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from hardware.gamry_client import run_gamry_step
from workflow.protocol_loader import ProtocolError, list_protocols, load_protocol

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None


COM_PORT = "COM"             # RDE RPM controller
ROTATION_COM_PORT = "COM"     # RDE arm rotation
LINEAR_COM_PORT = "COM"       # Z axis
HORIZONTAL_COM_PORT = "COM"   # X axis

BAUD_RATE = 115200
RPM_MIN = 30
RPM_MAX = 12000
STOP_RPM = 20

SAMPLE_X_OFFSETS = {
    1: -80000,
    2: 0,
    3: 80000,
}
SAMPLE_Z_DOWN_STEPS = 50000
MOVE_SETTLE_SECONDS = 5
SPIN_DOWN_SECONDS = 5

OUTPUT_ROOT = Path("output/runs")

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
stop_timer = None

rotation_lock = threading.Lock()
linear_lock = threading.Lock()
horizontal_lock = threading.Lock()
axis_position_lock = threading.Lock()

axis_positions = {
    "linear": 0,
    "horizontal": 0,
}

automation_lock = threading.Lock()
automation_state = {
    "running": False,
    "current_step": None,
    "last_error": None,
    "run_dir": None,
}

_NO_AUTOMATION_ERROR_UPDATE = object()
automation_abort_event = threading.Event()


class AutomationAbortRequested(Exception):
    pass


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_name(value: Any, fallback: str = "item") -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._- ")
    return text or fallback


def append_log(run_dir: Path, message: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat()} | {message}\n"
    with (run_dir / "log.txt").open("a", encoding="utf-8") as f:
        f.write(line)


def create_run_dir(run_label: str = "real_integration") -> Path:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = OUTPUT_ROOT / f"{utc_stamp()}_{safe_name(run_label)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "_jobs").mkdir(parents=True, exist_ok=True)
    (run_dir / "samples").mkdir(parents=True, exist_ok=True)
    (run_dir / "protocol_snapshots").mkdir(parents=True, exist_ok=True)
    return run_dir


def sample_dir_for(run_dir: Path, sample_index: int) -> Path:
    sample_dir = run_dir / "samples" / f"{sample_index:03d}_sample_{sample_index}"
    sample_dir.mkdir(parents=True, exist_ok=True)
    return sample_dir


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def step_output_name(step: dict[str, Any], step_index: int, used_names: set[str]) -> str:
    raw = str(step.get("output") or "").strip()

    if not raw:
        raw = str(step.get("output_prefix") or step.get("name") or f"step_{step_index}").strip()
        if not raw.lower().endswith(".dta"):
            raw += ".DTA"

    name = safe_name(Path(raw).name, f"step_{step_index}.DTA")

    if not name.lower().endswith(".dta"):
        name += ".DTA"

    if name not in used_names:
        used_names.add(name)
        return name

    stem = Path(name).stem
    suffix = Path(name).suffix or ".DTA"
    counter = 2

    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1


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

        return wait_for_axis_ack(linear_serial_conn, ack_timeout_seconds, LINEAR_COM_PORT, abort_event=abort_event)


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

        return wait_for_axis_ack(horizontal_serial_conn, ack_timeout_seconds, HORIZONTAL_COM_PORT, abort_event=abort_event)


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


def set_automation_state(
    *,
    running: bool | None = None,
    step: str | None = None,
    error: str | None | object = _NO_AUTOMATION_ERROR_UPDATE,
    run_dir: str | None | object = _NO_AUTOMATION_ERROR_UPDATE,
) -> None:
    with automation_lock:
        if running is not None:
            automation_state["running"] = running
        if step is not None:
            automation_state["current_step"] = step
        if error is not _NO_AUTOMATION_ERROR_UPDATE:
            automation_state["last_error"] = error
        if run_dir is not _NO_AUTOMATION_ERROR_UPDATE:
            automation_state["run_dir"] = run_dir


def set_stopped(error: str | None = None) -> None:
    state["running"] = False
    state["target_rpm"] = None
    state["duration_seconds"] = None
    state["started_at"] = None
    state["ends_at"] = None
    state["last_error"] = error


def start_rpm_hold(rpm: int) -> None:
    with state_lock:
        send_rpm(rpm)
        now = datetime.now(timezone.utc)
        state["running"] = True
        state["target_rpm"] = rpm
        state["duration_seconds"] = None
        state["started_at"] = now
        state["ends_at"] = None
        state["last_error"] = None


def stop_rpm_hold(error: str | None = None) -> None:
    with state_lock:
        send_rpm(STOP_RPM)
        set_stopped(error)


def sleep_interruptible(seconds: float, abort_event: threading.Event | None = None) -> None:
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if abort_event is not None and abort_event.is_set():
            raise AutomationAbortRequested("Abort requested during delay.")
        time.sleep(0.1)


def home_axes_internal(abort_event: threading.Event | None = None) -> dict[str, Any]:
    with axis_position_lock:
        linear_offset = axis_positions["linear"]
        horizontal_offset = axis_positions["horizontal"]

    linear_home_cmd = -linear_offset
    horizontal_home_cmd = -horizontal_offset

    if linear_home_cmd != 0:
        send_linear_text(str(linear_home_cmd), abort_event=abort_event)
    rotation_ack = send_rotation_command(0)
    if horizontal_home_cmd != 0:
        send_horizontal_text(str(horizontal_home_cmd), abort_event=abort_event)

    with axis_position_lock:
        axis_positions["linear"] = 0
        axis_positions["horizontal"] = 0

    return {
        "linear_command": linear_home_cmd,
        "horizontal_command": horizontal_home_cmd,
        "rotation_command": 0,
        "rotation_ack": rotation_ack,
    }


def horizontal_offset_for_sample(sample_index: int) -> int:
    try:
        return SAMPLE_X_OFFSETS[int(sample_index)]
    except KeyError as exc:
        raise ValueError(f"Invalid sample index: {sample_index}") from exc


def run_protocol_for_sample(
    *,
    run_dir: Path,
    sample_index: int,
    protocol_name: str,
    sample_label: str,
) -> None:
    sample_dir = sample_dir_for(run_dir, sample_index)

    try:
        protocol = load_protocol(protocol_name)
    except ProtocolError as exc:
        raise RuntimeError(f"{sample_label}: unable to load protocol '{protocol_name}': {exc}") from exc

    snapshot = run_dir / "protocol_snapshots" / f"{sample_index:03d}_{safe_name(protocol_name)}.json"
    save_json(snapshot, protocol)
    append_log(run_dir, f"{sample_label}: protocol snapshot saved: {snapshot}")

    used_output_names: set[str] = set()
    steps = protocol.get("steps", [])

    if not steps:
        raise RuntimeError(f"{sample_label}: protocol '{protocol_name}' contains no steps.")

    for step_index, step in enumerate(steps, start=1):
        if automation_abort_event.is_set():
            raise AutomationAbortRequested("Abort requested before EChem step.")

        if not bool(step.get("enabled", True)):
            append_log(run_dir, f"{sample_label}: skipping disabled EChem step {step_index}.")
            continue

        technique = str(step.get("technique") or "echem")
        step_name = str(step.get("name") or f"step_{step_index}")
        output_name = step_output_name(step, step_index, used_output_names)
        output_path = sample_dir / output_name

        set_automation_state(step=f"{sample_label}: EChem {step_index}/{len(steps)} - {technique} / {step_name}")
        append_log(run_dir, f"{sample_label}: starting EChem step {step_index}: {technique} / {step_name}")

        result = run_gamry_step(
            step=step,
            outputs=[str(output_path)],
            run_dir=run_dir,
            sample_id=f"sample_{sample_index:03d}",
        )

        append_log(run_dir, f"{sample_label}: finished EChem step {step_index}: {result}")


def automation_worker(samples: list[dict[str, Any]], repetitions: int) -> None:
    run_dir = create_run_dir("real_gamry_integration")
    set_automation_state(run_dir=str(run_dir))
    save_json(
        run_dir / "run_request.json",
        {
            "samples": samples,
            "repetitions": repetitions,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "hardware": {
                "rpm": COM_PORT,
                "rotation": ROTATION_COM_PORT,
                "z_linear": LINEAR_COM_PORT,
                "x_horizontal": HORIZONTAL_COM_PORT,
            },
        },
    )
    append_log(run_dir, f"Automation started. Repetitions={repetitions}, enabled samples={len(samples)}.")

    try:
        first_sample_index = samples[0]["sample_index"]

        for repetition in range(1, repetitions + 1):
            set_automation_state(step=f"Preparing repetition {repetition}/{repetitions}")
            append_log(run_dir, f"Preparing repetition {repetition}/{repetitions}.")

            current_horizontal_offset = horizontal_offset_for_sample(first_sample_index)
            if current_horizontal_offset != 0:
                move_horizontal_steps(current_horizontal_offset, abort_event=automation_abort_event)
            sleep_interruptible(MOVE_SETTLE_SECONDS, abort_event=automation_abort_event)
            move_linear_steps(SAMPLE_Z_DOWN_STEPS, abort_event=automation_abort_event)

            for i, sample in enumerate(samples):
                sample_num = int(sample["sample_index"])
                rpm = int(sample["rpm"])
                stabilization_seconds = float(sample.get("stabilization_seconds", 0))
                protocol_name = str(sample.get("protocol") or "ocp_only")
                sample_label = f"Rep {repetition}/{repetitions} - Sample {sample_num}"

                set_automation_state(step=f"{sample_label}: start RPM and stabilize")
                append_log(
                    run_dir,
                    f"{sample_label}: protocol={protocol_name}, rpm={rpm}, stabilization={stabilization_seconds}s.",
                )

                if rpm > 0:
                    start_rpm_hold(rpm)
                else:
                    append_log(run_dir, f"{sample_label}: RPM skipped because rpm <= 0.")

                try:
                    sleep_interruptible(stabilization_seconds, abort_event=automation_abort_event)
                    run_protocol_for_sample(
                        run_dir=run_dir,
                        sample_index=sample_num,
                        protocol_name=protocol_name,
                        sample_label=sample_label,
                    )
                finally:
                    if rpm > 0:
                        stop_rpm_hold(None)

                if SPIN_DOWN_SECONDS > 0:
                    set_automation_state(step=f"{sample_label}: spin-down wait")
                    sleep_interruptible(SPIN_DOWN_SECONDS, abort_event=automation_abort_event)

                if i < len(samples) - 1:
                    next_sample_index = int(samples[i + 1]["sample_index"])
                    set_automation_state(step=f"Transition to sample {next_sample_index}")
                    move_linear_steps(-SAMPLE_Z_DOWN_STEPS, abort_event=automation_abort_event)
                    sleep_interruptible(MOVE_SETTLE_SECONDS, abort_event=automation_abort_event)

                    next_horizontal_offset = horizontal_offset_for_sample(next_sample_index)
                    horizontal_delta = next_horizontal_offset - current_horizontal_offset
                    if horizontal_delta != 0:
                        move_horizontal_steps(horizontal_delta, abort_event=automation_abort_event)
                    current_horizontal_offset = next_horizontal_offset

                    sleep_interruptible(MOVE_SETTLE_SECONDS, abort_event=automation_abort_event)
                    move_linear_steps(SAMPLE_Z_DOWN_STEPS, abort_event=automation_abort_event)

            set_automation_state(step=f"Rep {repetition}/{repetitions}: final Z return")
            move_linear_steps(-SAMPLE_Z_DOWN_STEPS, abort_event=automation_abort_event)

        set_automation_state(step="Homing axes")
        home_axes_internal(abort_event=automation_abort_event)
        append_log(run_dir, "Automation complete.")
        save_json(run_dir / "manifest.json", {"ok": True, "completed_at": datetime.now(timezone.utc).isoformat()})
        set_automation_state(running=False, step="Automation complete", error=None)

    except AutomationAbortRequested:
        append_log(run_dir, "Automation abort requested.")
        try:
            set_automation_state(step="Abort requested: stopping motor")
            stop_rpm_hold(None)
        except Exception as exc:
            append_log(run_dir, f"RDE stop during abort failed: {exc}")

        try:
            set_automation_state(step="Abort requested: homing axes")
            home_axes_internal()
            append_log(run_dir, "Automation aborted and homed.")
            save_json(run_dir / "manifest.json", {"ok": False, "aborted": True, "completed_at": datetime.now(timezone.utc).isoformat()})
            set_automation_state(running=False, step="Automation aborted and homed", error=None)
        except Exception as exc:
            append_log(run_dir, f"Abort cleanup failed: {exc}")
            save_json(run_dir / "manifest.json", {"ok": False, "error": str(exc)})
            set_automation_state(running=False, step="Automation abort failed", error=str(exc))

    except Exception as exc:
        append_log(run_dir, f"Automation failed: {exc}")
        try:
            stop_rpm_hold(str(exc))
        except Exception as stop_exc:
            append_log(run_dir, f"RDE stop after failure failed: {stop_exc}")

        save_json(run_dir / "manifest.json", {"ok": False, "error": str(exc)})
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
    )


@app.get("/api/protocols")
def protocols_list():
    return jsonify({"ok": True, "protocols": list_protocols()})


@app.get("/api/status")
def status():
    with state_lock:
        with axis_position_lock:
            linear_pos = axis_positions["linear"]
            horizontal_pos = axis_positions["horizontal"]
        with automation_lock:
            automation_running = automation_state["running"]
            automation_step = automation_state["current_step"]
            automation_error = automation_state["last_error"]
            automation_run_dir = automation_state["run_dir"]

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
                "linear_position": linear_pos,
                "horizontal_position": horizontal_pos,
                "automation_running": automation_running,
                "automation_step": automation_step,
                "automation_error": automation_error,
                "automation_run_dir": automation_run_dir,
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
        return jsonify({"error": f"Unable to send '{command}' to {ROTATION_COM_PORT}: {exc}."}), 500

    return jsonify({"ok": True, "command": command, "com_port": ROTATION_COM_PORT, "ack": ack})


@app.post("/api/linear/send")
def linear_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual linear/Z commands are disabled."}), 409

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
        return jsonify({"error": f"Unable to send '{command}' to {LINEAR_COM_PORT}: {exc}."}), 500

    return jsonify({"ok": True, "command": command, "com_port": LINEAR_COM_PORT, "ack": ack})


@app.post("/api/horizontal/send")
def horizontal_send():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; manual horizontal/X commands are disabled."}), 409

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
        return jsonify({"error": f"Unable to send '{command}' to {HORIZONTAL_COM_PORT}: {exc}."}), 500

    return jsonify({"ok": True, "command": command, "com_port": HORIZONTAL_COM_PORT, "ack": ack})


@app.post("/api/axes/home")
def axes_home():
    with automation_lock:
        if automation_state["running"]:
            return jsonify({"error": "automation is running; home is disabled."}), 409

    try:
        result = home_axes_internal()
    except Exception as exc:
        return jsonify({"error": f"Unable to return axes to home position: {exc}."}), 500

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
                "run_dir": automation_state["run_dir"],
            }
        )


@app.post("/api/automation/start")
def automation_start():
    payload = request.get_json(silent=True) or {}
    samples = payload.get("samples")
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
            stabilization_seconds = float(sample.get("stabilization_seconds", sample.get("duration_seconds", 0)))
        except (TypeError, ValueError):
            return jsonify({"error": f"sample {idx}: rpm and stabilization_seconds must be numbers."}), 400

        if rpm != 0 and (rpm < RPM_MIN or rpm > RPM_MAX):
            return jsonify({"error": f"sample {idx}: rpm must be 0 or between {RPM_MIN} and {RPM_MAX}."}), 400

        if stabilization_seconds < 0:
            return jsonify({"error": f"sample {idx}: stabilization_seconds cannot be negative."}), 400

        protocol_name = str(sample.get("protocol") or "").strip()
        if not protocol_name:
            return jsonify({"error": f"sample {idx}: select an EChem protocol."}), 400

        try:
            load_protocol(protocol_name)
        except Exception as exc:
            return jsonify({"error": f"sample {idx}: unable to load protocol '{protocol_name}': {exc}"}), 400

        parsed_samples.append(
            {
                "sample_index": idx,
                "rpm": rpm,
                "stabilization_seconds": stabilization_seconds,
                "protocol": protocol_name,
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
        automation_state["run_dir"] = None
        automation_abort_event.clear()

    worker = threading.Thread(
        target=automation_worker,
        args=(parsed_samples, repetitions),
        daemon=True,
    )
    worker.start()

    return jsonify(
        {
            "ok": True,
            "selected_samples": [s["sample_index"] for s in parsed_samples],
            "protocols": [s["protocol"] for s in parsed_samples],
            "spin_down_seconds": SPIN_DOWN_SECONDS,
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
    import os
    port = int(os.environ.get("PORT", "5055"))
    app.run(host="127.0.0.1", port=port, debug=False)
