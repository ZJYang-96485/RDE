"""Normalized live-point adapters.

The mock path supplies normalized dictionaries today.  The TODOs in these
small functions are the only place that should need ToolkitPy row/field
mapping once acquisition rows are available on the Windows Gamry machine.
No ToolkitPy import or unverified column index belongs here.
"""

from __future__ import annotations

from typing import Any


def _copy_required(raw_data: Any, fields: tuple[str, ...], technique: str) -> dict[str, Any]:
    if not isinstance(raw_data, dict):
        raise ValueError(f"{technique} live data must be a normalized dictionary")
    point = {field: raw_data[field] for field in fields if field in raw_data}
    missing = [field for field in fields if field not in point]
    if missing:
        raise ValueError(f"{technique} live data is missing: {', '.join(missing)}")
    point["technique"] = technique
    return point


def normalize_ocp_acq_rows(raw_data: Any) -> dict[str, Any]:
    # TODO(Windows ToolkitPy): map the verified OcvCurve acquisition row here.
    return _copy_required(raw_data, ("t_s", "e_v"), "ocp")


def normalize_ca_acq_rows(raw_data: Any) -> dict[str, Any]:
    # TODO(Windows ToolkitPy): map the verified ChronoCurve acquisition row here.
    return _copy_required(raw_data, ("t_s", "e_v", "i_a"), "ca")


def normalize_cv_acq_rows(raw_data: Any) -> dict[str, Any]:
    # TODO(Windows ToolkitPy): map the verified RcvCurve acquisition row here.
    return _copy_required(raw_data, ("t_s", "e_v", "i_a"), "cv")


def normalize_lsv_acq_rows(raw_data: Any) -> dict[str, Any]:
    # TODO(Windows ToolkitPy): map the verified LSV/RcvCurve acquisition row here.
    return _copy_required(raw_data, ("t_s", "e_v", "i_a"), "lsv")


def normalize_eis_point(raw_data: Any) -> dict[str, Any]:
    # TODO(Windows ToolkitPy): map the verified ReadZ/ZCurve point here.
    return _copy_required(
        raw_data,
        ("freq_hz", "zreal_ohm", "zimag_ohm", "zmod_ohm", "phase_deg"),
        "eis",
    )
