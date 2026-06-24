from __future__ import annotations

from typing import Any

try:
    from gamry_worker.mock_gamry import run_eis as _run_eis
except ModuleNotFoundError:
    from mock_gamry import run_eis as _run_eis


def run(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
) -> dict[str, Any]:
    return _run_eis(
        step=step,
        outputs=outputs,
        sample_id=sample_id,
    )