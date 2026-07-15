from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None,
) -> dict[str, Any]:
    try:
        from gamry_worker.mock_gamry import run_step
    except ModuleNotFoundError:
        from mock_gamry import run_step

    output_records = [
        {
            "path": output,
            "index": index,
        }
        for index, output in enumerate(outputs, start=1)
    ]
    result = run_step(step=step, outputs=output_records)
    result["sample_id"] = sample_id
    return result


def real_runner_for_technique(technique: str):
    modules = {
        "ocp": "run_ocp",
        "ca": "run_ca",
        "ca_staircase": "run_ca",
        "lsv": "run_lsv",
        "cv": "run_cv",
        "eis": "run_eis",
    }
    module_name = modules.get(technique)

    if module_name is None:
        raise GamryWorkerError(
            "Real Gamry mode supports ocp, ca, ca_staircase, lsv, cv, and eis. "
            f"Requested technique: '{technique}'."
        )

    try:
        module = importlib.import_module(f"gamry_worker.{module_name}")
    except ModuleNotFoundError as exc:
        if exc.name not in {"gamry_worker", f"gamry_worker.{module_name}"}:
            raise

        module = importlib.import_module(module_name)

    return module.run

def dispatch_real_step(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    technique = str(step.get("technique", "")).strip().lower()
    gamry_config = job.get("gamry", {}) if isinstance(job, dict) else {}

    try:
        from gamry_worker.device import configured_step
    except ModuleNotFoundError:
        from device import configured_step

    runner = real_runner_for_technique(technique)
    return runner(
        step=configured_step(step, gamry_config),
        outputs=outputs,
        sample_id=sample_id,
    )


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    mode = str(job.get("mode", "mock")).strip().lower()
    step = job.get("step", {})
    outputs = normalize_outputs(job.get("outputs", []))
    sample_id = job.get("sample_id")

    if not isinstance(step, dict):
        raise GamryWorkerError("job.step must be an object.")

    if sample_id is not None:
        sample_id = str(sample_id)

    if mode == "mock":
        result = dispatch_mock_step(
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
