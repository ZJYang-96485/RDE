from __future__ import annotations

import unittest

from workflow.run_plan_loader import (
    RunPlanError,
    load_run_plan,
    validate_run_plan_payload,
)


class RunPlanRinseActionTest(unittest.TestCase):
    def test_arm_only_run_plan_action_is_replaced_by_rinse(self) -> None:
        payload = {
            "schema_version": 2,
            "run_name": "obsolete_arm_only_rinse",
            "groups": [
                {
                    "label": "Rinse",
                    "steps": [
                        {
                            "name": "Old arm-only action",
                            "action": "rinse_arm_oscillation",
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(
            RunPlanError,
            "unsupported action 'rinse_arm_oscillation'",
        ):
            validate_run_plan_payload(payload)

    def test_shipped_rinse_check_uses_one_packaged_rinse_step(self) -> None:
        plan = load_run_plan("Rinse Check")
        rinse_group = next(
            group for group in plan["groups"] if group["label"] == "Rinse"
        )

        self.assertEqual(len(rinse_group["steps"]), 1)
        step = rinse_group["steps"][0]
        self.assertEqual(step["action"], "rinse")
        self.assertEqual(step["cycles"], 8)
        self.assertEqual(step["disk_rotation"]["rpm"], 300)
        self.assertTrue(
            step["disk_rotation"]["immersed_rotation_confirmed"]
        )

    def test_grouped_rinse_action_is_validated_and_restored(self) -> None:
        payload = {
            "schema_version": 2,
            "run_name": "packaged_rinse_action",
            "groups": [
                {
                    "label": "Cleaning",
                    "steps": [
                        {
                            "name": "Concurrent rinse",
                            "action": "rinse",
                            "enabled": True,
                            "cycles": 8,
                            "diamond": {
                                "x_radius_steps": 5000,
                                "z_radius_steps": 7000,
                            },
                            "arm_oscillation": {
                                "enabled": True,
                                "amplitude_deg": 2.0,
                                "pause_between_moves_s": 0.1,
                                "mode": "continuous_until_diamond_complete",
                                "stop_policy": "finish_closed_cycle",
                            },
                            "disk_rotation": {
                                "enabled": True,
                                "rpm": 300,
                                "settle_s": 1.0,
                                "mode": "continuous_for_entire_rinse_step",
                                "stop_after": True,
                                "immersed_rotation_confirmed": True,
                            },
                            "inter_cycle_pause_s": 0.0,
                            "cycle_timeout_s": 30.0,
                            "require_closed_paths": True,
                        }
                    ],
                }
            ],
        }

        plan = validate_run_plan_payload(payload)
        step = plan["groups"][0]["steps"][0]
        self.assertEqual(step["action"], "rinse")
        self.assertEqual(step["cycles"], 8)
        self.assertEqual(step["arm_oscillation"]["amplitude_steps"], 9)

    def test_legacy_rinse_after_requires_explicit_steps(self) -> None:
        payload = {
            "run_name": "legacy_rinse",
            "samples": [
                {
                    "label": "Sample 1",
                    "position": {"x": 0, "y": 0, "z": 0},
                    "rinse_after": True,
                }
            ],
        }

        with self.assertRaisesRegex(
            RunPlanError,
            "rinse_after is no longer supported",
        ):
            validate_run_plan_payload(payload)


if __name__ == "__main__":
    unittest.main()
