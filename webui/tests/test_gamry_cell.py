from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from hardware import gamry_cell_client
from workflow import recipe_runner
from workflow import run_plan_loader
from workflow.run_plan_loader import RunPlanError, validate_run_plan_payload


def cell_state_payload(known_state: str = "off") -> dict:
    return {
        "ok": True,
        "mode": "mock",
        "known_state": known_state,
        "actual_state": "unknown",
        "instrument": "mock-potentiostat",
        "last_command": "off",
        "last_result": "Mock command completed.",
        "last_error": None,
        "updated_at": "2026-07-20T00:00:00+00:00",
    }


class GamryCellClientTests(unittest.TestCase):
    def setUp(self) -> None:
        output_dir = Path(__file__).resolve().parents[1] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = output_dir / f"test_gamry_cell_state_{uuid.uuid4().hex}.json"
        self.state_patch = patch.object(
            gamry_cell_client,
            "GAMRY_CELL_STATE_PATH",
            self.state_path,
        )
        self.state_patch.start()
        self.config_patch = patch.object(
            gamry_cell_client,
            "get_gamry_config",
            return_value={
                "mode": "mock",
                "instrument_label": "",
                "live_plot": {"mock_time_scale": 0},
            },
        )
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.state_patch.stop()
        self.state_path.unlink(missing_ok=True)
        self.state_path.with_suffix(".json.tmp").unlink(missing_ok=True)

    def test_mock_indefinite_on_status_and_off_persist_state(self) -> None:
        on = gamry_cell_client.gamry_cell_on(None)
        self.assertEqual(on["known_state"], "on")
        self.assertEqual(on["last_command"], "on_until_off")

        status = gamry_cell_client.gamry_cell_status()
        self.assertEqual(status["known_state"], "on")
        self.assertEqual(status["actual_state"], "unknown")

        off = gamry_cell_client.gamry_cell_off()
        self.assertEqual(off["known_state"], "off")
        self.assertTrue(self.state_path.is_file())

    def test_mock_timed_on_finishes_off(self) -> None:
        result = gamry_cell_client.gamry_cell_on(5)
        self.assertEqual(result["known_state"], "off")
        self.assertEqual(result["last_command"], "on_5s")
        self.assertEqual(result["command_result"]["final_state"], "off")

    def test_rejects_zero_negative_and_nonfinite_duration(self) -> None:
        for duration in (0, -1, float("nan"), float("inf")):
            with self.subTest(duration=duration):
                with self.assertRaises(gamry_cell_client.GamryCellClientError):
                    gamry_cell_client.gamry_cell_on(duration)

    @patch("hardware.serial_base.available_serial_ports", side_effect=AssertionError("serial ports consulted"))
    @patch.object(
        gamry_cell_client,
        "run_real_command",
        return_value={
            "ok": True,
            "instrument": "IFC1010-test",
            "requested_state": "on",
            "duration_s": 5,
            "final_state": "off",
            "actual_state": "off",
            "message": "Isolated Gamry command completed.",
            "time": "2026-07-20T00:00:00+00:00",
        },
    )
    @patch.object(gamry_cell_client, "get_gamry_config", return_value={"mode": "real"})
    def test_real_gamry_command_is_independent_of_serial_ports(
        self,
        _get_config,
        run_real_command,
        available_serial_ports,
    ) -> None:
        result = gamry_cell_client.gamry_cell_on(5)

        self.assertEqual(result["known_state"], "off")
        run_real_command.assert_called_once()
        available_serial_ports.assert_not_called()


class GamryCellApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.config.update(TESTING=True)
        self.client = app.test_client()

    @patch("app.gamry_cell_status", return_value=cell_state_payload())
    def test_status_route(self, status) -> None:
        response = self.client.get("/api/gamry-cell/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["known_state"], "off")
        status.assert_called_once_with()

    @patch("app.echem_measurement_is_active", return_value=False)
    @patch("app.automation_is_running", return_value=False)
    @patch("app.gamry_cell_on", return_value=cell_state_payload("off"))
    def test_timed_on_route(self, cell_on, _automation, _echem) -> None:
        response = self.client.post("/api/gamry-cell/on", json={"duration_s": 5})
        self.assertEqual(response.status_code, 200)
        cell_on.assert_called_once_with(5.0)

    @patch("app.automation_is_running", return_value=True)
    @patch("app.gamry_cell_on")
    def test_on_is_blocked_during_automation(self, cell_on, _automation) -> None:
        response = self.client.post("/api/gamry-cell/on", json={"duration_s": 5})
        self.assertEqual(response.status_code, 409)
        cell_on.assert_not_called()

    @patch("app.echem_measurement_is_active", return_value=True)
    @patch("app.automation_is_running", return_value=False)
    @patch("app.gamry_cell_on")
    def test_on_is_blocked_during_active_echem(self, cell_on, _automation, _echem) -> None:
        response = self.client.post("/api/gamry-cell/on", json={"duration_s": None})
        self.assertEqual(response.status_code, 409)
        cell_on.assert_not_called()

    @patch("app.automation_is_running", return_value=True)
    @patch("app.gamry_cell_off", return_value=cell_state_payload("off"))
    def test_off_is_allowed_during_automation(self, cell_off, _automation) -> None:
        response = self.client.post("/api/gamry-cell/off")
        self.assertEqual(response.status_code, 200)
        cell_off.assert_called_once_with()

    def test_on_rejects_invalid_duration(self) -> None:
        response = self.client.post("/api/gamry-cell/on", json={"duration_s": 0})
        self.assertEqual(response.status_code, 400)


class GamryCellRunPlanValidationTests(unittest.TestCase):
    def payload(self, steps: list[dict]) -> dict:
        return {
            "run_name": "cell_actions",
            "repetitions": 1,
            "groups": [{"label": "Cell Test", "steps": steps}],
        }

    def test_actions_normalize_type_label_and_durations(self) -> None:
        validated = validate_run_plan_payload(
            self.payload(
                [
                    {"type": "gamry_cell_on", "label": "Timed ON", "duration_s": 5},
                    {"type": "gamry_cell_on", "duration_s": ""},
                    {"type": "gamry_cell_off", "label": "OFF"},
                ]
            )
        )
        steps = validated["groups"][0]["steps"]
        self.assertEqual(steps[0]["name"], "Timed ON")
        self.assertEqual(steps[0]["duration_s"], 5.0)
        self.assertIsNone(steps[1]["duration_s"])
        self.assertEqual(steps[2]["action"], "gamry_cell_off")
        self.assertNotIn("duration_s", steps[2])

    def test_cell_on_rejects_nonpositive_duration(self) -> None:
        for duration in (0, -1, "nan", "inf"):
            with self.subTest(duration=duration):
                with self.assertRaises(RunPlanError):
                    validate_run_plan_payload(
                        self.payload([{"type": "gamry_cell_on", "duration_s": duration}])
                    )

    def test_saved_plan_round_trips_cell_actions(self) -> None:
        plan_dir = (
            Path(__file__).resolve().parents[1]
            / "output"
            / f"test_cell_run_plans_{uuid.uuid4().hex}"
        )
        plan_dir.mkdir(parents=True)
        try:
            with patch.object(run_plan_loader, "run_plans_dir", return_value=plan_dir):
                run_plan_loader.save_run_plan(
                    self.payload(
                        [
                            {"type": "gamry_cell_on", "duration_s": 5},
                            {"type": "gamry_cell_off"},
                        ]
                    )
                )
                loaded = run_plan_loader.load_run_plan("cell_actions")

            self.assertEqual(
                loaded["groups"][0]["steps"],
                [
                    {
                        "name": "Step 1",
                        "action": "gamry_cell_on",
                        "enabled": True,
                        "duration_s": 5.0,
                    },
                    {
                        "name": "Step 2",
                        "action": "gamry_cell_off",
                        "enabled": True,
                    },
                ],
            )
        finally:
            shutil.rmtree(plan_dir, ignore_errors=True)


class GamryCellRunnerTests(unittest.TestCase):
    def run_group_actions(self, duration_s) -> list[tuple]:
        events: list[tuple] = []
        group = {
            "label": "Cell Test",
            "steps": [
                {"action": "gamry_cell_on", "name": "ON", "duration_s": duration_s},
                {"action": "wait", "name": "Wait", "duration_s": 2},
                {"action": "gamry_cell_off", "name": "OFF"},
            ],
        }

        with (
            patch.object(recipe_runner, "create_sample_workspace", return_value=Path("mock-group")),
            patch.object(recipe_runner, "append_log"),
            patch.object(recipe_runner, "set_automation_state"),
            patch.object(recipe_runner, "check_abort"),
            patch.object(
                recipe_runner,
                "gamry_cell_on",
                side_effect=lambda duration: events.append(("on", duration)) or cell_state_payload(),
            ),
            patch.object(
                recipe_runner,
                "sleep_interruptible",
                side_effect=lambda duration, _message: events.append(("wait", duration)),
            ),
            patch.object(
                recipe_runner,
                "gamry_cell_off",
                side_effect=lambda: events.append(("off",)) or cell_state_payload(),
            ),
        ):
            recipe_runner.run_group(
                run_dir=Path("mock-run"),
                group=group,
                group_index=1,
                repetition=1,
                repetitions=1,
                position_state={"x": 0, "z": 0},
            )

        return events

    def test_timed_on_wait_off_execute_in_order(self) -> None:
        self.assertEqual(
            self.run_group_actions(5),
            [("on", 5.0), ("wait", 2.0), ("off",)],
        )

    def test_indefinite_on_wait_off_execute_in_order(self) -> None:
        self.assertEqual(
            self.run_group_actions(None),
            [("on", None), ("wait", 2.0), ("off",)],
        )

    def test_on_failure_stops_group(self) -> None:
        group = {
            "label": "Cell Test",
            "steps": [{"action": "gamry_cell_on", "name": "ON", "duration_s": 5}],
        }
        with (
            patch.object(recipe_runner, "create_sample_workspace", return_value=Path("mock-group")),
            patch.object(recipe_runner, "append_log"),
            patch.object(recipe_runner, "set_automation_state"),
            patch.object(recipe_runner, "check_abort"),
            patch.object(recipe_runner, "gamry_cell_on", side_effect=RuntimeError("relay failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "relay failed"):
                recipe_runner.run_group(
                    Path("mock-run"), group, 1, 1, 1, {"x": 0, "z": 0}
                )


if __name__ == "__main__":
    unittest.main()
