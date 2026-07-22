from __future__ import annotations

import csv
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from workflow.dta_viewer import (
    MAX_DTA_FILE_BYTES,
    DtaViewerError,
    _as_finite_number,
    _find_table,
    _guess_technique,
)


class DtaCsvError(RuntimeError):
    pass


def _table_fields(line: str) -> list[str]:
    """Split a DTA table row without shifting intentionally empty fields."""

    text = str(line).strip("\r\n")
    if "\t" in text:
        fields = [value.strip() for value in text.split("\t")]
    elif "," in text:
        fields = [value.strip() for value in next(csv.reader([text]))]
    else:
        fields = [value for value in re.split(r"\s+", text.strip()) if value]

    # Gamry's default writer surrounds every CURVE row with a tab. Remove only
    # those framing cells; empty cells inside a row remain aligned.
    while fields and fields[0] == "":
        fields.pop(0)
    while fields and fields[-1] == "":
        fields.pop()
    return fields


def _declared_curve_count(lines: list[str], header_index: int) -> int | None:
    for line in reversed(lines[max(0, header_index - 4) : header_index]):
        fields = _table_fields(line)
        if len(fields) >= 3 and fields[0].strip().upper() == "CURVE" and fields[1].strip().upper() == "TABLE":
            try:
                count = int(fields[2])
            except (TypeError, ValueError):
                continue
            return count if count >= 0 else None
    return None


def _is_units_row(fields: list[str], column_count: int) -> bool:
    if not fields or len(fields) < column_count:
        return False
    # All supported data tables begin with a numeric point, time, frequency,
    # or potential value. The optional Gamry units row begins with # or text.
    return _as_finite_number(fields[0]) is None


def _unique_headers(columns: list[str], units: list[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, raw_column in enumerate(columns):
        column = str(raw_column or "").strip() or f"column_{index + 1}"
        unit = str(units[index] if index < len(units) else "").strip()
        header = f"{column} ({unit})" if unit else column
        count = seen.get(header.casefold(), 0) + 1
        seen[header.casefold()] = count
        headers.append(header if count == 1 else f"{header}_{count}")
    return headers


def _excel_friendly_value(value: str) -> str:
    text = str(value).strip()
    # Excel and most CSV readers understand E exponents but not Fortran's D
    # exponent syntax, which can appear in electrochemical exports.
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)[dD][+-]?\d+", text):
        return re.sub(r"[dD]", "E", text, count=1)
    return text


def extract_dta_table(path: str | Path) -> dict[str, Any]:
    dta_path = Path(path)
    if not dta_path.is_file():
        raise DtaCsvError(f"DTA file does not exist: {dta_path}")
    if dta_path.stat().st_size > MAX_DTA_FILE_BYTES:
        raise DtaCsvError(f"DTA file exceeds the {MAX_DTA_FILE_BYTES // (1024 * 1024)} MB conversion limit: {dta_path}")

    lines = dta_path.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        technique_hint = _guess_technique(dta_path, lines, [])
        header_index, raw_columns, canonical_columns = _find_table(lines, technique_hint)
    except DtaViewerError as exc:
        raise DtaCsvError(str(exc)) from exc

    column_count = len(raw_columns)
    if column_count < 2:
        raise DtaCsvError("DTA table must contain at least two columns.")

    units: list[str] = []
    data_start = header_index + 1
    if data_start < len(lines):
        possible_units = _table_fields(lines[data_start])
        if _is_units_row(possible_units, column_count):
            units = possible_units[:column_count]
            data_start += 1

    declared_count = _declared_curve_count(lines, header_index)
    rows: list[list[str]] = []
    for line in lines[data_start:]:
        fields = _table_fields(line)
        if not fields or _as_finite_number(fields[0]) is None:
            if rows and declared_count is None:
                break
            continue
        if len(fields) < column_count:
            fields.extend([""] * (column_count - len(fields)))
        rows.append([_excel_friendly_value(value) for value in fields[:column_count]])
        if declared_count is not None and len(rows) >= declared_count:
            break

    if not rows:
        raise DtaCsvError("No numeric data rows were found in the DTA table.")
    if declared_count is not None and len(rows) != declared_count:
        raise DtaCsvError(
            f"DTA CURVE declared {declared_count} rows but only {len(rows)} complete rows were found."
        )

    return {
        "source": str(dta_path),
        "technique": _guess_technique(dta_path, lines, canonical_columns),
        "columns": list(raw_columns),
        "units": units,
        "headers": _unique_headers(list(raw_columns), units),
        "rows": rows,
        "row_count": len(rows),
        "column_count": column_count,
    }


def convert_dta_to_csv(path: str | Path, csv_path: str | Path | None = None) -> dict[str, Any]:
    dta_path = Path(path).resolve()
    destination = Path(csv_path).resolve() if csv_path is not None else dta_path.with_suffix(".csv")
    if destination == dta_path:
        raise DtaCsvError("CSV destination must be different from the DTA source.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    table = extract_dta_table(dta_path)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(table["headers"])
            writer.writerows(table["rows"])
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "source_dta": str(dta_path),
        "csv_file": str(destination),
        "technique": table["technique"],
        "row_count": table["row_count"],
        "column_count": table["column_count"],
        "headers": table["headers"],
    }


def convert_dta_directory(root: str | Path) -> dict[str, Any]:
    directory = Path(root).resolve()
    if not directory.is_dir():
        raise DtaCsvError(f"DTA conversion directory does not exist: {directory}")

    converted: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    dta_files = sorted(
        (path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() == ".dta"),
        key=lambda path: path.as_posix().casefold(),
    )
    for dta_path in dta_files:
        try:
            record = convert_dta_to_csv(dta_path)
            record["source_dta"] = dta_path.resolve().relative_to(directory).as_posix()
            record["csv_file"] = Path(record["csv_file"]).resolve().relative_to(directory).as_posix()
            converted.append(record)
        except (DtaCsvError, OSError, UnicodeError) as exc:
            errors.append(
                {
                    "source_dta": dta_path.resolve().relative_to(directory).as_posix(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return {
        "root": str(directory),
        "dta_count": len(dta_files),
        "converted_count": len(converted),
        "error_count": len(errors),
        "converted": converted,
        "errors": errors,
    }
