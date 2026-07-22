"""ToolkitPy implementation of automatic OCP stabilization and fresh Ru measurement."""

from __future__ import annotations

import statistics
import time
from typing import Any, Callable

import toolkitpy as tkp

try:
    from gamry_worker.device import normalize_sections, select_pstat_name
    from gamry_worker.live_adapters import normalize_eis_point
    from gamry_worker.trial_preparation import CriticalHardwareError, default_trial_metadata, determine_ru, utc_now
except ModuleNotFoundError:
    from device import normalize_sections, select_pstat_name
    from live_adapters import normalize_eis_point
    from trial_preparation import CriticalHardwareError, default_trial_metadata, determine_ru, utc_now


def _emit(emit_event: Callable[..., Any] | None, event_type: str, **fields: Any) -> None:
    if emit_event is not None:
        emit_event(event_type, **fields)


def _ensure_valid(pstat: Any, context: str) -> None:
    if not tkp.pstat_is_valid(pstat):
        raise CriticalHardwareError(f"Potentiostat communication lost while {context}; pstat is invalid.")


def _disable_and_off(pstat: Any) -> None:
    errors = []
    try:
        pstat.set_pos_feed_enable(False)
    except Exception as exc:
        errors.append(f"disable iR compensation: {exc}")
    try:
        pstat.set_cell(tkp.CELL_OFF)
    except Exception as exc:
        errors.append(f"turn cell off: {exc}")
    if errors:
        raise CriticalHardwareError("Failed to return Gamry to a safe state: " + "; ".join(errors))


def _initialize_for_ru(pstat: Any) -> None:
    pstat.set_cell(tkp.CELL_OFF)
    pstat.set_pos_feed_enable(False)
    try:
        pstat.set_pos_feed_resistance(0.0)
    except Exception:
        pass
    pstat.set_ach_select(tkp.ACHSELECT_GND)
    pstat.set_ie_stability(tkp.STABILITY_FAST)
    pstat.set_ca_speed(tkp.CASPEED_NORM)
    pstat.set_ground(tkp.FLOAT)
    pstat.set_i_convention(tkp.ICONVENTION.ANODIC)
    pstat.set_ich_range(3.0)
    pstat.set_ich_range_mode(False)
    pstat.set_vch_range(3.0)
    pstat.set_vch_range_mode(False)
    pstat.set_ie_range_lower_limit(0)
    pstat.set_ctrl_mode(tkp.PSTATMODE)


def _set_verified_cell(pstat: Any, state: Any) -> None:
    pstat.set_cell(state)
    _ensure_valid(pstat, "verifying relay state")
    if hasattr(pstat, "cell"):
        actual = pstat.cell()
        if actual != state:
            raise CriticalHardwareError(
                f"Gamry relay state cannot be verified (requested {state!r}, read {actual!r})."
            )


def _wait_for_stable_ocp(
    pstat: Any,
    settings: dict[str, Any],
    emit_event: Callable[..., Any] | None,
) -> tuple[bool, float | None, list[float]]:
    minimum_s = max(0.0, float(settings.get("ocp_stabilization_s", 5.0)))
    timeout_s = max(minimum_s, float(settings.get("ocp_stabilization_timeout_s", 30.0)))
    interval_s = max(0.05, float(settings.get("ocp_sample_interval_s", 0.25)))
    window_size = max(2, int(settings.get("ocp_stability_window", 5)))
    stability_limit_v = max(0.0, float(settings.get("ocp_stability_limit_v", 0.005)))
    absolute_limit_v = abs(float(settings.get("ocp_abs_limit_v", 2.5)))
    started = time.monotonic()
    samples: list[float] = []
    _emit(emit_event, "ocp_stabilization_started", minimum_s=minimum_s, timeout_s=timeout_s)

    while True:
        _ensure_valid(pstat, "waiting for OCP stabilization")
        value = float(pstat.measure_v())
        if abs(value) > absolute_limit_v:
            raise CriticalHardwareError(
                f"Reference electrode/channel cannot be verified: OCP {value:g} V exceeds "
                f"the configured absolute limit of {absolute_limit_v:g} V."
            )
        samples.append(value)
        elapsed = time.monotonic() - started
        recent = samples[-window_size:]
        stable = (
            elapsed >= minimum_s
            and len(recent) >= window_size
            and max(recent) - min(recent) <= stability_limit_v
        )
        if stable:
            selected = float(statistics.mean(recent))
            _emit(emit_event, "ocp_stabilized", ocp_v=selected, elapsed_s=elapsed)
            return True, selected, samples
        if elapsed >= timeout_s:
            _emit(
                emit_event,
                "ocp_stabilization_failed",
                elapsed_s=elapsed,
                last_window_span_v=(max(recent) - min(recent)) if recent else None,
            )
            return False, (float(statistics.mean(recent)) if recent else None), samples
        time.sleep(interval_s)


def _measure_ru_once(pstat: Any, settings: dict[str, Any], ocp_v: float) -> float:
    _ensure_valid(pstat, "starting Ru measurement")
    frequency_hz = abs(float(settings.get("ru_frequency_hz", 100000.0)))
    ac_voltage_v = abs(float(settings.get("ru_ac_voltage_v", 0.005)))
    estimated_z = abs(float(settings.get("ru_estimated_z_ohm", 100.0)))
    if frequency_hz <= 0 or ac_voltage_v <= 0 or estimated_z <= 0:
        raise ValueError("Ru frequency, AC voltage, and estimated impedance must be positive")

    pstat.set_pos_feed_enable(False)
    pstat.set_voltage(float(ocp_v))
    _set_verified_cell(pstat, tkp.CELL_ON)
    time.sleep(max(0.0, float(settings.get("ru_settle_s", 0.10))))
    dc_current = float(pstat.measure_i())
    ie_range = pstat.test_ie_range(abs(dc_current) + 1.414 * (ac_voltage_v / estimated_z))
    pstat.set_ie_range(ie_range)
    pstat.set_ie_range_mode(False)

    readz = tkp.ReadZ(pstat)
    curve = tkp.ZCurve(1)
    try:
        readz.set_gain(1.0)
        readz.set_inoise(0.0)
        readz.set_vnoise(0.0)
        readz.set_ienoise(0.0)
        readz.set_zmod(estimated_z)
        readz.set_vdc(float(ocp_v))
        readz.set_speed(int(settings.get("ru_speed", 1)))
        readz.set_drift_cor(False)
        readz.set_idc(dc_current)
        status = readz.measure(frequency_hz, ac_voltage_v, float(ocp_v))
        _ensure_valid(pstat, "measuring Ru")
        if status is False:
            raise RuntimeError("Gamry did not accept the Ru impedance point")
        curve.add_point(readz, pstat.measure_temp())
        rows = curve.acq_data()
        if len(rows) < 1:
            raise RuntimeError("Gamry returned no Ru impedance data")
        point = normalize_eis_point(rows[0])
        return float(point["zreal_ohm"])
    finally:
        try:
            _set_verified_cell(pstat, tkp.CELL_MON)
        except Exception:
            # The outer finally performs strict safe-state cleanup.
            pass
        del readz
        del curve


def prepare_real_trial(
    step: dict[str, Any],
    settings: dict[str, Any],
    *,
    emit_event: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a fresh trial metadata object and an enriched acquisition step."""

    metadata = default_trial_metadata(settings)
    electrode_channel = str(step.get("electrode_channel") or settings.get("electrode_channel", "primary")).strip()
    _emit(emit_event, "electrode_channel_selected", electrode_channel=electrode_channel)
    if electrode_channel.lower() not in {"primary", "working", "we"}:
        raise CriticalHardwareError(f"Selected electrode channel '{electrode_channel}' cannot be verified.")

    initialized = False
    pstat = None
    try:
        tkp.toolkitpy_init("rde_trial_ru_preparation")
        initialized = True
        sections = normalize_sections(tkp.enum_sections())
        if bool(settings.get("require_single_instrument", True)) and len(sections) != 1:
            raise CriticalHardwareError(
                "Expected exactly one Gamry electrode channel/instrument, but ToolkitPy detected "
                f"{len(sections)}: {', '.join(sections) or 'none'}."
            )
        pstat_name = select_pstat_name(tkp, step)
        pstat = tkp.Pstat(pstat_name)
        if hasattr(pstat, "open"):
            pstat.open()
        _ensure_valid(pstat, "opening the selected electrode channel")
        _initialize_for_ru(pstat)
        _set_verified_cell(pstat, tkp.CELL_MON)
        _emit(emit_event, "electrode_channel_verified", electrode_channel=electrode_channel, pstat=pstat_name)

        stable, ocp_v, samples = _wait_for_stable_ocp(pstat, settings, emit_event)
        metadata["ocp_stabilization_status"] = "stable" if stable else "failed"
        metadata["ocp_voltage_v"] = ocp_v
        metadata["ocp_samples_v"] = samples
        if not stable or ocp_v is None:
            reason = "OCP did not stabilize within the configured timeout"
            metadata.update({"trial_status": "skipped", "skip_reason": reason, "completed_at": utc_now()})
            _emit(emit_event, "trial_skipped", reason=reason)
            return metadata, dict(step)

        metadata = determine_ru(
            lambda _attempt: _measure_ru_once(pstat, settings, float(ocp_v)),
            settings,
            metadata=metadata,
            emit_event=emit_event,
        )
        effective = dict(step)
        if metadata["ru_validation_passed"]:
            effective.update(
                {
                    "_trial_ru_validation_passed": True,
                    "_trial_ru_selected_ohm": metadata["ru_selected_ohm"],
                    "_trial_ru_applied_ohm": metadata["ru_applied_ohm"],
                    "_trial_fixed_current_range_a": float(settings.get("fixed_current_range_a", 0.003)),
                }
            )
        return metadata, effective
    finally:
        cleanup_error = None
        if pstat is not None:
            try:
                _disable_and_off(pstat)
                _emit(emit_event, "gamry_settings_reset", ir_compensation="disabled", cell="off")
            except Exception as exc:
                cleanup_error = exc
            del pstat
        if initialized:
            try:
                tkp.toolkitpy_close()
            except Exception as exc:
                cleanup_error = cleanup_error or CriticalHardwareError(f"ToolkitPy close failed: {exc}")
        if cleanup_error is not None:
            raise cleanup_error
