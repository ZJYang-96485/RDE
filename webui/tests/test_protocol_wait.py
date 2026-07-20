from __future__ import annotations

import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from workflow.data_manager import prepare_protocol_outputs
from workflow.protocol_loader import ProtocolError, validate_protocol_payload
from workflow.recipe_runner import run_protocol_for_sample


class ProtocolWaitValidationTests(unittest.TestCase):
    def test_wait_is_validated_without_an_output_file(self) -> None:
        protocol = validate_protocol_payload(
            {
                "protocol_name": "rest_between_measurements",
                "steps": [
                    {
                        "name": "settle",
                        "technique": "wait",
                        "duration_s": 12.5,
                        "output": "must_not_be_used.DTA",
                    }
                ],
            }
        )

        step = protocol["steps"][0]
        self.assertEqual(step["technique"], "wait")
        self.assertEqual(step["duration_s"], 12.5)
        self.assertNotIn("output", step)

    def test_wait_duration_must_be_positive(self) -> None:
        for duration_s in (0, -1):
            with self.subTest(duration_s=duration_s):
                with self.assertRaises(ProtocolError):
                    validate_protocol_payload(
                        {
                            "protocol_name": "invalid_wait",
                            "steps": [
                                {
                                    "name": "wait",
                                    "technique": "wait",
                                    "duration_s": duration_s,
                                }
                            ],
                        }
                    )


class ProtocolWaitExecutionTests(unittest.TestCase):
    def test_wait_reserves_no_measurement_output(self) -> None:
        protocol = {
            "protocol_name": "wait_only",
            "steps": [
                {
                    "name": "settle",
                    "technique": "wait",
                    "enabled": True,
                    "duration_s": 2,
                }
            ],
        }

        with patch("workflow.data_manager.build_step_outputs") as build_outputs:
            records = prepare_protocol_outputs(
                run_dir=Path("unused-run"),
                sample_dir=Path("unused-sample"),
                sample_index=1,
                protocol=protocol,
            )

        self.assertEqual(records, [])
        build_outputs.assert_not_called()

    def test_wait_uses_interruptible_sleep_and_not_gamry(self) -> None:
        protocol = {
            "protocol_name": "wait_only",
            "steps": [
                {
                    "name": "settle",
                    "technique": "wait",
                    "enabled": True,
                    "duration_s": 2.5,
                }
            ],
        }

        run_dir = Path("unused-run")
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "workflow.recipe_runner.save_protocol_snapshot",
                    return_value=run_dir / "protocol.json",
                )
            )
            stack.enter_context(
                patch("workflow.recipe_runner.prepare_protocol_outputs", return_value=[])
            )
            stack.enter_context(patch("workflow.recipe_runner.append_log"))
            stack.enter_context(patch("workflow.recipe_runner.set_automation_state"))
            stack.enter_context(patch("workflow.recipe_runner.check_abort"))
            wait = stack.enter_context(
                patch("workflow.recipe_runner.sleep_interruptible")
            )
            run_gamry = stack.enter_context(
                patch("workflow.recipe_runner.run_gamry_step")
            )
            run_protocol_for_sample(
                run_dir=run_dir,
                sample_dir=run_dir / "sample",
                sample_index=1,
                sample={"sample_id": "sample-1", "label": "Sample 1"},
                protocol=protocol,
            )

        wait.assert_called_once_with(
            2.5,
            "Abort requested during EChem protocol wait.",
        )
        run_gamry.assert_not_called()


class ProtocolWaitBuilderTests(unittest.TestCase):
    def test_builder_exposes_wait_step_without_dta_output(self) -> None:
        page = Path(__file__).resolve().parents[1] / "templates" / "index.html"
        source = page.read_text(encoding="utf-8")

        self.assertIn('id="addEchemWaitBtn"', source)
        self.assertIn('wait: {\n          label: "Wait"', source)
        self.assertIn('<option value="wait">Wait</option>', source)
        self.assertIn('if (kind !== "ca_range" && kind !== "wait")', source)
        self.assertIn('if (kind !== "wait") {', source)


if __name__ == "__main__":
    unittest.main()
