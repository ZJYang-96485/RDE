from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Any


class AutomationAbortRequested(RuntimeError):
    pass


state_lock = threading.RLock()
axis_position_lock = threading.RLock()
automation_lock = threading.RLock()

abort_event = threading.Event()

rde_state = {
    "running": False,
    "target_rpm": None,
    "duration_seconds": None,
    "started_at": None,
    "ends_at": None,
    "last_error": None
}

axis_positions = {
    "linear": 0,
    "horizontal": 0,
    "vertical": 0
}

automation_state = {
    "running": False,
    "step": "Idle",
    "error": None,
    "run_dir": None,
    "started_at": None,
    "finished_at": None
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    if dt is None:
        dt = utc_now()

    return dt.isoformat()


def start_rde_run(rpm: int, duration_seconds: int | float) -> None:
    started = utc_now()
    duration = float(duration_seconds)
    ends = started + timedelta(seconds=duration)

    with state_lock:
        rde_state["running"] = True
        rde_state["target_rpm"] = int(rpm)
        rde_state["duration_seconds"] = duration
        rde_state["started_at"] = iso_utc(started)
        rde_state["ends_at"] = iso_utc(ends)
        rde_state["last_error"] = None


def stop_rde_run(error: str | None = None) -> None:
    with state_lock:
        rde_state["running"] = False
        rde_state["target_rpm"] = None
        rde_state["duration_seconds"] = None
        rde_state["started_at"] = None
        rde_state["ends_at"] = None

        if error:
            rde_state["last_error"] = str(error)


def set_rde_error(error: str | None) -> None:
    with state_lock:
        rde_state["last_error"] = str(error) if error else None


def get_rde_state() -> dict[str, Any]:
    with state_lock:
        return copy.deepcopy(rde_state)


def get_axis_positions() -> dict[str, int]:
    with axis_position_lock:
        return copy.deepcopy(axis_positions)


def get_axis_position(axis: str) -> int:
    axis = str(axis).strip().lower()

    with axis_position_lock:
        if axis not in axis_positions:
            raise ValueError(f"unknown axis: {axis}")

        return int(axis_positions[axis])


def set_axis_position(axis: str, position: int) -> None:
    axis = str(axis).strip().lower()

    with axis_position_lock:
        if axis not in axis_positions:
            raise ValueError(f"unknown axis: {axis}")

        axis_positions[axis] = int(position)


def add_axis_delta(axis: str, delta: int) -> int:
    axis = str(axis).strip().lower()

    with axis_position_lock:
        if axis not in axis_positions:
            raise ValueError(f"unknown axis: {axis}")

        axis_positions[axis] = int(axis_positions[axis]) + int(delta)
        return int(axis_positions[axis])


def reset_axis_positions() -> None:
    with axis_position_lock:
        axis_positions["linear"] = 0
        axis_positions["horizontal"] = 0
        axis_positions["vertical"] = 0


def set_axis_positions(positions: dict[str, Any]) -> None:
    with axis_position_lock:
        for axis in ["linear", "horizontal", "vertical"]:
            if axis in positions:
                axis_positions[axis] = int(positions[axis])


def start_automation(run_dir: str | None = None) -> None:
    with automation_lock:
        automation_state["running"] = True
        automation_state["step"] = "Starting automation"
        automation_state["error"] = None
        automation_state["run_dir"] = run_dir
        automation_state["started_at"] = iso_utc()
        automation_state["finished_at"] = None


def set_automation_state(
    step: str | None = None,
    error: str | None = None,
    run_dir: str | None = None,
) -> None:
    with automation_lock:
        if step is not None:
            automation_state["step"] = str(step)

        if error is not None:
            automation_state["error"] = str(error)

        if run_dir is not None:
            automation_state["run_dir"] = str(run_dir)


def finish_automation(step: str = "Automation complete") -> None:
    with automation_lock:
        automation_state["running"] = False
        automation_state["step"] = str(step)
        automation_state["finished_at"] = iso_utc()


def fail_automation(error: str, step: str = "Automation failed") -> None:
    with automation_lock:
        automation_state["running"] = False
        automation_state["step"] = str(step)
        automation_state["error"] = str(error)
        automation_state["finished_at"] = iso_utc()


def get_automation_state() -> dict[str, Any]:
    with automation_lock:
        return copy.deepcopy(automation_state)


def automation_is_running() -> bool:
    with automation_lock:
        return bool(automation_state["running"])


def request_abort() -> None:
    abort_event.set()


def clear_abort() -> None:
    abort_event.clear()


def abort_requested() -> bool:
    return abort_event.is_set()


def get_abort_event() -> threading.Event:
    return abort_event


def check_abort(message: str = "Automation abort requested.") -> None:
    if abort_event.is_set():
        raise AutomationAbortRequested(message)


def get_status_payload(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    rde = get_rde_state()
    axes = get_axis_positions()
    automation = get_automation_state()

    payload = {
        "running": rde["running"],
        "target_rpm": rde["target_rpm"],
        "duration_seconds": rde["duration_seconds"],
        "started_at": rde["started_at"],
        "ends_at": rde["ends_at"],
        "last_error": rde["last_error"],
        "linear_position": axes["linear"],
        "horizontal_position": axes["horizontal"],
        "vertical_position": axes["vertical"],
        "automation_running": automation["running"],
        "automation_step": automation["step"],
        "automation_error": automation["error"],
        "automation_run_dir": automation["run_dir"],
        "automation_started_at": automation["started_at"],
        "automation_finished_at": automation["finished_at"],
        "abort_requested": abort_requested()
    }

    if extra:
        payload.update(extra)

    return payload