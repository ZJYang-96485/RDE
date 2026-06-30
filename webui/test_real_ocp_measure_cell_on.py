from __future__ import annotations

import csv
import time
from pathlib import Path

import toolkitpy as tkp


def initialize_pstat_for_measurement(pstat) -> None:
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
    pstat.set_pos_feed_resistance(0.0)


def main() -> None:
    output = Path("output_real_ocp_measure_cell_on.csv")
    duration_s = 30
    sample_period_s = 0.5

    tkp.toolkitpy_init("test_real_ocp_measure_cell_on.py")

    pstat_name = tkp.enum_sections()[0]
    print(f"Using potentiostat: {pstat_name}")

    pstat = tkp.Pstat(pstat_name)

    try:
        if hasattr(pstat, "open"):
            pstat.open()

        initialize_pstat_for_measurement(pstat)

        print("Turning cell on for voltage measurement")
        pstat.set_cell(True)
        time.sleep(1.0)

        rows = []
        start = time.monotonic()

        while True:
            elapsed = time.monotonic() - start

            if elapsed > duration_s:
                break

            voltage = float(pstat.measure_v())
            current = float(pstat.measure_i())
            temperature = float(pstat.measure_temp())

            rows.append(
                {
                    "time_s": elapsed,
                    "voltage_v": voltage,
                    "current_a": current,
                    "temperature_c": temperature,
                }
            )

            print(f"t={elapsed:.2f}s, V={voltage:.6f} V, I={current:.6e} A")
            time.sleep(sample_period_s)

        with output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["time_s", "voltage_v", "current_a", "temperature_c"],
            )
            writer.writeheader()
            writer.writerows(rows)

        print(f"Saved: {output.resolve()}")

    finally:
        try:
            pstat.set_cell(False)
        except Exception:
            pass

        del pstat

        try:
            tkp.toolkitpy_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()