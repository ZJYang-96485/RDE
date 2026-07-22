"""Per-trial uncompensated-resistance validation and metadata.

This module is intentionally independent of ToolkitPy so the safety policy can
be tested with deterministic fakes.  Hardware acquisition is supplied through
``measure_ru`` and each call always starts with a fresh metadata object.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any, Callable


class CriticalHardwareError(RuntimeError):
    """A hardware condition that must abort the complete run plan."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_trial_metadata(config: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = config if isinstance(config, dict) else {}
    return {
        "ru_attempts_ohm": [],
        "ru_attempt_diagnostics": [],
        "ru_selected_ohm": None,
        "ru_repeatability": None,
        "ru_validation_passed": False,
        "compensation_fraction": float(settings.get("compensation_fraction", 0.80)),
        "ru_applied_ohm": None,
        "ir_compensation_enabled": False,
        "ocp_stabilization_status": None,
        "trial_status": None,
        "skip_reason": None,
        "started_at": utc_now(),
        "completed_at": None,
    }


def relative_difference(first: float, second: float) -> float:
    return abs(first - second) / ((first + second) / 2.0)


def _valid_number(value: Any, minimum: float, maximum: float) -> tuple[float | None, str | None]:
    if value is None:
        return None, "Ru measurement returned no value"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "Ru measurement was not numeric"
    if not math.isfinite(number):
        return None, "Ru measurement was not finite"
    if number <= 0:
        return None, "Ru measurement must be greater than zero"
    if number < minimum or number > maximum:
        return None, f"Ru measurement {number:g} ohm is outside {minimum:g}..{maximum:g} ohm"
    return number, None


def _critical_exception(exc: BaseException) -> bool:
    if isinstance(exc, CriticalHardwareError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "communication",
            "connection",
            "disconnected",
            "reference electrode",
            "compliance",
            "overload",
            "relay",
            "multiple electrode",
            "pstat is invalid",
            "potentiostat is invalid",
        )
    )


def _emit(emit_event: Callable[..., Any] | None, event_type: str, **fields: Any) -> None:
    if emit_event is not None:
        emit_event(event_type, **fields)


def determine_ru(
    measure_ru: Callable[[int], Any],
    config: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    emit_event: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Measure and validate a new Ru for exactly one trial.

    The first agreeing pair is averaged.  If the first pair disagrees, a third
    measurement is taken and its median is selected only when the median and
    its nearest neighbour satisfy the repeatability limit.
    """

    settings = config if isinstance(config, dict) else {}
    attempts_max = max(3, int(settings.get("ru_retry_count", 3)))
    tolerance = float(settings.get("ru_repeatability_limit", 0.05))
    minimum = float(settings.get("ru_min_ohm", 0.01))
    maximum = float(settings.get("ru_max_ohm", 100000.0))
    result = metadata if isinstance(metadata, dict) else default_trial_metadata(settings)
    valid_values: list[float] = []

    for attempt in range(1, attempts_max + 1):
        _emit(emit_event, "ru_measurement_started", attempt=attempt, attempt_limit=attempts_max)
        raw_value: Any = None
        try:
            raw_value = measure_ru(attempt)
        except Exception as exc:
            if _critical_exception(exc):
                _emit(emit_event, "critical_hardware_error", attempt=attempt, reason=str(exc))
                raise CriticalHardwareError(str(exc)) from exc
            reason = f"Ru measurement error: {type(exc).__name__}: {exc}"
            result["ru_attempts_ohm"].append(None)
            result["ru_attempt_diagnostics"].append({"attempt": attempt, "value_ohm": None, "valid": False, "reason": reason})
            _emit(emit_event, "ru_measurement_rejected", attempt=attempt, ru_ohm=None, reason=reason)
            continue

        value, reason = _valid_number(raw_value, minimum, maximum)
        result["ru_attempts_ohm"].append(value if value is not None else raw_value)
        result["ru_attempt_diagnostics"].append(
            {"attempt": attempt, "value_ohm": value, "valid": reason is None, "reason": reason}
        )
        if reason is not None:
            _emit(emit_event, "ru_measurement_rejected", attempt=attempt, ru_ohm=value, reason=reason)
            continue

        assert value is not None
        valid_values.append(value)
        _emit(emit_event, "ru_measurement_completed", attempt=attempt, ru_ohm=value)

        if len(valid_values) == 2:
            repeatability = relative_difference(valid_values[0], valid_values[1])
            result["ru_repeatability"] = repeatability
            if repeatability <= tolerance:
                selected = statistics.mean(valid_values)
                result.update(
                    {
                        "ru_selected_ohm": selected,
                        "ru_validation_passed": True,
                        "ru_applied_ohm": selected * float(result["compensation_fraction"]),
                    }
                )
                _emit(emit_event, "ru_validation_passed", ru_selected_ohm=selected, ru_repeatability=repeatability)
                return result
            _emit(emit_event, "ru_validation_retry", ru_repeatability=repeatability, limit=tolerance)

        if len(valid_values) >= 3:
            ordered = sorted(valid_values)
            median = float(statistics.median(ordered))
            neighbours = list(ordered)
            neighbours.remove(median)
            nearest = min(neighbours, key=lambda value: abs(value - median))
            repeatability = relative_difference(median, nearest)
            result["ru_repeatability"] = repeatability
            if repeatability <= tolerance:
                result.update(
                    {
                        "ru_selected_ohm": median,
                        "ru_validation_passed": True,
                        "ru_applied_ohm": median * float(result["compensation_fraction"]),
                    }
                )
                _emit(emit_event, "ru_validation_passed", ru_selected_ohm=median, ru_repeatability=repeatability)
                return result
            _emit(emit_event, "ru_validation_retry", ru_repeatability=repeatability, limit=tolerance)

    reason = "Unable to obtain a valid Ru after configured attempts"
    result.update(
        {
            "ru_validation_passed": False,
            "trial_status": "skipped",
            "skip_reason": reason,
            "completed_at": utc_now(),
        }
    )
    _emit(emit_event, "ru_validation_failed", reason=reason)
    _emit(emit_event, "trial_skipped", reason=reason)
    return result
