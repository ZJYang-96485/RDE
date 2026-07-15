from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import toolkitpy as tkp

try:
    from gamry_worker.device import select_pstat_name
    from gamry_worker.live_adapters import LiveCurveEmitter, normalize_cp_acq_rows
except ModuleNotFoundError:
    from device import select_pstat_name
    from live_adapters import LiveCurveEmitter, normalize_cp_acq_rows


def initialize_pstat(pstat: Any, sample_period_s: float, max_current_a: float) -> None:
    """Python mapping of the installed Framework Chronopotentiometry.exp setup."""
    pstat.set_cell(tkp.CELL_OFF)
    pstat.set_pos_feed_enable(False)
    pstat.set_ctrl_mode(tkp.GSTATMODE)
    pstat.set_ie_stability(tkp.STABILITY_FAST)
    pstat.set_ca_speed(tkp.CASPEED_NORM)
    pstat.set_sense_speed_mode(True)
    pstat.set_i_convention(tkp.ICONVENTION.ANODIC)
    pstat.set_ground(tkp.FLOAT)
    pstat.set_ich_range(3.0)
    pstat.set_ich_range_mode(True)
    pstat.set_ich_offset_enable(False)
    pstat.set_ich_filter(1.0 / sample_period_s)
    pstat.set_vch_range(10.0)
    pstat.set_vch_range_mode(True)
    pstat.set_vch_offset_enable(False)
    pstat.set_vch_filter(1.0 / sample_period_s)
    pstat.set_ach_range(3.0)
    pstat.set_ie_range_lower_limit(0)
    pstat.set_ie_range(pstat.test_ie_range(max(abs(max_current_a), 1e-12)))
    pstat.set_ie_range_mode(False)
    pstat.set_analog_out(0.0)
    pstat.set_voltage(0.0)
    pstat.set_irupt_mode(tkp.IRUPTOFF)


def _last_curve_value(data: Any, field: str) -> float | None:
    try:
        values = data[field]
        if len(values):
            return float(values[-1])
    except (KeyError, TypeError, ValueError):
        pass
    return None


def run_single_cp(
    pstat: Any,
    step: dict[str, Any],
    output_path: str,
    live_dir: str | None = None,
) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    current_a = float(step.get("current_a", 1e-5))
    duration_s = float(step.get("duration_s", 60.0))
    sample_period_s = float(step.get("sample_period_s", 0.5))
    voltage_min_v = float(step.get("voltage_limit_low_v", step.get("voltage_min_v", -10.0)))
    voltage_max_v = float(step.get("voltage_limit_high_v", step.get("voltage_max_v", 10.0)))
    pre_current_a = float(step.get("pre_current_a", 0.0))
    pre_step_time_s = float(step.get("pre_step_time_s", sample_period_s))
    post_current_a = float(step.get("post_current_a", 0.0))
    post_step_time_s = float(step.get("post_step_time_s", sample_period_s))
    max_size = int(step.get("max_size", 100000))
    expected_max_current_a = abs(
        float(
            step.get(
                "expected_max_current_a",
                max(abs(pre_current_a), abs(current_a), abs(post_current_a)),
            )
        )
    )

    if current_a == 0:
        raise ValueError("CP current_a must not be zero.")
    if duration_s <= 0 or sample_period_s <= 0:
        raise ValueError("CP duration_s and sample_period_s must be positive.")
    if pre_step_time_s < sample_period_s or post_step_time_s < sample_period_s:
        raise ValueError("CP pre/post step times must be at least one sample period.")
    if voltage_min_v >= voltage_max_v:
        raise ValueError("CP voltage_limit_low_v must be lower than voltage_limit_high_v.")
    if expected_max_current_a < max(abs(pre_current_a), abs(current_a), abs(post_current_a)):
        raise ValueError("CP expected_max_current_a must cover every applied current.")

    tkp.check_hve(pstat, max(abs(voltage_min_v), abs(voltage_max_v)))
    initialize_pstat(
        pstat,
        sample_period_s,
        expected_max_current_a,
    )
    if tkp.hve(pstat):
        pstat.set_electrometer(tkp.ELECTROMETER_HIGH_V)

    curve = tkp.ChronoCurve(pstat, max_size)
    emitter = LiveCurveEmitter(live_dir, normalize_cp_acq_rows)
    signal = None
    cutoff_reason = None

    try:
        # The local Framework IDSTEP signal maps to signal_d_step_new. In
        # GSTATMODE all three values are amperes (verified in installed docs).
        signal = pstat.signal_d_step_new(
            pre_current_a,
            pre_step_time_s,
            current_a,
            duration_s,
            post_current_a,
            post_step_time_s,
            sample_period_s,
            tkp.GSTATMODE,
        )
        pstat.set_signal_d_step(signal)
        pstat.init_signal()
        pstat.set_cell(tkp.CELL_ON)
        curve.run(True)

        while tkp.pstat_is_valid(pstat) and curve.running():
            data = curve.acq_data()
            emitter.emit_new(data)
            if len(data):
                # The installed Python ChronoCurve exposes only generic x-stop
                # methods, while Framework's CP script names these as voltage
                # limits. Inspect vf directly so the cutoff meaning is explicit.
                voltage_v = float(data["vf"][-1])
                if voltage_v <= voltage_min_v:
                    cutoff_reason = "voltage_limit_low_v"
                    curve.stop()
                elif voltage_v >= voltage_max_v:
                    cutoff_reason = "voltage_limit_high_v"
                    curve.stop()
            time.sleep(max(0.01, min(sample_period_s, 0.25)))

        data = curve.acq_data()
        emitter.emit_new(data)
        pstat_valid = bool(tkp.pstat_is_valid(pstat))
        elapsed_s = _last_curve_value(data, "time")
        final_voltage_v = _last_curve_value(data, "vf")
        if cutoff_reason is not None:
            stop_reason = "voltage_cutoff"
        elif pstat_valid:
            stop_reason = "duration_complete"
        else:
            stop_reason = "instrument_invalid"

        if pstat_valid:
            pstat.set_cell(tkp.CELL_OFF)
        tkp.print_default_dta_file(curve, pstat, str(output.resolve()), "CHRONOP")

        result = {
            "ok": True,
            "technique": "cp",
            "output": str(output),
            "current_a": current_a,
            "duration_s": duration_s,
            "sample_period_s": sample_period_s,
            "voltage_limit_low_v": voltage_min_v,
            "voltage_limit_high_v": voltage_max_v,
            "expected_max_current_a": expected_max_current_a,
            "cutoff_reason": cutoff_reason,
            "stop_reason": stop_reason,
            "stop_detail": cutoff_reason,
            "elapsed_s": elapsed_s,
            "final_voltage_v": final_voltage_v,
            "points": int(curve.count()),
        }
        result.update(emitter.result_fields())
        return result
    finally:
        try:
            if curve.running():
                curve.stop()
        except Exception:
            pass
        try:
            if tkp.pstat_is_valid(pstat):
                pstat.set_cell(tkp.CELL_OFF)
        except Exception:
            pass
        try:
            tkp.reset_hve(pstat)
        except Exception:
            pass
        try:
            curve.free()
        except Exception:
            pass
        if signal is not None:
            del signal
        del curve


def run(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
    live_dir: str | None = None,
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("outputs must contain at least one path.")
    tkp.toolkitpy_init("run_cp.py")
    pstat = None
    try:
        pstat_name = select_pstat_name(tkp, step)
        pstat = tkp.Pstat(pstat_name)
        if hasattr(pstat, "open"):
            pstat.open()
        result = run_single_cp(pstat, step, outputs[0], live_dir=live_dir)
        result["sample_id"] = sample_id
        result["pstat"] = pstat_name
        return result
    finally:
        if pstat is not None:
            try:
                if tkp.pstat_is_valid(pstat):
                    pstat.set_cell(tkp.CELL_OFF)
            except Exception:
                pass
            del pstat
        try:
            tkp.toolkitpy_close()
        except Exception:
            pass
