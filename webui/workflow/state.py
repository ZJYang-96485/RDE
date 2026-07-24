from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class AutomationAbortRequested(RuntimeError):
    pass


class AxisPositionStateError(RuntimeError):
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

axis_position_confidence = {
    "linear": "tracked",
    "horizontal": "tracked",
    "vertical": "tracked",
}

WEBUI_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AXIS_POSITION_STATE_PATH = (
    WEBUI_ROOT / "output" / "axis_position_state.json"
)
_AXES = ("linear", "horizontal", "vertical")
_POSITION_CONFIDENCE_VALUES = {"tracked", "uncertain"}
_axis_position_state_path: Path | None = None

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


def get_axis_position_confidence(axis: str) -> str:
    axis = str(axis).strip().lower()

    with axis_position_lock:
        if axis not in axis_position_confidence:
            raise ValueError(f"unknown axis: {axis}")

        return str(axis_position_confidence[axis])


def get_axis_position_confidences() -> dict[str, str]:
    with axis_position_lock:
        return copy.deepcopy(axis_position_confidence)


def _axis_position_payload_locked() -> dict[str, Any]:
    return {
        "version": 1,
        "positions": {
            axis: int(axis_positions[axis])
            for axis in _AXES
        },
        "confidence": {
            axis: str(axis_position_confidence[axis])
            for axis in _AXES
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_axis_position_state_locked() -> None:
    if _axis_position_state_path is None:
        return

    state_path = _axis_position_state_path
    temp_path = state_path.with_suffix(".json.tmp")

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(
                _axis_position_payload_locked(),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temp_path.replace(state_path)
    except OSError as exc:
        raise AxisPositionStateError(
            f"Unable to persist tracked axis positions to {state_path}: {exc}"
        ) from exc


def _read_axis_position_state(
    state_path: Path,
) -> tuple[dict[str, int], dict[str, str]] | None:
    if not state_path.is_file():
        return None

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AxisPositionStateError(
            f"Unable to read tracked axis positions from {state_path}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise AxisPositionStateError(
            f"Tracked axis-position state in {state_path} must be a JSON object."
        )

    raw_positions = payload.get("positions")
    raw_confidence = payload.get("confidence")
    if not isinstance(raw_positions, dict) or not isinstance(
        raw_confidence,
        dict,
    ):
        raise AxisPositionStateError(
            f"Tracked axis-position state in {state_path} is incomplete."
        )

    positions: dict[str, int] = {}
    confidence: dict[str, str] = {}
    for axis in _AXES:
        try:
            positions[axis] = int(raw_positions[axis])
        except (KeyError, TypeError, ValueError) as exc:
            raise AxisPositionStateError(
                f"Tracked axis-position state in {state_path} has an invalid "
                f"{axis} position."
            ) from exc

        axis_confidence = str(raw_confidence.get(axis, "")).strip().lower()
        if axis_confidence not in _POSITION_CONFIDENCE_VALUES:
            raise AxisPositionStateError(
                f"Tracked axis-position state in {state_path} has an invalid "
                f"{axis} confidence value."
            )
        confidence[axis] = axis_confidence

    return positions, confidence


def enable_axis_position_persistence(
    state_path: str | Path | None = None,
) -> Path:
    """
    Load tracked coordinates from disk and persist every later change.

    The production launchers call this explicitly. Importing the module alone
    does not touch disk, which keeps unit tests and one-off scripts isolated
    from the live station's coordinate record.
    """
    global _axis_position_state_path

    resolved_path = Path(
        state_path or DEFAULT_AXIS_POSITION_STATE_PATH
    ).resolve()
    saved_state = _read_axis_position_state(resolved_path)

    with axis_position_lock:
        _axis_position_state_path = resolved_path
        if saved_state is None:
            _write_axis_position_state_locked()
        else:
            saved_positions, saved_confidence = saved_state
            axis_positions.update(saved_positions)
            axis_position_confidence.update(saved_confidence)

    return resolved_path


def disable_axis_position_persistence() -> Path | None:
    """Disable disk persistence and return the previously configured path."""
    global _axis_position_state_path

    with axis_position_lock:
        previous_path = _axis_position_state_path
        _axis_position_state_path = None
        return previous_path


def mark_axis_positions_uncertain(axes: list[str] | tuple[str, ...]) -> None:
    with axis_position_lock:
        for raw_axis in axes:
            axis = str(raw_axis).strip().lower()
            if axis not in axis_position_confidence:
                raise ValueError(f"unknown axis: {axis}")
            axis_position_confidence[axis] = "uncertain"
        _write_axis_position_state_locked()


def set_axis_position(axis: str, position: int) -> None:
    axis = str(axis).strip().lower()

    with axis_position_lock:
        if axis not in axis_positions:
            raise ValueError(f"unknown axis: {axis}")

        axis_positions[axis] = int(position)
        axis_position_confidence[axis] = "tracked"
        _write_axis_position_state_locked()


def add_axis_delta(axis: str, delta: int) -> int:
    axis = str(axis).strip().lower()

    with axis_position_lock:
        if axis not in axis_positions:
            raise ValueError(f"unknown axis: {axis}")

        axis_positions[axis] = int(axis_positions[axis]) + int(delta)
        _write_axis_position_state_locked()
        return int(axis_positions[axis])


def reset_axis_positions() -> None:
    with axis_position_lock:
        axis_positions["linear"] = 0
        axis_positions["horizontal"] = 0
        axis_positions["vertical"] = 0
        axis_position_confidence["linear"] = "tracked"
        axis_position_confidence["horizontal"] = "tracked"
        axis_position_confidence["vertical"] = "tracked"
        _write_axis_position_state_locked()


def set_axis_positions(positions: dict[str, Any]) -> None:
    with axis_position_lock:
        for axis in ["linear", "horizontal", "vertical"]:
            if axis in positions:
                axis_positions[axis] = int(positions[axis])
                axis_position_confidence[axis] = "tracked"
        _write_axis_position_state_locked()


def start_automation(run_dir: str | None = None) -> None:
    with automation_lock:
        automation_state["running"] = True
        automation_state["step"] = "Starting automation"
        automation_state["error"] = None
        automation_state["run_dir"] = run_dir
        automation_state["started_at"] = iso_utc()
        automation_state["finished_at"] = None


def reserve_automation(step: str = "Queued automation") -> bool:
    with automation_lock:
        if bool(automation_state["running"]):
            return False

        automation_state["running"] = True
        automation_state["step"] = str(step)
        automation_state["error"] = None
        automation_state["run_dir"] = None
        automation_state["started_at"] = iso_utc()
        automation_state["finished_at"] = None
        return True


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
    axis_confidence = get_axis_position_confidences()
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
        "linear_position_confidence": axis_confidence["linear"],
        "horizontal_position_confidence": axis_confidence["horizontal"],
        "vertical_position_confidence": axis_confidence["vertical"],
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
