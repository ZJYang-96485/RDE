from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_gamry_config, get_path


class DataManagerError(RuntimeError):
    pass


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def safe_name(value: Any, fallback: str = "item") -> str:
    text = str(value or "").strip()

    if not text:
        text = fallback

    text = re.sub(r"[^A-Za-z0-9 _.-]", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._-")

    return text or fallback


def output_root() -> Path:
    path = get_path("output_dir")
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_extension() -> str:
    extension = str(
        get_gamry_config().get("default_file_extension", ".DTA") or ".DTA"
    ).strip()

    if not extension.startswith("."):
        extension = "." + extension

    return extension


def ensure_extension(filename: str, extension: str | None = None) -> str:
    filename = str(filename or "").strip()

    if not filename:
        filename = "output"

    if extension is None:
        extension = default_extension()

    if Path(filename).suffix:
        return filename

    return filename + extension


def make_run_id(run_plan: dict[str, Any]) -> str:
    name = safe_name(
        run_plan.get("run_name")
        or run_plan.get("display_name")
        or "run"
    )
    return f"{compact_timestamp()}_{name}"


def make_run_dir(run_plan: dict[str, Any]) -> Path:
    base = output_root() / make_run_id(run_plan)
    candidate = base
    counter = 2

    while candidate.exists():
        candidate = Path(f"{base}_{counter}")
        counter += 1

    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def system_dir(run_dir: str | Path) -> Path:
    path = Path(run_dir) / "_system"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def read_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise DataManagerError(f"JSON file must contain an object: {path}")

    return payload


def append_log(run_dir: str | Path, message: str) -> None:
    # One readable log at the run root.
    log_path = Path(run_dir) / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{utc_timestamp()}] {message}\n")


def manifest_path(run_dir: str | Path) -> Path:
    return system_dir(run_dir) / "manifest.json"


def load_manifest(run_dir: str | Path) -> dict[str, Any]:
    path = manifest_path(run_dir)

    if not path.exists():
        return {
            "status": "created",
            "created_at": utc_timestamp(),
            "completed_at": None,
            "samples": [],
            "protocols": [],
            "outputs": [],
            "trials": [],
            "analysis_results": [],
            "action_results": [],
            "errors": [],
        }

    return read_json(path)


def save_manifest(run_dir: str | Path, manifest: dict[str, Any]) -> None:
    write_json(manifest_path(run_dir), manifest)


def register_action_result(
    run_dir: str | Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Append a hardware action result to the run manifest."""

    record = dict(result)
    manifest = load_manifest(run_dir)
    manifest.setdefault("action_results", []).append(record)
    save_manifest(run_dir, manifest)
    return record


def write_storage_guide(run_dir: str | Path) -> None:
    guide = """RDE RUN DATA

The folders directly beside this file are the sample/group folders.
User-facing .DTA files, matching data-table .csv exports, and registered
post-run analysis artifacts are stored directly inside those folders.

Internal worker jobs, protocol snapshots, and the detailed manifest are
kept in _system/ and normally do not need to be opened.

Useful files:
- run_summary.json : final status, DTA/CSV files, and analysis artifacts
- run.log          : readable execution history
"""
    (Path(run_dir) / "README_DATA.txt").write_text(guide, encoding="utf-8")


def create_run_workspace(run_plan: dict[str, Any]) -> dict[str, Any]:
    run_dir = make_run_dir(run_plan)
    internal = system_dir(run_dir)

    write_json(internal / "run_plan.json", run_plan)

    manifest = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "run_name": run_plan.get("run_name"),
        "display_name": run_plan.get("display_name"),
        "status": "running",
        "created_at": utc_timestamp(),
        "completed_at": None,
        "samples": [],
        "protocols": [],
        "outputs": [],
        "trials": [],
        "analysis_results": [],
        "action_results": [],
        "errors": [],
    }

    save_manifest(run_dir, manifest)
    write_storage_guide(run_dir)
    append_log(run_dir, "Run workspace created.")

    return {
        "run_dir": run_dir,
        "manifest": manifest,
    }


def sample_dir_name(
    sample: dict[str, Any],
    sample_index: int,
    repetition: int = 1,
    repetitions: int = 1,
) -> str:
    label = sample.get("label") or sample.get("sample_id") or f"Sample_{sample_index}"
    sample_part = f"{sample_index:02d}_{safe_name(label, f'Sample_{sample_index}')}"

    if int(repetitions) > 1:
        return f"R{int(repetition):02d}_{sample_part}"

    return sample_part


def create_sample_workspace(
    run_dir: str | Path,
    sample: dict[str, Any],
    sample_index: int,
    repetition: int = 1,
    repetitions: int = 1,
) -> Path:
    run_dir = Path(run_dir)
    folder_name = sample_dir_name(
        sample,
        sample_index,
        repetition=repetition,
        repetitions=repetitions,
    )

    # Sample folders live directly at run root. They contain raw DTA files and
    # any explicitly registered post-run analysis artifacts.
    sample_dir = run_dir / folder_name
    sample_dir.mkdir(parents=True, exist_ok=True)

    # Sample metadata is internal, not mixed with DTA files.
    write_json(
        system_dir(run_dir) / "samples" / f"{folder_name}.json",
        sample,
    )

    manifest = load_manifest(run_dir)
    sample_record = {
        "sample_index": sample_index,
        "repetition": int(repetition),
        "sample_id": sample.get("sample_id"),
        "label": sample.get("label"),
        "sample_dir": str(sample_dir),
        "folder_name": folder_name,
        "created_at": utc_timestamp(),
    }

    existing = [
        item
        for item in manifest.get("samples", [])
        if not (
            int(item.get("sample_index", -1)) == int(sample_index)
            and int(item.get("repetition", 1)) == int(repetition)
        )
    ]
    existing.append(sample_record)
    manifest["samples"] = existing

    save_manifest(run_dir, manifest)
    append_log(run_dir, f"Sample folder created: {folder_name}")

    return sample_dir


def voltage_label(voltage: float) -> str:
    sign = "p" if voltage >= 0 else "m"
    value = abs(float(voltage))
    text = f"{value:.3f}".rstrip("0").rstrip(".").replace(".", "p")
    return sign + text + "V"


def prefixed_name(prefix: str | None, step_index: int, name: str) -> str:
    parts = []

    if prefix:
        parts.append(safe_name(prefix, "step"))

    parts.append(f"{int(step_index):02d}")
    parts.append(safe_name(name, f"step_{step_index:02d}"))

    return "_".join(parts)


def normal_step_filename(
    step: dict[str, Any],
    step_index: int,
    filename_prefix: str | None = None,
) -> str:
    output = str(step.get("output") or "").strip()

    if output:
        base_name = Path(output).stem
        extension = Path(output).suffix or default_extension()
    else:
        base_name = (
            step.get("name")
            or step.get("technique")
            or f"step_{step_index:02d}"
        )
        extension = default_extension()

    filename = prefixed_name(filename_prefix, step_index, str(base_name))
    return safe_name(filename) + extension


def staircase_step_filename(
    step: dict[str, Any],
    step_index: int,
    sub_index: int,
    voltage: float,
    filename_prefix: str | None = None,
) -> str:
    prefix = (
        step.get("output_prefix")
        or step.get("name")
        or f"CA_step_{step_index:02d}"
    )
    base = (
        f"{safe_name(prefix)}_{int(sub_index):02d}_"
        f"{voltage_label(voltage)}"
    )
    filename = prefixed_name(filename_prefix, step_index, base)
    return ensure_extension(safe_name(filename))


def build_step_outputs(
    sample_dir: str | Path,
    step: dict[str, Any],
    step_index: int,
    filename_prefix: str | None = None,
) -> list[str]:
    sample_dir = Path(sample_dir)
    technique = str(step.get("technique", "")).strip().lower()

    if technique == "ca_staircase":
        start_voltage = float(step.get("start_voltage_v", 0))
        step_voltage = float(step.get("step_voltage_v", 0))
        step_count = int(step.get("step_count", 1))

        outputs = []

        for sub_index in range(1, step_count + 1):
            voltage = start_voltage + (sub_index - 1) * step_voltage
            filename = staircase_step_filename(
                step,
                step_index,
                sub_index,
                voltage,
                filename_prefix=filename_prefix,
            )
            outputs.append(str(sample_dir / filename))

        return outputs

    filename = normal_step_filename(
        step,
        step_index,
        filename_prefix=filename_prefix,
    )
    return [str(sample_dir / filename)]


def register_step_outputs(
    run_dir: str | Path,
    sample_dir: str | Path,
    sample_index: int,
    step: dict[str, Any],
    step_index: int,
    outputs: list[str],
    filename_prefix: str | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)

    record = {
        "sample_index": sample_index,
        "sample_dir": str(sample_dir),
        "filename_prefix": filename_prefix,
        "step_index": step_index,
        "step_name": step.get("name"),
        "technique": step.get("technique"),
        "outputs": outputs,
        "created_at": utc_timestamp(),
    }

    manifest = load_manifest(run_dir)
    manifest.setdefault("outputs", []).append(record)
    save_manifest(run_dir, manifest)

    return record


def register_trial_result(
    run_dir: str | Path,
    output_record: dict[str, Any],
    trial_metadata: dict[str, Any],
    worker_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the automatic Ru/compensation outcome for one EChem trial."""

    run_dir = Path(run_dir)
    metadata = dict(trial_metadata) if isinstance(trial_metadata, dict) else {}
    status = str(metadata.get("trial_status") or "failed").strip().lower()
    display_status = {
        "completed": "Completed",
        "skipped": "Skipped",
        "failed": "Failed",
        "aborted": "Aborted",
    }.get(status, status.title() or "Failed")
    trial_id = str(
        output_record.get("trial_id")
        or f"sample-{output_record.get('sample_index')}-step-{output_record.get('step_index')}"
    )
    record = {
        "trial_id": trial_id,
        "sample_index": output_record.get("sample_index"),
        "sample_dir": output_record.get("sample_dir"),
        "step_index": output_record.get("step_index"),
        "step_name": output_record.get("step_name"),
        "technique": output_record.get("technique"),
        "outputs": list(output_record.get("outputs", [])),
        "status": display_status,
        "metadata": metadata,
        "worker_job_id": (worker_result or {}).get("job_id"),
        "registered_at": utc_timestamp(),
    }

    manifest = load_manifest(run_dir)
    output_paths = {
        Path(value).resolve()
        for value in output_record.get("outputs", [])
    }
    matching_analysis_results = []
    for analysis_result in manifest.get("analysis_results", []):
        raw_dta = analysis_result.get("raw_dta")
        if not raw_dta:
            continue
        if (run_dir / Path(str(raw_dta))).resolve() in output_paths:
            matching_analysis_results.append(dict(analysis_result))
    if matching_analysis_results:
        record["analysis_results"] = matching_analysis_results

    trials = [item for item in manifest.get("trials", []) if str(item.get("trial_id")) != trial_id]
    trials.append(record)
    manifest["trials"] = trials

    for planned in manifest.get("outputs", []):
        if (
            int(planned.get("sample_index", -1)) == int(output_record.get("sample_index", -2))
            and int(planned.get("step_index", -1)) == int(output_record.get("step_index", -2))
            and str(planned.get("sample_dir", "")) == str(output_record.get("sample_dir", ""))
            and str(planned.get("filename_prefix", "")) == str(output_record.get("filename_prefix", ""))
        ):
            planned["trial_id"] = trial_id
            planned["trial_status"] = display_status
            planned["trial_metadata"] = metadata
            if matching_analysis_results:
                planned["analysis_results"] = matching_analysis_results
            break

    save_manifest(run_dir, manifest)
    append_log(run_dir, f"Registered EChem trial {trial_id}: {display_status}.")
    return record


def relative_run_file(run_dir: str | Path, path: str | Path) -> str:
    root = Path(run_dir).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise DataManagerError(f"Analysis artifact is outside the run folder: {path}") from exc


def register_trial_analysis_result(
    run_dir: str | Path,
    *,
    raw_dta: str | Path,
    analysis_type: str,
    analysis_version: str,
    label: str,
    technique: str,
    status: str,
    analysis_artifacts: dict[str, str | Path] | None = None,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Register one analysis through a technique-independent manifest schema.

    New analyses should write their artifacts beside the source DTA, then call
    this function.  History can discover them through ``analysis_type`` and
    the standardized ``analysis_artifacts`` mapping without changing the
    acquisition path.
    """

    root = Path(run_dir).resolve()
    raw_path = Path(raw_dta).resolve()
    if not raw_path.is_file():
        raise DataManagerError(f"Raw analysis DTA does not exist: {raw_dta}")

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"complete", "failed"}:
        raise DataManagerError("Analysis status must be 'complete' or 'failed'.")

    artifact_paths = dict(analysis_artifacts or {})
    for key, value in artifact_paths.items():
        artifact_path = Path(value).resolve()
        if not artifact_path.is_file():
            raise DataManagerError(f"Analysis artifact does not exist ({key}): {value}")
        if artifact_path.parent != raw_path.parent:
            raise DataManagerError(
                f"Analysis artifact must be stored beside its raw DTA ({key}): {value}"
            )

    raw_relative = relative_run_file(root, raw_path)
    artifact_relatives = {
        str(key): relative_run_file(root, value)
        for key, value in artifact_paths.items()
    }
    result = {
        "analysis_type": str(analysis_type),
        "analysis_version": str(analysis_version),
        "analysis_status": normalized_status,
        "technique": str(technique),
        "label": str(label),
        "raw_dta": raw_relative,
        "analysis_artifacts": artifact_relatives,
        "summary": dict(summary or {}),
        "registered_at": utc_timestamp(),
    }
    if error:
        result["error"] = str(error)

    manifest = load_manifest(root)
    existing = [
        item
        for item in manifest.get("analysis_results", [])
        if not (
            str(item.get("raw_dta", "")) == raw_relative
            and str(item.get("analysis_type", "")) == str(analysis_type)
        )
    ]
    existing.append(result)
    manifest["analysis_results"] = existing

    for output_record in manifest.get("outputs", []):
        output_paths = [Path(value).resolve() for value in output_record.get("outputs", [])]
        if raw_path not in output_paths:
            continue
        output_analyses = [
            item
            for item in output_record.get("analysis_results", [])
            if str(item.get("analysis_type", "")) != str(analysis_type)
            or str(item.get("raw_dta", "")) != raw_relative
        ]
        output_analyses.append(result)
        output_record["analysis_results"] = output_analyses
        break

    for trial in manifest.get("trials", []):
        trial_paths = [Path(value).resolve() for value in trial.get("outputs", [])]
        if raw_path not in trial_paths:
            continue
        trial_analyses = [
            item
            for item in trial.get("analysis_results", [])
            if str(item.get("analysis_type", "")) != str(analysis_type)
            or str(item.get("raw_dta", "")) != raw_relative
        ]
        trial_analyses.append(result)
        trial["analysis_results"] = trial_analyses
        break

    save_manifest(root, manifest)
    detail = f"failed: {error}" if normalized_status == "failed" else "complete"
    append_log(
        root,
        f"Registered {analysis_type} analysis ({raw_relative}): {detail}.",
    )
    return result


def register_analysis_result(
    run_dir: str | Path,
    *,
    raw_dta: str | Path,
    analysis_artifacts: dict[str, str | Path],
    label: str = "Levich CA RPM Sweep",
    technique: str = "levich_rpm_sweep_ca",
    rpm_source: str = "commanded",
    stabilization_mode: str = "fixed delay",
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    raw_path = Path(raw_dta).resolve()
    if not raw_path.is_file():
        raise DataManagerError(f"Raw analysis DTA does not exist: {raw_dta}")
    for key, value in analysis_artifacts.items():
        artifact_path = Path(value).resolve()
        if not artifact_path.is_file():
            raise DataManagerError(f"Analysis artifact does not exist ({key}): {value}")
        if artifact_path.parent != raw_path.parent:
            raise DataManagerError(
                f"Analysis artifact must be stored beside its raw DTA ({key}): {value}"
            )
    raw_relative = relative_run_file(run_dir, raw_dta)
    artifact_relatives = {
        str(key): relative_run_file(run_dir, value)
        for key, value in analysis_artifacts.items()
    }
    result = {
        "technique": technique,
        "label": label,
        "raw_dta": raw_relative,
        "rpm_source": rpm_source,
        "stabilization_mode": stabilization_mode,
        "analysis_artifacts": artifact_relatives,
        "registered_at": utc_timestamp(),
    }

    manifest = load_manifest(run_dir)
    existing = [
        item
        for item in manifest.get("analysis_results", [])
        if str(item.get("raw_dta", "")) != raw_relative
    ]
    existing.append(result)
    manifest["analysis_results"] = existing

    raw_resolved = raw_path
    for output_record in manifest.get("outputs", []):
        output_paths = [Path(value).resolve() for value in output_record.get("outputs", [])]
        if raw_resolved in output_paths:
            output_record.update(
                {
                    "label": label,
                    "raw_dta": raw_relative,
                    "rpm_source": rpm_source,
                    "stabilization_mode": stabilization_mode,
                    "analysis_artifacts": artifact_relatives,
                }
            )
            break

    save_manifest(run_dir, manifest)
    append_log(run_dir, f"Registered analysis result: {label} ({raw_relative}).")
    return result


def prepare_protocol_outputs(
    run_dir: str | Path,
    sample_dir: str | Path,
    sample_index: int,
    protocol: dict[str, Any],
    filename_prefix: str | None = None,
) -> list[dict[str, Any]]:
    records = []

    for step_index, step in enumerate(protocol.get("steps", []), start=1):
        if not bool(step.get("enabled", True)):
            continue
        if str(step.get("technique") or "").lower() == "wait":
            continue

        outputs = build_step_outputs(
            sample_dir,
            step,
            step_index,
            filename_prefix=filename_prefix,
        )
        record = register_step_outputs(
            run_dir=run_dir,
            sample_dir=sample_dir,
            sample_index=sample_index,
            step=step,
            step_index=step_index,
            outputs=outputs,
            filename_prefix=filename_prefix,
        )
        records.append(record)

    return records


def save_protocol_snapshot(
    run_dir: str | Path,
    protocol: dict[str, Any],
) -> Path:
    run_dir = Path(run_dir)
    protocol_name = safe_name(
        protocol.get("protocol_name")
        or protocol.get("display_name")
        or "protocol"
    )

    snapshot_dir = system_dir(run_dir) / "protocols"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Add a timestamp because the same protocol can be used more than once.
    path = snapshot_dir / f"{compact_timestamp()}_{protocol_name}.json"
    counter = 2
    while path.exists():
        path = snapshot_dir / f"{compact_timestamp()}_{protocol_name}_{counter}.json"
        counter += 1

    write_json(path, protocol)

    manifest = load_manifest(run_dir)
    manifest.setdefault("protocols", []).append(
        {
            "protocol_name": protocol.get("protocol_name"),
            "display_name": protocol.get("display_name"),
            "path": str(path),
            "saved_at": utc_timestamp(),
        }
    )
    save_manifest(run_dir, manifest)

    return path


def relative_dta_files(run_dir: str | Path) -> list[str]:
    run_dir = Path(run_dir)
    return sorted(
        str(path.relative_to(run_dir))
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix.lower() == ".dta"
    )


def relative_csv_files(run_dir: str | Path) -> list[str]:
    run_dir = Path(run_dir)
    return sorted(
        str(path.relative_to(run_dir))
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix.lower() == ".csv"
    )


def export_run_dta_to_csv(run_dir: str | Path) -> dict[str, Any]:
    """Create a complete table CSV beside every DTA in a finished run."""

    from workflow.dta_csv import convert_dta_directory

    run_dir = Path(run_dir).resolve()
    report = convert_dta_directory(run_dir)
    exported_at = utc_timestamp()
    converted = [dict(item, exported_at=exported_at) for item in report["converted"]]
    errors = [dict(item, attempted_at=exported_at) for item in report["errors"]]
    csv_by_dta = {
        str(item["source_dta"]): str(item["csv_file"])
        for item in converted
    }

    manifest = load_manifest(run_dir)
    manifest["dta_csv_exports"] = converted
    manifest["dta_csv_errors"] = errors

    def mapped_csv_outputs(values: Any) -> list[str]:
        mapped: list[str] = []
        for value in values if isinstance(values, list) else []:
            try:
                relative = relative_run_file(run_dir, value)
            except DataManagerError:
                continue
            csv_relative = csv_by_dta.get(relative)
            if csv_relative:
                mapped.append(str(run_dir / Path(*Path(csv_relative).parts)))
        return mapped

    for output_record in manifest.get("outputs", []):
        output_record["csv_outputs"] = mapped_csv_outputs(output_record.get("outputs", []))
    for trial_record in manifest.get("trials", []):
        trial_record["csv_outputs"] = mapped_csv_outputs(trial_record.get("outputs", []))
    for analysis_result in manifest.get("analysis_results", []):
        raw_dta = str(analysis_result.get("raw_dta", ""))
        raw_csv = csv_by_dta.get(raw_dta)
        if raw_csv:
            analysis_result["raw_csv"] = raw_csv

    save_manifest(run_dir, manifest)
    append_log(
        run_dir,
        f"DTA-to-CSV export finished: {report['converted_count']} converted, "
        f"{report['error_count']} failed.",
    )
    for error in errors:
        append_log(
            run_dir,
            f"DTA-to-CSV export failed for {error['source_dta']}: {error['error']}",
        )
    return report


def write_run_summary(run_dir: str | Path) -> None:
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)

    summary = {
        "run_id": manifest.get("run_id"),
        "run_name": manifest.get("run_name"),
        "display_name": manifest.get("display_name"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "completed_at": manifest.get("completed_at"),
        "sample_folders": [
            item.get("folder_name")
            for item in manifest.get("samples", [])
            if item.get("folder_name")
        ],
        "dta_files": relative_dta_files(run_dir),
        "csv_files": relative_csv_files(run_dir),
        "dta_csv_exports": manifest.get("dta_csv_exports", []),
        "dta_csv_errors": manifest.get("dta_csv_errors", []),
        "analysis_results": manifest.get("analysis_results", []),
        "trials": manifest.get("trials", []),
        "action_results": manifest.get("action_results", []),
        "errors": manifest.get("errors", []),
    }

    write_json(run_dir / "run_summary.json", summary)


def mark_run_complete(run_dir: str | Path) -> None:
    manifest = load_manifest(run_dir)
    manifest["status"] = "complete"
    manifest["completed_at"] = utc_timestamp()
    save_manifest(run_dir, manifest)
    export_run_dta_to_csv(run_dir)
    write_run_summary(run_dir)
    append_log(run_dir, "Run marked complete.")


def mark_run_failed(run_dir: str | Path, error: str) -> None:
    manifest = load_manifest(run_dir)
    manifest["status"] = "failed"
    manifest["completed_at"] = utc_timestamp()
    manifest.setdefault("errors", []).append(
        {
            "error": str(error),
            "time": utc_timestamp(),
        }
    )
    save_manifest(run_dir, manifest)
    export_run_dta_to_csv(run_dir)
    write_run_summary(run_dir)
    append_log(run_dir, f"Run marked failed: {error}")


def mark_run_aborted(run_dir: str | Path) -> None:
    manifest = load_manifest(run_dir)
    manifest["status"] = "aborted"
    manifest["completed_at"] = utc_timestamp()
    save_manifest(run_dir, manifest)
    export_run_dta_to_csv(run_dir)
    write_run_summary(run_dir)
    append_log(run_dir, "Run marked aborted.")
