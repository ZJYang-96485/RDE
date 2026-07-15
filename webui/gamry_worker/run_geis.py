from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import toolkitpy as tkp

try:
    from gamry_worker.device import select_pstat_name
    from gamry_worker.live_adapters import LiveCurveEmitter, normalize_geis_point
except ModuleNotFoundError:
    from device import select_pstat_name
    from live_adapters import LiveCurveEmitter, normalize_geis_point


def initialize_pstat(pstat: Any) -> None:
    """Exact local galvanostatic_eis.py initialization."""
    pstat.set_cell(False)
    pstat.set_ach_select(tkp.ACHSELECT_GND)
    pstat.set_ie_stability(tkp.STABILITY_FAST)
    pstat.set_ca_speed(tkp.CASPEED_NORM)
    pstat.set_ground(tkp.FLOAT)
    pstat.set_i_convention(tkp.ICONVENTION.ANODIC)
    pstat.set_ich_range(3.0)
    pstat.set_ich_range_mode(False)
    pstat.set_ich_filter(3.0)
    pstat.set_vch_range(3.0)
    pstat.set_vch_range_mode(False)
    pstat.set_ich_offset_enable(True)
    pstat.set_vch_offset_enable(True)
    pstat.set_vch_filter(2.50)
    pstat.set_ach_range(3.0)
    pstat.set_ie_range(0.03)
    pstat.set_ie_range_mode(False)
    pstat.set_ie_range_lower_limit(0)
    pstat.set_analog_out(0.0)
    pstat.set_pos_feed_enable(False)
    pstat.set_irupt_mode(tkp.IRUPTOFF)


def _speed(value: Any) -> int:
    if value is None or str(value).strip().lower() in {"", "normal", "norm"}:
        return 1
    return int(value)


def run_single_geis(
    pstat: Any,
    step: dict[str, Any],
    output_path: str,
    live_dir: str | None = None,
) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    initial_freq = abs(float(step.get("initial_frequency_hz", step.get("initial_freq_hz", 100000.0))))
    final_freq = abs(float(step.get("final_frequency_hz", step.get("final_freq_hz", 1.0))))
    ac_current = float(step.get("ac_current_a", 1e-4))
    dc_current = float(step.get("dc_current_a", 0.0))
    estimated_z = abs(float(step.get("estimated_z_ohm", 100.0)))
    points_per_decade = int(step.get("points_per_decade", 10))
    settle_s = float(step.get("settle_s", 0.0))

    if ac_current <= 0:
        raise ValueError("GEIS ac_current_a must be positive.")
    if estimated_z <= 0 or points_per_decade < 1:
        raise ValueError("GEIS estimated_z_ohm and points_per_decade must be positive.")
    if initial_freq <= final_freq:
        raise ValueError("GEIS initial_frequency_hz must be greater than final_frequency_hz.")

    initial_freq = min(max(initial_freq, pstat.freq_limit_lower()), pstat.freq_limit_upper())
    final_freq = min(max(final_freq, pstat.freq_limit_lower()), pstat.freq_limit_upper())
    if initial_freq <= final_freq:
        raise ValueError("GEIS frequency limits collapse after applying the instrument range.")
    initialize_pstat(pstat)
    pstat.set_ctrl_mode(tkp.GSTATMODE)
    pstat.set_i_convention(tkp.ICONVENTION.ANODIC)
    ie_range = pstat.test_ie_range(abs(dc_current) + 1.414 * abs(ac_current))
    pstat.set_ie_range(ie_range)
    r_measure = pstat.ie_resistor(ie_range)
    pstat.set_voltage(r_measure * dc_current)
    pstat.set_cell(tkp.CELL_ON)
    if settle_s > 0:
        time.sleep(settle_s)
    dc_voltage = pstat.measure_v()

    readz = tkp.ReadZ(pstat)
    readz.set_gain(1.0)
    readz.set_inoise(0.0)
    readz.set_vnoise(0.0)
    readz.set_ienoise(0.0)
    readz.set_zmod(estimated_z)
    readz.set_idc(dc_current)
    readz.set_speed(_speed(step.get("speed", 1)))
    readz.set_drift_cor(bool(step.get("drift_correction", False)))

    log_increment = -1.0 / points_per_decade
    max_points = int(tkp.check_eis_points(initial_freq, final_freq, points_per_decade))
    zcurve = tkp.ZCurve(max_points)
    emitter = LiveCurveEmitter(live_dir, normalize_geis_point)
    measured_points = 0
    bad_points = 0

    try:
        for current_point in range(max_points):
            if not tkp.pstat_is_valid(pstat):
                break
            freq = math.pow(10.0, math.log10(initial_freq) + current_point * log_increment)
            status = readz.measure(freq, ac_current, dc_current)
            temp = pstat.measure_temp()
            if status is False:
                bad_points += 1
            else:
                zcurve.add_point(readz, temp)
                measured_points += 1
                data = zcurve.acq_data()
                emitter.emit_point(data[measured_points - 1])
            time.sleep(0.010)

        if tkp.pstat_is_valid(pstat):
            pstat.set_cell(tkp.CELL_OFF)
        tkp.print_default_dta_file(zcurve, pstat, str(output.resolve()), "GALVEIS")
        result = {
            "ok": True,
            "technique": "geis",
            "output": str(output),
            "initial_frequency_hz": initial_freq,
            "final_frequency_hz": final_freq,
            "ac_current_a": ac_current,
            "dc_current_a": dc_current,
            "dc_voltage_v": dc_voltage,
            "estimated_z_ohm": estimated_z,
            "points_per_decade": points_per_decade,
            "points": measured_points,
            "bad_points": bad_points,
            "max_points": max_points,
        }
        result.update(emitter.result_fields())
        return result
    finally:
        try:
            if tkp.pstat_is_valid(pstat):
                pstat.set_cell(tkp.CELL_OFF)
        except Exception:
            pass
        try:
            del readz
        except Exception:
            pass
        try:
            del zcurve
        except Exception:
            pass


def run(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
    live_dir: str | None = None,
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("outputs must contain at least one path.")
    tkp.toolkitpy_init("run_geis.py")
    pstat = None
    try:
        pstat_name = select_pstat_name(tkp, step)
        pstat = tkp.Pstat(pstat_name)
        if hasattr(pstat, "open"):
            pstat.open()
        result = run_single_geis(pstat, step, outputs[0], live_dir=live_dir)
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
