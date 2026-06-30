from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import toolkitpy as tkp


def initialize_pstat(pstat: Any) -> None:
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
    pstat.set_vch_filter(2.50)
    pstat.set_ich_offset_enable(True)
    pstat.set_vch_offset_enable(True)
    pstat.set_ach_range(3.0)
    pstat.set_ie_range(0.03)
    pstat.set_ie_range_mode(False)
    pstat.set_ie_range_lower_limit(0)
    pstat.set_analog_out(0.0)
    pstat.set_pos_feed_enable(False)
    pstat.set_irupt_mode(tkp.IRUPTOFF)


def first_pstat_name() -> str:
    names = list(tkp.enum_sections())

    if not names:
        raise RuntimeError("No Gamry potentiostat found by ToolkitPy.")

    return str(names[0])


def get_float(step: dict[str, Any], names: list[str], default: float) -> float:
    for name in names:
        if name in step:
            return float(step[name])

    return float(default)

def get_eis_speed(value: Any) -> int:
    if value is None:
        return 1

    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().lower()

    if text in {"", "normal", "norm"}:
        return 1

    return int(text)

def get_int(step: dict[str, Any], names: list[str], default: int) -> int:
    for name in names:
        if name in step:
            return int(step[name])

    return int(default)


def run_single_eis(
    pstat: Any,
    step: dict[str, Any],
    output_path: str,
) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    initial_freq = abs(
        get_float(step, ["initial_freq_hz", "start_freq_hz", "initial_freq"], 100000.0)
    )
    final_freq = abs(
        get_float(step, ["final_freq_hz", "end_freq_hz", "final_freq"], 1000.0)
    )
    ac_voltage = get_float(step, ["ac_voltage_v", "ac_voltage"], 0.005)
    dc_voltage = get_float(step, ["dc_voltage_v", "dc_voltage", "bias_voltage_v"], 0.0)
    estimated_z = abs(get_float(step, ["estimated_z_ohm", "estimated_z"], 2000.0))
    points_per_decade = get_int(step, ["points_per_decade", "ppd"], 5)

    if estimated_z <= 0:
        raise ValueError("EIS estimated_z_ohm must be positive.")

    if points_per_decade < 1:
        raise ValueError("EIS points_per_decade must be at least 1.")

    freq_lim_lower = pstat.freq_limit_lower()
    freq_lim_upper = pstat.freq_limit_upper()

    if initial_freq > freq_lim_upper:
        initial_freq = freq_lim_upper

    if final_freq > freq_lim_upper:
        final_freq = freq_lim_upper

    if initial_freq < freq_lim_lower:
        initial_freq = freq_lim_lower

    if final_freq < freq_lim_lower:
        final_freq = freq_lim_lower

    initialize_pstat(pstat)

    pstat.set_ctrl_mode(tkp.PSTATMODE)
    pstat.set_i_convention(tkp.ICONVENTION.ANODIC)
    pstat.set_voltage(dc_voltage)
    pstat.set_cell(tkp.CELL_ON)

    time.sleep(float(step.get("settle_s", 1.0)))

    dc_current = pstat.measure_i()
    ac_current_est = abs(ac_voltage) / estimated_z
    ie_range = pstat.test_ie_range(abs(dc_current) + 1.414 * abs(ac_current_est))
    pstat.set_ie_range(ie_range)

    readz = tkp.ReadZ(pstat)
    readz.set_gain(1.0)
    readz.set_inoise(0.0)
    readz.set_vnoise(0.0)
    readz.set_ienoise(0.0)
    readz.set_zmod(estimated_z)
    readz.set_vdc(dc_voltage)
    readz.set_speed(get_eis_speed(step.get("speed", 1)))
    readz.set_drift_cor(bool(step.get("drift_correction", False)))
    readz.set_idc(dc_current)

    log_increment = 1.0 / points_per_decade

    if initial_freq > final_freq:
        log_increment = -log_increment

    max_points = int(tkp.check_eis_points(initial_freq, final_freq, points_per_decade))
    zcurve = tkp.ZCurve(max_points)

    measured_points = 0
    bad_points = 0

    try:
        for current_point in range(max_points):
            if not tkp.pstat_is_valid(pstat):
                break

            freq = math.pow(
                10.0,
                math.log10(initial_freq) + current_point * log_increment,
            )

            status = readz.measure(freq, ac_voltage, dc_voltage)
            temp = pstat.measure_temp()

            if status is False:
                bad_points += 1
                time.sleep(0.010)
                continue

            zcurve.add_point(readz, temp)
            measured_points += 1
            time.sleep(0.010)

        if tkp.pstat_is_valid(pstat):
            pstat.set_cell(False)

        tkp.print_default_dta_file(zcurve, pstat, str(output.resolve()), "EISPOT")

        return {
            "ok": True,
            "technique": "eis",
            "output": str(output),
            "initial_freq_hz": initial_freq,
            "final_freq_hz": final_freq,
            "ac_voltage_v": ac_voltage,
            "dc_voltage_v": dc_voltage,
            "estimated_z_ohm": estimated_z,
            "points_per_decade": points_per_decade,
            "points": measured_points,
            "bad_points": bad_points,
            "max_points": max_points,
        }

    finally:
        try:
            if tkp.pstat_is_valid(pstat):
                pstat.set_cell(False)
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
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("outputs must contain at least one path.")

    tkp.toolkitpy_init("run_eis.py")

    pstat = None

    try:
        pstat_name = str(step.get("instrument_label") or first_pstat_name())
        pstat = tkp.Pstat(pstat_name)

        if hasattr(pstat, "open"):
            pstat.open()

        result = run_single_eis(
            pstat=pstat,
            step=step,
            output_path=outputs[0],
        )

        result["sample_id"] = sample_id
        result["pstat"] = pstat_name
        return result

    finally:
        if pstat is not None:
            try:
                if tkp.pstat_is_valid(pstat):
                    pstat.set_cell(False)
            except Exception:
                pass

            del pstat

        try:
            tkp.toolkitpy_close()
        except Exception:
            pass