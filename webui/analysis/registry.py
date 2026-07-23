"""Extensible protocol-facing registry for optional post-acquisition analyses."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict


ANALYSIS_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "cumulative_charge": {
        "label": "Cumulative charge",
        "supported_techniques": {"ca", "ca_staircase"},
        "method": "trapezoidal",
        "analysis_version": "ca-charge-v1",
        "plot_adapters": {
            "series_csv": "analysis.ca_charge:load_charge_series",
        },
        "history": {
            "raw_label": "Raw CA DTA",
            "default_label": "CA Cumulative Charge",
            "meta_items": [
                "Signed Gamry current",
                "Final result recomputed from DTA",
            ],
            "artifacts": {
                "series_csv": {
                    "label": "Charge time series CSV",
                    "plot_kind": "xy",
                    "plot_label": "Cumulative Charge vs Time",
                },
                "summary_json": {
                    "label": "Charge analysis JSON",
                },
            },
            "summary_items": [
                {
                    "label": "Final signed charge",
                    "path": ["result", "final_signed_charge_c"],
                    "format": "charge",
                },
                {
                    "label": "Integrated duration",
                    "path": ["result", "duration_s"],
                    "format": "duration",
                },
                {
                    "label": "Integrated intervals",
                    "path": ["result", "integrated_intervals"],
                    "format": "integer",
                },
                {
                    "label": "Skipped intervals",
                    "path": ["result", "skipped_intervals"],
                    "format": "integer",
                },
                {
                    "label": "Method",
                    "path": ["integration_method"],
                    "format": "text",
                },
                {
                    "label": "Source",
                    "path": ["source", "label"],
                    "format": "text",
                },
                {
                    "label": "Source points",
                    "path": ["source", "source_points"],
                    "format": "integer",
                },
                {
                    "label": "Time monotonic",
                    "path": ["quality", "time_monotonic"],
                    "format": "boolean",
                },
                {
                    "label": "Analysis version",
                    "path": ["analysis_version"],
                    "format": "text",
                },
            ],
        },
    },
}


def normalize_analysis_config(
    raw_analysis: Any,
    *,
    technique: str,
    default_cumulative_charge: bool = False,
) -> Dict[str, Any]:
    """Validate known analysis blocks while preserving an extensible envelope."""

    if raw_analysis in (None, ""):
        raw: Dict[str, Any] = {}
    elif isinstance(raw_analysis, dict):
        raw = dict(raw_analysis)
    else:
        raise ValueError("analysis must be an object")

    unknown = sorted(set(raw) - set(ANALYSIS_DEFINITIONS))
    if unknown:
        raise ValueError(f"unsupported analysis option(s): {', '.join(unknown)}")

    normalized: Dict[str, Any] = {}
    definition = ANALYSIS_DEFINITIONS["cumulative_charge"]
    block = raw.get("cumulative_charge")
    if block is None:
        enabled = bool(default_cumulative_charge)
        method = str(definition["method"])
    elif isinstance(block, dict):
        enabled = bool(block.get("enabled", False))
        method = str(block.get("method", definition["method"])).strip().lower()
    else:
        raise ValueError("analysis.cumulative_charge must be an object")

    normalized_technique = str(technique or "").strip().lower()
    if method != definition["method"]:
        raise ValueError("analysis.cumulative_charge.method must be 'trapezoidal'")
    if enabled and normalized_technique not in definition["supported_techniques"]:
        raise ValueError(
            "cumulative-charge analysis is supported only for CA in pilot v1; "
            "Levich RPM sweep CA remains unsupported"
        )
    if block is not None or enabled:
        normalized["cumulative_charge"] = {
            "enabled": enabled,
            "method": method,
        }
    return normalized


def analysis_enabled(step: Dict[str, Any], analysis_key: str) -> bool:
    analysis = step.get("analysis", {})
    if not isinstance(analysis, dict):
        return False
    block = analysis.get(analysis_key, {})
    return bool(isinstance(block, dict) and block.get("enabled", False))


def load_analysis_plot(
    analysis_type: str,
    artifact_key: str,
    path: str | Path,
) -> Dict[str, Any]:
    """Dispatch a registered artifact to its analysis-owned plot adapter."""

    definition = ANALYSIS_DEFINITIONS.get(str(analysis_type), {})
    adapters = definition.get("plot_adapters", {})
    adapter_path = adapters.get(str(artifact_key)) if isinstance(adapters, dict) else None
    if not adapter_path:
        raise ValueError("This registered artifact has no interactive plot adapter.")
    module_name, separator, function_name = str(adapter_path).partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("Analysis plot adapter configuration is invalid.")
    module = importlib.import_module(module_name)
    adapter = getattr(module, function_name, None)
    if not callable(adapter):
        raise ValueError("Analysis plot adapter is unavailable.")
    return adapter(path)
