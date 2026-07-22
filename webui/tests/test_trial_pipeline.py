from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from hardware.gamry_client import GamryClientError
from workflow.recipe_runner import run_protocol_for_sample


def output_record(index: int) -> dict:
    return {
        "sample_index": 1,
        "sample_dir": ".",
        "filename_prefix": None,
        "step_index": index,
        "step_name": f"step {index}",
        "technique": "ocp",
        "outputs": [f"step_{index}.DTA"],
    }


class TrialPipelineTests(unittest.TestCase):
    @patch("workflow.recipe_runner.append_log")
    @patch("workflow.recipe_runner.set_automation_state")
    @patch("workflow.recipe_runner.check_abort")
    @patch("workflow.recipe_runner.register_trial_result")
    @patch("workflow.recipe_runner.prepare_protocol_outputs")
    @patch("workflow.recipe_runner.save_protocol_snapshot", return_value=Path("protocol.json"))
    @patch("workflow.recipe_runner.run_gamry_step")
    def test_next_trial_starts_after_ru_bypass(
        self,
        run_step,
        _snapshot,
        prepare_outputs,
        register_trial,
        _check_abort,
        _set_state,
        _log,
    ) -> None:
        prepare_outputs.return_value = [output_record(1), output_record(2)]
        run_step.side_effect = [
            {
                "ok": True,
                "trial_metadata": {
                    "trial_status": "skipped",
                    "skip_reason": "Unable to obtain a valid Ru",
                },
            },
            {
                "ok": True,
                "trial_metadata": {
                    "trial_status": "completed",
                    "ru_selected_ohm": 12.0,
                    "ru_applied_ohm": 9.6,
                },
            },
        ]
        protocol = {
            "protocol_name": "two_trials",
            "steps": [
                {"name": "first", "technique": "ocp", "enabled": True},
                {"name": "second", "technique": "ocp", "enabled": True},
            ],
        }
        run_protocol_for_sample(Path("."), Path("."), 1, {"sample_id": "s1", "label": "S1"}, protocol)
        self.assertEqual(run_step.call_count, 2)
        self.assertEqual(register_trial.call_count, 2)

    @patch("workflow.recipe_runner.append_log")
    @patch("workflow.recipe_runner.set_automation_state")
    @patch("workflow.recipe_runner.check_abort")
    @patch("workflow.recipe_runner.register_trial_result")
    @patch("workflow.recipe_runner.prepare_protocol_outputs", return_value=[output_record(1)])
    @patch("workflow.recipe_runner.save_protocol_snapshot", return_value=Path("protocol.json"))
    @patch("workflow.recipe_runner.run_gamry_step", side_effect=GamryClientError("communication loss"))
    def test_critical_worker_failure_propagates_to_abort_full_run(
        self,
        _run_step,
        _snapshot,
        _prepare,
        register_trial,
        _check_abort,
        _set_state,
        _log,
    ) -> None:
        protocol = {
            "protocol_name": "one_trial",
            "steps": [{"name": "first", "technique": "ocp", "enabled": True}],
        }
        with self.assertRaises(GamryClientError):
            run_protocol_for_sample(Path("."), Path("."), 1, {"sample_id": "s1", "label": "S1"}, protocol)
        self.assertEqual(register_trial.call_count, 1)


if __name__ == "__main__":
    unittest.main()
