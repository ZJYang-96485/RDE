from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gamry_worker.device import GamryDeviceError, configured_step, select_pstat_name
from gamry_worker.worker import GamryWorkerError, run_job
from gamry_worker.trial_preparation import default_trial_metadata


class FakeToolkit:
    def __init__(self, sections: list[str]) -> None:
        self.sections = sections

    def enum_sections(self) -> list[str]:
        return self.sections


class RealGamryWorkerTest(unittest.TestCase):
    def test_real_measurement_continues_without_ir_when_ru_is_unavailable(self) -> None:
        captured = {}

        def fake_runner(step, outputs, sample_id=None):
            captured["step"] = step
            for output in outputs:
                Path(output).write_text("uncompensated output\n", encoding="utf-8")
            return {"ok": True, "ir_compensation_enabled": False}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "uncompensated.DTA"
            job = {
                "job_id": "test_ru_fallback",
                "mode": "real",
                "sample_id": "sample_001",
                "step": {"name": "ca", "technique": "ca", "duration_s": 1},
                "outputs": [str(output_path)],
                "gamry": {
                    "instrument_label": "IFC1010-36030",
                    "ru_preparation": {"continue_without_ir_on_ru_failure": True},
                },
            }
            metadata = default_trial_metadata(job["gamry"]["ru_preparation"])
            metadata.update(
                {
                    "ocp_stabilization_status": "stable",
                    "ru_validation_passed": False,
                    "ru_failure_reason": "Unable to obtain a valid Ru after configured attempts",
                    "measurement_without_ir_compensation": True,
                    "trial_status": "ready_without_ir_compensation",
                }
            )
            with patch(
                "gamry_worker.worker.real_runner_for_technique",
                return_value=fake_runner,
            ), patch(
                "gamry_worker.worker.prepare_real_trial_for_job",
                return_value=(metadata, dict(job["step"])),
            ):
                result = run_job(job)

            self.assertTrue(output_path.exists())
            self.assertEqual(result["trial_metadata"]["trial_status"], "completed")
            self.assertTrue(result["trial_metadata"]["measurement_without_ir_compensation"])
            self.assertFalse(result["trial_metadata"]["ir_compensation_enabled"])
            self.assertNotIn("_trial_ru_applied_ohm", captured["step"])

    def test_real_mode_dispatches_direct_runner_with_configured_instrument(self) -> None:
        captured = {}

        def fake_runner(step, outputs, sample_id=None):
            captured["step"] = step
            captured["sample_id"] = sample_id

            for output in outputs:
                Path(output).write_text("direct ToolkitPy output\n", encoding="utf-8")

            return {"ok": True, "pstat": step["instrument_label"]}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "sample_001_ocp.DTA"
            job = {
                "job_id": "test_real_ocp",
                "mode": "real",
                "sample_id": "sample_001",
                "step": {
                    "name": "ocp",
                    "technique": "ocp",
                    "duration_s": 1,
                    "sample_period_s": 1,
                },
                "outputs": [str(output_path)],
                "gamry": {
                    "instrument_label": "IFC1010-36030",
                    "instrument_index": 0,
                },
            }

            metadata = default_trial_metadata({"compensation_fraction": 0.8})
            metadata.update(
                {
                    "ocp_stabilization_status": "stable",
                    "ru_attempts_ohm": [10.0, 10.1],
                    "ru_selected_ohm": 10.05,
                    "ru_validation_passed": True,
                    "ru_applied_ohm": 8.04,
                }
            )
            prepared_step = configured_step(job["step"], job["gamry"])
            prepared_step.update(
                {
                    "_trial_ru_validation_passed": True,
                    "_trial_ru_selected_ohm": 10.05,
                    "_trial_ru_applied_ohm": 8.04,
                    "_trial_fixed_current_range_a": 0.003,
                }
            )
            with patch(
                "gamry_worker.worker.real_runner_for_technique",
                return_value=fake_runner,
            ), patch(
                "gamry_worker.worker.prepare_real_trial_for_job",
                return_value=(metadata, prepared_step),
            ):
                result = run_job(job)

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "real")
            self.assertTrue(output_path.exists())
            self.assertEqual(captured["sample_id"], "sample_001")
            self.assertEqual(captured["step"]["instrument_label"], "IFC1010-36030")
            self.assertEqual(captured["step"]["instrument_index"], 0)

    def test_step_instrument_label_overrides_global_config(self) -> None:
        step = configured_step(
            {
                "technique": "ocp",
                "instrument_label": "IFC1010-LOCAL",
            },
            {
                "instrument_label": "IFC1010-GLOBAL",
                "instrument_index": 2,
            },
        )

        self.assertEqual(step["instrument_label"], "IFC1010-LOCAL")
        self.assertEqual(step["instrument_index"], 2)

    def test_selects_configured_label_or_index(self) -> None:
        toolkit = FakeToolkit(["IFC1010-A", "IFC1010-B"])

        self.assertEqual(
            select_pstat_name(toolkit, {"instrument_label": "IFC1010-B"}),
            "IFC1010-B",
        )
        self.assertEqual(
            select_pstat_name(toolkit, {"instrument_index": 0}),
            "IFC1010-A",
        )

    def test_missing_configured_instrument_has_actionable_error(self) -> None:
        with self.assertRaises(GamryDeviceError) as context:
            select_pstat_name(
                FakeToolkit(["IFC1010-A"]),
                {"instrument_label": "IFC1010-B"},
            )

        self.assertIn("IFC1010-A", str(context.exception))

    def test_real_mode_rejects_unsupported_technique(self) -> None:
        with self.assertRaises(GamryWorkerError):
            run_job(
                {
                    "mode": "real",
                    "step": {"technique": "unsupported"},
                    "outputs": ["unused.DTA"],
                    "gamry": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
