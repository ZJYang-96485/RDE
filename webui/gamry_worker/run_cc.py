from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import toolkitpy as tkp

try:
    from gamry_worker.device import select_pstat_name
    from gamry_worker.live_adapters import (
        LiveCurveEmitter,
        normalize_cc_charge_acq_rows,
        normalize_cc_discharge_acq_rows,
    )
except ModuleNotFoundError:
    from device import select_pstat_name
    from live_adapters import LiveCurveEmitter, normalize_cc_charge_acq_rows, normalize_cc_discharge_acq_rows


DEFAULT_PERTURBATION_RATE = 0.01
DEFAULT_PERTURBATION_WIDTH = 0.003333
DEFAULT_TIMER_RESOLUTION = 0.0016666666
DEFAULT_MAXIMUM_STEP = 0.05
DEFAULT_MINIMUM_DIFFERENCE = 0.15
DEFAULT_CV_CP_GAIN = 0.05
DEFAULT_TI = 5.0


def initialize_pstat(pstat: Any) -> None:
    """Exact local pwr_charge.py/pwr_discharge.py hardware setup."""
    pstat.set_cell(tkp.CELL_OFF)
    pstat.set_ctrl_mode(tkp.GSTATMODE)
    pstat.set_ie_stability(tkp.STABILITY_FAST)
    pstat.set_ca_speed(tkp.CASPEED_NORM)
    pstat.set_sense_speed(tkp.SENSE_SLOW)
    pstat.set_sense_speed_mode(False)
    pstat.set_ground(tkp.FLOAT)
    pstat.set_ich_range(3.0)
    pstat.set_ich_range_mode(True)
    pstat.set_ich_offset_enable(False)
    pstat.set_ich_filter(60000.0)
    pstat.set_vch_range(10.0)
    pstat.set_vch_range_mode(True)
    pstat.set_vch_offset_enable(False)
    pstat.set_vch_filter(60000.0)
    pstat.set_ach_range(3.0)
    pstat.set_ach_offset_enable(False)
    pstat.set_ach_range_mode(True)
    pstat.set_ach_filter(60000.0)
    pstat.set_ie_range_lower_limit(7)
    pstat.set_analog_out(0.0)
    pstat.set_pos_feed_enable(False)
    pstat.set_dds_enable(False)


def _last_curve_value(data: Any, field: str) -> float | None:
    try:
        values = data[field]
        if len(values):
            return float(values[-1])
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _capacity_ah(data: Any) -> float:
    """Integrate the installed PwrCurve time/im columns using trapezoids."""
    try:
        times = data["time"]
        currents = data["im"]
        count = min(len(times), len(currents))
    except (KeyError, TypeError):
        return 0.0

    charge_c = 0.0
    for index in range(1, count):
        dt_s = max(0.0, float(times[index]) - float(times[index - 1]))
        average_current_a = (abs(float(currents[index - 1])) + abs(float(currents[index]))) / 2.0
        charge_c += average_current_a * dt_s
    return charge_c / 3600.0


def _stop_tests(data: Any) -> list[str]:
    tests = []
    for field in ("stop_test", "stop_test2"):
        raw_value = _last_curve_value(data, field)
        if raw_value is None or int(raw_value) == 0:
            continue
        try:
            tests.append(str(tkp.interpret_stop_test_int(int(raw_value))))
        except (TypeError, ValueError):
            tests.append(f"{field}={int(raw_value)}")
    return tests


def run_single_cc(
    pstat: Any,
    step: dict[str, Any],
    output_path: str,
    live_dir: str | None = None,
) -> dict[str, Any]:
    technique = str(step.get("technique", "")).strip().lower()
    if technique not in {"cc_charge", "cc_discharge"}:
        raise ValueError(f"run_cc requires cc_charge or cc_discharge, got '{technique}'.")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    current_a = float(step.get("current_a", 1e-5))
    duration_s = float(step.get("duration_s", 60.0))
    sample_period_s = float(step.get("sample_period_s", 1.0))
    voltage_cutoff_v = float(step.get("voltage_cutoff_v", 4.2 if technique == "cc_charge" else 3.0))
    working_positive = bool(step.get("working_positive", True))
    capacity_raw = step.get("capacity_cutoff_ah")
    capacity_cutoff_ah = None if capacity_raw in {None, ""} else float(capacity_raw)
    expected_max_current_a = float(step.get("expected_max_current_a", current_a))
    max_size = int(step.get("max_size", 100000))

    if current_a <= 0:
        raise ValueError(f"{technique} current_a is a positive magnitude; direction is selected by the technique.")
    if duration_s <= 0 or sample_period_s <= 0:
        raise ValueError(f"{technique} duration_s and sample_period_s must be positive.")
    if voltage_cutoff_v <= 0:
        raise ValueError(f"{technique} voltage_cutoff_v is an absolute voltage magnitude and must be positive.")
    if capacity_cutoff_ah is not None and capacity_cutoff_ah <= 0:
        raise ValueError(f"{technique} capacity_cutoff_ah must be positive when supplied.")
    if expected_max_current_a <= 0 or expected_max_current_a < current_a:
        raise ValueError(f"{technique} expected_max_current_a must be at least current_a.")

    initialize_pstat(pstat)
    curve = tkp.PwrCurve(pstat, max_size)
    normalizer = normalize_cc_charge_acq_rows if technique == "cc_charge" else normalize_cc_discharge_acq_rows
    emitter = LiveCurveEmitter(live_dir, normalizer)
    signal = None
    mode = tkp.PWR_CHARGE if technique == "cc_charge" else tkp.PWR_CURRENT_DISCHARGE
    dta_tag = "PWR800_CHARGE" if technique == "cc_charge" else "PWR800_DISCHARGE"

    try:
        if technique == "cc_charge":
            curve.set_stop_av_max(True, voltage_cutoff_v)
        else:
            curve.set_stop_av_min(True, voltage_cutoff_v)
        if capacity_cutoff_ah is not None:
            # Installed API capacity limits are coulombs, while protocols use Ah.
            curve.set_stop_aq_max(True, capacity_cutoff_ah * 3600.0)

        signal = pstat.signal_pwr_step_new(
            [current_a, current_a],
            [0.0, 0.0],
            DEFAULT_CV_CP_GAIN,
            DEFAULT_TI,
            DEFAULT_MINIMUM_DIFFERENCE,
            DEFAULT_MAXIMUM_STEP,
            [duration_s, 0.0],
            sample_period_s,
            DEFAULT_PERTURBATION_RATE,
            DEFAULT_PERTURBATION_WIDTH,
            DEFAULT_TIMER_RESOLUTION,
            [mode, mode],
            working_positive,
        )
        pstat.set_signal_pwr_step(signal)
        pstat.init_signal()
        pstat.set_cell(tkp.CELL_RELAY)
        started_at = time.monotonic()
        curve.run(True)

        while tkp.pstat_is_valid(pstat) and curve.running():
            emitter.emit_new(curve.acq_data())
            time.sleep(max(0.01, min(sample_period_s, 0.25)))
        data = curve.acq_data()
        emitter.emit_new(data)
        pstat_valid = bool(tkp.pstat_is_valid(pstat))
        elapsed_s = _last_curve_value(data, "time")
        if elapsed_s is None:
            elapsed_s = time.monotonic() - started_at
        final_voltage_v = _last_curve_value(data, "vf")
        capacity_ah = _capacity_ah(data)
        stop_tests = _stop_tests(data)

        voltage_reached = any("STOP_V_" in stop_test for stop_test in stop_tests)
        if final_voltage_v is not None:
            tolerance_v = max(1e-6, voltage_cutoff_v * 1e-3)
            if technique == "cc_charge":
                voltage_reached = voltage_reached or final_voltage_v >= voltage_cutoff_v - tolerance_v
            else:
                voltage_reached = voltage_reached or final_voltage_v <= voltage_cutoff_v + tolerance_v
        capacity_reached = (
            any("STOP_Q_" in stop_test for stop_test in stop_tests)
            or (
                capacity_cutoff_ah is not None
                and capacity_ah >= capacity_cutoff_ah * 0.999
            )
        )

        if not pstat_valid:
            stop_reason = "instrument_invalid"
        elif voltage_reached:
            stop_reason = "voltage_cutoff"
        elif capacity_reached:
            stop_reason = "capacity_cutoff"
        elif elapsed_s + sample_period_s >= duration_s:
            stop_reason = "duration_complete"
        else:
            stop_reason = "curve_complete"

        if pstat_valid:
            pstat.set_cell(tkp.CELL_OFF)
        tkp.print_default_dta_file(curve, pstat, str(output.resolve()), dta_tag, working_positive)

        result = {
            "ok": True,
            "technique": technique,
            "output": str(output),
            "current_a": current_a,
            "current_interpretation": "positive magnitude; technique and working_positive select direction",
            "expected_max_current_a": expected_max_current_a,
            "duration_s": duration_s,
            "sample_period_s": sample_period_s,
            "voltage_cutoff_v": voltage_cutoff_v,
            "capacity_cutoff_ah": capacity_cutoff_ah,
            "working_positive": working_positive,
            "elapsed_s": elapsed_s,
            "final_voltage_v": final_voltage_v,
            "capacity_ah": capacity_ah,
            "stop_reason": stop_reason,
            "stop_tests": stop_tests,
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
    tkp.toolkitpy_init("run_cc.py")
    pstat = None
    try:
        pstat_name = select_pstat_name(tkp, step)
        pstat = tkp.Pstat(pstat_name)
        if hasattr(pstat, "open"):
            pstat.open()
        result = run_single_cc(pstat, step, outputs[0], live_dir=live_dir)
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
