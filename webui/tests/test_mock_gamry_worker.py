from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gamry_worker.worker import run_job


class MockGamryWorkerTest(unittest.TestCase):
    def run_mock_step(self, step: dict, output_count: int = 1) -> dict:
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = [
                str(Path(tmpdir) / f"output_{index}.DTA")
                for index in range(1, output_count + 1)
            ]

            result = run_job(
                {
                    "job_id": f"test_{step['technique']}",
                    "mode": "mock",
                    "sample_id": "sample_001",
                    "step": step,
                    "outputs": outputs,
                }
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "mock")

            for output in outputs:
                self.assertTrue(Path(output).exists(), output)

            return result

    def test_mock_ocp_step(self) -> None:
        self.run_mock_step(
            {
                "name": "ocp",
                "technique": "ocp",
                "duration_s": 1,
                "sample_period_s": 1,
            }
        )

    def test_mock_ca_step(self) -> None:
        self.run_mock_step(
            {
                "name": "ca",
                "technique": "ca",
                "voltage_v": -0.2,
                "duration_s": 1,
                "sample_period_s": 1,
            }
        )

    def test_mock_ca_staircase_step(self) -> None:
        self.run_mock_step(
            {
                "name": "ca_staircase",
                "technique": "ca_staircase",
                "start_voltage_v": -0.1,
                "step_voltage_v": -0.1,
                "step_count": 3,
                "step_time_s": 1,
                "sample_period_s": 1,
            },
            output_count=3,
        )

    def test_mock_cv_step(self) -> None:
        self.run_mock_step(
            {
                "name": "cv",
                "technique": "cv",
                "initial_voltage_v": 0,
                "first_vertex_v": 0.1,
                "second_vertex_v": -0.1,
                "final_voltage_v": 0,
                "scan_rate_v_s": 1,
                "sample_period_s": 0.5,
                "cycles": 1,
            }
        )

    def test_mock_lsv_step(self) -> None:
        self.run_mock_step(
            {
                "name": "lsv",
                "technique": "lsv",
                "start_voltage_v": 0.2,
                "end_voltage_v": -0.2,
                "scan_rate_v_s": 1,
                "sample_period_s": 0.5,
            }
        )

    def test_mock_eis_step(self) -> None:
        self.run_mock_step(
            {
                "name": "eis",
                "technique": "eis",
                "initial_frequency_hz": 1000,
                "final_frequency_hz": 10,
                "points_per_decade": 2,
                "estimated_z_ohm": 100,
            }
        )


if __name__ == "__main__":
    unittest.main()
