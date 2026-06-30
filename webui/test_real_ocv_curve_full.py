from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import toolkitpy as tkp


def initialize_pstat(pstat) -> None:
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


def main() -> None:
    duration_s = 30
    poll_s = 0.5

    csv_output = Path("output_real_ocv_curve_full.csv")
    dta_output = Path("output_real_ocv_curve_full.DTA")

    tkp.toolkitpy_init("test_real_ocv_curve_full.py")

    pstat_name = tkp.enum_sections()[0]
    print(f"Using potentiostat: {pstat_name}")

    pstat = tkp.Pstat(pstat_name)

    if hasattr(pstat, "open"):
        pstat.open()

    initialize_pstat(pstat)

    curve = tkp.OcvCurve(pstat, 10000)
    chunks = []

    try:
        curve.set_stop_adv_min(True, -10.0)
        curve.set_stop_adv_max(True, 10.0)

        pstat.set_cell(True)
        time.sleep(0.1)

        print("Starting OCV curve")
        curve.run(True)

        deadline = time.monotonic() + duration_s

        while time.monotonic() < deadline:
            running = curve.running()
            count = curve.count()
            data = curve.acq_data()

            rows = 0 if data is None else len(data)
            print(f"running={running}, count={count}, chunk_rows={rows}")

            if data is not None and len(data) > 0:
                chunks.append(data.copy())
                print("last:", data[-1])

            if not running and count == 0:
                break

            time.sleep(poll_s)

        if curve.running():
            curve.stop()

        data = curve.acq_data()
        if data is not None and len(data) > 0:
            chunks.append(data.copy())

        try:
            tkp.print_default_dta_file(curve, pstat, str(dta_output.resolve()), "OCV")
            print(f"Saved DTA: {dta_output.resolve()}")
        except Exception as exc:
            print(f"DTA save failed: {exc}")

        if not chunks:
            print("No chunk data collected.")
            print("curve.count():", curve.count())
            try:
                print("last_data_point:", curve.last_data_point())
            except Exception as exc:
                print("last_data_point failed:", exc)
            return

        all_data = np.concatenate(chunks)

        print("dtype names:", all_data.dtype.names)
        print("total rows:", len(all_data))
        print("first rows:")
        print(all_data[:5])
        print("last rows:")
        print(all_data[-5:])

        np.savetxt(
            csv_output,
            all_data,
            delimiter=",",
            newline="\n",
            header=",".join(all_data.dtype.names),
            comments="",
        )

        print(f"Saved CSV: {csv_output.resolve()}")

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

        del curve
        del pstat

        try:
            tkp.toolkitpy_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()