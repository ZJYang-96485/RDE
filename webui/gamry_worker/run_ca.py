from __future__ import annotations

from typing import Any

try:
    from gamry_worker.mock_gamry import run_ca as _run_ca
    from gamry_worker.mock_gamry import run_ca_staircase as _run_ca_staircase
except ModuleNotFoundError:
    from mock_gamry import run_ca as _run_ca
    from mock_gamry import run_ca_staircase as _run_ca_staircase


def run(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("outputs must contain at least one path.")

    technique = str(step.get("technique", "")).strip().lower()

    if technique == "ca_staircase":
        start_voltage = float(step.get("start_voltage_v", 0))
        step_voltage = float(step.get("step_voltage_v", 0))
        output_records = [
            {
                "index": index,
                "path": output_path,
                "voltage_v": start_voltage + (index - 1) * step_voltage,
            }
            for index, output_path in enumerate(outputs, start=1)
        ]

        return _run_ca_staircase(
            step=step,
            outputs=output_records,
        )

    return _run_ca(
        step=step,
        output_path=outputs[0],
    )
