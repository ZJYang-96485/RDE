from __future__ import annotations

import unittest

from workflow.run_plan_loader import RunPlanError, validate_run_plan_payload


def plan_with_step(step: dict) -> dict:
    return {
        "schema_version": 2,
        "run_name": "arm_rinse_test",
        "repetitions": 1,
        "groups": [
            {
                "group_id": "sample_1",
                "label": "Sample 1",
                "enabled": True,
                "steps": [step],
            }
        ],
    }


class RinseArmRunPlanTests(unittest.TestCase):
    def test_valid_action_persists_derived_steps(self) -> None:
        plan = validate_run_plan_payload(
            plan_with_step(
                {
                    "action": "rinse_arm_oscillation",
                    "name": "Arm rinse",
                    "enabled": True,
                    "oscillation_enabled": True,
                    "amplitude_deg": 2,
                    "cycles": 1,
                    "pause_between_moves_s": 0.2,
                    "return_to_start": True,
                }
            )
        )
        step = plan["groups"][0]["steps"][0]
        self.assertEqual(step["amplitude_steps"], 9)
        self.assertTrue(step["oscillation_enabled"])
        self.assertTrue(step["return_to_start"])

    def test_action_is_opt_in_by_default(self) -> None:
        plan = validate_run_plan_payload(
            plan_with_step(
                {
                    "action": "rinse_arm_oscillation",
                    "name": "Arm rinse",
                }
            )
        )
        self.assertFalse(plan["groups"][0]["steps"][0]["oscillation_enabled"])

    def test_invalid_amplitude_fails_before_execution(self) -> None:
        with self.assertRaisesRegex(RunPlanError, "too large"):
            validate_run_plan_payload(
                plan_with_step(
                    {
                        "action": "rinse_arm_oscillation",
                        "oscillation_enabled": True,
                        "amplitude_deg": 10,
                        "cycles": 1,
                        "pause_between_moves_s": 0,
                        "return_to_start": True,
                    }
                )
            )

    def test_enable_flag_must_be_a_real_boolean(self) -> None:
        with self.assertRaisesRegex(RunPlanError, "must be true or false"):
            validate_run_plan_payload(
                plan_with_step(
                    {
                        "action": "rinse_arm_oscillation",
                        "oscillation_enabled": "true",
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
