from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from workflow.data_manager import load_manifest


class HistoryArtifactError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


ARTIFACT_LABELS = {
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
    for result in manifest.get("analysis_results", []):
        raw = result.get("raw_dta")
        if raw:
            paths.add(normalized_relative_path(raw))
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


def list_analysis_groups(run_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(run_dir).resolve()
    manifest = load_manifest(root)
    groups: list[dict[str, Any]] = []
    for index, result in enumerate(manifest.get("analysis_results", []), start=1):
        try:
            raw = file_payload(
                root,
                str(result.get("raw_dta", "")),
                kind="raw_dta",
                label="Raw continuous CA DTA",
            )
            artifacts = []
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
                "technique": str(result.get("technique") or "levich_rpm_sweep_ca"),
                "label": str(result.get("label") or "Levich CA RPM Sweep"),
                "rpm_source": str(result.get("rpm_source") or "commanded"),
                "stabilization_mode": str(
                    result.get("stabilization_mode") or "fixed delay"
                ),
                "raw_dta": raw,
                "artifacts": artifacts,
            }
        )
    return groups
