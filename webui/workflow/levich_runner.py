from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from gamry_worker.live_writer import read_live_points, read_live_status
from workflow.config_loader import get_gamry_config
from workflow.data_manager import append_log, register_analysis_result
from workflow.levich_analysis import run_levich_analysis


class LevichRunnerError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def progress_path(live_dir: str | Path) -> Path:
    return Path(live_dir) / "levich_progress.json"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        delay = 0.01
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                retryable = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
                if not retryable or attempt == 7:
                    raise
                time.sleep(delay)
                delay = min(0.2, delay * 2)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_levich_progress(live_dir: str | Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(progress_path(live_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def update_progress(live_dir: Path, base: dict[str, Any], **updates: Any) -> dict[str, Any]:
    payload = dict(base)
    current = read_levich_progress(live_dir)
    if current:
        payload.update(current)
    payload.update(updates)
    payload["updated_at"] = utc_now()
    write_json_atomic(progress_path(live_dir), payload)
    return payload


def schedule_path_for(raw_dta: str | Path) -> Path:
    raw = Path(raw_dta)
    return raw.with_name(f"{raw.stem}_rpm_schedule.json")


def mock_time_scale() -> float:
    config = get_gamry_config()
    if str(config.get("mode", "mock")).strip().lower() != "mock":
        return 1.0
    live = config.get("live_plot", {})
    if not isinstance(live, dict):
        return 0.05
    return max(0.0, float(live.get("mock_time_scale", 0.05)))


def run_levich_rpm_sweep_ca(
    *,
    step: dict[str, Any],
    raw_dta: str | Path,
    run_dir: str | Path,
    sample_id: str | None,
    sample_label: str,
    protocol_name: str,
    sleep_fn: Callable[[float, str], None],
    check_abort_fn: Callable[[str], None],
    run_gamry_step_fn: Callable[..., dict[str, Any]] | None = None,
    send_rpm_fn: Callable[[int], Any] | None = None,
    stop_rde_fn: Callable[[str | None], Any] | None = None,
) -> dict[str, Any]:
    if run_gamry_step_fn is None:
        from hardware.gamry_client import run_gamry_step as run_gamry_step_fn
    if send_rpm_fn is None or stop_rde_fn is None:
        from hardware.rde_controller import send_rpm, stop_rde

        send_rpm_fn = send_rpm_fn or send_rpm
        stop_rde_fn = stop_rde_fn or stop_rde

    run_path = Path(run_dir)
    raw_path = Path(raw_dta)
    live_dir = run_path / "_system" / "live"
    rpms = [int(value) for value in step["rpm_values"]]
    pre_stabilization_s = float(step["pre_stabilization_s"])
    stabilization_s = float(step["stabilization_s"])
    collection_s = float(step["collection_s"])
    scale = mock_time_scale()
    result_box: dict[str, Any] = {}
    worker_done = threading.Event()
    base_progress = {
        "run_id": run_path.name,
        "technique": "levich_rpm_sweep_ca",
        "display_label": "Live CA trace for Levich RPM sweep",
        "sample_id": sample_id,
        "sample_label": sample_label,
        "protocol_name": protocol_name,
        "step_name": str(step.get("name") or "Levich CA RPM Sweep"),
        "rpm_source": "commanded",
        "stabilization_mode": "fixed delay",
        "active": True,
        "status": "running",
    }
    completed = False
    rde_stopped = False

    def report_progress(**updates: Any) -> dict[str, Any] | None:
        try:
            return update_progress(live_dir, base_progress, **updates)
        except Exception as exc:
            try:
                append_log(
                    run_path,
                    f"Levich sweep: live progress update failed; acquisition continues: {exc}.",
                )
            except Exception:
                pass
            return None

    def scaled_sleep(seconds: float, message: str) -> None:
        sleep_fn(float(seconds) * scale, message)

    def worker_target() -> None:
        try:
            result_box["result"] = run_gamry_step_fn(
                step=step,
                outputs=[str(raw_path)],
                run_dir=run_path,
                sample_id=sample_id,
                sample_label=sample_label,
                protocol_name=protocol_name,
            )
        except BaseException as exc:
            result_box["error"] = exc
        finally:
            worker_done.set()

    first_commanded_at_dt = datetime.now(timezone.utc)
    first_commanded_at = first_commanded_at_dt.isoformat()
    first_acknowledged_at_dt: datetime | None = None
    try:
        report_progress(
            phase="pre-stabilizing",
            commanded_rpm=rpms[0],
            point_index=1,
            point_count=len(rpms),
        )
        send_rpm_fn(rpms[0])
        first_acknowledged_at_dt = datetime.now(timezone.utc)
        append_log(run_path, f"Levich sweep: commanded {rpms[0]} RPM; fixed pre-stabilization started.")
        scaled_sleep(pre_stabilization_s, "Abort requested during Levich RPM pre-stabilization.")
        check_abort_fn("Abort requested before continuous Levich CA.")

        prior_live_status = read_live_status(live_dir) or {}
        prior_stream_started_at = str(prior_live_status.get("started_at") or "")
        worker = threading.Thread(target=worker_target, daemon=True)
        worker.start()

        acquisition_started_at: str | None = None
        start_deadline = time.monotonic() + max(
            30.0,
            min(120.0, float(step.get("acquisition_start_timeout_s", 60))),
        )
        while time.monotonic() < start_deadline:
            check_abort_fn("Abort requested while starting continuous Levich CA.")
            status = read_live_status(live_dir) or {}
            stream_started_at = str(status.get("started_at") or "")
            current_acquisition_started_at = (
                str(status.get("acquisition_started_at") or "").strip() or None
            )
            is_current_stream = (
                str(status.get("technique") or "").strip().lower()
                == "levich_rpm_sweep_ca"
                and bool(stream_started_at)
                and stream_started_at != prior_stream_started_at
            )
            if is_current_stream and current_acquisition_started_at:
                acquisition_started_at = current_acquisition_started_at
                break
            if is_current_stream and int(status.get("point_count", 0) or 0) > 0:
                first_points = read_live_points(live_dir, after=0, limit=1)
                if first_points:
                    first_point = first_points[0]
                    try:
                        first_written_at = datetime.fromisoformat(
                            str(first_point["timestamp_utc"]).replace("Z", "+00:00")
                        )
                        if first_written_at.tzinfo is None:
                            first_written_at = first_written_at.replace(tzinfo=timezone.utc)
                        first_elapsed_s = float(first_point.get("t_s", 0) or 0)
                        acquisition_started_at = (
                            first_written_at - timedelta(seconds=max(0.0, first_elapsed_s))
                        ).isoformat()
                        break
                    except (KeyError, TypeError, ValueError):
                        pass
            if worker_done.is_set():
                error = result_box.get("error")
                if error:
                    raise LevichRunnerError(f"Continuous Levich CA failed to start: {error}")
                worker_result = result_box.get("result", {})
                trial_metadata = (
                    worker_result.get("trial_metadata", {})
                    if isinstance(worker_result, dict)
                    else {}
                )
                if str(trial_metadata.get("trial_status", "")).lower() == "skipped":
                    stop_rde_fn(None)
                    rde_stopped = True
                    report_progress(active=False, status="skipped", phase="finished")
                    append_log(run_path, f"Levich sweep bypassed: {trial_metadata.get('skip_reason')}.")
                    return {
                        "ok": True,
                        "skipped": True,
                        "technique": "levich_rpm_sweep_ca",
                        "gamry": worker_result,
                    }
                raise LevichRunnerError("Continuous Levich CA finished before its acquisition start was recorded.")
            time.sleep(0.05)
        if not acquisition_started_at:
            raise LevichRunnerError("Timed out waiting for the continuous Levich CA acquisition to start.")

        try:
            acquisition_origin = datetime.fromisoformat(acquisition_started_at.replace("Z", "+00:00"))
            if acquisition_origin.tzinfo is None:
                acquisition_origin = acquisition_origin.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise LevichRunnerError("Continuous Levich CA reported an invalid start time.") from exc
    except BaseException:
        try:
            stop_rde_fn(None)
            rde_stopped = True
            append_log(run_path, "Levich sweep: RDE stopped during startup cleanup.")
        finally:
            try:
                report_progress(
                    active=False,
                    status="stopped",
                    phase="stopped",
                )
            except Exception as progress_exc:
                append_log(run_path, f"Levich sweep: progress cleanup failed: {progress_exc}.")
        raise

    def logical_elapsed() -> float:
        actual = max(0.0, (datetime.now(timezone.utc) - acquisition_origin).total_seconds())
        return actual / scale if 0 < scale < 1 else actual

    rpm_points: list[dict[str, Any]] = []
    try:
        for index, rpm in enumerate(rpms, start=1):
            check_abort_fn("Abort requested during Levich RPM sweep.")
            if index == 1:
                commanded_at_s = (
                    first_commanded_at_dt - acquisition_origin
                ).total_seconds()
                command_acknowledged_at_s = (
                    (first_acknowledged_at_dt or first_commanded_at_dt)
                    - acquisition_origin
                ).total_seconds()
            else:
                report_progress(
                    phase="changing RPM",
                    commanded_rpm=rpm,
                    point_index=index,
                    point_count=len(rpms),
                )
                commanded_at_s = logical_elapsed()
                send_rpm_fn(rpm)
                command_acknowledged_at_s = logical_elapsed()
                append_log(run_path, f"Levich sweep: commanded {rpm} RPM.")
                report_progress(
                    phase="pre-stabilizing",
                    commanded_rpm=rpm,
                    point_index=index,
                    point_count=len(rpms),
                )
                scaled_sleep(
                    stabilization_s,
                    "Abort requested during Levich fixed RPM stabilization.",
                )

            collection_start_s = logical_elapsed()
            report_progress(
                phase="collecting",
                commanded_rpm=rpm,
                point_index=index,
                point_count=len(rpms),
                collection_start_s=collection_start_s,
            )
            scaled_sleep(collection_s, "Abort requested during Levich CA collection window.")
            collection_end_s = logical_elapsed()
            rpm_points.append(
                {
                    "index": index,
                    "commanded_rpm": rpm,
                    "rpm_source": "commanded",
                    "commanded_at_s": commanded_at_s,
                    "command_acknowledged_at_s": command_acknowledged_at_s,
                    "fixed_stabilization_s": 0.0 if index == 1 else stabilization_s,
                    "collection_start_s": collection_start_s,
                    "collection_end_s": collection_end_s,
                }
            )
            if worker_done.is_set() and index < len(rpms):
                error = result_box.get("error")
                if error:
                    raise LevichRunnerError(f"Continuous Levich CA failed during the RPM sweep: {error}")
                raise LevichRunnerError("Continuous Levich CA ended before all RPM points were collected.")

        schedule_path = schedule_path_for(raw_path)
        schedule = {
            "technique": "levich_rpm_sweep_ca",
            "label": "Levich CA RPM Sweep",
            "created_at": utc_now(),
            "acquisition_started_at": acquisition_started_at,
            "initial_rpm_commanded_at": first_commanded_at,
            "initial_rpm_acknowledged_at": (
                first_acknowledged_at_dt.isoformat()
                if first_acknowledged_at_dt is not None
                else None
            ),
            "rpm_source": "commanded",
            "stabilization_mode": "fixed delay",
            "pre_stabilization_s": pre_stabilization_s,
            "fixed_stabilization_s": stabilization_s,
            "collection_s": collection_s,
            "raw_dta": raw_path.name,
            "rpm_points": rpm_points,
        }
        write_json_atomic(schedule_path, schedule)
        append_log(run_path, f"Levich sweep: commanded-RPM schedule saved at {schedule_path.name}.")

        while not worker_done.wait(0.1):
            check_abort_fn("Abort requested while finishing continuous Levich CA.")
        if result_box.get("error"):
            raise LevichRunnerError(f"Continuous Levich CA failed: {result_box['error']}")
        if not raw_path.is_file():
            raise LevichRunnerError("Continuous Levich CA finished without creating its DTA file.")

        stop_rde_fn(None)
        rde_stopped = True
        append_log(run_path, "Levich sweep: RDE stopped after the continuous CA sweep.")
        report_progress(phase="finished", commanded_rpm=rpms[-1])
        analysis_result = run_levich_analysis(
            raw_path,
            schedule_path,
            area_cm2=float(step.get("area_cm2", 1)),
        )
        registered = register_analysis_result(
            run_path,
            raw_dta=raw_path,
            analysis_artifacts=analysis_result["artifacts"],
            label="Levich CA RPM Sweep",
            technique="levich_rpm_sweep_ca",
            rpm_source="commanded",
            stabilization_mode="fixed delay",
        )
        append_log(run_path, "Levich sweep: post-run Levich and Koutecky-Levich analysis complete.")
        completed = True
        return {
            "ok": True,
            "technique": "levich_rpm_sweep_ca",
            "gamry": result_box.get("result"),
            "schedule": str(schedule_path),
            "analysis": analysis_result["analysis"],
            "registered_history_result": registered,
        }
    finally:
        if not rde_stopped:
            try:
                stop_rde_fn(None)
                append_log(run_path, "Levich sweep: RDE stopped during cleanup.")
            except Exception as exc:
                append_log(run_path, f"Levich sweep: RDE stop failed: {exc}.")
                raise
        try:
            report_progress(
                active=False,
                status="complete" if completed else "stopped",
                phase="finished" if completed else "stopped",
            )
        except Exception as progress_exc:
            append_log(run_path, f"Levich sweep: progress cleanup failed: {progress_exc}.")
