from __future__ import annotations

import unittest
from unittest.mock import patch

from hardware.serial_base import SerialDevice
from workflow.config_loader import (
    get_serial_device_status,
    get_serial_port,
    normalize_device_serial,
)


def station_config() -> dict:
    return {
        "serial": {
            "ports": {
                "rde": "COM6",
                "rotation": "COM3",
            },
            "device_serials": {
                "rde": "85MTYM7MQIR6NV61IOIO",
                "rotation": "6333736D33BBB9C4",
            },
        }
    }


class SerialPortResolutionTests(unittest.TestCase):
    def test_normalizes_bootloader_serial_padding(self) -> None:
        self.assertEqual(
            normalize_device_serial("00000000000000006333736D33BBB9C4"),
            normalize_device_serial("6333736D33BBB9C4"),
        )

    @patch(
        "workflow.config_loader.connected_serial_devices",
        return_value=[
            {
                "port": "COM7",
                "serial_number": "00000000000000006333736D33BBB9C4",
            }
        ],
    )
    @patch("workflow.config_loader.load_config", side_effect=station_config)
    def test_resolves_current_port_from_stable_usb_serial(
        self,
        _load_config,
        _connected_devices,
    ) -> None:
        self.assertEqual(get_serial_port("rotation"), "COM7")

    @patch("workflow.config_loader.connected_serial_devices", return_value=[])
    @patch("workflow.config_loader.load_config", side_effect=station_config)
    def test_uses_configured_fallback_when_device_is_absent(
        self,
        _load_config,
        _connected_devices,
    ) -> None:
        self.assertEqual(get_serial_port("rotation"), "COM3")

    @patch(
        "workflow.config_loader.connected_serial_devices",
        return_value=[
            {
                "port": "COM7",
                "serial_number": "00000000000000006333736D33BBB9C4",
                "vid": 0x2341,
                "pid": 0x005A,
            }
        ],
    )
    @patch("workflow.config_loader.load_config", side_effect=station_config)
    def test_reports_nano_bootloader_as_firmware_not_ready(
        self,
        _load_config,
        _connected_devices,
    ) -> None:
        status = get_serial_device_status("rotation")

        self.assertTrue(status["connected"])
        self.assertFalse(status["firmware_ready"])
        self.assertEqual(status["mode"], "bootloader")
        self.assertEqual(status["port"], "COM7")

    @patch(
        "workflow.config_loader.connected_serial_devices",
        return_value=[
            {
                "port": "COM3",
                "serial_number": "6333736D33BBB9C4",
                "vid": 0x2341,
                "pid": 0x805A,
            }
        ],
    )
    @patch("workflow.config_loader.load_config", side_effect=station_config)
    def test_reports_application_usb_device_as_firmware_ready(
        self,
        _load_config,
        _connected_devices,
    ) -> None:
        status = get_serial_device_status("rotation")

        self.assertTrue(status["firmware_ready"])
        self.assertEqual(status["mode"], "application")

    @patch("workflow.config_loader.get_serial_port", return_value="COM7")
    def test_existing_serial_device_refreshes_port_before_connect(
        self,
        _get_serial_port,
    ) -> None:
        device = SerialDevice(
            name="Rotation",
            port="COM3",
            port_key="rotation",
            baud_rate=115200,
        )

        device.refresh_configured_port()

        self.assertEqual(device.port, "COM7")

    @patch(
        "workflow.config_loader.get_serial_device_status",
        return_value={
            "mode": "bootloader",
            "port": "COM7",
        },
    )
    @patch("workflow.config_loader.get_serial_port", return_value="COM7")
    def test_connect_rejects_bootloader_before_opening_serial(
        self,
        _get_serial_port,
        _get_device_status,
    ) -> None:
        device = SerialDevice(
            name="Rotation",
            port="COM3",
            port_key="rotation",
            baud_rate=115200,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "bootloader mode",
        ):
            device.connect()


if __name__ == "__main__":
    unittest.main()
