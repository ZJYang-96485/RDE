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
    technique = str(step.get("technique", "")).strip().lower()

    if technique == "ca_staircase":
        return _run_ca_staircase(
            step=step,
            outputs=outputs,
            sample_id=sample_id,
        )

    return _run_ca(
        step=step,
        outputs=outputs,
        sample_id=sample_id,
    )