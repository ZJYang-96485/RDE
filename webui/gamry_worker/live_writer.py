"""Small, file-based live acquisition stream used by the web UI.

The stream is deliberately separate from the final DTA output.  A worker may
append points while Flask is reading them, so status files are replaced
atomically and JSONL readers ignore a partial final line.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_stream_locks: dict[str, threading.RLock] = {}
_stream_locks_guard = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def live_path(live_dir: str | Path, filename: str) -> Path:
    return Path(live_dir) / filename


def _lock_for(live_dir: str | Path) -> threading.RLock:
    key = str(Path(live_dir).resolve())
    with _stream_locks_guard:
        return _stream_locks.setdefault(key, threading.RLock())


def _write_status_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _default_status() -> dict[str, Any]:
    return {
        "active": False,
        "run_id": None,
        "sample_id": None,
        "sample_label": None,
        "protocol_name": None,
        "step_name": None,
        "technique": None,
        "started_at": None,
        "finished_at": None,
        "last_update_at": None,
        "point_count": 0,
        "status": "idle",
        "error": None,
    }


def read_live_status(live_dir: str | Path) -> dict[str, Any] | None:
    path = live_path(live_dir, "status.json")
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    if not isinstance(payload, dict):
        return None

    status = _default_status()
    status.update(payload)
    return status


def initialize_live_stream(
    live_dir: str | Path,
    *,
    run_id: str | None = None,
    sample_id: str | None = None,
    sample_label: str | None = None,
    protocol_name: str | None = None,
    step_name: str | None = None,
    technique: str | None = None,
) -> dict[str, Any]:
    """Reset the temporary stream for one EChem step and mark it running."""

    directory = Path(live_dir)
    points_path = live_path(directory, "points.jsonl")
    status_path = live_path(directory, "status.json")
    now = utc_now()
    status = _default_status()
    status.update(
        {
            "active": True,
            "run_id": run_id,
            "sample_id": sample_id,
            "sample_label": sample_label,
            "protocol_name": protocol_name,
            "step_name": step_name,
            "technique": technique,
            "started_at": now,
            "status": "running",
            "last_update_at": now,
        }
    )

    with _lock_for(directory):
        directory.mkdir(parents=True, exist_ok=True)
        # Reset only the temporary stream. Final DTA files are elsewhere.
        with points_path.open("w", encoding="utf-8"):
            pass
        _write_status_atomic(status_path, status)

    return status


def update_live_status(live_dir: str | Path, **updates: Any) -> dict[str, Any]:
    directory = Path(live_dir)
    with _lock_for(directory):
        current = read_live_status(directory) or _default_status()
        current.update(updates)
        current["last_update_at"] = utc_now()
        _write_status_atomic(live_path(directory, "status.json"), current)
        return current


def append_live_points(
    live_dir: str | Path,
    points: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append complete JSON objects and assign monotonically increasing seqs."""

    directory = Path(live_dir)
    normalized = [dict(point) for point in points]
    if not normalized:
        return []

    with _lock_for(directory):
        status = read_live_status(directory) or _default_status()
        next_seq = int(status.get("point_count", 0) or 0) + 1
        written: list[dict[str, Any]] = []
        directory.mkdir(parents=True, exist_ok=True)

        with live_path(directory, "points.jsonl").open("a", encoding="utf-8") as stream:
            for point in normalized:
                technique = str(point.get("technique", "") or "").strip().lower()
                if not technique:
                    raise ValueError("every live point needs a technique")
                point["seq"] = next_seq
                point["timestamp_utc"] = str(point.get("timestamp_utc") or utc_now())
                stream.write(json.dumps(point, separators=(",", ":"), allow_nan=False) + "\n")
                written.append(point)
                next_seq += 1
            stream.flush()

        status["point_count"] = next_seq - 1
        status["last_update_at"] = utc_now()
        _write_status_atomic(live_path(directory, "status.json"), status)

    return written


def append_live_point(live_dir: str | Path, point: dict[str, Any]) -> dict[str, Any]:
    written = append_live_points(live_dir, [point])
    return written[0]


def finish_live_stream(live_dir: str | Path) -> dict[str, Any]:
    return update_live_status(
        live_dir,
        active=False,
        status="complete",
        finished_at=utc_now(),
        error=None,
    )


def fail_live_stream(
    live_dir: str | Path,
    error: str,
    *,
    status: str = "error",
) -> dict[str, Any]:
    return update_live_status(
        live_dir,
        active=False,
        status=status,
        finished_at=utc_now(),
        error=str(error),
    )


def clear_live_stream(live_dir: str | Path) -> None:
    """Delete only temporary live files; never touches final experiment data."""

    directory = Path(live_dir)
    with _lock_for(directory):
        for filename in ("status.json", "points.jsonl"):
            try:
                live_path(directory, filename).unlink()
            except FileNotFoundError:
                pass


def read_live_points(
    live_dir: str | Path,
    *,
    after: int = 0,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Read a bounded page of valid points, skipping a torn final line."""

    if after < 0:
        raise ValueError("after must be >= 0")
    if limit <= 0:
        raise ValueError("limit must be > 0")

    points_path = live_path(live_dir, "points.jsonl")
    points: list[dict[str, Any]] = []
    try:
        with points_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if len(points) >= limit:
                    break
                try:
                    point = json.loads(line)
                except json.JSONDecodeError:
                    # A worker may have been interrupted between write calls.
                    continue
                if not isinstance(point, dict):
                    continue
                try:
                    sequence = int(point.get("seq"))
                except (TypeError, ValueError):
                    continue
                if sequence > after:
                    points.append(point)
    except (FileNotFoundError, OSError):
        return []

    return points
