from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from gamry_worker.device import configured_step
    from gamry_worker.live_writer import (
        append_live_event,
        fail_live_stream,
        finish_live_stream,
        initialize_live_stream,
        read_live_status,
        update_live_status,
    )
    from gamry_worker.ir_compensation import technique_supports_positive_feedback
    from gamry_worker.trial_preparation import CriticalHardwareError, default_trial_metadata, determine_ru, utc_now as trial_utc_now
except ModuleNotFoundError:
    from device import configured_step
    from live_writer import (
        append_live_event,
        fail_live_stream,
        finish_live_stream,
        initialize_live_stream,
        read_live_status,
        update_live_status,
    )
    from ir_compensation import technique_supports_positive_feedback
    from trial_preparation import CriticalHardwareError, default_trial_metadata, determine_ru, utc_now as trial_utc_now


class GamryWorkerError(RuntimeError):
    pass


REAL_RUNNER_MODULES = {
    "ocp": "run_ocp",
    "ca": "run_ca",
    "ca_staircase": "run_ca",
    "levich_rpm_sweep_ca": "run_ca",
    "cv": "run_cv",
    "lsv": "run_lsv",
    "eis": "run_eis",
    "cp": "run_cp",
    "cc_charge": "run_cc",
    "cc_discharge": "run_cc",
    "geis": "run_geis",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise GamryWorkerError("job file must contain a JSON object.")
    return payload


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")


def normalize_outputs(outputs: Any) -> list[str]:
    if isinstance(outputs, str):
        outputs = [outputs]
    if not isinstance(outputs, list):
        raise GamryWorkerError("outputs must be a list of file paths.")
    normalized = [str(output).strip() for output in outputs if str(output).strip()]
    if not normalized:
        raise GamryWorkerError("at least one output path is required.")
    return normalized


def live_enabled_for_job(job: dict[str, Any]) -> bool:
    return bool(job.get("live_enabled", True) and str(job.get("live_dir", "") or "").strip())


def start_live_for_job(
    job: dict[str, Any],
    step: dict[str, Any],
    sample_id: str | None,
) -> bool:
    if not live_enabled_for_job(job):
        return False
    try:
        initialize_live_stream(
            job["live_dir"],
            run_id=str(job.get("run_id", "") or "") or None,
            sample_id=sample_id,
            sample_label=str(job.get("sample_label", "") or "") or None,
            protocol_name=str(job.get("protocol_name", "") or "") or None,
            step_name=str(step.get("name", "") or "") or None,
            technique=str(step.get("technique", "") or "").strip().lower() or None,
        )
        return True
    except Exception:
        # A plot/status-file problem must never prevent the actual Gamry trial.
        return False


def real_runner_for_technique(technique: str) -> Callable[..., dict[str, Any]]:
    module_name = REAL_RUNNER_MODULES.get(technique)
    if module_name is None:
        raise GamryWorkerError(f"unsupported real Gamry technique: {technique}")
    try:
        module = importlib.import_module(f"gamry_worker.{module_name}")
    except ModuleNotFoundError as exc:
        if exc.name not in {f"gamry_worker.{module_name}", "gamry_worker"}:
            raise
        module = importlib.import_module(module_name)
    return module.run


def dispatch_mock_step(
    job: dict[str, Any],
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None,
) -> dict[str, Any]:
    del step, outputs, sample_id
    try:
        from gamry_worker.mock_gamry import run_job as run_mock_job
    except ModuleNotFoundError:
        from mock_gamry import run_job as run_mock_job
    return run_mock_job(job)


def trial_settings(job: dict[str, Any]) -> dict[str, Any]:
    gamry = job.get("gamry", {})
    if not isinstance(gamry, dict):
        return {}
    value = gamry.get("ru_preparation", {})
    return dict(value) if isinstance(value, dict) else {}


def event_emitter(job: dict[str, Any], step: dict[str, Any], sample_id: str | None) -> Callable[..., Any]:
    context = {
        "trial_id": str(step.get("_trial_id") or job.get("job_id") or ""),
        "trial_number": step.get("_trial_index"),
        "sample_id": sample_id,
        "sample_name": str(job.get("sample_label", "") or "") or None,
        "technique": str(step.get("technique", "") or "").strip().lower(),
        "electrode_channel": str(step.get("electrode_channel") or trial_settings(job).get("electrode_channel", "primary")),
    }

    def emit(event_type: str, **fields: Any) -> Any:
        if not live_enabled_for_job(job):
            return None
        payload = dict(context)
        payload.update(fields)
        try:
            return append_live_event(job["live_dir"], event_type, **payload)
        except Exception as exc:
            try:
                update_live_status(job["live_dir"], stream_error=f"Live event update failed: {exc}")
            except Exception:
                pass
            return None

    return emit


def prepare_mock_trial(
    job: dict[str, Any],
    step: dict[str, Any],
    emit: Callable[..., Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = trial_settings(job)
    metadata = default_trial_metadata(settings)
    metadata["ocp_stabilization_status"] = "stable"
    emit("electrode_channel_selected")
    emit("electrode_channel_verified")
    emit("ocp_stabilization_started", minimum_s=float(settings.get("ocp_stabilization_s", 5.0)))
    emit("ocp_stabilized", mock=True)
    if step.get("mock_ru_critical_error"):
        raise CriticalHardwareError(str(step["mock_ru_critical_error"]))
    values = step.get("mock_ru_attempts_ohm", settings.get("mock_ru_attempts_ohm", [10.0, 10.1]))
    if not isinstance(values, list):
        values = [values]
    errors = step.get("mock_ru_errors", [])
    if not isinstance(errors, list):
        errors = [errors]

    def measure(attempt: int) -> Any:
        if attempt <= len(errors) and errors[attempt - 1]:
            raise RuntimeError(str(errors[attempt - 1]))
        return values[attempt - 1] if attempt <= len(values) else None

    metadata = determine_ru(measure, settings, metadata=metadata, emit_event=emit)
    effective = dict(step)
    if metadata["ru_validation_passed"]:
        effective.update(
            {
                "_trial_ru_validation_passed": True,
                "_trial_ru_selected_ohm": metadata["ru_selected_ohm"],
                "_trial_ru_applied_ohm": metadata["ru_applied_ohm"],
                "_trial_fixed_current_range_a": float(settings.get("fixed_current_range_a", 0.003)),
            }
        )
    return metadata, effective


def prepare_real_trial_for_job(
    job: dict[str, Any],
    step: dict[str, Any],
    emit: Callable[..., Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from gamry_worker.real_trial_preparation import prepare_real_trial
    except ModuleNotFoundError as exc:
        if exc.name not in {"gamry_worker.real_trial_preparation", "gamry_worker"}:
            raise
        from real_trial_preparation import prepare_real_trial
    return prepare_real_trial(
        configured_step(step, job.get("gamry", {})),
        trial_settings(job),
        emit_event=emit,
    )


def dispatch_real_step(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    technique = str(step.get("technique", "")).strip().lower()
    runner = real_runner_for_technique(technique)
    gamry_config = (job or {}).get("gamry", {})
    if not isinstance(gamry_config, dict):
        gamry_config = {}
    effective_step = configured_step(step, gamry_config)
    kwargs: dict[str, Any] = {
        "step": effective_step,
        "outputs": outputs,
        "sample_id": sample_id,
    }
    # Existing third-party/test runners do not necessarily know about live_dir.
    # Locally maintained runners accept it so real acquisition rows can stream.
    if "live_dir" in inspect.signature(runner).parameters:
        kwargs["live_dir"] = (
            str((job or {}).get("live_dir", "") or "")
            if job is not None and live_enabled_for_job(job)
            else None
        )
    return runner(**kwargs)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    mode = str(job.get("mode", "mock")).strip().lower()
    step = job.get("step", {})
    outputs = normalize_outputs(job.get("outputs", []))
    sample_id = job.get("sample_id")
    if not isinstance(step, dict):
        raise GamryWorkerError("job.step must be an object.")
    if sample_id is not None:
        sample_id = str(sample_id)

    live_started = start_live_for_job(job, step, sample_id)
    emit = event_emitter(job, step, sample_id)
    metadata = default_trial_metadata(trial_settings(job))
    emit("trial_started", step_name=str(step.get("name", "") or ""))
    try:
        if mode == "mock":
            metadata, effective_step = prepare_mock_trial(job, step, emit)
        elif mode in {"real", "toolkitpy", "gamry"}:
            technique = str(step.get("technique", "")).strip().lower()
            if technique not in REAL_RUNNER_MODULES:
                raise GamryWorkerError(f"unsupported real Gamry technique: {technique}")
            metadata, effective_step = prepare_real_trial_for_job(job, step, emit)
        else:
            raise GamryWorkerError(f"unsupported Gamry mode: {mode}")

        if not metadata.get("ru_validation_passed", False):
            metadata["trial_status"] = "skipped"
            metadata["completed_at"] = metadata.get("completed_at") or trial_utc_now()
            result = {"ok": True, "skipped": True, "reason": metadata.get("skip_reason")}
            if live_started:
                try:
                    update_live_status(job["live_dir"], active=False, status="skipped", finished_at=trial_utc_now())
                except Exception:
                    pass
        else:
            supports_ir = technique_supports_positive_feedback(effective_step.get("technique"))
            emit(
                "ir_compensation_configured" if supports_ir else "ir_compensation_skipped",
                compensation_fraction=metadata["compensation_fraction"],
                ru_selected_ohm=metadata["ru_selected_ohm"],
                ru_applied_ohm=metadata["ru_applied_ohm"],
                reason=None if supports_ir else "Technique does not use positive-feedback compensation",
            )
            emit("measurement_started", step_name=str(step.get("name", "") or ""))
            if mode == "mock":
                effective_job = dict(job)
                effective_job["step"] = effective_step
                result = dispatch_mock_step(effective_job, effective_step, outputs, sample_id)
            else:
                result = dispatch_real_step(effective_step, outputs, sample_id=sample_id, job=job)
            metadata["ir_compensation_enabled"] = bool(
                supports_ir and result.get("ir_compensation_enabled", True)
            )
            metadata["trial_status"] = "completed"
            metadata["completed_at"] = trial_utc_now()
            emit("measurement_completed", outputs=outputs)
            emit("ir_compensation_disabled")
            emit("gamry_settings_reset", ir_compensation="disabled", cell="off")
            emit("trial_completed")
    except Exception as exc:
        metadata["trial_status"] = "failed"
        metadata["completed_at"] = trial_utc_now()
        metadata["skip_reason"] = str(exc)
        emit("trial_failed", reason=str(exc), critical=isinstance(exc, CriticalHardwareError))
        try:
            exc.trial_metadata = metadata
        except Exception:
            pass
        if live_started:
            try:
                fail_live_stream(job["live_dir"], str(exc))
            except Exception:
                pass
        if isinstance(exc, GamryWorkerError):
            raise
        raise

    if live_started and metadata.get("trial_status") != "skipped":
        try:
            finish_live_stream(job["live_dir"])
        except Exception:
            pass
    live_status = read_live_status(job["live_dir"]) if live_started else None

    return {
        "ok": True,
        "job_id": job.get("job_id"),
        "mode": mode,
        "sample_id": sample_id,
        "technique": str(step.get("technique", "")).strip().lower(),
        "step_name": str(step.get("name", "")).strip(),
        "outputs": outputs if metadata.get("trial_status") != "skipped" else [],
        "result": result,
        "trial_metadata": metadata,
        "live_stream": live_status,
        "finished_at": utc_now(),
    }


def error_payload(job: dict[str, Any] | None, exc: BaseException) -> dict[str, Any]:
    if isinstance(job, dict) and live_enabled_for_job(job):
        try:
            fail_live_stream(job["live_dir"], str(exc))
        except Exception:
            pass
    return {
        "ok": False,
        "job_id": job.get("job_id") if isinstance(job, dict) else None,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "trial_metadata": getattr(exc, "trial_metadata", None),
        "traceback": traceback.format_exc(),
        "finished_at": utc_now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--job")
    source.add_argument("--probe", action="store_true")
    parser.add_argument("--result", required=False)
    args = parser.parse_args()
    job = None
    result_path = args.result
    try:
        if args.probe:
            try:
                from gamry_worker.device import probe_toolkitpy
            except ModuleNotFoundError:
                from device import probe_toolkitpy
            result = probe_toolkitpy()
        else:
            job = read_json(args.job)
            job["_job_path"] = str(Path(args.job))
            if not result_path:
                result_path = job.get("result_path")
            if result_path:
                job["result_path"] = str(result_path)
            result = run_job(job)
        if result_path:
            write_json(result_path, result)
        else:
            print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        result = error_payload(job, exc)
        if result_path:
            write_json(result_path, result)
        else:
            print(json.dumps(result, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
