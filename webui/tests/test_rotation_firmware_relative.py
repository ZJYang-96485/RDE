from __future__ import annotations

import unittest
from pathlib import Path


class RotationFirmwareRelativeCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[2] / "arduino" / "rotation" / "rotation.ino"
        ).read_text(encoding="utf-8")

    def test_relative_command_has_limit_and_structured_ack(self) -> None:
        self.assertIn("MAX_RELATIVE_STEPS = 44", self.source)
        self.assertIn("runRelative(long signedSteps)", self.source)
        self.assertIn('"ACK REL requested="', self.source)
        self.assertIn('"ACK STOP REL requested="', self.source)
        self.assertIn('" executed="', self.source)
        self.assertIn('" direction="', self.source)

    def test_help_and_legacy_commands_remain(self) -> None:
        self.assertIn("REL <signed_steps>", self.source)
        self.assertIn('line == "1"', self.source)
        self.assertIn('line == "0"', self.source)
        self.assertIn('line.equalsIgnoreCase("STATUS")', self.source)
        self.assertIn('line.equalsIgnoreCase("HELP")', self.source)
        self.assertIn("isStopCommand(line)", self.source)


if __name__ == "__main__":
    unittest.main()
