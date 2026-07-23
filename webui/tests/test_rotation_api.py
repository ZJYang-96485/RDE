from __future__ import annotations

import unittest
from unittest.mock import patch

from app import app, manual_arm_motion_lock
from hardware.rotation_controller import RotationMoveResult


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

    @patch("app.automation_is_running", return_value=False)
    @patch("app.get_serial_port", return_value="COM3")
    @patch("app.stop_rde")
    @patch("app.get_rotation_controller")
    def test_manual_short_angle_stops_disk_and_reports_exact_move(
        self,
        get_controller,
        stop_rde,
        _get_serial_port,
        _automation_is_running,
    ) -> None:
        get_controller.return_value.relative_steps.return_value = RotationMoveResult(
            requested_steps=9,
            executed_steps=9,
            requested_angle_deg=2.0,
            executed_angle_deg=2.025,
            direction="CCW",
            status="completed",
            raw_response="ACK REL requested=9 executed=9 direction=CCW",
            angle_confidence="tracked",
        )
        get_controller.return_value.expected_relative_state.return_value = {
            "expected_offset_steps": 0,
            "angle_confidence": "tracked",
        }

        response = self.client.post(
            "/api/rotation/relative-angle",
            json={"angle_deg": 2},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["disk_rpm_stopped"])
        self.assertEqual(payload["move"]["requested_steps"], 9)
        self.assertEqual(payload["move"]["executed_steps"], 9)
        stop_rde.assert_called_once_with(None)
        get_controller.return_value.relative_steps.assert_called_once_with(
            9,
            requested_angle_deg=2.0,
        )

    @patch("app.automation_is_running", return_value=False)
    @patch("app.stop_rde")
    @patch("app.get_rotation_controller")
    def test_invalid_manual_short_angle_sends_nothing(
        self,
        get_controller,
        stop_rde,
        _automation_is_running,
    ) -> None:
        response = self.client.post(
            "/api/rotation/relative-angle",
            json={"angle_deg": 100},
        )

        self.assertEqual(response.status_code, 400)
        stop_rde.assert_not_called()
        get_controller.assert_not_called()

    @patch("app.automation_is_running", return_value=False)
    @patch("app.get_serial_port", return_value="COM3")
    @patch("app.execute_rinse_arm_oscillation")
    @patch("app.stop_rde")
    def test_manual_oscillation_reuses_package_and_stops_disk(
        self,
        stop_rde,
        execute,
        _get_serial_port,
        _automation_is_running,
    ) -> None:
        execute.return_value = {
            "status": "completed",
            "cycles_completed": 1,
            "segments_completed": 3,
            "net_relative_steps": 0,
        }

        response = self.client.post(
            "/api/rotation/oscillate",
            json={
                "amplitude_deg": 2,
                "cycles": 1,
                "pause_between_moves_s": 0.2,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["disk_rpm_stopped"])
        self.assertEqual(payload["oscillation"]["segments_completed"], 3)
        stop_rde.assert_called_once_with(None)
        kwargs = execute.call_args.kwargs
        self.assertEqual(kwargs["amplitude_deg"], 2.0)
        self.assertEqual(kwargs["amplitude_steps"], 9)
        self.assertEqual(kwargs["cycles"], 1)
        self.assertEqual(kwargs["pause_between_moves_s"], 0.2)

    @patch("app.automation_is_running", return_value=True)
    @patch("app.stop_rde")
    @patch("app.get_rotation_controller")
    def test_manual_arm_endpoints_are_blocked_during_automation(
        self,
        get_controller,
        stop_rde,
        _automation_is_running,
    ) -> None:
        relative = self.client.post(
            "/api/rotation/relative-angle",
            json={"angle_deg": 2},
        )
        oscillation = self.client.post(
            "/api/rotation/oscillate",
            json={
                "amplitude_deg": 2,
                "cycles": 1,
                "pause_between_moves_s": 0.2,
            },
        )

        self.assertEqual(relative.status_code, 409)
        self.assertEqual(oscillation.status_code, 409)
        stop_rde.assert_not_called()
        get_controller.assert_not_called()

    def test_motor_control_renders_short_angle_and_oscillation_controls(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        for element_id in (
            "shortRotationAngle",
            "shortRotationCcwBtn",
            "shortRotationCwBtn",
            "manualOscillationAmplitude",
            "manualOscillationCycles",
            "manualOscillationPause",
            "manualOscillationPresetBtn",
            "manualOscillationStartBtn",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', page)
        self.assertIn("Safe preset: 2° / 1 cycle", page)
        self.assertIn("/api/rotation/relative-angle", page)
        self.assertIn("/api/rotation/oscillate", page)

    @patch("app.automation_is_running", return_value=False)
    def test_manual_arm_lock_blocks_other_motion_and_automation(
        self,
        _automation_is_running,
    ) -> None:
        manual_arm_motion_lock.acquire()
        try:
            responses = (
                self.client.post(
                    "/api/start",
                    json={"rpm": 1000, "duration_seconds": 1},
                ),
                self.client.post(
                    "/api/horizontal/send",
                    json={"command": "10"},
                ),
                self.client.post(
                    "/api/rotation/send",
                    json={"command": "1"},
                ),
                self.client.post(
                    "/api/automation/start",
                    json={"groups": []},
                ),
            )
        finally:
            manual_arm_motion_lock.release()

        for index, response in enumerate(responses):
            with self.subTest(request_index=index):
                self.assertEqual(response.status_code, 409)

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
