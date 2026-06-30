from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import toolkitpy as tkp


def main() -> None:
    output = Path("output_real_ocp_test_open_cell.csv")
    duration_s = 30
    poll_s = 0.5

    tkp.toolkitpy_init("test_real_ocp_open_cell.py")

    pstat_name = tkp.enum_sections()[0]
    print(f"Using potentiostat: {pstat_name}")

    pstat = tkp.Pstat(pstat_name)

    if hasattr(pstat, "open"):
        print("Opening pstat")
        pstat.open()

    if hasattr(pstat, "set_cell"):
        print("Turning cell on")
        pstat.set_cell(True)

    curve = tkp.OcvCurve(pstat, 10000)
    chunks = []

    try:
        curve.set_stop_adv_min(False, 0.0)
        curve.set_stop_adv_max(False, 0.0)

        print("Starting OCV curve")
        curve.run(True)

        deadline = time.monotonic() + duration_s

        while time.monotonic() < deadline:
            is_running = curve.running()
            count = curve.count()
            last = curve.last_data_point() if count > 0 else None
            data = curve.acq_data()

            print(f"running={is_running}, count={count}, chunk_rows={0 if data is None else len(data)}, last={last}")

            if data is not None and len(data) > 0:
                chunks.append(data.copy())

            if not is_running and count == 0:
                break

            time.sleep(poll_s)

        if curve.running():
            curve.stop()

        data = curve.acq_data()

        if data is not None and len(data) > 0:
            chunks.append(data.copy())

        if not chunks:
            raise RuntimeError("No OCP data acquired from ToolkitPy.")

        all_data = np.concatenate(chunks)

        print("dtype names:", all_data.dtype.names)
        print("total rows:", len(all_data))
        print("first rows:")
        print(all_data[:5])
        print("last rows:")
        print(all_data[-5:])

        np.savetxt(
            output,
            all_data,
            delimiter=",",
            newline="\n",
            header=",".join(all_data.dtype.names),
            comments="",
        )

        print(f"Saved: {output.resolve()}")

    finally:
        try:
            if curve.running():
                curve.stop()
        except Exception:
            pass

        try:
            curve.free()
        except Exception:
            pass

        try:
            if hasattr(pstat, "set_cell"):
                pstat.set_cell(False)
        except Exception:
            pass

        del curve
        del pstat


if __name__ == "__main__":
    main()