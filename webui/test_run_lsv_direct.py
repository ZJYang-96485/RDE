from __future__ import annotations

from gamry_worker.run_lsv import run


step = {
    "technique": "lsv",
    "initial_voltage_v": 0.0,
    "final_voltage_v": 0.05,
    "scan_rate_v_s": 0.05,
    "step_size_v": 0.002,
}

outputs = ["output/test_real_lsv_0_to_50mV.DTA"]

result = run(
    step=step,
    outputs=outputs,
    sample_id="direct_lsv_test",
)

print(result)