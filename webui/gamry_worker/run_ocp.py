from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import toolkitpy as tkp

try:
    from gamry_worker.device import select_pstat_name
except ModuleNotFoundError:
    from device import select_pstat_name


def get_float(step: dict[str, Any], names: list[str], default: float) -> float:
    for name in names:
        if name in step:
            return float(step[name])

    return float(default)


def run_single_ocp(
    pstat: Any,
    step: dict[str, Any],
    output_path: str,
) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    total_time = get_float(step, ["duration_s", "total_time_s", "total_time"], 30.0)
    sample_time = get_float(step, ["sample_period_s", "sample_time_s", "sample_time"], 0.5)
    max_size = int(step.get("max_size", 100000))

    if total_time <= 0:
        raise ValueError("OCP duration_s must be positive.")

    if sample_time <= 0:
        raise ValueError("OCP sample_period_s must be positive.")

    pstat.set_ctrl_mode(tkp.PSTATMODE)

    curve = tkp.OcvCurve(pstat, max_size)
    signal = None

    try:
        signal = pstat.signal_const_new(
            0.0,
            total_time,
            sample_time,
            tkp.PSTATMODE,
        )

        pstat.set_cell(False)
        pstat.set_signal_const(signal)
        pstat.init_signal()

        curve.run(True)

        while tkp.pstat_is_valid(pstat) and curve.running():
            curve.acq_data()
            time.sleep(max(0.01, min(sample_time, 0.25)))

        tkp.print_default_dta_file(curve, pstat, str(output.resolve()), "CORPOT")

        return {
            "ok": True,
            "technique": "ocp",
            "output": str(output),
            "duration_s": total_time,
            "sample_period_s": sample_time,
            "points": int(curve.count()),
        }

    finally:
        try:
            if curve.running():
                curve.stop()
        except Exception:
            pass

        try:
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

    tkp.toolkitpy_init("run_ocp.py")

    pstat = None

    try:
        pstat_name = select_pstat_name(tkp, step)
        pstat = tkp.Pstat(pstat_name)

        if hasattr(pstat, "open"):
            pstat.open()

        result = run_single_ocp(
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
                pstat.set_cell(False)
            except Exception:
                pass

            del pstat

        try:
            tkp.toolkitpy_close()
        except Exception:
            pass
