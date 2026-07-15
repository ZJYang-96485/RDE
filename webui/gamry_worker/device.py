from __future__ import annotations

import sys
from typing import Any


class GamryDeviceError(RuntimeError):
    pass


def normalize_sections(values: Any) -> list[str]:
    if values is None:
        return []

    return [str(value).strip() for value in values if str(value).strip()]


def configured_step(
    step: dict[str, Any],
    gamry_config: dict[str, Any] | None,
) -> dict[str, Any]:
    result = dict(step)
    config = gamry_config if isinstance(gamry_config, dict) else {}

    if not str(result.get("instrument_label", "") or "").strip():
        label = str(config.get("instrument_label", "") or "").strip()

        if label:
            result["instrument_label"] = label

    if "instrument_index" not in result and "instrument_index" in config:
        result["instrument_index"] = config["instrument_index"]

    return result


def select_pstat_name(tkp: Any, step: dict[str, Any]) -> str:
    sections = normalize_sections(tkp.enum_sections())

    if not sections:
        raise GamryDeviceError(
            "ToolkitPy did not find a Gamry potentiostat. Check instrument power and the "
            "USB/Ethernet connection, then close Gamry Framework or Instrument Manager "
            "before trying again."
        )

    configured_label = str(step.get("instrument_label", "") or "").strip()

    if configured_label:
        if configured_label not in sections:
            raise GamryDeviceError(
                f"Configured Gamry instrument '{configured_label}' is not available. "
                f"ToolkitPy detected: {', '.join(sections)}."
            )

        return configured_label

    try:
        instrument_index = int(step.get("instrument_index", 0))
    except (TypeError, ValueError) as exc:
        raise GamryDeviceError("Gamry instrument_index must be an integer.") from exc

    if instrument_index < 0 or instrument_index >= len(sections):
        raise GamryDeviceError(
            f"Gamry instrument_index {instrument_index} is out of range; "
            f"ToolkitPy detected {len(sections)} instrument(s): {', '.join(sections)}."
        )

    return sections[instrument_index]


def probe_toolkitpy(tkp: Any = None) -> dict[str, Any]:
    if tkp is None:
        import toolkitpy as tkp_module

        tkp = tkp_module

    initialized = False

    try:
        tkp.toolkitpy_init("rde_gamry_probe")
        initialized = True
        sections = normalize_sections(tkp.enum_sections())

        return {
            "ok": True,
            "connected": bool(sections),
            "sections": sections,
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "toolkitpy_path": str(getattr(tkp, "__file__", "") or ""),
        }
    finally:
        if initialized:
            try:
                tkp.toolkitpy_close()
            except Exception:
                pass
