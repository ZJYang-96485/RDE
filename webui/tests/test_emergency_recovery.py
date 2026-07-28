from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import app
from hardware.gamry_client import GamryClient
from workflow.state import (
    begin_emergency_stop_recovery,
    clear_abort,
    finish_emergency_stop_recovery,
    get_emergency_stop_state,
    request_abort,
)


class GamryEmergencyDisconnectTests(unittest.TestCase):
    def test_active_worker_is_terminated_and_generation_guarded(self) -> None:
        client = GamryClient()
        process = MagicMock()
        process.pid = 1234
        process.poll.return_value = None
        process.wait.return_value = 0
        client._active_process = process
        client._active_job_id = "job-1"

        result = client.disconnect_active_worker()

        self.assertTrue(result["worker_was_active"])
        self.assertTrue(result["worker_terminated"])
        self.assertFalse(result["worker_force_killed"])
        process.terminate.assert_called_once_with()
        self.assertTrue(client.emergency_disconnect_in_progress())
        self.assertFalse(
            client.finish_emergency_disconnect(result["generation"] - 1)
        )
        self.assertTrue(
            client.finish_emergency_disconnect(result["generation"])
        )
        self.assertFalse(client.emergency_disconnect_in_progress())


class EmergencyRecoveryCoordinatorTests(unittest.TestCase):
    def tearDown(self) -> None:
        generation = begin_emergency_stop_recovery("test cleanup")
        finish_emergency_stop_recovery(generation, "Emergency stop is ready.")
        clear_abort()

    @patch("app.finish_emergency_stop_recovery")
    @patch("app.clear_abort")
    @patch("app.manual_arm_is_idle", return_value=True)
    @patch("app.get_rotation_controller")
    @patch("app.wait_for_motion_idle", return_value=True)
    @patch("app.automation_is_running", return_value=False)
    @patch("app.gamry_cell_off")
    @patch("app.get_gamry_client")
    def test_recovery_disconnects_cell_then_releases_abort(
        self,
        get_gamry_client,
        gamry_cell_off,
        _automation_is_running,
        _wait_for_motion_idle,
        get_rotation_controller,
        _manual_arm_is_idle,
        clear_abort_mock,
        finish_recovery,
    ) -> None:
        generation = begin_emergency_stop_recovery("test emergency")
        request_abort()
        get_rotation_controller.return_value.wait_until_idle.return_value = True

        app.complete_emergency_stop_recovery(generation, 41)

        gamry_cell_off.assert_called_once_with()
        get_gamry_client.return_value.finish_emergency_disconnect.assert_called_once_with(
            41
        )
        clear_abort_mock.assert_called_once_with()
        finish_recovery.assert_called_once()

    def test_status_state_distinguishes_recovering_from_ready(self) -> None:
        generation = begin_emergency_stop_recovery("test emergency")
        self.assertTrue(get_emergency_stop_state()["recovering"])
        finish_emergency_stop_recovery(generation, "ready")
        state = get_emergency_stop_state()
        self.assertFalse(state["recovering"])
        self.assertEqual(state["message"], "ready")


class ResolvedGamryBannerTests(unittest.TestCase):
    @patch("app.set_rde_error")
    @patch("app.clear_automation_error")
    @patch("app.automation_is_running", return_value=False)
    @patch("app.get_gamry_client")
    def test_successful_probe_clears_only_live_error_state(
        self,
        get_gamry_client,
        _automation_is_running,
        clear_automation_error,
        set_rde_error,
    ) -> None:
        get_gamry_client.return_value.probe.return_value = {
            "ok": True,
            "connected": True,
            "selected_instrument": "IFC1010-36030",
        }
        app.app.config.update(TESTING=True)

        response = app.app.test_client().post("/api/gamry/probe")

        self.assertEqual(response.status_code, 200)
        clear_automation_error.assert_called_once_with(
            "Idle — Gamry connection verified"
        )
        set_rde_error.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
