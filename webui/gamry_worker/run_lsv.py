from __future__ import annotations

from typing import Any

try:
    from gamry_worker.mock_gamry import run_lsv as _run_lsv
except ModuleNotFoundError:
    from mock_gamry import run_lsv as _run_lsv


def run(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("outputs must contain at least one path.")

    return _run_lsv(
        step=step,
        output_path=outputs[0],
    )
