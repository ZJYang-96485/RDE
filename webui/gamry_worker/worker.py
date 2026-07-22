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
        fail_live_stream,
        finish_live_stream,
        initialize_live_stream,
        read_live_status,
    )
except ModuleNotFoundError:
    from device import configured_step
    from live_writer import (
        fail_live_stream,
        finish_live_stream,
        initialize_live_stream,
        read_live_status,
    )


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
    try:
        if mode == "mock":
            result = dispatch_mock_step(job, step, outputs, sample_id)
        elif mode in {"real", "toolkitpy", "gamry"}:
            result = dispatch_real_step(step, outputs, sample_id=sample_id, job=job)
        else:
            raise GamryWorkerError(f"unsupported Gamry mode: {mode}")
    except Exception as exc:
        if live_started:
            fail_live_stream(job["live_dir"], str(exc))
        if isinstance(exc, GamryWorkerError):
            raise
        raise

    if live_started:
        finish_live_stream(job["live_dir"])
    live_status = read_live_status(job["live_dir"]) if live_started else None

    return {
        "ok": True,
        "job_id": job.get("job_id"),
        "mode": mode,
        "sample_id": sample_id,
        "technique": str(step.get("technique", "")).strip().lower(),
        "step_name": str(step.get("name", "")).strip(),
        "outputs": outputs,
        "result": result,
        "live_stream": live_status,
        "finished_at": utc_now(),
    }


def error_payload(job: dict[str, Any] | None, exc: BaseException) -> dict[str, Any]:
    if isinstance(job, dict) and live_enabled_for_job(job):
        fail_live_stream(job["live_dir"], str(exc))
    return {
        "ok": False,
        "job_id": job.get("job_id") if isinstance(job, dict) else None,
        "error": str(exc),
        "error_type": type(exc).__name__,
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
