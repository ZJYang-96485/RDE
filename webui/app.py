from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request

from hardware.motion_controller import (
    home_axes_internal,
    move_horizontal_steps,
    move_linear_steps,
    move_vertical_steps,
)
from hardware.rde_controller import send_rpm, stop_rde
from hardware.rotation_controller import send_rotation_text
from workflow.config_loader import (
    ConfigError,
    get_baud_rate,
    get_max_axis_command,
    get_motion_config,
    get_rde_limits,
    get_safe_z,
    get_serial_port,
    load_config,
    set_gamry_mode,
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
from workflow.run_plan_loader import (
    RunPlanError,
    default_run_plan_payload,
    delete_run_plan,
    list_run_plans,
    load_run_plan,
    save_run_plan,
    validate_run_plan_payload,
)
from workflow.safety import validate_axis_move, validate_duration_seconds, validate_rpm
from workflow.state import (
    automation_is_running,
    get_rde_state,
    get_status_payload,
    start_rde_run,
)

app = Flask(__name__)

stop_timer: threading.Timer | None = None


def config_payload() -> dict[str, Any]:
    config = load_config()
    rde = get_rde_limits()
    motion = get_motion_config()
    gamry = config["gamry"]
    real_command = gamry.get("real_worker_command", [])
    real_runner_configured = bool(str(gamry.get("real_worker_script", "") or "").strip())

    if isinstance(real_command, list):
        real_runner_configured = real_runner_configured or any(
            str(item).strip()
            for item in real_command
        )
    else:
        real_runner_configured = real_runner_configured or bool(str(real_command or "").strip())

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
        "gamry_mode": gamry["mode"],
        "gamry_real_runner_configured": real_runner_configured,
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
    return jsonify(payload)


@app.post("/api/start")
def start():
    global stop_timer

    if automation_is_running():
        return json_error("automation is running; manual RDE control is disabled.", 409)

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

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()

    if not command:
        return json_error("command must be a non-empty string.", 400)

    try:
        ack = send_rotation_text(command)
    except Exception as exc:
        return json_error(f"Unable to send '{command}' to rotation controller: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "command": command,
            "com_port": get_serial_port("rotation"),
            "ack": ack,
        }
    )


@app.post("/api/rotation/ccw")
def rotation_ccw():
    return rotation_send_with_command("1")


@app.post("/api/rotation/home")
def rotation_home_route():
    return rotation_send_with_command("0")


def rotation_send_with_command(command: str):
    if automation_is_running():
        return json_error("automation is running; manual rotation commands are disabled.", 409)

    try:
        ack = send_rotation_text(command)
    except Exception as exc:
        return json_error(f"Unable to send '{command}' to rotation controller: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "value": command,
            "command": command,
            "com_port": get_serial_port("rotation"),
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
    if automation_is_running():
        return json_error("automation is running; home is disabled.", 409)

    try:
        result = home_axes_internal()
    except Exception as exc:
        return json_error(f"Unable to return axes to home position: {exc}", 500)

    return jsonify(
        {
            "ok": True,
            "linear_command": result["linear_command"],
            "horizontal_command": result["horizontal_command"],
            "vertical_command": result["vertical_command"],
            "linear_position": 0,
            "horizontal_position": 0,
            "vertical_position": 0,
            "linear_com_port": get_serial_port("linear"),
            "rotation_com_port": get_serial_port("rotation"),
            "horizontal_com_port": get_serial_port("horizontal"),
            "vertical_com_port": get_serial_port("vertical"),
            "rotation_command": result["rotation_command"],
            "rotation_ack": result["rotation_ack"],
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

    try:
        result = save_protocol(payload)
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

    try:
        validated = validate_protocol_payload(payload)
        raw_payload = dict(payload)
        raw_payload["protocol_name"] = validated["protocol_name"]
        raw_payload["display_name"] = validated["display_name"]
        raw_payload["description"] = validated["description"]
        raw_payload["saved_at"] = datetime.now(timezone.utc).isoformat()

        path = protocol_path_for_name(validated["protocol_name"])
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

    try:
        result = save_run_plan(payload)
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


def legacy_step_to_sample(step: dict[str, Any], index: int) -> dict[str, Any]:
    protocol = str(step.get("protocol", "ca_steps_backward") or "ca_steps_backward").strip()

    return {
        "sample_id": f"sample_{index:03d}",
        "label": str(step.get("name") or f"Sample {index}"),
        "enabled": bool(step.get("enabled", True)),
        "position": {
            "x": int(step.get("x", step.get("horizontal", 0)) or 0),
            "y": int(step.get("vertical", step.get("y", 0)) or 0),
            "z": int(step.get("z", step.get("linear", 0)) or 0),
        },
        "rpm": int(step.get("rpm", 0) or 0),
        "stabilization_s": float(step.get("duration_seconds", step.get("seconds", 0)) or 0),
        "protocol": protocol,
        "rotation_command": str(step.get("rotation_command", "") or "").strip(),
        "post_echem_wait_s": 0,
        "rinse_after": bool(step.get("rinse_after", False)),
    }


def sample_to_legacy_step(sample: dict[str, Any]) -> dict[str, Any]:
    position = sample.get("position", {})

    return {
        "name": sample.get("label", sample.get("sample_id", "Sample")),
        "enabled": bool(sample.get("enabled", True)),
        "x": int(position.get("x", 0)),
        "vertical": int(position.get("y", 0)),
        "z": int(position.get("z", 0)),
        "rpm": int(sample.get("rpm", 0)),
        "duration_seconds": int(float(sample.get("stabilization_s", 0))),
        "rotation_command": sample.get("rotation_command", ""),
        "protocol": sample.get("protocol", "ca_steps_backward"),
        "rinse_after": bool(sample.get("rinse_after", False)),
    }


def legacy_payload_to_run_plan(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "default") or "default").strip()
    raw_steps = payload.get("steps", [])

    if not isinstance(raw_steps, list):
        raise RunPlanError("steps must be a list.")

    samples = [
        legacy_step_to_sample(step, index)
        for index, step in enumerate(raw_steps, start=1)
        if isinstance(step, dict)
    ]

    return {
        "run_name": name,
        "display_name": name,
        "description": "Compatibility run plan created from the original Automation Recipe panel.",
        "repetitions": int(payload.get("repetitions", 1) or 1),
        "samples": samples,
    }


def run_plan_to_legacy_payload(run_plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": run_plan.get("run_name", "default"),
        "repetitions": run_plan.get("repetitions", 1),
        "steps": [sample_to_legacy_step(sample) for sample in run_plan.get("samples", [])],
        "saved_at": run_plan.get("saved_at"),
    }


@app.get("/api/recipes")
def list_recipes():
    recipes = []

    for plan in list_run_plans():
        recipes.append(
            {
                "name": plan["run_name"],
                "repetitions": plan["repetitions"],
                "step_count": plan["sample_count"],
                "saved_at": plan.get("saved_at"),
            }
        )

    return jsonify({"ok": True, "recipes": recipes})


@app.get("/api/recipe")
def load_recipe():
    name = request.args.get("name", "single_sample_test")

    try:
        run_plan = load_run_plan(name)
    except RunPlanError:
        run_plan = default_run_plan_payload()
        run_plan["run_name"] = name
    except Exception as exc:
        return json_error(f"Unable to load recipe: {exc}", 500)

    return jsonify({"ok": True, **run_plan_to_legacy_payload(run_plan)})


@app.post("/api/recipe")
def save_recipe():
    payload = request.get_json(silent=True) or {}

    try:
        run_plan = legacy_payload_to_run_plan(payload)
        result = save_run_plan(run_plan)
    except Exception as exc:
        return json_error(str(exc), 400)

    return jsonify(
        {
            "ok": True,
            "name": result["run_name"],
            "count": result["sample_count"],
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
        return json_error(f"Unable to delete recipe: {exc}", 500)

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

    rde_state = get_rde_state()

    if rde_state["running"]:
        return json_error("motor is currently running; stop it before automation.", 409)

    payload = request.get_json(silent=True) or {}

    try:
        if "run_plan_name" in payload:
            run_plan = load_run_plan(str(payload["run_plan_name"]))
        elif "samples" in payload:
            run_plan = validate_run_plan_payload(payload)
        else:
            run_plan = validate_run_plan_payload(legacy_payload_to_run_plan(payload))
    except Exception as exc:
        return json_error(str(exc), 400)

    try:
        run_plan_payload_background(run_plan)
    except RecipeRunnerError as exc:
        return json_error(str(exc), 409)

    selected_samples = [
        sample["label"]
        for sample in run_plan["samples"]
        if bool(sample.get("enabled", True))
    ]

    return jsonify(
        {
            "ok": True,
            "selected_steps": selected_samples,
            "selected_samples": selected_samples,
            "repetitions": run_plan["repetitions"],
        }
    )


@app.post("/api/automation/abort-home")
def automation_abort_home():
    if not automation_is_running():
        return json_error("automation is not running.", 409)

    abort_automation()

    return jsonify({"ok": True, "message": "Abort requested. System will stop and go home."})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5055"))
    app.run(host="127.0.0.1", port=port, debug=False)
