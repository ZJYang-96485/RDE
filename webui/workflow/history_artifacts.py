from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from analysis.registry import ANALYSIS_DEFINITIONS
from workflow.data_manager import load_manifest


class HistoryArtifactError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


ARTIFACT_LABELS = {
    "raw_csv": "Raw data CSV",
    "series_csv": "Charge time series CSV",
    "summary_json": "Charge analysis JSON",
    "rpm_schedule_json": "RPM schedule JSON",
    "summary_csv": "Summary CSV",
    "analysis_json": "Analysis JSON",
    "trace_plot_png": "Trace with RPM windows",
    "levich_plot_png": "Levich plot",
    "kl_plot_png": "Koutecky-Levich plot",
}

ALLOWED_SUFFIXES = {".dta", ".csv", ".json", ".png"}


def normalized_relative_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise HistoryArtifactError("Artifact path must be a safe relative path.")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise HistoryArtifactError("Artifact file type is not allowed.")
    return path.as_posix()


def registered_paths(run_dir: str | Path) -> set[str]:
    manifest = load_manifest(run_dir)
    paths: set[str] = set()
    for export in manifest.get("dta_csv_exports", []):
        if not isinstance(export, dict):
            continue
        for key in ("source_dta", "csv_file"):
            value = export.get(key)
            if value:
                paths.add(normalized_relative_path(value))
    for result in manifest.get("analysis_results", []):
        raw = result.get("raw_dta")
        if raw:
            paths.add(normalized_relative_path(raw))
        raw_csv = result.get("raw_csv")
        if raw_csv:
            paths.add(normalized_relative_path(raw_csv))
        artifacts = result.get("analysis_artifacts", {})
        if isinstance(artifacts, dict):
            for value in artifacts.values():
                paths.add(normalized_relative_path(value))
    return paths


def resolve_registered_artifact(run_dir: str | Path, relative_path: Any) -> Path:
    root = Path(run_dir).resolve()
    normalized = normalized_relative_path(relative_path)
    if normalized not in registered_paths(root):
        raise HistoryArtifactError("Artifact is not registered for the current run.", 404)
    candidate = (root / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HistoryArtifactError("Artifact path is outside the current run.", 403) from exc
    if not candidate.is_file():
        raise HistoryArtifactError("Registered artifact file does not exist.", 404)
    return candidate


def analysis_artifact_descriptor(
    run_dir: str | Path,
    relative_path: Any,
) -> dict[str, str]:
    """Identify an analysis artifact without coupling the API to filenames."""

    normalized = normalized_relative_path(relative_path)
    manifest = load_manifest(run_dir)
    for result in manifest.get("analysis_results", []):
        artifacts = result.get("analysis_artifacts", {})
        if not isinstance(artifacts, dict):
            continue
        for artifact_key, artifact_path in artifacts.items():
            try:
                candidate = normalized_relative_path(artifact_path)
            except HistoryArtifactError:
                continue
            if candidate == normalized:
                return {
                    "analysis_type": str(result.get("analysis_type") or ""),
                    "artifact_key": str(artifact_key),
                }
    raise HistoryArtifactError(
        "Artifact is registered, but it is not an analysis plot artifact.",
        400,
    )


def file_payload(run_dir: Path, relative_path: str, *, kind: str, label: str) -> dict[str, Any]:
    path = resolve_registered_artifact(run_dir, relative_path)
    stat = path.stat()
    return {
        "kind": kind,
        "label": label,
        "filename": path.name,
        "relative_path": normalized_relative_path(relative_path),
        "size_bytes": stat.st_size,
        "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "preview": path.suffix.lower() == ".png",
    }


def nested_value(payload: dict[str, Any], path: Any) -> Any:
    value: Any = payload
    for key in path if isinstance(path, (list, tuple)) else []:
        if not isinstance(value, dict):
            return None
        value = value.get(str(key))
    return value


def list_analysis_groups(run_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(run_dir).resolve()
    manifest = load_manifest(root)
    groups: list[dict[str, Any]] = []
    for index, result in enumerate(manifest.get("analysis_results", []), start=1):
        analysis_type = str(result.get("analysis_type") or "")
        definition = ANALYSIS_DEFINITIONS.get(analysis_type, {})
        history_definition = (
            definition.get("history", {}) if isinstance(definition, dict) else {}
        )
        if isinstance(history_definition, dict) and history_definition:
            try:
                raw = file_payload(
                    root,
                    str(result.get("raw_dta", "")),
                    kind="raw_dta",
                    label=str(history_definition.get("raw_label") or "Raw DTA"),
                )
                status = str(result.get("analysis_status") or "complete").lower()
                artifacts = []
                raw_relative = normalized_relative_path(result.get("raw_dta", ""))
                for export in manifest.get("dta_csv_exports", []):
                    if not isinstance(export, dict):
                        continue
                    try:
                        source_relative = normalized_relative_path(
                            export.get("source_dta", "")
                        )
                    except HistoryArtifactError:
                        continue
                    if source_relative != raw_relative or not export.get("csv_file"):
                        continue
                    artifacts.append(
                        file_payload(
                            root,
                            str(export["csv_file"]),
                            kind="raw_csv",
                            label=ARTIFACT_LABELS["raw_csv"],
                        )
                    )
                    break
                if status == "complete":
                    artifact_map = result.get("analysis_artifacts", {})
                    if not isinstance(artifact_map, dict):
                        raise HistoryArtifactError("Analysis artifact record is incomplete.")
                    artifact_specs = history_definition.get("artifacts", {})
                    if not isinstance(artifact_specs, dict):
                        raise HistoryArtifactError("Analysis History definition is invalid.")
                    for kind, artifact_spec in artifact_specs.items():
                        relative = artifact_map.get(kind)
                        if not relative:
                            raise HistoryArtifactError("Analysis artifact record is incomplete.")
                        if not isinstance(artifact_spec, dict):
                            raise HistoryArtifactError("Analysis History definition is invalid.")
                        artifact = file_payload(
                            root,
                            str(relative),
                            kind=str(kind),
                            label=str(artifact_spec.get("label") or kind),
                        )
                        for presentation_key in ("plot_kind", "plot_label"):
                            if artifact_spec.get(presentation_key):
                                artifact[presentation_key] = str(
                                    artifact_spec[presentation_key]
                                )
                        artifacts.append(artifact)
            except HistoryArtifactError:
                # A complete result is shown only after both final artifacts
                # exist. A registered failed result still shows its source DTA.
                continue

            summary = result.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            quality = summary.get("quality", {})
            if not isinstance(quality, dict):
                quality = {}
            summary_items = []
            if status == "complete":
                summary_context = dict(summary)
                summary_context["analysis_version"] = result.get("analysis_version")
                summary_specs = history_definition.get("summary_items", [])
                if not isinstance(summary_specs, list):
                    summary_specs = []
                for summary_spec in summary_specs:
                    if not isinstance(summary_spec, dict):
                        continue
                    summary_items.append(
                        {
                            "label": str(summary_spec.get("label") or "Result"),
                            "value": nested_value(
                                summary_context,
                                summary_spec.get("path"),
                            ),
                            "format": str(summary_spec.get("format") or "text"),
                        }
                    )
            meta_items = history_definition.get("meta_items", [])
            if not isinstance(meta_items, list):
                meta_items = []
            groups.append(
                {
                    "id": f"analysis-{index}",
                    "analysis_type": analysis_type,
                    "analysis_status": status,
                    "technique": str(result.get("technique") or ""),
                    "label": str(
                        result.get("label")
                        or history_definition.get("default_label")
                        or analysis_type
                    ),
                    "meta_items": list(meta_items),
                    "summary_items": summary_items,
                    "warnings": list(quality.get("warnings", []))
                    if isinstance(quality.get("warnings"), list)
                    else [],
                    "error": str(result.get("error") or ""),
                    "raw_dta": raw,
                    "artifacts": artifacts,
                }
            )
            continue

        try:
            raw = file_payload(
                root,
                str(result.get("raw_dta", "")),
                kind="raw_dta",
                label="Raw continuous CA DTA",
            )
            artifacts = []
            raw_csv = result.get("raw_csv")
            if raw_csv:
                artifacts.append(
                    file_payload(
                        root,
                        str(raw_csv),
                        kind="raw_csv",
                        label=ARTIFACT_LABELS["raw_csv"],
                    )
                )
            artifact_map = result.get("analysis_artifacts", {})
            if not isinstance(artifact_map, dict):
                raise HistoryArtifactError("Analysis artifact record is incomplete.")
            for kind in (
                "rpm_schedule_json",
                "summary_csv",
                "analysis_json",
                "trace_plot_png",
                "levich_plot_png",
                "kl_plot_png",
            ):
                relative = artifact_map.get(kind)
                if not relative:
                    raise HistoryArtifactError("Analysis artifact record is incomplete.")
                artifacts.append(
                    file_payload(
                        root,
                        str(relative),
                        kind=kind,
                        label=ARTIFACT_LABELS[kind],
                    )
                )
        except HistoryArtifactError:
            # A partially written analysis never appears as a misleading
            # complete History result. It becomes visible after registration
            # and every expected file exists.
            continue
        groups.append(
            {
                "id": f"analysis-{index}",
                "analysis_type": str(
                    result.get("analysis_type") or "levich_koutecky_levich"
                ),
                "analysis_status": str(result.get("analysis_status") or "complete"),
                "technique": str(result.get("technique") or "levich_rpm_sweep_ca"),
                "label": str(result.get("label") or "Levich CA RPM Sweep"),
                "rpm_source": str(result.get("rpm_source") or "commanded"),
                "stabilization_mode": str(
                    result.get("stabilization_mode") or "fixed delay"
                ),
                "meta_items": [
                    f"RPM source: {result.get('rpm_source') or 'commanded'}",
                    (
                        "Stabilization mode: "
                        f"{result.get('stabilization_mode') or 'fixed delay'}"
                    ),
                ],
                "summary_items": [],
                "warnings": [],
                "error": "",
                "raw_dta": raw,
                "artifacts": artifacts,
            }
        )
    return groups
