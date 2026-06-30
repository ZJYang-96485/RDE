from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def dispatch_mock_step(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None,
) -> dict[str, Any]:
    from gamry_worker import mock_gamry

    technique = str(step.get("technique", "")).strip().lower()

    if not outputs:
        raise ValueError("outputs must contain at least one path.")

    if technique == "ocp":
        return mock_gamry.run_ocp(step=step, output_path=outputs[0])

    if technique == "ca":
        return mock_gamry.run_ca(step=step, output_path=outputs[0])

    if technique == "ca_staircase":
        start_voltage = float(step.get("start_voltage_v", 0.0))
        step_voltage = float(step.get("step_voltage_v", 0.0))

        output_records = [
            {
                "index": index,
                "path": output_path,
                "voltage_v": start_voltage + (index - 1) * step_voltage,
            }
            for index, output_path in enumerate(outputs, start=1)
        ]

        return mock_gamry.run_ca_staircase(step=step, outputs=output_records)

    if technique == "cv":
        return mock_gamry.run_cv(step=step, output_path=outputs[0])

    if technique == "lsv":
        return mock_gamry.run_lsv(step=step, output_path=outputs[0])

    if technique == "eis":
        return mock_gamry.run_eis(step=step, output_path=outputs[0])

    raise ValueError(f"Unsupported mock Gamry technique: {technique}")


def dispatch_real_step(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None,
) -> dict[str, Any]:
    technique = str(step.get("technique", "")).strip().lower()

    if technique in {"ca", "ca_staircase"}:
        from gamry_worker.run_ca import run as run_ca

        return run_ca(
            step=step,
            outputs=outputs,
            sample_id=sample_id,
        )

    raise NotImplementedError(
        f"Real Gamry mode currently supports only 'ca' and 'ca_staircase'. "
        f"Requested technique: '{technique}'."
    )


def dispatch_job(job: dict[str, Any]) -> dict[str, Any]:
    mode = str(job.get("mode", "mock")).strip().lower()
    step = job.get("step") or {}
    outputs = [str(path) for path in job.get("outputs", [])]
    sample_id = job.get("sample_id")

    if mode == "mock":
        result = dispatch_mock_step(
            step=step,
            outputs=outputs,
            sample_id=sample_id,
        )
    elif mode == "real":
        result = dispatch_real_step(
            step=step,
            outputs=outputs,
            sample_id=sample_id,
        )
    else:
        raise ValueError(f"Unsupported Gamry worker mode: {mode}")

    if not isinstance(result, dict):
        result = {"result": result}

    result.setdefault("ok", True)
    result.setdefault("mode", mode)
    result.setdefault("sample_id", sample_id)

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()

    job_path = Path(args.job)
    result_path = Path(args.result)

    try:
        job = read_json(job_path)
        result = dispatch_job(job)
        write_json(result_path, result)
        return 0

    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        write_json(result_path, result)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())