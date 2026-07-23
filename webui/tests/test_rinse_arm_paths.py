from __future__ import annotations

import unittest

from hardware.rotation_controller import angle_to_steps
from workflow.rinse_arm_paths import (
    build_symmetric_arm_oscillation,
    validate_rinse_arm_settings,
)


class RinseArmPathTests(unittest.TestCase):
    def test_configured_angle_conversion(self) -> None:
        expected = {2: 9, 3: 13, 5: 22, 10: 44}
        for angle, steps in expected.items():
            with self.subTest(angle=angle):
                self.assertEqual(
                    angle_to_steps(
                        angle,
                        motor_full_steps_per_rev=200,
                        microstep=8,
                    ),
                    steps,
                )

        self.assertEqual(
            angle_to_steps(
                0.01,
                motor_full_steps_per_rev=200,
                microstep=8,
            ),
            0,
        )

    def test_one_cycle_is_symmetric_and_zero_net(self) -> None:
        path = build_symmetric_arm_oscillation(
            9,
            1,
            max_relative_steps=44,
        )
        self.assertEqual([item.relative_steps for item in path], [9, -18, 9])
        self.assertEqual([item.direction for item in path], ["CCW", "CW", "CCW"])
        self.assertEqual(path[-1].expected_offset_after_segment, 0)

    def test_multiple_cycles_return_to_start_after_every_cycle(self) -> None:
        path = build_symmetric_arm_oscillation(
            22,
            3,
            max_relative_steps=44,
        )
        self.assertEqual(len(path), 9)
        self.assertEqual([path[index].expected_offset_after_segment for index in (2, 5, 8)], [0, 0, 0])
        self.assertEqual(sum(item.relative_steps for item in path), 0)

    def test_validation_rejects_center_crossing_above_board_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, r"\+A, -2A, \+A"):
            validate_rinse_arm_settings(
                amplitude_deg=10,
                cycles=1,
                pause_between_moves_s=0.2,
                return_to_start=True,
                motor_full_steps_per_rev=200,
                microstep=8,
                max_relative_steps=44,
            )

    def test_validation_requires_return_to_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "return_to_start"):
            validate_rinse_arm_settings(
                amplitude_deg=2,
                cycles=1,
                pause_between_moves_s=0,
                return_to_start=False,
                motor_full_steps_per_rev=200,
                microstep=8,
                max_relative_steps=44,
            )

    def test_invalid_numeric_settings_are_rejected(self) -> None:
        invalid_cases = (
            {"amplitude_deg": 0},
            {"amplitude_deg": -2},
            {"amplitude_deg": float("nan")},
            {"amplitude_deg": float("inf")},
            {"amplitude_deg": 0.01},
            {"cycles": 0},
            {"cycles": -1},
            {"cycles": 1.5},
            {"pause_between_moves_s": -0.1},
        )
        defaults = {
            "amplitude_deg": 2,
            "cycles": 1,
            "pause_between_moves_s": 0.2,
            "return_to_start": True,
            "motor_full_steps_per_rev": 200,
            "microstep": 8,
            "max_relative_steps": 44,
        }
        for changes in invalid_cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    validate_rinse_arm_settings(**{**defaults, **changes})


if __name__ == "__main__":
    unittest.main()
