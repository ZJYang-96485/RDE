from __future__ import annotations

from gamry_worker.run_ca import run


step = {
    "technique": "ca",
    "voltage_v": 0.0,
    "duration_s": 5.0,
    "sample_period_s": 0.05,
    "expected_max_v": 1.0,
}

outputs = ["output/test_real_ca_0V_5s.DTA"]

result = run(
    step=step,
    outputs=outputs,
    sample_id="direct_ca_test",
)

print(result)