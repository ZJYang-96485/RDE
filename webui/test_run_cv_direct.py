from __future__ import annotations

from gamry_worker.run_cv import run


step = {
    "technique": "cv",
    "initial_voltage_v": 0.0,
    "apex1_voltage_v": 0.05,
    "apex2_voltage_v": 0.0,
    "final_voltage_v": 0.0,
    "scan_rate_v_s": 0.05,
    "step_size_v": 0.002,
    "cycles": 1,
    "precharge_s": 1.0,
}

outputs = ["output/test_real_cv_0_to_50mV.DTA"]

result = run(
    step=step,
    outputs=outputs,
    sample_id="direct_cv_test",
)

print(result)