from __future__ import annotations

import unittest

from workflow.run_plan_loader import RunPlanError, validate_run_plan_payload


class RunPlanRinseRemovalTest(unittest.TestCase):
    def test_grouped_rinse_action_is_rejected(self) -> None:
        payload = {
            "schema_version": 2,
            "run_name": "removed_rinse_action",
            "groups": [
                {
                    "label": "Cleaning",
                    "steps": [
                        {
                            "name": "Old built-in rinse",
                            "action": "rinse",
                            "enabled": True,
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(RunPlanError, "unsupported action 'rinse'"):
            validate_run_plan_payload(payload)

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
