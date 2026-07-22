from __future__ import annotations

import unittest
from unittest.mock import patch

from hardware.rotation_controller import RotationController, RotationControllerError
from hardware.serial_base import (
    MockSerialConnection,
    SerialConnectionError,
    SerialDevice,
)
from workflow.state import AutomationAbortRequested


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

    @patch("hardware.serial_base.available_serial_ports", return_value=["COM3", "COM8", "COM11"])
    @patch("hardware.serial_base.serial.Serial", side_effect=OSError("port missing"))
    def test_missing_configured_port_reports_detected_ports(
        self,
        _serial,
        _available_ports,
    ) -> None:
        device = SerialDevice(
            name="RDE",
            port="COM6",
            baud_rate=115200,
            startup_delay_s=0,
        )

        with self.assertRaisesRegex(
            SerialConnectionError,
            r"RDE: configured port COM6 is unavailable\. "
            r"Detected serial ports: COM3, COM8, COM11\. "
            r"The configured port was not remapped",
        ):
            device.connect()

    @patch("hardware.serial_base.available_serial_ports", return_value=["COM3", "COM6", "COM8"])
    @patch(
        "hardware.serial_base.serial.Serial",
        side_effect=PermissionError(13, "Access is denied", "COM6"),
    )
    def test_detected_busy_port_is_not_reported_as_missing(
        self,
        _serial,
        _available_ports,
    ) -> None:
        device = SerialDevice(
            name="RDE",
            port="COM6",
            baud_rate=115200,
            startup_delay_s=0,
        )

        with self.assertRaisesRegex(
            SerialConnectionError,
            r"configured port COM6 is detected, but Windows refused to open it.*"
            r"probably already in use by another RDE web server.*"
            r"Open error: PermissionError",
        ):
            device.connect()

    def test_wait_for_expected_response_discards_stale_input(self) -> None:
        device = SerialDevice(
            name="Rotation",
            port="MOCK",
            baud_rate=115200,
        )
        connection = MockSerialConnection("Rotation", "MOCK")
        connection.responses.append(b"Moved 180 deg CCW\n")
        device.conn = connection

        response = device.send_line_wait_for_response(
            "0",
            timeout_s=1,
            expected_prefixes=("ACK MOCK Rotation 0",),
        )

        self.assertEqual(response, "ACK MOCK Rotation 0")

    def test_wait_for_expected_response_rejects_mismatch(self) -> None:
        device = SerialDevice(
            name="Rotation",
            port="MOCK",
            baud_rate=115200,
        )
        device.conn = MockSerialConnection("Wrong Device", "MOCK")

        with self.assertRaises(SerialConnectionError):
            device.send_line_wait_for_response(
                "0",
                timeout_s=0.01,
                expected_prefixes=("ACK MOCK Rotation 0",),
            )

    def test_rotation_controller_waits_for_matching_mock_completion(self) -> None:
        controller = RotationController()
        with patch.object(SerialDevice, "mock_serial_enabled", return_value=True):
            response = controller.send_text("1")

        self.assertEqual(response, "ACK MOCK Rotation 1")

    def test_rotation_controller_propagates_stop_as_abort(self) -> None:
        controller = RotationController()

        with (
            patch.object(
                controller.device,
                "send_line_wait_for_response",
                return_value="ACK STOP 120/800 POSITION 120",
            ),
            patch.object(controller.device, "close") as close,
        ):
            with self.assertRaisesRegex(AutomationAbortRequested, "Rotation arm stopped"):
                controller.send_text("1")

        close.assert_called_once_with()

    def test_rotation_emergency_stop_bypasses_active_transaction(self) -> None:
        controller = RotationController()
        controller.device.conn = MockSerialConnection("Rotation", "MOCK")

        self.assertTrue(controller.emergency_stop())
        self.assertEqual(
            controller.device.conn.readline().decode("utf-8").strip(),
            "ACK STOP MOCK Rotation",
        )

    def test_rotation_controller_rejects_concurrent_command_without_queueing(self) -> None:
        controller = RotationController()
        controller.command_lock.acquire()

        try:
            with self.assertRaisesRegex(RotationControllerError, "rejected and was not queued"):
                controller.send_text("0")
        finally:
            controller.command_lock.release()

    def test_rotation_controller_closes_serial_connection_after_failure(self) -> None:
        controller = RotationController()

        with (
            patch.object(
                controller.device,
                "send_line_wait_for_response",
                side_effect=SerialConnectionError("no response"),
            ),
            patch.object(controller.device, "close") as close,
        ):
            with self.assertRaisesRegex(RotationControllerError, "no response"):
                controller.send_text("1")

        close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
