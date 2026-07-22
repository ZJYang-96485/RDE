from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import toolkitpy as tkp

try:
    from gamry_worker.device import select_pstat_name
    from gamry_worker.live_adapters import LiveCurveEmitter, normalize_ca_acq_rows
    from gamry_worker.live_writer import update_live_status, utc_now
    from gamry_worker.ir_compensation import apply_trial_settings, disable_ir_compensation
except ModuleNotFoundError:
    from device import select_pstat_name
    from live_adapters import LiveCurveEmitter, normalize_ca_acq_rows
    from live_writer import update_live_status, utc_now
    from ir_compensation import apply_trial_settings, disable_ir_compensation


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


def run_single_ca(
    pstat: Any,
    step: dict[str, Any],
    output_path: str,
    voltage_v: float | None = None,
    live_dir: str | None = None,
) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    target_voltage = float(
        voltage_v
        if voltage_v is not None
        else step.get("voltage_v", step.get("potential_v", step.get("step1_voltage_v", 0.0)))
    )

    duration_s = float(step.get("duration_s", step.get("step1_time_s", 10.0)))
    sample_period_s = float(step.get("sample_period_s", step.get("sample_time_s", 0.05)))

    initial_voltage = float(step.get("initial_voltage_v", target_voltage))
    initial_time = float(step.get("initial_time_s", sample_period_s))

    step1_voltage = float(step.get("step1_voltage_v", target_voltage))
    step1_time = float(step.get("step1_time_s", duration_s))

    step2_voltage = float(step.get("step2_voltage_v", target_voltage))
    step2_time = float(step.get("step2_time_s", sample_period_s))

    expected_max_v = float(
        step.get(
            "expected_max_v",
            max(
                10.0,
                abs(initial_voltage),
                abs(step1_voltage),
                abs(step2_voltage),
                abs(target_voltage),
            ),
        )
    )

    max_size = int(step.get("max_size", 100000))

    tkp.check_hve(pstat, expected_max_v)

    if tkp.hve(pstat):
        pstat.set_electrometer(tkp.ELECTROMETER_HIGH_V)
        pstat.set_ctrl_mode(tkp.ZRAX4MODE)
        initial_voltage *= 0.25
        step1_voltage *= 0.25
        step2_voltage *= 0.25
    else:
        pstat.set_ctrl_mode(tkp.PSTATMODE)

    initialize_pstat(pstat)
    trial_settings = apply_trial_settings(pstat, step)

    curve = tkp.ChronoCurve(pstat, max_size)
    emitter = LiveCurveEmitter(live_dir, normalize_ca_acq_rows)
    signal = None

    try:
        signal = pstat.signal_d_step_new(
            initial_voltage,
            initial_time,
            step1_voltage,
            step1_time,
            step2_voltage,
            step2_time,
            sample_period_s,
            tkp.PSTATMODE,
        )

        pstat.set_signal_d_step(signal)
        pstat.init_signal()
        pstat.set_cell(True)

        time.sleep(0.010)

        if live_dir:
            try:
                update_live_status(
                    live_dir,
                    acquisition_started_at=utc_now(),
                    display_label=(
                        "Live CA trace for Levich RPM sweep"
                        if str(step.get("technique", "")).strip().lower()
                        == "levich_rpm_sweep_ca"
                        else None
                    ),
                )
            except Exception:
                # Live metadata is best-effort. The first streamed point gives
                # the orchestrator a second way to establish the CA time base.
                pass
        curve.run(True)

        while tkp.pstat_is_valid(pstat) and curve.running():
            emitter.emit_new(curve.acq_data())
            time.sleep(max(0.01, min(sample_period_s, 0.25)))

        emitter.emit_new(curve.acq_data())

        tkp.reset_hve(pstat)

        if tkp.pstat_is_valid(pstat):
            pstat.set_cell(False)

        tkp.print_default_dta_file(curve, pstat, str(output.resolve()), "CHRONOA")

        result = {
            "ok": True,
            "technique": "ca",
            "output": str(output),
            "voltage_v": target_voltage,
            "duration_s": duration_s,
            "sample_period_s": sample_period_s,
            "points": int(curve.count()),
            "ir_compensation_enabled": bool(trial_settings["ir_compensation_enabled"]),
            "ir_compensation_reason": trial_settings.get("ir_compensation_reason"),
            "ir_compensation_resistance_readback_ohm": trial_settings.get(
                "ir_compensation_resistance_readback_ohm"
            ),
            "ru_selected_ohm": step.get("_trial_ru_selected_ohm"),
            "ru_applied_ohm": step.get("_trial_ru_applied_ohm"),
        }
        result.update(emitter.result_fields())
        return result

    finally:
        try:
            disable_ir_compensation(pstat)
        except Exception:
            pass

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


def run_ca_staircase(
    pstat: Any,
    step: dict[str, Any],
    outputs: list[str],
    live_dir: str | None = None,
) -> dict[str, Any]:
    start_voltage = float(step.get("start_voltage_v", 0.0))
    step_voltage = float(step.get("step_voltage_v", 0.0))
    wait_s = float(step.get("wait_s_between_steps", 0.0))

    records = []

    for index, output_path in enumerate(outputs, start=1):
        voltage = start_voltage + (index - 1) * step_voltage
        sub_step = dict(step)
        sub_step["voltage_v"] = voltage

        record = run_single_ca(
            pstat=pstat,
            step=sub_step,
            output_path=output_path,
            voltage_v=voltage,
            live_dir=live_dir,
        )

        record["index"] = index
        records.append(record)

        if wait_s > 0 and index < len(outputs):
            time.sleep(wait_s)

    return {
        "ok": True,
        "technique": "ca_staircase",
        "outputs": records,
    }


def run(
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
    live_dir: str | None = None,
) -> dict[str, Any]:
    if not outputs:
        raise ValueError("outputs must contain at least one path.")

    tkp.toolkitpy_init("run_ca.py")

    pstat = None

    try:
        pstat_name = select_pstat_name(tkp, step)
        pstat = tkp.Pstat(pstat_name)

        if hasattr(pstat, "open"):
            pstat.open()

        technique = str(step.get("technique", "")).strip().lower()

        if technique == "ca_staircase":
            result = run_ca_staircase(
                pstat=pstat,
                step=step,
                outputs=outputs,
                live_dir=live_dir,
            )
        else:
            result = run_single_ca(
                pstat=pstat,
                step=step,
                output_path=outputs[0],
                live_dir=live_dir,
            )

        result["sample_id"] = sample_id
        result["pstat"] = pstat_name
        return result

    finally:
        if pstat is not None:
            try:
                disable_ir_compensation(pstat)
            except Exception:
                pass
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
