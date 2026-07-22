from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.dta_viewer import parse_dta_file
from workflow.simple_png_plot import render_xy_plot


class LevichAnalysisError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")


def artifact_paths(raw_dta: str | Path) -> dict[str, Path]:
    raw_path = Path(raw_dta)
    stem = raw_path.stem
    return {
        "summary_csv": raw_path.with_name(f"{stem}_summary.csv"),
        "analysis_json": raw_path.with_name(f"{stem}_analysis.json"),
        "trace_plot_png": raw_path.with_name(f"{stem}_trace_with_rpm.png"),
        "levich_plot_png": raw_path.with_name(f"{stem}_levich_plot.png"),
        "kl_plot_png": raw_path.with_name(f"{stem}_kl_plot.png"),
    }


def linear_fit(points: list[tuple[float, float]]) -> dict[str, float] | None:
    if len(points) < 2:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    if denominator <= 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    intercept = y_mean - slope * x_mean
    residual = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    total = sum((y - y_mean) ** 2 for y in ys)
    r_squared = 1.0 - residual / total if total > 0 else 1.0
    return {
        "slope": slope,
        "intercept": intercept,
        "r_squared": r_squared,
        "point_count": float(len(points)),
    }


def fit_line_points(
    fit: dict[str, float] | None,
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    if fit is None or not points:
        return []
    low = min(point[0] for point in points)
    high = max(point[0] for point in points)
    return [
        (low, fit["slope"] * low + fit["intercept"]),
        (high, fit["slope"] * high + fit["intercept"]),
    ]


def decimate(points: list[tuple[float, float]], limit: int = 6000) -> list[tuple[float, float]]:
    if len(points) <= limit:
        return points
    stride = int(math.ceil(len(points) / float(limit)))
    result = points[::stride]
    if result[-1] != points[-1]:
        result.append(points[-1])
    return result


def load_schedule(path: str | Path) -> dict[str, Any]:
    schedule_path = Path(path)
    try:
        payload = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LevichAnalysisError(f"Unable to read RPM schedule: {exc}") from exc
    if not isinstance(payload, dict):
        raise LevichAnalysisError("RPM schedule must contain a JSON object.")
    if payload.get("rpm_source") != "commanded":
        raise LevichAnalysisError("Only commanded-RPM schedules are supported.")
    if payload.get("stabilization_mode") != "fixed delay":
        raise LevichAnalysisError("Only fixed-delay stabilization schedules are supported.")
    if not isinstance(payload.get("rpm_points"), list) or len(payload["rpm_points"]) < 2:
        raise LevichAnalysisError("RPM schedule must contain at least two collection windows.")
    return payload


def analyze_windows(
    trace: list[tuple[float, float]],
    schedule: dict[str, Any],
    area_cm2: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in schedule["rpm_points"]:
        start = float(window["collection_start_s"])
        end = float(window["collection_end_s"])
        currents = [current for elapsed, current in trace if start <= elapsed <= end]
        if not currents:
            raise LevichAnalysisError(
                f"No CA points were found for commanded RPM {window.get('commanded_rpm')} "
                f"between {start:g} and {end:g} seconds."
            )
        mean_current = sum(currents) / len(currents)
        variance = sum((value - mean_current) ** 2 for value in currents) / len(currents)
        rpm = int(window["commanded_rpm"])
        angular_velocity = 2.0 * math.pi * rpm / 60.0
        mean_current_density = mean_current / area_cm2
        rows.append(
            {
                "index": int(window.get("index", len(rows) + 1)),
                "commanded_rpm": rpm,
                "rpm_source": "commanded",
                "stabilization_mode": "fixed delay",
                "collection_start_s": start,
                "collection_end_s": end,
                "point_count": len(currents),
                "mean_current_a": mean_current,
                "current_std_a": math.sqrt(max(0.0, variance)),
                "mean_current_density_a_cm2": mean_current_density,
                "angular_velocity_rad_s": angular_velocity,
                "sqrt_rpm": math.sqrt(rpm),
                "inverse_sqrt_rpm": 1.0 / math.sqrt(rpm),
                "sqrt_angular_velocity_rad_s_half": math.sqrt(angular_velocity),
                "inverse_sqrt_angular_velocity_s_half_rad_minus_half": (
                    1.0 / math.sqrt(angular_velocity)
                ),
                "inverse_mean_current_per_a": (
                    1.0 / mean_current if abs(mean_current) > 1e-30 else None
                ),
                "inverse_mean_current_density_cm2_per_a": (
                    1.0 / mean_current_density
                    if abs(mean_current_density) > 1e-30
                    else None
                ),
            }
        )
    return rows


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "index",
        "commanded_rpm",
        "rpm_source",
        "stabilization_mode",
        "collection_start_s",
        "collection_end_s",
        "point_count",
        "mean_current_a",
        "current_std_a",
        "mean_current_density_a_cm2",
        "angular_velocity_rad_s",
        "sqrt_rpm",
        "inverse_sqrt_rpm",
        "sqrt_angular_velocity_rad_s_half",
        "inverse_sqrt_angular_velocity_s_half_rad_minus_half",
        "inverse_mean_current_per_a",
        "inverse_mean_current_density_cm2_per_a",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_levich_analysis(
    raw_dta: str | Path,
    rpm_schedule_json: str | Path,
    *,
    area_cm2: float = 1.0,
) -> dict[str, Any]:
    raw_path = Path(raw_dta)
    schedule_path = Path(rpm_schedule_json)
    if not raw_path.is_file():
        raise LevichAnalysisError(f"Raw Levich CA DTA does not exist: {raw_path}")
    if not math.isfinite(float(area_cm2)) or float(area_cm2) <= 0:
        raise LevichAnalysisError("Electrode area must be greater than zero.")

    schedule = load_schedule(schedule_path)
    parsed = parse_dta_file(
        raw_path,
        max_points=2_000_000,
        allow_analysis_point_limit=True,
    )
    trace = [
        (float(point["x"]), float(point["y"]))
        for point in parsed["points"]
        if math.isfinite(float(point["x"])) and math.isfinite(float(point["y"]))
    ]
    if len(trace) < 2:
        raise LevichAnalysisError("Raw Levich CA DTA contains fewer than two usable points.")

    rows = analyze_windows(trace, schedule, float(area_cm2))
    levich_points = [
        (
            row["sqrt_angular_velocity_rad_s_half"],
            row["mean_current_density_a_cm2"],
        )
        for row in rows
    ]
    kl_points = [
        (
            row["inverse_sqrt_angular_velocity_s_half_rad_minus_half"],
            row["inverse_mean_current_density_cm2_per_a"],
        )
        for row in rows
        if row["inverse_mean_current_density_cm2_per_a"] is not None
    ]
    if not kl_points:
        raise LevichAnalysisError(
            "Koutecky-Levich analysis requires at least one non-zero mean current density."
        )
    levich_fit = linear_fit(levich_points)
    kl_fit = linear_fit(kl_points)
    paths = artifact_paths(raw_path)

    write_summary_csv(paths["summary_csv"], rows)
    bands = [
        {
            "start": row["collection_start_s"],
            "end": row["collection_end_s"],
            "label": f"{row['commanded_rpm']} RPM",
        }
        for row in rows
    ]
    render_xy_plot(
        paths["trace_plot_png"],
        title="LEVICH CA RPM SWEEP - CURRENT VS TIME",
        x_label="TIME (S)",
        y_label="CURRENT (A)",
        points=decimate(trace),
        bands=bands,
    )
    render_xy_plot(
        paths["levich_plot_png"],
        title="LEVICH PLOT",
        x_label="SQRT(COMMANDED OMEGA)",
        y_label="MEAN CURRENT DENSITY (A/CM2)",
        points=levich_points,
        fit_points=fit_line_points(levich_fit, levich_points),
    )
    render_xy_plot(
        paths["kl_plot_png"],
        title="KOUTECKY-LEVICH PLOT",
        x_label="1 / SQRT(COMMANDED OMEGA)",
        y_label="1 / MEAN CURRENT DENSITY (CM2/A)",
        points=kl_points,
        fit_points=fit_line_points(kl_fit, kl_points),
    )

    analysis = {
        "technique": "levich_rpm_sweep_ca",
        "label": "Levich CA RPM Sweep",
        "generated_at": utc_now(),
        "rpm_source": "commanded",
        "stabilization_mode": "fixed delay",
        "raw_dta": raw_path.name,
        "rpm_schedule_json": schedule_path.name,
        "electrode_area_cm2": float(area_cm2),
        "trace_axes": {"x": "time_s", "y": "current_a"},
        "current_sign_convention": "raw Gamry current sign preserved",
        "levich_axes": {
            "x": "sqrt_commanded_angular_velocity_rad_s_half",
            "y": "mean_current_density_a_cm2",
        },
        "koutecky_levich_axes": {
            "x": "inverse_sqrt_commanded_angular_velocity_s_half_rad_minus_half",
            "y": "inverse_mean_current_density_cm2_per_a",
        },
        "rpm_points": rows,
        "levich_fit": levich_fit,
        "koutecky_levich_fit": kl_fit,
        "analysis_artifacts": {
            "rpm_schedule_json": schedule_path.name,
            **{key: path.name for key, path in paths.items()},
        },
    }
    write_json(paths["analysis_json"], analysis)
    return {
        "analysis": analysis,
        "artifacts": {
            "rpm_schedule_json": schedule_path,
            **paths,
        },
    }
