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

    @patch("app.gamry_cell_off")
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
        gamry_cell_off,
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
        gamry_cell_off.assert_called_once_with()

    @patch("app.gamry_cell_off")
    @patch("app.stop_rde")
    @patch("app.emergency_stop_rotation", return_value=True)
    @patch(
        "app.emergency_stop_motion",
        return_value={"linear": True, "horizontal": True, "vertical": False},
    )
    @patch("app.abort_automation")
    @patch("app.automation_is_running", return_value=False)
    def test_manual_motor_emergency_stop_works_without_automation(
        self,
        _automation_is_running,
        abort_automation,
        emergency_stop_motion,
        emergency_stop_rotation,
        stop_rde,
        gamry_cell_off,
    ) -> None:
        response = self.client.post("/api/motors/emergency-stop")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["automation_was_running"])
        self.assertTrue(payload["rotation_stop_sent"])
        self.assertEqual(
            payload["motion_stop_sent"],
            {"linear": True, "horizontal": True, "vertical": False},
        )
        abort_automation.assert_not_called()
        emergency_stop_motion.assert_called_once_with()
        emergency_stop_rotation.assert_called_once_with()
        stop_rde.assert_called_once_with("Manual motor emergency stop requested.")
        gamry_cell_off.assert_called_once_with()

    @patch("app.gamry_cell_off", side_effect=RuntimeError("cell relay unavailable"))
    @patch("app.stop_rde")
    @patch("app.emergency_stop_rotation", return_value=True)
    @patch("app.emergency_stop_motion", return_value={"linear": True, "horizontal": True})
    @patch("app.automation_is_running", return_value=False)
    def test_emergency_stop_reports_cell_off_failure_without_masking_motors(
        self,
        _automation_is_running,
        emergency_stop_motion,
        _emergency_stop_rotation,
        stop_rde,
        _gamry_cell_off,
    ) -> None:
        response = self.client.post("/api/motors/emergency-stop")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["gamry_cell_off_error"], "cell relay unavailable")
        emergency_stop_motion.assert_called_once_with()
        stop_rde.assert_called_once_with("Manual motor emergency stop requested.")


if __name__ == "__main__":
    unittest.main()
