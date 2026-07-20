from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import app
from gamry_worker.live_adapters import (
    normalize_cc_charge_acq_rows,
    normalize_cp_acq_rows,
    normalize_geis_point,
)
from gamry_worker.live_writer import read_live_points, read_live_status
from gamry_worker.worker import REAL_RUNNER_MODULES, run_job
from workflow.protocol_loader import ProtocolError, validate_protocol_payload


class CurrentControlledTechniqueTest(unittest.TestCase):
    def test_current_controlled_buttons_share_automation_lockout(self) -> None:
        page = app.test_client().get("/").get_data(as_text=True)
        disable_block = page.split("function setProtocolBuilderDisabled", 1)[1].split(
            "function renderGamryModeStatus",
            1,
        )[0]

        for button in (
            "addEchemCpBtn",
            "addEchemCcChargeBtn",
            "addEchemCcDischargeBtn",
            "addEchemGeisBtn",
        ):
            with self.subTest(button=button):
                self.assertIn(button, disable_block)

    def mock_job(self, technique: str, step: dict) -> tuple[dict, Path, Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        output = root / "sample" / f"{technique}.DTA"
        live_dir = root / "_system" / "live"
        result = run_job(
            {
                "job_id": f"mock-{technique}",
                "mode": "mock",
                "run_id": "run-current",
                "sample_id": "sample-1",
                "step": {"name": technique, "technique": technique, **step},
                "outputs": [str(output)],
                "live_dir": str(live_dir),
                "live_enabled": True,
                "gamry": {"live_plot": {"mock_time_scale": 0}},
                "mock_delay_s": 0,
            }
        )
        return result, output, live_dir

    def test_mock_cp_charge_discharge_and_geis_create_outputs_and_live_points(self) -> None:
        cases = {
            "cp": {"current_a": -1e-5, "duration_s": 0.2, "sample_period_s": 0.1, "voltage_limit_low_v": -1, "voltage_limit_high_v": 1, "expected_max_current_a": 1e-5},
            "cc_charge": {"current_a": 1e-5, "duration_s": 0.2, "sample_period_s": 0.1, "voltage_cutoff_v": 4.3},
            "cc_discharge": {"current_a": 1e-5, "duration_s": 0.2, "sample_period_s": 0.1, "voltage_cutoff_v": 2.9},
            "geis": {"initial_frequency_hz": 1000, "final_frequency_hz": 10, "points_per_decade": 2, "ac_current_a": 1e-5, "dc_current_a": 0, "estimated_z_ohm": 100},
        }
        for technique, step in cases.items():
            with self.subTest(technique=technique):
                result, output, live_dir = self.mock_job(technique, step)
                self.assertTrue(result["ok"])
                self.assertTrue(output.is_file())
                technique_result = result["result"]["result"]
                self.assertIn("stop_reason", technique_result)
                self.assertIn("elapsed_s", technique_result)
                dta_text = output.read_text(encoding="utf-8")
                points = read_live_points(live_dir, limit=100)
                self.assertGreater(len(points), 0)
                self.assertEqual(points[0]["technique"], technique)
                self.assertEqual(points[0]["index"], points[0]["seq"])
                self.assertEqual(read_live_status(live_dir)["status"], "complete")
                if technique == "geis":
                    self.assertIn("Pt\tT\tFreq\tZreal\tZimag\tZmod\tZphz\tIdc\tVdc", dta_text)
                    self.assertIn("zreal_ohm", points[0])
                    self.assertIn("zimag_ohm", points[0])
                else:
                    self.assertIn("Pt\tT\tVf\tIm\tQ_Ah", dta_text)
                    self.assertIn("final_voltage_v", technique_result)
                    self.assertIn("t_s", points[0])
                    self.assertIn("e_v", points[0])
                    self.assertIn("i_a", points[0])

    def test_verified_toolkit_field_adapters(self) -> None:
        chrono_row = {"time": 1.25, "vf": 0.42, "im": -2e-5}
        self.assertEqual(normalize_cp_acq_rows(chrono_row)["e_v"], 0.42)
        self.assertEqual(normalize_cc_charge_acq_rows(chrono_row)["i_a"], -2e-5)
        z_row = {"zfreq": 100, "zreal": 12, "zimag": -3, "zmod": 12.369, "zphz": -14}
        self.assertEqual(normalize_geis_point(z_row)["technique"], "geis")
        self.assertEqual(normalize_geis_point(z_row)["freq_hz"], 100)

    def test_protocol_validation_and_current_sign_conventions(self) -> None:
        payload = {
            "protocol_name": "current_controlled",
            "steps": [
                {"name": "cp", "technique": "cp", "current_a": -1e-5, "duration_s": 1, "sample_period_s": 0.1, "voltage_limit_low_v": -1, "voltage_limit_high_v": 1, "expected_max_current_a": 1e-5},
                {"name": "charge", "technique": "cc_charge", "current_a": 1e-5, "expected_max_current_a": 2e-5, "duration_s": 1, "sample_period_s": 0.1, "voltage_cutoff_v": 4.2, "output": "unsafe/subfolder/charge.txt"},
                {"name": "discharge", "technique": "cc_discharge", "current_a": 1e-5, "duration_s": 1, "sample_period_s": 0.1, "voltage_cutoff_v": 3.0, "capacity_cutoff_ah": 1e-6},
                {"name": "geis", "technique": "geis", "initial_freq_hz": 10, "final_freq_hz": 1000, "points_per_decade": 3, "ac_current_a": 1e-5, "dc_current_a": -1e-5, "estimated_z_ohm": 100, "speed": "slow", "output": "geis.csv"},
            ],
        }
        validated = validate_protocol_payload(payload)
        self.assertEqual([step["technique"] for step in validated["steps"]], ["cp", "cc_charge", "cc_discharge", "geis"])
        self.assertLess(validated["steps"][0]["current_a"], 0)
        self.assertEqual(validated["steps"][0]["voltage_limit_high_v"], 1)
        self.assertGreater(validated["steps"][1]["current_a"], 0)
        self.assertEqual(validated["steps"][1]["expected_max_current_a"], 2e-5)
        self.assertEqual(validated["steps"][1]["output"], "charge.DTA")
        self.assertEqual(validated["steps"][3]["speed"], "low")
        self.assertEqual(validated["steps"][3]["output"], "geis.DTA")

        bad = dict(payload)
        bad["steps"] = [{"name": "bad", "technique": "cc_discharge", "current_a": -1e-5}]
        with self.assertRaises(ProtocolError):
            validate_protocol_payload(bad)

        bad["steps"] = [{"name": "bad speed", "technique": "geis", "initial_frequency_hz": 10, "final_frequency_hz": 100, "speed": "turbo"}]
        with self.assertRaises(ProtocolError):
            validate_protocol_payload(bad)

    def test_mock_geis_supports_low_to_high_frequency_sweeps(self) -> None:
        result, _, live_dir = self.mock_job(
            "geis",
            {
                "initial_frequency_hz": 10,
                "final_frequency_hz": 1000,
                "points_per_decade": 2,
                "ac_current_a": 1e-5,
                "estimated_z_ohm": 100,
            },
        )
        points = read_live_points(live_dir, limit=100)
        self.assertAlmostEqual(points[0]["freq_hz"], 10)
        self.assertAlmostEqual(points[-1]["freq_hz"], 1000)
        self.assertEqual(result["result"]["result"]["stop_reason"], "frequency_sweep_complete")

    def test_real_dispatch_has_local_runner_for_each_new_technique(self) -> None:
        for technique in ("cp", "cc_charge", "cc_discharge", "geis"):
            self.assertIn(technique, REAL_RUNNER_MODULES)


if __name__ == "__main__":
    unittest.main()
