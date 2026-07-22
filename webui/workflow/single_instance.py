from __future__ import annotations

import atexit
import json
import os
import socket
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SingleInstanceError(RuntimeError):
    pass


_guard = threading.Lock()
_lock_handle: Any = None


def lock_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return base / "RDEAutomation" / "rde_webui_server.lock"


def reject_existing_webui_listener(port: int, host: str = "127.0.0.1") -> None:
    """Reject an older server that predates the process-lock implementation."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex((host, int(port))) == 0:
            raise SingleInstanceError(
                f"TCP port {int(port)} already has a listening server. Close the older "
                "RDE Web UI process before starting another one; it may retain the "
                "configured COM ports."
            )


def _lock_first_byte(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)

    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_first_byte(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _owner_text(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(1)
            payload = json.loads(stream.read().decode("utf-8"))
        if isinstance(payload, dict):
            pid = payload.get("pid")
            started_at = payload.get("started_at")
            if pid:
                return f" Existing lock owner: PID {pid}, started {started_at or 'time unknown'}."
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return ""


def acquire_webui_instance_lock() -> Any:
    """Hold one process-wide lock so duplicate servers cannot compete for COM ports."""

    global _lock_handle
    with _guard:
        if _lock_handle is not None:
            return _lock_handle

        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            _lock_first_byte(handle)
        except OSError as exc:
            handle.close()
            raise SingleInstanceError(
                "Another RDE Web UI server is already running. Do not start app.py and "
                "start_rde_automation.bat at the same time; the older process may hold "
                f"the configured COM ports.{_owner_text(path)}"
            ) from exc

        metadata = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        handle.seek(1)
        handle.truncate()
        handle.write(json.dumps(metadata).encode("utf-8"))
        handle.flush()
        _lock_handle = handle
        return handle


def release_webui_instance_lock() -> None:
    global _lock_handle
    with _guard:
        handle = _lock_handle
        _lock_handle = None
        if handle is None:
            return
        try:
            _unlock_first_byte(handle)
        finally:
            handle.close()


atexit.register(release_webui_instance_lock)
