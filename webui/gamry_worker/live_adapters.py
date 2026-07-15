"""Map verified local ToolkitPy acquisition fields into browser live points."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

try:
    from gamry_worker.live_writer import append_live_point, append_live_points, update_live_status
except ModuleNotFoundError:
    from live_writer import append_live_point, append_live_points, update_live_status


def _value(raw_data: Any, source_name: str) -> float:
    try:
        value = raw_data[source_name]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"ToolkitPy live row is missing '{source_name}'") from exc
    if hasattr(value, "item"):
        value = value.item()
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"ToolkitPy live field '{source_name}' is not finite")
    return result


def _normalized(
    raw_data: Any,
    technique: str,
    mapping: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    # Mock data already uses normalized browser field names. Real ToolkitPy
    # numpy rows use the locally verified names on the right side of mapping.
    point: dict[str, Any] = {"technique": technique}
    for normalized_name, toolkit_name in mapping:
        source_name = normalized_name if isinstance(raw_data, dict) and normalized_name in raw_data else toolkit_name
        point[normalized_name] = _value(raw_data, source_name)
    return point


def normalize_ocp_acq_rows(raw_data: Any) -> dict[str, Any]:
    # Installed OcvCurve: time, vf.
    return _normalized(raw_data, "ocp", (("t_s", "time"), ("e_v", "vf")))


def normalize_ca_acq_rows(raw_data: Any) -> dict[str, Any]:
    # Installed ChronoCurve: time, vf, im.
    return _normalized(raw_data, "ca", (("t_s", "time"), ("e_v", "vf"), ("i_a", "im")))


def normalize_cv_acq_rows(raw_data: Any) -> dict[str, Any]:
    # Installed RcvCurve: time, vf, im.
    return _normalized(raw_data, "cv", (("t_s", "time"), ("e_v", "vf"), ("i_a", "im")))


def normalize_lsv_acq_rows(raw_data: Any) -> dict[str, Any]:
    return _normalized(raw_data, "lsv", (("t_s", "time"), ("e_v", "vf"), ("i_a", "im")))


def normalize_cp_acq_rows(raw_data: Any) -> dict[str, Any]:
    # Installed ChronoCurve in GSTATMODE: time, vf, im.
    return _normalized(raw_data, "cp", (("t_s", "time"), ("e_v", "vf"), ("i_a", "im")))


def normalize_cc_charge_acq_rows(raw_data: Any) -> dict[str, Any]:
    # Installed PwrCurve: time, vf, im.
    return _normalized(raw_data, "cc_charge", (("t_s", "time"), ("e_v", "vf"), ("i_a", "im")))


def normalize_cc_discharge_acq_rows(raw_data: Any) -> dict[str, Any]:
    return _normalized(raw_data, "cc_discharge", (("t_s", "time"), ("e_v", "vf"), ("i_a", "im")))


def normalize_eis_point(raw_data: Any) -> dict[str, Any]:
    # Installed ZCurve: zfreq, zreal, zimag, zmod, zphz.
    return _normalized(
        raw_data,
        "eis",
        (
            ("freq_hz", "zfreq"),
            ("zreal_ohm", "zreal"),
            ("zimag_ohm", "zimag"),
            ("zmod_ohm", "zmod"),
            ("phase_deg", "zphz"),
        ),
    )


def normalize_geis_point(raw_data: Any) -> dict[str, Any]:
    point = normalize_eis_point(raw_data)
    point["technique"] = "geis"
    return point


class LiveCurveEmitter:
    """Best-effort stream: a plot failure never interrupts the measurement."""

    def __init__(
        self,
        live_dir: str | Path | None,
        normalizer: Callable[[Any], dict[str, Any]],
    ) -> None:
        self.live_dir = Path(live_dir) if live_dir else None
        self.normalizer = normalizer
        self.emitted_count = 0
        self.errors: list[str] = []

    def _record_error(self, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if message not in self.errors:
            self.errors.append(message)
        if self.live_dir is not None:
            try:
                update_live_status(self.live_dir, stream_error=message)
            except Exception:
                pass

    def emit_new(self, acquisition_rows: Any) -> int:
        if self.live_dir is None or acquisition_rows is None:
            return 0
        try:
            count = len(acquisition_rows)
            if count < self.emitted_count:
                # ToolkitPy ring-buffer wrap: emit the currently available rows.
                self.emitted_count = 0
            rows = [self.normalizer(acquisition_rows[index]) for index in range(self.emitted_count, count)]
            if rows:
                append_live_points(self.live_dir, rows)
            self.emitted_count = count
            return len(rows)
        except Exception as exc:
            self._record_error(exc)
            return 0

    def emit_point(self, acquisition_row: Any) -> bool:
        if self.live_dir is None:
            return False
        try:
            append_live_point(self.live_dir, self.normalizer(acquisition_row))
            self.emitted_count += 1
            return True
        except Exception as exc:
            self._record_error(exc)
            return False

    def result_fields(self) -> dict[str, Any]:
        return {
            "live_points_emitted": self.emitted_count,
            "live_stream_errors": list(self.errors),
        }
