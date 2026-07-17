from __future__ import annotations

import unittest
from unittest.mock import patch

from app import app


class RotationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.config.update(TESTING=True)
        self.client = app.test_client()

    @patch("app.automation_is_running", return_value=False)
    @patch("app.get_serial_port", return_value="COM3")
    @patch("app.send_rotation_text", return_value="Moved 180 deg CCW")
    def test_rotation_success_reports_board_completion(
        self,
        _send_rotation_text,
        _get_serial_port,
        _automation_is_running,
    ) -> None:
        response = self.client.post("/api/rotation/send", json={"command": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": True,
                "command": "1",
                "com_port": "COM3",
                "ack": "Moved 180 deg CCW",
            },
        )

    @patch("app.automation_is_running", return_value=False)
    @patch("app.get_serial_port", return_value="COM3")
    @patch("app.send_rotation_text", side_effect=TimeoutError("no matching board response"))
    def test_rotation_failure_returns_port_and_logs_exception(
        self,
        _send_rotation_text,
        _get_serial_port,
        _automation_is_running,
    ) -> None:
        with self.assertLogs(app.logger.name, level="ERROR") as captured:
            response = self.client.post("/api/rotation/send", json={"command": "0"})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(),
            {"error": "Rotation command '0' failed on COM3: no matching board response"},
        )
        self.assertIn("Manual rotation command '0' failed on COM3", "\n".join(captured.output))

    @patch("app.stop_rde")
    @patch("app.emergency_stop_rotation", return_value=True)
    @patch(
        "app.emergency_stop_motion",
        return_value={"linear": True, "horizontal": True, "vertical": False},
    )
    @patch("app.abort_automation")
    @patch("app.automation_is_running", return_value=True)
    def test_automation_abort_stops_rotation_and_axes(
        self,
        _automation_is_running,
        abort_automation,
        _emergency_stop_motion,
        emergency_stop_rotation,
        _stop_rde,
    ) -> None:
        response = self.client.post("/api/automation/abort")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["rotation_stop_sent"])
        self.assertEqual(
            payload["motion_stop_sent"],
            {"linear": True, "horizontal": True, "vertical": False},
        )
        abort_automation.assert_called_once_with()
        emergency_stop_rotation.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
