from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from gamry_worker.live_writer import (
        fail_live_stream,
        finish_live_stream,
        initialize_live_stream,
    )
except ModuleNotFoundError:
    from live_writer import fail_live_stream, finish_live_stream, initialize_live_stream


class GamryWorkerError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise GamryWorkerError("job file must contain a JSON object.")

    return payload


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def normalize_outputs(outputs: Any) -> list[str]:
    if isinstance(outputs, str):
        return [outputs]

    if not isinstance(outputs, list):
        raise GamryWorkerError("outputs must be a list of file paths.")

    normalized = []

    for output in outputs:
        output_text = str(output).strip()

        if output_text:
            normalized.append(output_text)

    if not normalized:
        raise GamryWorkerError("at least one output path is required.")

    return normalized


def dispatch_mock_step(
    job: dict[str, Any],
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None,
) -> dict[str, Any]:
    # Imported only for mock jobs so a Mac does not need ToolkitPy just to
    # start the Flask app or run mock experiments.
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
    # The existing real path is an external Windows worker boundary. It keeps
    # ToolkitPy out of the Mac process and leaves the small run_* modules
    # available for later verified ToolkitPy adapter insertion.
    try:
        from gamry_worker.real_gamry import run as run_real_gamry
    except ModuleNotFoundError:
        from real_gamry import run as run_real_gamry

    return run_real_gamry(
        job=job or {},
        step=step,
        outputs=outputs,
        sample_id=sample_id,
    )


def live_enabled_for_job(job: dict[str, Any]) -> bool:
    return bool(job.get("live_enabled", True)) and bool(str(job.get("live_dir", "")).strip())


def live_technique(step: dict[str, Any]) -> str:
    technique = str(step.get("technique", "")).strip().lower()
    return "ca" if technique == "ca_staircase" else technique


def start_live_for_job(job: dict[str, Any], step: dict[str, Any], sample_id: str | None) -> bool:
    if not live_enabled_for_job(job):
        return False

    initialize_live_stream(
        job["live_dir"],
        run_id=str(job.get("run_id") or "") or None,
        sample_id=sample_id,
        sample_label=str(job.get("sample_label") or "") or None,
        protocol_name=str(job.get("protocol_name") or "") or None,
        step_name=str(step.get("name") or "") or None,
        technique=live_technique(step),
    )
    return True


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
            result = dispatch_mock_step(
                job=job,
                step=step,
                outputs=outputs,
                sample_id=sample_id,
            )
        elif mode in {"real", "toolkitpy", "gamry"}:
            result = dispatch_real_step(
                job=job,
                step=step,
                outputs=outputs,
                sample_id=sample_id,
            )
        else:
            raise GamryWorkerError(f"unsupported Gamry mode: {mode}")
    except Exception as exc:
        if live_started:
            fail_live_stream(job["live_dir"], str(exc))
        raise

    if live_started:
        finish_live_stream(job["live_dir"])

    return {
        "ok": True,
        "job_id": job.get("job_id"),
        "mode": mode,
        "sample_id": sample_id,
        "technique": str(step.get("technique", "")).strip().lower(),
        "step_name": str(step.get("name", "")).strip(),
        "outputs": outputs,
        "result": result,
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
    parser.add_argument("--job", required=True)
    parser.add_argument("--result", required=False)
    args = parser.parse_args()

    job = None
    result_path = args.result

    try:
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
