from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import toolkitpy as tkp

try:
    from gamry_worker.device import select_pstat_name
except ModuleNotFoundError:
    from device import select_pstat_name


def initialize_pstat(pstat: Any) -> None:
    pstat.set_ach_select(tkp.ACHSELECT_GND)
    pstat.set_ie_stability(tkp.STABILITY_NORM)
    pstat.set_ca_speed(tkp.CASPEED_NORM)
    pstat.set_ground(tkp.FLOAT)
    pstat.set_ich_range(3.0)
    pstat.set_ich_range_mode(False)
    pstat.set_ich_offset_enable(False)
    pstat.set_vch_range(10.0)
    pstat.set_vch_range_mode(True)
    pstat.set_vch_offset_enable(False)
    pstat.set_ach_range(3.0)
    pstat.set_ie_range_lower_limit(0)
    pstat.set_pos_feed_enable(False)
    pstat.set_analog_out(0.0)
    pstat.set_voltage(0.0)
    pstat.set_pos_feed_resistance(0.0)


def get_float(step: dict[str, Any], names: list[str], default: float) -> float:
    for name in names:
        if name in step:
            return float(step[name])

    return float(default)


def run_single_lsv(
    pstat: Any,
    step: dict[str, Any],
    output_path: str,
) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    initial_voltage = get_float(
        step,
        ["initial_voltage_v", "start_voltage_v", "from_voltage_v", "e_initial_v"],
        0.0,
    )

    final_voltage = get_float(
        step,
        ["final_voltage_v", "end_voltage_v", "to_voltage_v", "e_final_v"],
        0.5,
    )

    scan_rate = get_float(
        step,
        ["scan_rate_v_s", "scan_rate", "scan_rate_v_per_s"],
        0.05,
    )

    step_size = get_float(
        step,
        ["step_size_v", "step_voltage_v", "potential_step_v"],
        0.002,
    )

    if scan_rate == 0:
        raise ValueError("LSV scan_rate must not be zero.")

    if step_size <= 0:
        raise ValueError("LSV step_size_v must be positive.")

    sample_time = abs(step_size / scan_rate)
    estimated_time = abs(final_voltage - initial_voltage) / abs(scan_rate)
    max_size = int(step.get("max_size", max(10000, int(estimated_time / sample_time) + 1000)))

    signal = None
    curve = tkp.RcvCurve(pstat, max_size)

    try:
        signal = pstat.signal_ramp_new(
            initial_voltage,
            final_voltage,
            scan_rate,
            sample_time,
            tkp.PSTATMODE,
        )

        initialize_pstat(pstat)

        pstat.set_signal_ramp(signal)
        pstat.init_signal()
        pstat.set_cell(True)

        time.sleep(0.010)

        curve.run(True)

        while tkp.pstat_is_valid(pstat) and curve.running():
            curve.acq_data()
            time.sleep(max(0.01, min(sample_time, 0.25)))

        if tkp.pstat_is_valid(pstat):
            pstat.set_cell(False)

        tkp.print_default_dta_file(curve, pstat, str(output.resolve()), "LSV")

        return {
            "ok": True,
            "technique": "lsv",
            "output": str(output),
            "initial_voltage_v": initial_voltage,
            "final_voltage_v": final_voltage,
            "scan_rate_v_s": scan_rate,
            "step_size_v": step_size,
            "sample_period_s": sample_time,
            "estimated_time_s": estimated_time,
            "points": int(curve.count()),
        }

    finally:
        try:
            if curve.running():
                curve.stop()
        except Exception:
            pass

        try:
            if tkp.pstat_is_valid(pstat):
                pstat.set_cell(False)
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
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("outputs must contain at least one path.")

    tkp.toolkitpy_init("run_lsv.py")

    pstat = None

    try:
        pstat_name = select_pstat_name(tkp, step)
        pstat = tkp.Pstat(pstat_name)

        if hasattr(pstat, "open"):
            pstat.open()

        result = run_single_lsv(
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
