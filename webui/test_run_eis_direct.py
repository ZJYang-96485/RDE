from __future__ import annotations

from gamry_worker.run_eis import run


step = {
    "technique": "eis",
    "initial_freq_hz": 10000.0,
    "final_freq_hz": 1000.0,
    "points_per_decade": 3,
    "estimated_z_ohm": 2000.0,
    "dc_voltage_v": 0.0,
    "ac_voltage_v": 0.005,
    "settle_s": 1.0,
}

outputs = ["output/test_real_eis_10k_to_1kHz.DTA"]

result = run(
    step=step,
    outputs=outputs,
    sample_id="direct_eis_test",
)

print(result)