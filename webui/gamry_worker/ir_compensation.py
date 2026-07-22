"""Shared fixed-range and positive-feedback application/cleanup helpers."""

from __future__ import annotations

import math
from typing import Any


POSITIVE_FEEDBACK_TECHNIQUES = {
    "ca",
    "ca_staircase",
    "levich_rpm_sweep_ca",
    "cv",
    "lsv",
}


def technique_supports_positive_feedback(technique: Any) -> bool:
    return str(technique or "").strip().lower() in POSITIVE_FEEDBACK_TECHNIQUES


def disable_ir_compensation(pstat: Any) -> None:
    """Best effort is left to callers; this function itself does not mask errors."""

    pstat.set_pos_feed_enable(False)
    try:
        pstat.set_pos_feed_resistance(0.0)
    except Exception:
        # Some ToolkitPy/device combinations accept disabling but reject a
        # resistance write while the cell is already off.
        pass


def apply_trial_settings(pstat: Any, step: dict[str, Any]) -> dict[str, Any]:
    """Apply this trial's fixed current range and conservative positive feed."""

    disable_ir_compensation(pstat)
    fixed_current = abs(float(step.get("_trial_fixed_current_range_a", 0.003)))
    if fixed_current <= 0:
        raise ValueError("fixed current range must be greater than zero")
    current_range = pstat.test_ie_range(fixed_current)
    pstat.set_ie_range(current_range)
    pstat.set_ie_range_mode(False)

    technique = str(step.get("technique", "")).strip().lower()
    validated = bool(step.get("_trial_ru_validation_passed", False))
    applied = step.get("_trial_ru_applied_ohm")
    requested = bool(validated and technique_supports_positive_feedback(technique))
    enabled = False
    reason = None
    resistance_readback = None
    if requested:
        resistance = float(applied)
        if resistance <= 0:
            raise ValueError("applied compensation resistance must be positive")
        # Gamry requires positive-feedback mode to be enabled before writing
        # its resistance.  Some instruments accept the calls but return False
        # because the hardware does not support positive feedback, so verify
        # both settings instead of reporting success from call completion.
        pstat.set_pos_feed_enable(True)
        pstat.set_pos_feed_resistance(resistance)

        enabled_reader = getattr(pstat, "pos_feed_enable", None)
        resistance_reader = getattr(pstat, "pos_feed_resistance", None)
        if callable(enabled_reader) and callable(resistance_reader):
            enabled_readback = bool(enabled_reader())
            resistance_readback = float(resistance_reader())
            enabled = bool(
                enabled_readback
                and math.isclose(resistance_readback, resistance, rel_tol=0.02, abs_tol=1e-6)
            )
        else:
            # Deterministic fakes and older Toolkit bindings may not expose
            # getters.  In that case successful setters are the best evidence.
            enabled = True

        if not enabled:
            reason = (
                "Positive-feedback iR compensation is not supported or was rejected by "
                "this potentiostat; measurement continued uncompensated"
            )
            disable_ir_compensation(pstat)

    return {
        "fixed_current_range_a": fixed_current,
        "fixed_current_range_setting": current_range,
        "ir_compensation_requested": requested,
        "ir_compensation_enabled": enabled,
        "ir_compensation_reason": reason,
        "ir_compensation_resistance_readback_ohm": resistance_readback,
    }
