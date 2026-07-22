from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MAX_DTA_FILE_BYTES = 50 * 1024 * 1024
MAX_PLOT_POINTS = 5000


class DtaViewerError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def _run_root(run_dir: str | Path) -> Path:
    return Path(run_dir).expanduser().resolve()


def list_dta_files(run_dir: str | Path) -> list[dict[str, Any]]:
    root = _run_root(run_dir)
    if not root.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".dta":
            continue

        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            # Ignore a symlink that resolves outside the active run folder.
            continue

        stat = resolved.stat()
        relative_path = relative.as_posix()
        sample = relative.parts[0] if len(relative.parts) > 1 else "Run root"
        csv_path = resolved.with_suffix(".csv")
        csv_relative_path = None
        csv_size_bytes = None
        if csv_path.is_file():
            try:
                csv_relative_path = csv_path.resolve().relative_to(root).as_posix()
                csv_size_bytes = int(csv_path.stat().st_size)
            except ValueError:
                csv_relative_path = None
                csv_size_bytes = None
        records.append(
            {
                "label": " / ".join(relative.parts),
                "sample": sample,
                "filename": relative.name,
                "relative_path": relative_path,
                "size_bytes": int(stat.st_size),
                "csv_filename": csv_path.name if csv_relative_path else None,
                "csv_relative_path": csv_relative_path,
                "csv_size_bytes": csv_size_bytes,
                "modified_time": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )

    records.sort(key=lambda item: str(item["relative_path"]).casefold())
    return records


def resolve_listed_dta_path(run_dir: str | Path, relative_path: str) -> Path:
    root = _run_root(run_dir)
    raw = str(relative_path or "").strip().replace("\\", "/")
    candidate_relative = PurePosixPath(raw)

    if (
        not raw
        or candidate_relative.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate_relative.parts)
        or re.match(r"^[A-Za-z]:", raw)
    ):
        raise DtaViewerError("path must be a safe relative DTA path.", 400)

    if candidate_relative.suffix.lower() != ".dta":
        raise DtaViewerError("path must identify a .DTA file.", 400)

    allowed = {
        str(item["relative_path"])
        for item in list_dta_files(root)
    }
    normalized = candidate_relative.as_posix()
    if normalized not in allowed:
        raise DtaViewerError("DTA file is not part of the current automation trial.", 404)

    resolved = (root / Path(*candidate_relative.parts)).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DtaViewerError("DTA path is outside the current automation trial.", 403) from exc

    if not resolved.is_file():
        raise DtaViewerError("DTA file does not exist.", 404)
    if resolved.stat().st_size > MAX_DTA_FILE_BYTES:
        raise DtaViewerError("DTA file is larger than the 50 MB viewer limit.", 413)
    return resolved


def _normalized_column(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


COLUMN_ALIASES = {
    "point": {"pt", "point", "index", "seq"},
    "time": {"t", "ts", "time", "times", "timesec", "timesecs", "elapsed", "elapseds"},
    "potential": {
        "v",
        "vf",
        "vm",
        "e",
        "ewe",
        "potential",
        "potentialv",
        "appliedvoltagev",
        "vdc",
    },
    "current": {"i", "im", "idc", "current", "currenta"},
    "frequency": {"freq", "frequency", "frequencyhz", "freqhz"},
    "zreal": {"zreal", "zrealohm", "zre"},
    "zimag": {"zimag", "zimagohm", "zim"},
    "zmod": {"zmod", "zmodohm"},
    "phase": {"zphz", "phase", "phasedeg"},
}


# Final plot axes are technique-defined. These canonical fields map both
# ToolkitPy/Gamry names (T, Vf, Im, Zreal, Zimag) and mock column names through
# COLUMN_ALIASES above.
TECHNIQUE_PLOT_SPECS = {
    "ocp": {
        "x": "time",
        "y": "potential",
        "x_label": "Time (s)",
        "y_label": "Potential (V)",
        "invert_y": False,
    },
    "ca": {
        "x": "time",
        "y": "current",
        "x_label": "Time (s)",
        "y_label": "Current (A)",
        "invert_y": False,
    },
    "cp": {
        "x": "time",
        "y": "potential",
        "x_label": "Time (s)",
        "y_label": "Potential (V)",
        "invert_y": False,
    },
    "cc_charge": {
        "x": "time",
        "y": "potential",
        "x_label": "Time (s)",
        "y_label": "Potential (V)",
        "invert_y": False,
    },
    "cc_discharge": {
        "x": "time",
        "y": "potential",
        "x_label": "Time (s)",
        "y_label": "Potential (V)",
        "invert_y": False,
    },
    "cv": {
        "x": "potential",
        "y": "current",
        "x_label": "Potential (V)",
        "y_label": "Current (A)",
        "invert_y": False,
    },
    "lsv": {
        "x": "potential",
        "y": "current",
        "x_label": "Potential (V)",
        "y_label": "Current (A)",
        "invert_y": False,
    },
    "eis": {
        "x": "zreal",
        "y": "zimag",
        "x_label": "Zreal (ohm)",
        "y_label": "-Zimag (ohm)",
        "invert_y": True,
    },
    "geis": {
        "x": "zreal",
        "y": "zimag",
        "x_label": "Zreal (ohm)",
        "y_label": "-Zimag (ohm)",
        "invert_y": True,
    },
}


def _canonical_column(value: str) -> str | None:
    normalized = _normalized_column(value)
    for canonical, aliases in COLUMN_ALIASES.items():
        if normalized in aliases:
            return canonical
    return None


def _split_columns(line: str) -> list[str]:
    stripped = line.strip()
    if "\t" in stripped:
        return [part.strip() for part in stripped.split("\t") if part.strip()]
    if "," in stripped:
        return [part.strip() for part in stripped.split(",") if part.strip()]
    return [part for part in re.split(r"\s+", stripped) if part]


def _find_table(
    lines: list[str],
    technique_hint: str = "auto",
) -> tuple[int, list[str], list[str | None]]:
    first_candidate: tuple[int, list[str], list[str | None]] | None = None
    plot_spec = TECHNIQUE_PLOT_SPECS.get(technique_hint)
    for line_index, line in enumerate(lines):
        columns = _split_columns(line)
        if len(columns) < 2:
            continue
        canonical = [_canonical_column(column) for column in columns]
        # Metadata rows can repeat the same field name, for example Gamry's
        # ``TIME  LABEL  13:58:39  Time``. Require distinct data concepts so
        # that row cannot be mistaken for the CURVE table header.
        data_columns = {name for name in canonical if name and name != "point"}
        if len(data_columns) >= 2:
            candidate = (line_index, columns, canonical)
            if first_candidate is None:
                first_candidate = candidate
            if plot_spec and {str(plot_spec["x"]), str(plot_spec["y"])}.issubset(data_columns):
                return candidate

    if first_candidate is not None:
        return first_candidate

    # Best-effort fallback for an unfamiliar export whose column names are not
    # in the aliases above. Require a non-JSON header followed closely by a row
    # with at least two numeric values.
    for line_index, line in enumerate(lines):
        if any(marker in line for marker in ('{', '}', '":')):
            continue
        columns = _split_columns(line)
        if len(columns) < 2 or sum(_as_finite_number(value) is not None for value in columns) >= 2:
            continue
        for candidate in lines[line_index + 1 : line_index + 5]:
            values = _split_columns(candidate)
            if len(values) >= 2 and sum(_as_finite_number(value) is not None for value in values) >= 2:
                return line_index, columns, [_canonical_column(column) for column in columns]
    raise DtaViewerError("No supported numeric table header was found in the DTA file.", 422)


def _as_finite_number(value: str) -> float | None:
    try:
        number = float(str(value).strip().replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _technique_from_text(text: str) -> str | None:
    normalized = str(text or "").lower()
    patterns = (
        ("cc_charge", r"(?:^|[^a-z0-9])cc[_ -]?charge(?:$|[^a-z0-9])"),
        ("cc_discharge", r"(?:^|[^a-z0-9])cc[_ -]?discharge(?:$|[^a-z0-9])"),
        ("geis", r"(?:^|[^a-z0-9])geis(?:$|[^a-z0-9])"),
        ("eis", r"(?:^|[^a-z0-9])eis(?:$|[^a-z0-9])"),
        ("ocp", r"(?:^|[^a-z0-9])ocp(?:$|[^a-z0-9])"),
        ("lsv", r"(?:^|[^a-z0-9])lsv(?:$|[^a-z0-9])"),
        ("cv", r"(?:^|[^a-z0-9])cv(?:$|[^a-z0-9])"),
        ("cp", r"(?:^|[^a-z0-9])cp(?:$|[^a-z0-9])"),
        ("ca", r"(?:^|[^a-z0-9])ca(?:$|[^a-z0-9])"),
    )
    for technique, pattern in patterns:
        if re.search(pattern, normalized):
            return technique
    return None


def _guess_technique(
    path: Path,
    lines: list[str],
    canonical_columns: list[str | None],
) -> str:
    for line in lines[:100]:
        parts = _split_columns(line)
        if len(parts) >= 2 and _normalized_column(parts[0]) == "technique":
            detected = _technique_from_text(parts[1])
            if detected:
                return detected

    filename_guess = _technique_from_text(path.stem)
    if filename_guess:
        return filename_guess

    available = {name for name in canonical_columns if name}
    if {"zreal", "zimag"}.issubset(available):
        return "eis"
    return "auto"


def _plot_columns(
    technique: str,
    canonical_columns: list[str | None],
    raw_columns: list[str],
) -> tuple[int, int, str, str, bool]:
    first_index: dict[str, int] = {}
    for index, canonical in enumerate(canonical_columns):
        if canonical and canonical not in first_index:
            first_index[canonical] = index

    plot_spec = TECHNIQUE_PLOT_SPECS.get(technique)
    if plot_spec:
        x_name = str(plot_spec["x"])
        y_name = str(plot_spec["y"])
        if x_name not in first_index or y_name not in first_index:
            missing = [name for name in (x_name, y_name) if name not in first_index]
            raise DtaViewerError(
                f"{technique.upper()} plotting requires {x_name} and {y_name} columns; "
                f"missing: {', '.join(missing)}.",
                422,
            )
        return (
            first_index[x_name],
            first_index[y_name],
            str(plot_spec["x_label"]),
            str(plot_spec["y_label"]),
            bool(plot_spec["invert_y"]),
        )

    numeric_candidates = list(range(len(raw_columns)))
    if len(numeric_candidates) < 2:
        raise DtaViewerError("DTA table does not contain two plottable columns.", 422)
    return (
        numeric_candidates[0],
        numeric_candidates[1],
        f"Auto-detected: {raw_columns[0]}",
        f"Auto-detected: {raw_columns[1]}",
        False,
    )


def _decimate(points: list[dict[str, float]], limit: int) -> list[dict[str, float]]:
    if len(points) <= limit:
        return points
    if limit <= 1:
        return [points[0]]
    last = len(points) - 1
    return [points[round(index * last / (limit - 1))] for index in range(limit)]


def parse_dta_file(
    path: str | Path,
    max_points: int = MAX_PLOT_POINTS,
    *,
    allow_analysis_point_limit: bool = False,
) -> dict[str, Any]:
    dta_path = Path(path)
    if not dta_path.is_file():
        raise DtaViewerError("DTA file does not exist.", 404)
    if dta_path.stat().st_size > MAX_DTA_FILE_BYTES:
        raise DtaViewerError("DTA file is larger than the 50 MB viewer limit.", 413)

    text = dta_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    technique_hint = _guess_technique(dta_path, lines, [])
    header_index, raw_columns, canonical_columns = _find_table(lines, technique_hint)
    technique = _guess_technique(dta_path, lines, canonical_columns)
    x_index, y_index, x_label, y_label, invert_y = _plot_columns(
        technique, canonical_columns, raw_columns
    )

    points: list[dict[str, float]] = []
    minimum_columns = max(x_index, y_index) + 1
    for line in lines[header_index + 1 :]:
        values = _split_columns(line)
        if len(values) < minimum_columns:
            continue
        x_value = _as_finite_number(values[x_index])
        y_value = _as_finite_number(values[y_index])
        if x_value is None or y_value is None:
            continue
        points.append({"x": x_value, "y": -y_value if invert_y else y_value})

    if not points:
        raise DtaViewerError("No numeric plot points were found in the DTA table.", 422)

    original_point_count = len(points)
    requested_limit = max(1, int(max_points))
    point_limit = (
        requested_limit
        if allow_analysis_point_limit
        else min(requested_limit, MAX_PLOT_POINTS)
    )
    points = _decimate(points, point_limit)
    return {
        "technique_guess": technique,
        "x_label": x_label,
        "y_label": y_label,
        "points": points,
        "point_count": len(points),
        "original_point_count": original_point_count,
        "decimated": original_point_count > len(points),
    }
