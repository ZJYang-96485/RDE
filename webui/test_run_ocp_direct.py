from __future__ import annotations

from gamry_worker.run_ocp import run


step = {
    "technique": "ocp",
    "duration_s": 10.0,
    "sample_period_s": 0.5
}

outputs = ["output/test_real_ocp_10s.DTA"]

result = run(
    step=step,
    outputs=outputs,
    sample_id="direct_ocp_test",
)

print(result)