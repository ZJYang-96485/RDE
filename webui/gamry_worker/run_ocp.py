from __future__ import annotations

from typing import Any

try:
    from gamry_worker.mock_gamry import run_ocp as _run_ocp
except ModuleNotFoundError:
    from mock_gamry import run_ocp as _run_ocp


def run(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
) -> dict[str, Any]:
    return _run_ocp(
        step=step,
        outputs=outputs,
        sample_id=sample_id,
    )