from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import app
from hardware.motion_controller import MotionController


class ZDiagnosticTests(unittest.TestCase):
    @patch("app.emergency_stop_is_recovering", return_value=False)
    @patch("app.automation_is_running", return_value=False)
    @patch(
        "app.diagnose_linear_firmware",
        return_value={
            "ping": "ACK PONG Z",
            "status": "ACK STATUS AXIS Z ENA LOW PUL LOW DIR HIGH",
        },
    )
    @patch("app.get_serial_port", return_value="COM4")
    def test_api_diagnostic_sends_no_movement_command(
        self,
        _get_serial_port,
        diagnose,
        _automation_is_running,
        _emergency_stop_is_recovering,
    ) -> None:
        app.config.update(TESTING=True)
        response = app.test_client().post("/api/linear/diagnose")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["movement_command_sent"])
        self.assertEqual(payload["ping"], "ACK PONG Z")
        diagnose.assert_called_once_with()

    @patch("hardware.motion_controller.load_config")
    @patch("hardware.motion_controller.get_serial_port")
    @patch("hardware.motion_controller.get_baud_rate", return_value=115200)
    def test_controller_uses_only_ping_and_status(
        self,
        _get_baud_rate,
        get_serial_port,
        load_config,
    ) -> None:
        get_serial_port.side_effect = lambda name: {
            "linear": "COM4",
            "horizontal": "COM8",
            "vertical": "COM9",
        }[name]
        load_config.return_value = {
            "serial": {
                "timeouts": {
                    "axis_s": 0.4,
                    "write_s": 1.0,
                    "startup_delay_s": 0.0,
                }
            }
        }
        controller = MotionController()
        device = controller.devices["linear"]
        device.send_line_wait_for_response = MagicMock(
            side_effect=[
                "ACK PONG Z",
                "ACK STATUS AXIS Z ENA LOW PUL LOW DIR HIGH",
            ]
        )

        result = controller.diagnose_linear_firmware()

        self.assertEqual(result["ping"], "ACK PONG Z")
        commands = [
            call.args[0]
            for call in device.send_line_wait_for_response.call_args_list
        ]
        self.assertEqual(commands, ["PING", "STATUS"])

    def test_status_poll_does_not_restore_stale_rde_error(self) -> None:
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("setError(data.last_error", template)


if __name__ == "__main__":
    unittest.main()
