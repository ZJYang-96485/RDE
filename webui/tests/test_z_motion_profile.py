from __future__ import annotations

import unittest
from pathlib import Path

from workflow.safety import axis_ack_timeout_seconds


class ZMotionProfileTest(unittest.TestCase):
    def test_large_move_uses_original_distance_based_timeout(self) -> None:
        self.assertEqual(axis_ack_timeout_seconds(70_000), 50.0)

    def test_z_firmware_uses_original_distance_speed_multiplier(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "arduino"
            / "linearmovement"
            / "linearmovement.ino"
        ).read_text(encoding="utf-8")

        self.assertIn("speedMultiplierFor", source)
        self.assertIn("const unsigned int START_BASE_STEP_PULSE_US = 800;", source)
        self.assertIn("const unsigned int CRUISE_BASE_STEP_PULSE_US = 500;", source)
        self.assertIn('Serial.println("ACK PONG Z");', source)
        self.assertIn("ENABLE LOW|HIGH|DEFAULT", source)


if __name__ == "__main__":
    unittest.main()
