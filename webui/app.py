from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file

from analysis.ca_charge import CaChargeAnalysisError
from analysis.registry import load_analysis_plot
from hardware.motion_controller import (
    emergency_stop_motion,
    move_horizontal_steps,
    move_linear_steps,
    move_vertical_steps,
)
from hardware.gamry_client import GamryClientError, get_gamry_client
from hardware.gamry_cell_client import (
    GamryCellClientError,
    gamry_cell_off,
    gamry_cell_on,
    gamry_cell_status,
)
from hardware.rde_controller import send_rpm, stop_rde
from hardware.rotation_controller import (
    RotationControllerError,
    RotationMoveInterrupted,
    angle_to_steps,
    emergency_stop_rotation,
    get_rotation_controller,
    send_rotation_text,
)
from workflow.config_loader import (
    ConfigError,
    get_baud_rate,
    get_max_axis_command,
    get_live_plot_config,
    get_motion_config,
    get_rde_limits,
    get_rotation_config,
    get_safe_z,
    get_serial_port,
    load_config,
    set_gamry_mode,
    user_axis_to_internal_axis,
)
from workflow.protocol_loader import (
    ProtocolError,
    default_protocol_payload,
    delete_protocol,
    list_protocols,
    load_protocol,
    normalize_protocol_name,
    protocol_path_for_name,
    save_protocol,
    validate_protocol_payload,
)
from workflow.recipe_runner import RecipeRunnerError, abort_automation, run_plan_payload_background
from workflow.rinse_arm_oscillation import execute_rinse_arm_oscillation
from workflow.rinse_arm_paths import validate_rinse_arm_settings
from gamry_worker.live_writer import clear_live_stream, read_live_events, read_live_points, read_live_status
from workflow.dta_viewer import (
    DtaViewerError,
    list_dta_files,
    parse_dta_file,
    resolve_listed_dta_path,
)
from workflow.history_artifacts import (
    HistoryArtifactError,
    analysis_artifact_descriptor,
    list_analysis_groups,
    resolve_registered_artifact,
)
from workflow.levich_runner import read_levich_progress
from workflow.run_plan_loader import (
    RunPlanError,
    default_run_plan_payload,
    delete_run_plan,
    list_run_plans,
    load_run_plan,
    save_run_plan,
    validate_run_plan_payload,
)
from workflow.safety import (
    validate_axis_move,
    validate_axis_position,
    validate_duration_seconds,
    validate_rpm,
)
from workflow.state import (
    automation_is_running,
    get_rde_state,
    get_status_payload,
    get_automation_state,
    set_axis_position,
    start_rde_run,
)

# IMPORTANT: hardware ports remain configuration-driven through webui/config.json.
# Current station mapping: RDE RPM=COM6, rotation=COM3, Z/linear=COM4, X/horizontal=COM8.
# Do not replace this full application with a simplified serial-only app; this file preserves
# the Gamry backend, protocol builder, saved protocols, saved run plans, and automation runner.

app = Flask(__name__)

stop_timer: threading.Timer | None = None
manual_arm_motion_lock = threading.Lock()


def config_payload() -> dict[str, Any]:
    config = load_config()
    rde = get_rde_limits()
    motion = get_motion_config()
    gamry = config["gamry"]
    gamry_runtime = get_gamry_client().runtime_status()
    rotation = get_rotation_config()
    rinse_oscillation = rotation.get("rinse_oscillation", {})
    degrees_per_step = 360.0 / (
        int(rotation["motor_full_steps_per_rev"]) * int(rotation["microstep"])
    )

    return {
        "baud_rate": get_baud_rate(),
        "com_port": get_serial_port("rde"),
        "rotation_com_port": get_serial_port("rotation"),
        "linear_com_port": get_serial_port("linear"),
        "horizontal_com_port": get_serial_port("horizontal"),
        "vertical_com_port": get_serial_port("vertical"),
        "rpm_min": rde["rpm_min"],
        "rpm_max": rde["rpm_max"],
        "stop_rpm": rde["stop_rpm"],
        "safe_z": get_safe_z(),
        "max_axis_command": get_max_axis_command(),
        "axis_limits": motion["axis_limits"],
        "axis_mapping": motion["axis_mapping"],
        "rotation_arm": {
            "motor_full_steps_per_rev": int(rotation["motor_full_steps_per_rev"]),
            "microstep": int(rotation["microstep"]),
            "degrees_per_step": degrees_per_step,
            "max_relative_steps": int(rotation["max_relative_steps"]),
            "rinse_oscillation": {
                "enabled": bool(rinse_oscillation.get("enabled", False)),
                "amplitude_deg": float(rinse_oscillation.get("amplitude_deg", 5.0)),
                "cycles": int(rinse_oscillation.get("cycles", 3)),
                "pause_between_moves_s": float(
                    rinse_oscillation.get("pause_between_moves_s", 0.2)
                ),
                "return_to_start": True,
            },
        },
        "gamry_mode": gamry["mode"],
        "gamry_real_runner_configured": bool(gamry_runtime.get("configured", False)),
        "gamry_instrument_label": str(gamry.get("instrument_label", "") or ""),
        "gamry_runtime": gamry_runtime,
        "live_plot": get_live_plot_config(),
    }


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def auto_stop() -> None:
    global stop_timer

    try:
        stop_rde(None)
    finally:
        stop_timer = None


@app.get("/")
def index():
    cfg = config_payload()

    return render_template(
        "index.html",
        rpm_min=cfg["rpm_min"],
        rpm_max=cfg["rpm_max"],
        stop_rpm=cfg["stop_rpm"],
        com_port=cfg["com_port"],
        rotation_com_port=cfg["rotation_com_port"],
        linear_com_port=cfg["linear_com_port"],
        horizontal_com_port=cfg["horizontal_com_port"],
        vertical_com_port=cfg["vertical_com_port"],
    )


@app.get("/api/config")
def api_config():
    return jsonify({"ok": True, "config": config_payload()})


@app.post("/api/config/gamry-mode")
def api_config_gamry_mode():
    if automation_is_running():
        return json_error("automation is running; Gamry mode cannot be changed.", 409)

    payload = request.get_json(silent=True) or {}

    try:
        mode = str(payload.get("mode", "") or "").strip().lower()
        config = set_gamry_mode(mode)
    except ConfigError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:
        return json_error(f"Unable to update Gamry mode: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "gamry_mode": config["gamry"]["mode"],
            "config": config_payload(),
        }
    )


@app.post("/api/gamry/probe")
def api_gamry_probe():
    if automation_is_running():
        return json_error("automation is running; the Gamry device cannot be checked.", 409)

    try:
        probe = get_gamry_client().probe()
    except GamryClientError as exc:
        return json_error(str(exc), 503)
    except Exception as exc:
        return json_error(f"Unable to check the Gamry device: {exc}", 500)

    return jsonify({"ok": True, "probe": probe})


@app.get("/api/status")
def status():
    cfg = config_payload()
    payload = get_status_payload(
        {
            "com_port": cfg["com_port"],
            "rotation_com_port": cfg["rotation_com_port"],
            "linear_com_port": cfg["linear_com_port"],
            "horizontal_com_port": cfg["horizontal_com_port"],
            "vertical_com_port": cfg["vertical_com_port"],
            "axis_limits": cfg["axis_limits"],
            "axis_mapping": cfg["axis_mapping"],
            "safe_z": cfg["safe_z"],
            "stop_rpm": cfg["stop_rpm"],
            "gamry_mode": cfg["gamry_mode"],
            "gamry_real_runner_configured": cfg["gamry_real_runner_configured"],
        }
    )
    payload["rotation_arm_state"] = (
        get_rotation_controller().relative_diagnostic_state()
    )
    return jsonify(payload)


def current_automation_run_dir() -> Path | None:
    run_dir = get_automation_state().get("run_dir")
    if not run_dir:
        return None
    return Path(str(run_dir)).resolve()


def current_live_dir() -> Path | None:
    run_dir = current_automation_run_dir()
    return run_dir / "_system" / "live" if run_dir is not None else None


def current_run_display_path(run_dir: Path) -> str:
    try:
        return run_dir.relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        return run_dir.name


def idle_live_status() -> dict[str, Any]:
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
        "stream_error": None,
        "phase": None,
        "commanded_rpm": None,
        "rpm_source": None,
        "stabilization_mode": None,
    }


def live_status_with_progress(live_dir: Path | None) -> dict[str, Any]:
    current = read_live_status(live_dir) if live_dir is not None else None
    payload = current or idle_live_status()
    if live_dir is None:
        return payload
    progress = read_levich_progress(live_dir)
    if (
        isinstance(progress, dict)
        and progress.get("technique") == "levich_rpm_sweep_ca"
        and (not payload.get("run_id") or progress.get("run_id") == payload.get("run_id"))
        and (
            str(payload.get("technique") or "").lower() == "levich_rpm_sweep_ca"
            or (bool(progress.get("active", False)) and not payload.get("run_id"))
        )
    ):
        payload.update(progress)
    return payload


def echem_measurement_is_active() -> bool:
    live_dir = current_live_dir()
    current = read_live_status(live_dir) if live_dir is not None else None
    return bool(current and current.get("active", False))


@app.get("/api/gamry-cell/status")
def api_gamry_cell_status():
    try:
        return jsonify(gamry_cell_status())
    except GamryCellClientError as exc:
        return json_error(str(exc), 503)
    except Exception as exc:
        return json_error(f"Unable to read Gamry cell status: {exc}", 500)


@app.post("/api/gamry-cell/on")
def api_gamry_cell_on():
    if automation_is_running():
        return json_error("automation is running; manual Gamry Cell ON is disabled.", 409)
    if echem_measurement_is_active():
        return json_error("an EChem measurement is active; manual Gamry Cell ON is disabled.", 409)

    payload = request.get_json(silent=True) or {}
    raw_duration = payload.get("duration_s")
    duration_s: float | None

    if raw_duration is None or raw_duration == "":
        duration_s = None
    else:
        try:
            duration_s = float(raw_duration)
        except (TypeError, ValueError):
            return json_error("duration_s must be a positive number or null.", 400)
        if not math.isfinite(duration_s) or duration_s <= 0:
            return json_error("duration_s must be greater than 0.", 400)

    try:
        return jsonify(gamry_cell_on(duration_s))
    except GamryCellClientError as exc:
        return json_error(str(exc), 503)
    except Exception as exc:
        return json_error(f"Unable to turn the Gamry cell ON: {exc}", 500)


@app.post("/api/gamry-cell/off")
def api_gamry_cell_off():
    # OFF is intentionally always allowed, including during automation/abort.
    try:
        return jsonify(gamry_cell_off())
    except GamryCellClientError as exc:
        return json_error(str(exc), 503)
    except Exception as exc:
        return json_error(f"Unable to turn the Gamry cell OFF: {exc}", 500)


@app.get("/api/live/status")
def live_status():
    live_dir = current_live_dir()
    payload = live_status_with_progress(live_dir)
    return jsonify({"ok": True, "active": bool(payload.get("active", False)), "status": payload})


@app.get("/api/live/points")
def live_points():
    live_config = get_live_plot_config()

    def query_int(name: str, default: int) -> int | tuple[Any, int]:
        raw = request.args.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return json_error(f"{name} must be an integer.", 400)
        if name == "after" and value < 0:
            return json_error("after must be >= 0.", 400)
        if name == "limit" and value <= 0:
            return json_error("limit must be > 0.", 400)
        return value

    after = query_int("after", 0)
    if not isinstance(after, int):
        return after
    limit = query_int("limit", live_config["max_browser_points"])
    if not isinstance(limit, int):
        return limit
    limit = min(limit, live_config["max_browser_points"])

    live_dir = current_live_dir()
    status_payload = live_status_with_progress(live_dir)
    points = read_live_points(live_dir, after=after, limit=limit) if live_dir is not None else []
    latest_seq = int(status_payload.get("point_count", 0) or 0)
    if points:
        latest_seq = max(latest_seq, max(int(point.get("seq", 0)) for point in points))

    return jsonify(
        {
            "ok": True,
            "active": bool(status_payload.get("active", False)),
            "status": status_payload,
            "points": points,
            "latest_seq": latest_seq,
        }
    )


@app.post("/api/live/clear-view")
def live_clear_view():
    # The first frontend version clears only its in-memory canvas. Keep this
    # endpoint intentionally non-destructive so it can never remove a DTA.
    return jsonify({"ok": True, "message": "Browser live view can be cleared without changing stored data."})


@app.post("/api/live/clear")
def live_clear():
    """Clear only an inactive temporary stream; final DTA files are untouched."""
    live_dir = current_live_dir()
    if live_dir is None:
        return jsonify({"ok": True, "message": "No live stream exists."})
    current = read_live_status(live_dir)
    if current and bool(current.get("active", False)):
        return json_error("live acquisition is active; pause the display instead of clearing its stream.", 409)
    clear_live_stream(live_dir)
    return jsonify({"ok": True, "message": "Temporary live buffer cleared. Final DTA files are unchanged."})


@app.get("/api/current-run/dta-files")
def current_run_dta_files():
    run_dir = current_automation_run_dir()
    if run_dir is None:
        return jsonify(
            {
                "ok": True,
                "run_id": None,
                "run_dir": None,
                "files": [],
                "message": "No current automation trial is available.",
            }
        )

    files = list_dta_files(run_dir)
    analysis_groups = list_analysis_groups(run_dir)
    csv_count = sum(1 for item in files if item.get("csv_relative_path"))
    return jsonify(
        {
            "ok": True,
            "run_id": run_dir.name,
            "run_dir": current_run_display_path(run_dir),
            "files": files,
            "analysis_groups": analysis_groups,
            "message": (
                f"{len(files)} DTA file(s), {csv_count} matching CSV export(s), and "
                f"{len(analysis_groups)} grouped analysis result(s) "
                "are available from this automation trial."
                if files or analysis_groups
                else "No completed DTA files or analysis results are available in this automation trial yet."
            ),
        }
    )


@app.get("/api/live/events")
def live_events():
    try:
        after = int(request.args.get("after", 0) or 0)
        limit = min(1000, int(request.args.get("limit", 200) or 200))
    except (TypeError, ValueError):
        return json_error("after and limit must be integers.", 400)
    if after < 0 or limit <= 0:
        return json_error("after must be >= 0 and limit must be > 0.", 400)
    live_dir = current_live_dir()
    events = read_live_events(live_dir, after=after, limit=limit) if live_dir is not None else []
    latest_seq = max([after] + [int(event.get("seq", 0) or 0) for event in events])
    return jsonify({"ok": True, "events": events, "latest_seq": latest_seq})


@app.get("/api/current-run/history-artifact")
def current_run_history_artifact():
    run_dir = current_automation_run_dir()
    if run_dir is None:
        return json_error("No current automation trial is available.", 404)
    try:
        path = resolve_registered_artifact(run_dir, request.args.get("path", ""))
    except HistoryArtifactError as exc:
        return json_error(str(exc), exc.status_code)
    download = str(request.args.get("download", "")).strip().lower() in {"1", "true", "yes"}
    return send_file(
        path,
        as_attachment=download,
        download_name=path.name,
        conditional=True,
    )


@app.get("/api/current-run/dta-data")
def current_run_dta_data():
    run_dir = current_automation_run_dir()
    if run_dir is None:
        return json_error("No current automation trial is available.", 404)

    relative_path = request.args.get("path", "")
    try:
        dta_path = resolve_listed_dta_path(run_dir, relative_path)
        parsed = parse_dta_file(dta_path)
    except DtaViewerError as exc:
        return json_error(str(exc), exc.status_code)
    except OSError as exc:
        return json_error(f"Unable to read DTA file: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "run_id": run_dir.name,
            "relative_path": dta_path.relative_to(run_dir).as_posix(),
            **parsed,
        }
    )


@app.get("/api/current-run/analysis-data")
def current_run_analysis_data():
    """Return a registered analysis series for the shared History plotter."""

    run_dir = current_automation_run_dir()
    if run_dir is None:
        return json_error("No current automation trial is available.", 404)
    try:
        relative_path = request.args.get("path", "")
        path = resolve_registered_artifact(run_dir, relative_path)
        descriptor = analysis_artifact_descriptor(run_dir, relative_path)
        parsed = load_analysis_plot(
            descriptor["analysis_type"],
            descriptor["artifact_key"],
            path,
        )
    except HistoryArtifactError as exc:
        return json_error(str(exc), exc.status_code)
    except ValueError as exc:
        return json_error(str(exc), 400)
    except (CaChargeAnalysisError, OSError) as exc:
        return json_error(f"Unable to read analysis data: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "run_id": run_dir.name,
            "relative_path": path.relative_to(run_dir).as_posix(),
            **parsed,
        }
    )


@app.post("/api/start")
def start():
    global stop_timer

    if automation_is_running():
        return json_error("automation is running; manual RDE control is disabled.", 409)
    if manual_arm_motion_lock.locked():
        return json_error(
            "manual arm movement is running; RDE start is disabled until it finishes.",
            409,
        )

    payload = request.get_json(silent=True) or {}

    try:
        rpm = int(payload.get("rpm"))
        duration = int(payload.get("duration_seconds"))
        validate_rpm(rpm)
        validate_duration_seconds(duration)
    except Exception as exc:
        return json_error(str(exc), 400)

    rde_state = get_rde_state()

    if rde_state["running"]:
        return json_error("Motor is already running.", 409)

    try:
        send_rpm(rpm)
        start_rde_run(rpm, duration)
    except Exception as exc:
        return json_error(f"Unable to send rpm: {exc}", 500)

    if stop_timer is not None:
        stop_timer.cancel()

    stop_timer = threading.Timer(duration, auto_stop)
    stop_timer.daemon = True
    stop_timer.start()

    return jsonify({"ok": True})


@app.post("/api/stop")
def stop():
    global stop_timer

    if stop_timer is not None:
        stop_timer.cancel()
        stop_timer = None

    try:
        stop_rde(None)
    except Exception as exc:
        return json_error(f"Unable to stop RDE: {exc}", 500)

    return jsonify({"ok": True, "stop_rpm": get_rde_limits()["stop_rpm"]})


@app.post("/api/rotation/send")
def rotation_send():
    if automation_is_running():
        return json_error("automation is running; manual rotation commands are disabled.", 409)
    if manual_arm_motion_lock.locked():
        return json_error("another manual arm movement is already running.", 409)

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()

    if not command:
        return json_error("command must be a non-empty string.", 400)

    com_port = get_serial_port("rotation")
    app.logger.info(
        "Sending manual rotation command %r to %s and waiting for completion.",
        command,
        com_port,
    )
    try:
        ack = send_rotation_text(command)
    except Exception as exc:
        app.logger.exception(
            "Manual rotation command %r failed on %s.",
            command,
            com_port,
        )
        return json_error(
            f"Rotation command '{command}' failed on {com_port}: {exc}",
            500,
        )

    app.logger.info("Rotation command %r completed on %s: %s", command, com_port, ack)

    return jsonify(
        {
            "ok": True,
            "command": command,
            "com_port": com_port,
            "ack": ack,
        }
    )


def stop_rde_before_manual_arm_motion() -> None:
    """Stop timed/manual disk rotation before moving the arm mechanism."""

    global stop_timer

    if stop_timer is not None:
        stop_timer.cancel()
        stop_timer = None
    stop_rde(None)


def relative_tracking_locked_response(controller):
    state = controller.relative_diagnostic_state()
    message = (
        "Rotation-arm angle confidence is uncertain. Inspect the arm, then use "
        "'Arm Inspected — Reset Relative Tracking' before another relative move."
    )
    if state.get("last_relative_error"):
        message += f" Original failure: {state['last_relative_error']}"
    return (
        jsonify(
            {
                "error": message,
                "rotation_arm_state": state,
            }
        ),
        409,
    )


@app.post("/api/rotation/confirm-inspected")
def rotation_confirm_inspected():
    if automation_is_running():
        return json_error(
            "automation is running; relative tracking cannot be reset.",
            409,
        )

    if not manual_arm_motion_lock.acquire(blocking=False):
        return json_error("another manual arm movement is already running.", 409)

    try:
        controller = get_rotation_controller()
        if controller.expected_relative_state()["angle_confidence"] == "tracked":
            return json_error("rotation-arm relative tracking is already enabled.", 409)

        reset_state = controller.confirm_operator_inspection()
        return jsonify(
            {
                "ok": True,
                "message": (
                    "Operator inspection confirmed. The current physical arm angle "
                    "is now the software-only tracked starting angle; no motor "
                    "command was sent."
                ),
                "rotation_arm_state": controller.relative_diagnostic_state(),
                "reset": reset_state,
            }
        )
    finally:
        manual_arm_motion_lock.release()


@app.post("/api/rotation/check-relative-firmware")
def rotation_check_relative_firmware():
    if automation_is_running():
        return json_error(
            "automation is running; rotation firmware cannot be checked.",
            409,
        )

    if not manual_arm_motion_lock.acquire(blocking=False):
        return json_error("another manual arm movement is already running.", 409)

    try:
        controller = get_rotation_controller()
        capability = controller.check_relative_firmware_support()
        return jsonify(
            {
                "ok": bool(capability["supported"]),
                "com_port": get_serial_port("rotation"),
                "capability": capability,
                "rotation_arm_state": controller.relative_diagnostic_state(),
            }
        )
    finally:
        manual_arm_motion_lock.release()


@app.post("/api/rotation/relative-angle")
def rotation_relative_angle():
    if automation_is_running():
        return json_error(
            "automation is running; manual short-angle movement is disabled.",
            409,
        )

    payload = request.get_json(silent=True) or {}
    rotation = get_rotation_config()
    try:
        requested_angle_deg = float(payload.get("angle_deg"))
        requested_steps = angle_to_steps(
            requested_angle_deg,
            motor_full_steps_per_rev=int(rotation["motor_full_steps_per_rev"]),
            microstep=int(rotation["microstep"]),
        )
        if requested_steps == 0:
            raise RotationControllerError(
                "angle_deg rounds to zero motor steps."
            )
        maximum = int(rotation["max_relative_steps"])
        if abs(requested_steps) > maximum:
            raise RotationControllerError(
                f"angle_deg converts to {requested_steps} steps; "
                f"the configured limit is +/-{maximum} steps."
            )
    except (TypeError, ValueError, RotationControllerError) as exc:
        return json_error(f"Invalid short-angle movement: {exc}", 400)

    if not manual_arm_motion_lock.acquire(blocking=False):
        return json_error("another manual arm movement is already running.", 409)
    try:
        com_port = get_serial_port("rotation")
        controller = get_rotation_controller()
        if controller.expected_relative_state()["angle_confidence"] != "tracked":
            return relative_tracking_locked_response(controller)
        try:
            stop_rde_before_manual_arm_motion()
            result = controller.relative_steps(
                requested_steps,
                requested_angle_deg=requested_angle_deg,
            )
        except RotationMoveInterrupted as exc:
            return (
                jsonify(
                    {
                        "error": (
                            "Short-angle movement was interrupted; inspect the arm "
                            "before sending another relative movement."
                        ),
                        "com_port": com_port,
                        "move": asdict(exc.result),
                    }
                ),
                500,
            )
        except Exception as exc:
            app.logger.exception(
                "Manual short-angle movement failed on %s.",
                com_port,
            )
            return (
                jsonify(
                    {
                        "error": f"Short-angle movement failed on {com_port}: {exc}",
                        "rotation_arm_state": (
                            controller.relative_diagnostic_state()
                        ),
                    }
                ),
                500,
            )

        return jsonify(
            {
                "ok": True,
                "com_port": com_port,
                "disk_rpm_stopped": True,
                "move": asdict(result),
            }
        )
    finally:
        manual_arm_motion_lock.release()


@app.post("/api/rotation/oscillate")
def rotation_oscillate():
    if automation_is_running():
        return json_error(
            "automation is running; manual arm oscillation is disabled.",
            409,
        )

    payload = request.get_json(silent=True) or {}
    rotation = get_rotation_config()
    try:
        settings = validate_rinse_arm_settings(
            amplitude_deg=payload.get("amplitude_deg"),
            cycles=payload.get("cycles"),
            pause_between_moves_s=payload.get("pause_between_moves_s", 0.2),
            return_to_start=True,
            motor_full_steps_per_rev=int(rotation["motor_full_steps_per_rev"]),
            microstep=int(rotation["microstep"]),
            max_relative_steps=int(rotation["max_relative_steps"]),
        )
    except (TypeError, ValueError) as exc:
        return json_error(f"Invalid arm oscillation: {exc}", 400)

    if not manual_arm_motion_lock.acquire(blocking=False):
        return json_error("another manual arm movement is already running.", 409)
    try:
        com_port = get_serial_port("rotation")
        controller = get_rotation_controller()
        if controller.expected_relative_state()["angle_confidence"] != "tracked":
            return relative_tracking_locked_response(controller)
        try:
            stop_rde_before_manual_arm_motion()
            result = execute_rinse_arm_oscillation(
                run_dir=Path(__file__).resolve().parent,
                label="Manual Motor Control arm oscillation",
                amplitude_deg=float(settings["amplitude_deg"]),
                amplitude_steps=int(settings["amplitude_steps"]),
                cycles=int(settings["cycles"]),
                pause_between_moves_s=float(settings["pause_between_moves_s"]),
                controller=controller,
                pause_fn=time.sleep,
                abort_check_fn=lambda _message: None,
                record_fn=lambda _run_dir, record: record,
                log_fn=lambda _run_dir, _message: None,
            )
        except Exception as exc:
            app.logger.exception(
                "Manual arm oscillation failed on %s.",
                com_port,
            )
            return (
                jsonify(
                    {
                        "error": (
                            f"Arm oscillation failed on {com_port}: {exc}. "
                            "No automatic return or homing was attempted; inspect "
                            "the arm."
                        ),
                        "rotation_arm_state": (
                            controller.relative_diagnostic_state()
                        ),
                    }
                ),
                500,
            )

        return jsonify(
            {
                "ok": True,
                "com_port": com_port,
                "disk_rpm_stopped": True,
                "oscillation": result,
            }
        )
    finally:
        manual_arm_motion_lock.release()


@app.post("/api/rotation/ccw")
def rotation_ccw():
    return rotation_send_with_command("1")


@app.post("/api/rotation/home")
def rotation_home_route():
    return rotation_send_with_command("0")


def rotation_send_with_command(command: str):
    if automation_is_running():
        return json_error("automation is running; manual rotation commands are disabled.", 409)
    if manual_arm_motion_lock.locked():
        return json_error("another manual arm movement is already running.", 409)

    com_port = get_serial_port("rotation")
    app.logger.info(
        "Sending manual rotation command %r to %s and waiting for completion.",
        command,
        com_port,
    )
    try:
        ack = send_rotation_text(command)
    except Exception as exc:
        app.logger.exception(
            "Manual rotation command %r failed on %s.",
            command,
            com_port,
        )
        return json_error(
            f"Rotation command '{command}' failed on {com_port}: {exc}",
            500,
        )

    app.logger.info("Rotation command %r completed on %s: %s", command, com_port, ack)

    return jsonify(
        {
            "ok": True,
            "value": command,
            "command": command,
            "com_port": com_port,
            "ack": ack,
        }
    )


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
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return steps


@app.post("/api/linear/send")
def linear_send():
    if automation_is_running():
        return json_error("automation is running; manual linear commands are disabled.", 409)
    if manual_arm_motion_lock.locked():
        return json_error("manual arm movement is running; X/Z commands are disabled.", 409)

    steps_or_error = parse_axis_command_request("linear")

    if not isinstance(steps_or_error, int):
        return steps_or_error

    try:
        ack = move_linear_steps(steps_or_error)
    except Exception as exc:
        return json_error(f"Unable to send '{steps_or_error}' to linear controller: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "command": str(steps_or_error),
            "com_port": get_serial_port("linear"),
            "ack": ack,
        }
    )


@app.post("/api/horizontal/send")
def horizontal_send():
    if automation_is_running():
        return json_error("automation is running; manual horizontal commands are disabled.", 409)
    if manual_arm_motion_lock.locked():
        return json_error("manual arm movement is running; X/Z commands are disabled.", 409)

    steps_or_error = parse_axis_command_request("horizontal")

    if not isinstance(steps_or_error, int):
        return steps_or_error

    try:
        ack = move_horizontal_steps(steps_or_error)
    except Exception as exc:
        return json_error(f"Unable to send '{steps_or_error}' to horizontal controller: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "command": str(steps_or_error),
            "com_port": get_serial_port("horizontal"),
            "ack": ack,
        }
    )


@app.post("/api/vertical/send")
def vertical_send():
    if automation_is_running():
        return json_error("automation is running; manual vertical commands are disabled.", 409)
    if manual_arm_motion_lock.locked():
        return json_error("manual arm movement is running; X/Z commands are disabled.", 409)

    steps_or_error = parse_axis_command_request("vertical")

    if not isinstance(steps_or_error, int):
        return steps_or_error

    try:
        ack = move_vertical_steps(steps_or_error)
    except Exception as exc:
        return json_error(f"Unable to send '{steps_or_error}' to vertical controller: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "command": str(steps_or_error),
            "com_port": get_serial_port("vertical"),
            "ack": ack,
        }
    )


@app.post("/api/axes/home")
def axes_home():
    return json_error(
        "Axis homing is temporarily disabled while the physical home position is being calibrated.",
        409,
    )


@app.post("/api/axes/tracked-position")
def axes_tracked_position():
    """
    Correct one software-tracked coordinate without commanding any hardware.

    This is intentionally separate from the motion and homing routes. The
    caller must explicitly acknowledge that the physical axis will not move.
    """
    if automation_is_running():
        return json_error(
            "automation is running; tracked positions cannot be corrected.",
            409,
        )

    payload = request.get_json(silent=True) or {}
    if payload.get("confirm_software_only") is not True:
        return json_error(
            "confirm_software_only must be true; this changes only the software record.",
            400,
        )

    user_axis = str(payload.get("axis", "") or "").strip().lower()
    if user_axis not in {"x", "y", "z"}:
        return json_error("axis must be x, y, or z.", 400)

    try:
        position = int(payload.get("position"))
        internal_axis = user_axis_to_internal_axis(user_axis)
        validate_axis_position(internal_axis, position)
        set_axis_position(internal_axis, position)
    except (TypeError, ValueError, ConfigError) as exc:
        return json_error(str(exc), 400)

    return jsonify(
        {
            "ok": True,
            "axis": user_axis,
            "internal_axis": internal_axis,
            "position": position,
            "hardware_command_sent": False,
            "message": (
                f"Tracked {user_axis.upper()} was corrected to {position}. "
                "No physical movement or serial command was performed."
            ),
        }
    )


@app.get("/api/protocols")
def protocols_list():
    return jsonify({"ok": True, "protocols": list_protocols()})


@app.get("/api/protocol")
def protocol_load():
    name = str(request.args.get("name", "") or "").strip()

    if not name:
        return jsonify({"ok": True, **default_protocol_payload()})

    try:
        data = load_protocol(name)
    except ProtocolError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(f"Unable to load protocol: {exc}", 500)

    return jsonify({"ok": True, **data})


@app.post("/api/protocol")
def protocol_save():
    payload = request.get_json(silent=True) or {}
    create_mode = str(payload.get("editor_mode", "edit") or "edit").strip().lower() == "create"

    try:
        result = save_protocol(payload, overwrite=not create_mode)
    except ProtocolError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:
        return json_error(f"Unable to save protocol: {exc}", 500)

    return jsonify(result)


@app.post("/api/protocols")
def protocols_save_alias():
    return protocol_save()


@app.post("/api/echem-recipe")
def echem_recipe_save_alias():
    return protocol_save()


@app.post("/api/protocol/compact")
def protocol_save_compact():
    payload = request.get_json(silent=True) or {}
    create_mode = str(payload.get("editor_mode", "edit") or "edit").strip().lower() == "create"

    try:
        validated = validate_protocol_payload(payload)
        raw_payload = dict(payload)
        raw_payload["protocol_name"] = validated["protocol_name"]
        raw_payload["display_name"] = validated["display_name"]
        raw_payload["description"] = validated["description"]
        raw_payload["saved_at"] = datetime.now(timezone.utc).isoformat()

        path = protocol_path_for_name(validated["protocol_name"])
        if path.exists() and create_mode:
            raise ProtocolError(
                f"protocol '{validated['protocol_name']}' already exists; choose a unique name for a new protocol."
            )
        with path.open("w", encoding="utf-8") as f:
            json.dump(raw_payload, f, indent=2)
            f.write("\n")

    except ProtocolError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:
        return json_error(f"Unable to save compact protocol: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "protocol_name": validated["protocol_name"],
            "display_name": validated["display_name"],
            "step_count": len(validated["steps"]),
            "builder_step_count": len(raw_payload.get("steps", [])),
            "path": str(path),
            "saved_at": raw_payload["saved_at"],
        }
    )


@app.get("/api/protocol/raw")
def protocol_load_raw():
    name = str(request.args.get("name", "") or "").strip()

    if not name:
        return jsonify({"ok": True, **default_protocol_payload()})

    try:
        protocol_name = normalize_protocol_name(name)
        path = protocol_path_for_name(protocol_name)

        if not path.exists():
            raise ProtocolError(f"protocol '{protocol_name}' does not exist.")

        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        validated = validate_protocol_payload(payload)

    except ProtocolError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(f"Unable to load raw protocol: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            **payload,
            "expanded_step_count": len(validated["steps"]),
        }
    )


@app.delete("/api/protocol")
def protocol_delete():
    name = request.args.get("name", "")

    try:
        result = delete_protocol(name)
    except ProtocolError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(f"Unable to delete protocol: {exc}", 500)

    return jsonify(result)


@app.get("/api/run-plans")
def run_plans_list():
    return jsonify({"ok": True, "run_plans": list_run_plans()})


@app.get("/api/run-plan")
def run_plan_load():
    name = request.args.get("name", "single_sample_test")

    try:
        data = load_run_plan(name)
    except RunPlanError:
        data = default_run_plan_payload()
    except Exception as exc:
        return json_error(f"Unable to load run plan: {exc}", 500)

    return jsonify({"ok": True, **data})


@app.post("/api/run-plan")
def run_plan_save():
    payload = request.get_json(silent=True) or {}
    create_mode = str(payload.get("editor_mode", "edit") or "edit").strip().lower() == "create"

    try:
        result = save_run_plan(payload, overwrite=not create_mode)
    except RunPlanError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:
        return json_error(f"Unable to save run plan: {exc}", 500)

    return jsonify(result)


@app.delete("/api/run-plan")
def run_plan_delete():
    name = request.args.get("name", "")

    try:
        result = delete_run_plan(name)
    except RunPlanError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(f"Unable to delete run plan: {exc}", 500)

    return jsonify(result)


def sample_to_atomic_group(sample: dict[str, Any], index: int) -> dict[str, Any]:
    """
    Convert an older all-in-one sample block for UI display.

    Legacy X/Z values were absolute targets, while the grouped builder now uses
    signed relative steps. Non-zero legacy positions are therefore imported as
    disabled review steps instead of being executed silently with new meaning.
    """
    position = sample.get("position", {})
    label = str(sample.get("label") or sample.get("sample_id") or f"Sample {index}")
    steps: list[dict[str, Any]] = []

    legacy_x = int(position.get("x", 0))
    legacy_z = int(position.get("z", 0))

    if legacy_x != 0:
        steps.append(
            {
                "name": "REVIEW legacy X absolute target",
                "action": "move_x",
                "enabled": False,
                "steps": legacy_x,
            }
        )

    if legacy_z != 0:
        steps.append(
            {
                "name": "REVIEW legacy Z absolute target",
                "action": "move_z",
                "enabled": False,
                "steps": legacy_z,
            }
        )

    rotation_command = str(sample.get("rotation_command", "") or "").strip()
    if rotation_command:
        steps.append(
            {
                "name": "Rotate RDE Arm",
                "action": "rotation",
                "enabled": True,
                "command": rotation_command,
            }
        )

    rpm = int(sample.get("rpm", 0) or 0)
    if rpm > 0:
        steps.append(
            {
                "name": "Set RDE RPM",
                "action": "set_rpm",
                "enabled": True,
                "rpm": rpm,
            }
        )

    stabilization_s = float(sample.get("stabilization_s", 0) or 0)
    if stabilization_s > 0:
        steps.append(
            {
                "name": "Stabilization Wait",
                "action": "wait",
                "enabled": True,
                "duration_s": stabilization_s,
            }
        )

    protocol_name = str(sample.get("protocol", "ocp_only") or "ocp_only")
    steps.append(
        {
            "name": "EChem Measurement",
            "action": "echem",
            "enabled": True,
            "protocol": protocol_name,
        }
    )

    post_wait = float(sample.get("post_echem_wait_s", 0) or 0)
    if post_wait > 0:
        steps.append(
            {
                "name": "Post-EChem Wait",
                "action": "wait",
                "enabled": True,
                "duration_s": post_wait,
            }
        )

    if rpm > 0:
        steps.append(
            {
                "name": "Stop RDE",
                "action": "stop_rpm",
                "enabled": True,
            }
        )

    return {
        "group_id": str(sample.get("sample_id") or f"group_{index:03d}"),
        "label": label,
        "enabled": bool(sample.get("enabled", True)),
        "steps": steps,
    }


def grouped_payload_to_run_plan(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or payload.get("run_name") or "default").strip()
    groups = payload.get("groups", [])

    if not isinstance(groups, list):
        raise RunPlanError("groups must be a list.")

    return {
        "schema_version": 2,
        "run_name": name,
        "display_name": str(payload.get("display_name") or name),
        "description": str(payload.get("description") or "Grouped atomic-step run plan created in the web app."),
        "repetitions": int(payload.get("repetitions", 1) or 1),
        "groups": groups,
    }


def run_plan_to_ui_payload(run_plan: dict[str, Any]) -> dict[str, Any]:
    if "groups" in run_plan:
        groups = run_plan.get("groups", [])
    else:
        groups = [
            sample_to_atomic_group(sample, index)
            for index, sample in enumerate(run_plan.get("samples", []), start=1)
        ]

    return {
        "name": run_plan.get("run_name", "default"),
        "display_name": run_plan.get("display_name", run_plan.get("run_name", "default")),
        "description": run_plan.get("description", ""),
        "repetitions": run_plan.get("repetitions", 1),
        "groups": groups,
        "saved_at": run_plan.get("saved_at"),
    }


@app.get("/api/recipes")
def list_recipes():
    recipes = []

    for plan in list_run_plans():
        group_count = int(plan.get("group_count", plan.get("sample_count", 0)) or 0)
        recipes.append(
            {
                "name": plan["run_name"],
                "repetitions": plan["repetitions"],
                "group_count": group_count,
                "step_count": int(plan.get("step_count", 0) or 0),
                "saved_at": plan.get("saved_at"),
            }
        )

    return jsonify({"ok": True, "recipes": recipes})


@app.get("/api/recipe")
def load_recipe():
    name = request.args.get("name", "default")

    try:
        run_plan = load_run_plan(name)
    except RunPlanError:
        run_plan = default_run_plan_payload()
        run_plan["run_name"] = name
    except Exception as exc:
        return json_error(f"Unable to load run plan: {exc}", 500)

    return jsonify({"ok": True, **run_plan_to_ui_payload(run_plan)})


@app.post("/api/recipe")
def save_recipe():
    payload = request.get_json(silent=True) or {}
    create_mode = str(payload.get("editor_mode", "edit") or "edit").strip().lower() == "create"

    try:
        run_plan = grouped_payload_to_run_plan(payload)
        result = save_run_plan(run_plan, overwrite=not create_mode)
    except Exception as exc:
        return json_error(str(exc), 400)

    return jsonify(
        {
            "ok": True,
            "name": result["run_name"],
            "group_count": result.get("group_count", result.get("sample_count", 0)),
            "step_count": result.get("step_count", 0),
            "repetitions": run_plan["repetitions"],
        }
    )


@app.delete("/api/recipe")
def delete_recipe():
    name = request.args.get("name", "")

    try:
        result = delete_run_plan(name)
    except RunPlanError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(f"Unable to delete run plan: {exc}", 500)

    return jsonify({"ok": True, "name": result["run_name"]})


@app.get("/api/automation/status")
def automation_status():
    status_payload = get_status_payload()
    return jsonify(
        {
            "running": status_payload["automation_running"],
            "current_step": status_payload["automation_step"],
            "last_error": status_payload["automation_error"],
            "run_dir": status_payload["automation_run_dir"],
        }
    )


@app.post("/api/automation/start")
def automation_start():
    if automation_is_running():
        return json_error("automation is already running.", 409)
    if manual_arm_motion_lock.locked():
        return json_error(
            "manual arm movement is running; wait for it to finish before automation.",
            409,
        )

    rde_state = get_rde_state()
    if rde_state["running"]:
        return json_error("motor is currently running; stop it before automation.", 409)

    payload = request.get_json(silent=True) or {}

    try:
        if "run_plan_name" in payload:
            run_plan = load_run_plan(str(payload["run_plan_name"]))
        elif "groups" in payload:
            run_plan = validate_run_plan_payload(grouped_payload_to_run_plan(payload))
        elif "samples" in payload:
            run_plan = validate_run_plan_payload(payload)
        else:
            raise RunPlanError("automation request must contain groups, samples, or run_plan_name.")
    except Exception as exc:
        return json_error(str(exc), 400)

    # Always send the physical stop RPM before starting the run thread.
    # This prevents a stale/manual RPM command from carrying into the first
    # X/Z movement after an app restart or a previous interrupted run.
    try:
        stop_rde(None)
    except Exception as exc:
        return json_error(f"Unable to confirm RDE stop before automation: {exc}", 500)

    try:
        run_plan_payload_background(run_plan)
    except RecipeRunnerError as exc:
        return json_error(str(exc), 409)

    if "groups" in run_plan:
        selected = [group["label"] for group in run_plan["groups"] if bool(group.get("enabled", True))]
    else:
        selected = [sample["label"] for sample in run_plan["samples"] if bool(sample.get("enabled", True))]

    return jsonify(
        {
            "ok": True,
            "selected_steps": selected,
            "selected_groups": selected,
            "selected_samples": selected,
            "repetitions": run_plan["repetitions"],
        }
    )


def perform_emergency_stop(reason: str) -> dict[str, Any]:
    """Stop every motor immediately, during either manual or automated use."""
    global stop_timer

    automation_was_running = automation_is_running()

    if automation_was_running:
        # Set the shared abort flag first so automation wait/motion loops exit.
        abort_automation()

    if stop_timer is not None:
        stop_timer.cancel()
        stop_timer = None

    # Send STOP directly to every open axis serial connection. This bypasses
    # the normal transaction lock held by the thread waiting for an axis ACK.
    motion_stop_result = emergency_stop_motion()
    rotation_stop_sent = emergency_stop_rotation()

    # Stop RDE immediately instead of waiting for recipe-runner cleanup.
    rde_stop_error = None
    try:
        stop_rde(reason)
    except Exception as exc:
        rde_stop_error = str(exc)

    # Attempt cell OFF only after the immediate motor STOP commands have been
    # issued. A ToolkitPy failure must never hide the motor/RDE abort result.
    gamry_cell_off_error = None
    try:
        gamry_cell_off()
    except Exception as exc:
        gamry_cell_off_error = str(exc)

    payload = {
        "ok": True,
        "message": (
            "Emergency stop sent: RDE stop and X/Z/rotation STOP commands "
            "were issued immediately, then Gamry Cell OFF was attempted. "
            "Motion remains in place."
        ),
        "automation_was_running": automation_was_running,
        "motion_stop_sent": motion_stop_result,
        "rotation_stop_sent": rotation_stop_sent,
    }

    if rde_stop_error:
        payload["rde_stop_error"] = rde_stop_error

    if gamry_cell_off_error:
        payload["gamry_cell_off_error"] = gamry_cell_off_error

    return payload


@app.post("/api/motors/emergency-stop")
def motor_emergency_stop_route():
    return jsonify(perform_emergency_stop("Manual motor emergency stop requested."))


@app.post("/api/automation/abort")
@app.post("/api/automation/abort-home")
def automation_abort_route():
    if not automation_is_running():
        return json_error("automation is not running.", 409)

    return jsonify(perform_emergency_stop("Immediate automation abort requested."))


if __name__ == "__main__":
    from workflow.single_instance import (
        SingleInstanceError,
        acquire_webui_instance_lock,
        reject_existing_webui_listener,
    )

    port = int(os.environ.get("PORT", "5055"))
    try:
        reject_existing_webui_listener(port)
        acquire_webui_instance_lock()
    except SingleInstanceError as exc:
        raise SystemExit(str(exc)) from exc

    app.run(host="127.0.0.1", port=port, debug=False)
