from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.recipe_runner import run_group


class RinseArmRunnerIntegrationTests(unittest.TestCase):
    def test_z_finishes_then_disk_stops_then_arm_package_runs(self) -> None:
        order: list[str] = []
        group = {
            "group_id": "sample_1",
            "label": "Sample 1",
            "enabled": True,
            "steps": [
                {
                    "action": "move_z",
                    "name": "Lower into rinse",
                    "enabled": True,
                    "steps": 100,
                },
                {
                    "action": "rinse_arm_oscillation",
                    "name": "Arm rinse",
                    "enabled": True,
                    "oscillation_enabled": True,
                    "amplitude_deg": 2.0,
                    "amplitude_steps": 9,
                    "cycles": 1,
                    "pause_between_moves_s": 0.2,
                    "return_to_start": True,
                },
            ],
        }

        with (
            patch(
                "workflow.recipe_runner.create_sample_workspace",
                return_value=Path("."),
            ),
            patch("workflow.recipe_runner.append_log"),
            patch("workflow.recipe_runner.set_automation_state"),
            patch("workflow.recipe_runner.check_abort"),
            patch("workflow.recipe_runner.get_abort_event", return_value=None),
            patch(
                "workflow.recipe_runner.move_linear_steps",
                side_effect=lambda *_args, **_kwargs: order.append("z_ack") or "ACK",
            ),
            patch(
                "workflow.recipe_runner.stop_rde",
                side_effect=lambda *_args, **_kwargs: order.append("disk_stop"),
            ),
            patch(
                "workflow.recipe_runner.execute_rinse_arm_oscillation",
                side_effect=lambda **_kwargs: order.append("arm_package"),
            ),
        ):
            run_group(
                run_dir=Path("."),
                group=group,
                group_index=1,
                repetition=1,
                repetitions=1,
                position_state={"x": 0, "z": 0},
            )

        self.assertEqual(order, ["z_ack", "disk_stop", "arm_package"])

    def test_disabled_package_sends_no_stop_or_arm_command(self) -> None:
        group = {
            "group_id": "sample_1",
            "label": "Sample 1",
            "enabled": True,
            "steps": [
                {
                    "action": "rinse_arm_oscillation",
                    "name": "Arm rinse",
                    "enabled": True,
                    "oscillation_enabled": False,
                    "amplitude_deg": 5.0,
                    "amplitude_steps": 22,
                    "cycles": 3,
                    "pause_between_moves_s": 0.2,
                    "return_to_start": True,
                }
            ],
        }

        with (
            patch(
                "workflow.recipe_runner.create_sample_workspace",
                return_value=Path("."),
            ),
            patch("workflow.recipe_runner.append_log"),
            patch("workflow.recipe_runner.set_automation_state"),
            patch("workflow.recipe_runner.check_abort"),
            patch("workflow.recipe_runner.stop_rde") as stop,
            patch(
                "workflow.recipe_runner.execute_rinse_arm_oscillation"
            ) as execute,
        ):
            run_group(
                run_dir=Path("."),
                group=group,
                group_index=1,
                repetition=1,
                repetitions=1,
                position_state={"x": 0, "z": 0},
            )

        stop.assert_not_called()
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
