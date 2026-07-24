from __future__ import annotations

import unittest

from workflow.rinse_paths import build_diamond_cycle, validate_rinse_settings


def valid_settings(**changes):
    values = {
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
    values.update(changes)
    return values


class RinsePathTests(unittest.TestCase):
    def test_diamond_cycle_has_five_expected_segments_and_closes(self) -> None:
        path = build_diamond_cycle(5000, 7000)

        self.assertEqual(
            [(item.x_steps, item.z_steps) for item in path],
            [
                (5000, -7000),
                (-10000, 0),
                (0, 14000),
                (10000, 0),
                (-5000, -7000),
            ],
        )
        self.assertEqual(path[-1].expected_x_offset_after_segment, 0)
        self.assertEqual(path[-1].expected_z_offset_after_segment, 0)

    def test_validation_derives_arm_steps_but_not_arm_cycle_count(self) -> None:
        settings = validate_rinse_settings(**valid_settings())

        self.assertEqual(settings["cycles"], 8)
        self.assertEqual(settings["arm_oscillation"]["amplitude_steps"], 9)
        self.assertNotIn("cycles", settings["arm_oscillation"])

    def test_cycles_are_limited_to_one_through_twenty(self) -> None:
        for cycles in (0, 21):
            with self.subTest(cycles=cycles):
                with self.assertRaisesRegex(ValueError, "between 1 and 20"):
                    validate_rinse_settings(
                        **valid_settings(cycles=cycles)
                    )

    def test_unsafe_arm_and_rpm_modes_are_rejected(self) -> None:
        unsafe_arm = dict(valid_settings()["arm_oscillation"])
        unsafe_arm["stop_policy"] = "immediate"
        with self.assertRaisesRegex(ValueError, "finish_closed_cycle"):
            validate_rinse_settings(
                **valid_settings(arm_oscillation=unsafe_arm)
            )

        unsafe_disk = dict(valid_settings()["disk_rotation"])
        unsafe_disk["mode"] = "restart_each_cycle"
        with self.assertRaisesRegex(ValueError, "continuous_for_entire"):
            validate_rinse_settings(
                **valid_settings(disk_rotation=unsafe_disk)
            )

        single_cycle_arm = dict(valid_settings()["arm_oscillation"])
        single_cycle_arm["cycles"] = 1
        with self.assertRaisesRegex(ValueError, "must run continuously"):
            validate_rinse_settings(
                **valid_settings(arm_oscillation=single_cycle_arm)
            )

    def test_immersed_rotation_requires_explicit_confirmation(self) -> None:
        disk = dict(valid_settings()["disk_rotation"])
        disk["immersed_rotation_confirmed"] = False
        with self.assertRaisesRegex(ValueError, "must be true"):
            validate_rinse_settings(**valid_settings(disk_rotation=disk))


if __name__ == "__main__":
    unittest.main()
