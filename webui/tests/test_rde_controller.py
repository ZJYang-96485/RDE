from __future__ import annotations

import unittest
from unittest.mock import patch

from hardware.rde_controller import RDEController
from hardware.serial_base import SerialDevice


class RDEControllerTest(unittest.TestCase):
    def test_set_rpm_waits_for_board_acknowledgement(self) -> None:
        controller = RDEController()

        with patch.object(SerialDevice, "mock_serial_enabled", return_value=True):
            response = controller.set_rpm(1600)

        self.assertEqual(response, "ACK MOCK RDE 1600")

    def test_legacy_rpm_response_remains_compatible(self) -> None:
        controller = RDEController()

        with patch.object(
            controller.device,
            "send_line_wait_for_response",
            return_value="RPM: 1600 -> Duty: 52",
        ) as send:
            response = controller.set_rpm(1600)

        self.assertEqual(response, "RPM: 1600 -> Duty: 52")
        self.assertIn("RPM: 1600", send.call_args.kwargs["expected_prefixes"])


if __name__ == "__main__":
    unittest.main()
