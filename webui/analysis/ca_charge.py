"""CA cumulative signed-charge live decoration and authoritative final analysis."""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from analysis.integration import StreamingTrapezoidAccumulator
from analysis.registry import ANALYSIS_DEFINITIONS, analysis_enabled


ANALYSIS_TYPE = "cumulative_charge"
ANALYSIS_VERSION = str(ANALYSIS_DEFINITIONS[ANALYSIS_TYPE]["analysis_version"])


class CaChargeAnalysisError(RuntimeError):
    pass


def cumulative_charge_enabled(step: Dict[str, Any]) -> bool:
    technique = str(step.get("technique", "") or "").strip().lower()
    return technique in {"ca", "ca_staircase"} and analysis_enabled(
        step, ANALYSIS_TYPE
    )


class LiveChargeDecorator:
    """Decorate normalized CA live points with per-trial charge state."""

    def __init__(self, segment_id: int = 1) -> None:
        self.accumulator = StreamingTrapezoidAccumulator(deduplicate=True)
        self.segment_id = max(1, int(segment_id))

    def __call__(self, point: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        normalized = dict(point)
        charge = self.accumulator.add_point(
            normalized.get("t_s"), normalized.get("i_a")
        )
        if not self.accumulator.last_point_accepted:
            return None
        normalized.update(
            {
                "q_live_c": charge,
                "q_live_integrated_intervals": self.accumulator.integrated_interval_count,
                "q_live_skipped_intervals": self.accumulator.skipped_interval_count,
                "q_live_status": (
                    "warning" if self.accumulator.warnings else "live_estimate"
                ),
                "q_live_warnings": self.accumulator.warnings,
                "q_live_segment": self.segment_id,
            }
        )
        return normalized

    def status_fields(self) -> Dict[str, Any]:
        result = self.accumulator.result()
        return {
            "charge_analysis_enabled": True,
            "charge_analysis_status": (
                "warning" if result.warnings else "live_estimate"
            ),
            "charge_live_c": result.final_integral,
            "charge_integrated_intervals": result.integrated_interval_count,
            "charge_skipped_intervals": result.skipped_interval_count,
            "charge_analysis_warnings": result.warnings,
            "charge_analysis_source": "live estimate",
            "charge_analysis_segment": self.segment_id,
        }


def artifact_paths(raw_dta: str | Path) -> Dict[str, Path]:
    raw_path = Path(raw_dta)
    base = raw_path.with_suffix("")
    return {
        "series_csv": base.with_name(base.name + "_charge_analysis.csv"),
        "summary_json": base.with_name(base.name + "_charge_analysis.json"),
    }


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(str(value).strip().replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _column_indexes(columns: List[str]) -> Dict[str, int]:
    # Import here so the pure integration module stays independent from the
    # repository's DTA parsing layer.
    from workflow.dta_viewer import _canonical_column

    indexes: Dict[str, int] = {}
    for index, column in enumerate(columns):
        canonical = _canonical_column(column)
        if canonical and canonical not in indexes:
            indexes[canonical] = index
    missing = [
        name for name in ("time", "potential", "current") if name not in indexes
    ]
    if missing:
        raise CaChargeAnalysisError(
            "CA charge analysis requires DTA time, potential, and current columns; missing: "
            + ", ".join(missing)
        )
    return indexes


def _atomic_csv(path: Path, headers: List[str], rows: List[List[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(headers)
            for row in rows:
                writer.writerow([format(value, ".17g") for value in row])
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def run_ca_charge_analysis(raw_dta: str | Path) -> Dict[str, Any]:
    """Recompute total signed CA charge directly from the completed DTA."""

    from workflow.dta_csv import extract_dta_table

    raw_path = Path(raw_dta).resolve()
    if not raw_path.is_file():
        raise CaChargeAnalysisError(f"Completed CA DTA does not exist: {raw_path}")

    table = extract_dta_table(raw_path)
    indexes = _column_indexes(list(table["columns"]))
    accumulator = StreamingTrapezoidAccumulator(deduplicate=False)
    series_rows: List[List[float]] = []
    potential_index = indexes.get("potential")

    for raw_row in table["rows"]:
        time_value = raw_row[indexes["time"]] if indexes["time"] < len(raw_row) else None
        current_value = raw_row[indexes["current"]] if indexes["current"] < len(raw_row) else None
        charge = accumulator.add_point(time_value, current_value)
        if not accumulator.last_point_accepted:
            continue
        t_s = _finite(time_value)
        i_a = _finite(current_value)
        potential_v = (
            _finite(raw_row[potential_index])
            if potential_index is not None and potential_index < len(raw_row)
            else None
        )
        if t_s is None or i_a is None or potential_v is None or not math.isfinite(charge):
            continue
        series_rows.append([t_s, potential_v, i_a, charge])

    result = accumulator.result()
    if not result.time_s:
        raise CaChargeAnalysisError("Completed CA DTA contains no finite time/current points.")
    if not series_rows:
        raise CaChargeAnalysisError(
            "Completed CA DTA contains no finite time/potential/current rows."
        )

    paths = artifact_paths(raw_path)
    _atomic_csv(
        paths["series_csv"],
        ["time_s", "potential_v", "current_a", "cumulative_charge_c"],
        series_rows,
    )
    summary: Dict[str, Any] = {
        "analysis_type": ANALYSIS_TYPE,
        "analysis_version": ANALYSIS_VERSION,
        "analysis_status": "complete",
        "label": "CA Cumulative Charge",
        "technique": "ca",
        "integration_method": "composite_trapezoidal",
        "current_sign_convention": "gamry_anodic_positive",
        "source": {
            "type": "dta",
            "label": "Recomputed from DTA",
            "file": raw_path.name,
            "source_points": result.source_point_count,
        },
        "result": {
            "final_signed_charge_c": result.final_integral,
            "duration_s": result.integrated_duration_s,
            "integrated_intervals": result.integrated_interval_count,
            "skipped_intervals": result.skipped_interval_count,
            "series_points": len(series_rows),
        },
        "quality": {
            "time_monotonic": result.time_monotonic,
            "finite_data": True,
            "warnings": result.warnings,
        },
        "artifacts": {
            "series_csv": paths["series_csv"].name,
        },
        "limitations": {
            "background_subtracted": False,
            "smoothing_applied": False,
            "manual_window_applied": False,
            "faradaic_mass_conversion": False,
        },
    }
    _atomic_json(paths["summary_json"], summary)
    return {
        "ok": True,
        "analysis_type": ANALYSIS_TYPE,
        "analysis_version": ANALYSIS_VERSION,
        "source": "dta",
        "raw_dta": str(raw_path),
        "summary": summary,
        "artifacts": {key: str(value) for key, value in paths.items()},
    }


def load_charge_series(path: str | Path, max_points: int = 5000) -> Dict[str, Any]:
    csv_path = Path(path)
    points: List[Dict[str, float]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"time_s", "cumulative_charge_c"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise CaChargeAnalysisError("Charge-analysis CSV is missing required columns.")
        for row in reader:
            x = _finite(row.get("time_s"))
            y = _finite(row.get("cumulative_charge_c"))
            if x is not None and y is not None:
                points.append({"x": x, "y": y})
    original_count = len(points)
    limit = max(1, int(max_points))
    if original_count > limit:
        if limit == 1:
            points = [points[-1]]
        else:
            last = original_count - 1
            points = [points[round(index * last / (limit - 1))] for index in range(limit)]
    maximum = max([abs(point["y"]) for point in points] or [0.0])
    if maximum == 0 or maximum >= 1:
        charge_unit = "C"
        charge_scale = 1.0
    elif maximum >= 0.001:
        charge_unit = "mC"
        charge_scale = 1000.0
    else:
        charge_unit = "µC"
        charge_scale = 1_000_000.0
    display_points = [
        {"x": point["x"], "y": point["y"] * charge_scale}
        for point in points
    ]
    return {
        "technique_guess": "ca_cumulative_charge",
        "x_label": "Time (s)",
        "y_label": f"Cumulative signed charge ({charge_unit})",
        "charge_unit": charge_unit,
        "points": display_points,
        "point_count": len(display_points),
        "original_point_count": original_count,
        "decimated": original_count > len(display_points),
        "source_label": "Recomputed from DTA",
    }
