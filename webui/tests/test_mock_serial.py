from __future__ import annotations

import unittest
from unittest.mock import patch

from hardware.serial_base import SerialDevice


class MockSerialTest(unittest.TestCase):
    def test_mock_serial_waits_for_ack(self) -> None:
        device = SerialDevice(
            name="Test Axis",
            port="MOCK",
            baud_rate=115200,
        )

        with patch.object(SerialDevice, "mock_serial_enabled", return_value=True):
            ack = device.send_line_wait_for_ack("120", timeout_s=1)

        self.assertEqual(ack, "ACK MOCK Test Axis 120")

    def test_mock_serial_reads_first_response(self) -> None:
        device = SerialDevice(
            name="Rotation",
            port="MOCK",
            baud_rate=115200,
        )

        with patch.object(SerialDevice, "mock_serial_enabled", return_value=True):
            response = device.send_line_read_first_response("0")

        self.assertEqual(response, "ACK MOCK Rotation 0")


if __name__ == "__main__":
    unittest.main()
