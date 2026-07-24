from __future__ import annotations

import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from hardware.rotation_controller import RotationMoveResult
from workflow.recipe_runner import run_group
from workflow.rinse import execute_rinse
from workflow.rinse_paths import validate_rinse_settings
from workflow.state import (
    get_axis_position_confidence,
    reset_axis_positions,
)


def completed_arm_result(steps: int) -> RotationMoveResult:
    return RotationMoveResult(
        requested_steps=steps,
        executed_steps=steps,
        requested_angle_deg=steps * 0.225,
        executed_angle_deg=steps * 0.225,
        direction="CCW" if steps > 0 else "CW",
        status="completed",
        raw_response=f"ACK REL {steps}",
        angle_confidence="tracked",
    )


class FakeArmController:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.offset = 0
        self.confidence = "tracked"
        self.commands: list[int] = []
        self.lock = threading.Lock()
        self.two_cycles = threading.Event()
        self.marked_uncertain = False

    def expected_relative_state(self):
        with self.lock:
            return {
                "expected_offset_steps": self.offset,
                "angle_confidence": self.confidence,
            }

    def max_relative_steps(self) -> int:
        return 44

    def relative_steps(self, steps: int) -> RotationMoveResult:
        with self.lock:
            self.order.append(f"arm:{steps}")
            self.commands.append(steps)
            self.offset += steps
            if len(self.commands) >= 6:
                self.two_cycles.set()
        time.sleep(0.0005)
        return completed_arm_result(steps)

    def mark_angle_uncertain(self, _reason=None) -> None:
        with self.lock:
            self.confidence = "uncertain"
            self.marked_uncertain = True


def settings(cycles=3):
    return validate_rinse_settings(
        cycles=cycles,
        diamond={"x_radius_steps": 5, "z_radius_steps": 7},
        arm_oscillation={
            "enabled": True,
            "amplitude_deg": 2.0,
            "pause_between_moves_s": 0.0,
            "mode": "continuous_until_diamond_complete",
            "stop_policy": "finish_closed_cycle",
        },
        disk_rotation={
            "enabled": True,
            "rpm": 300,
            "settle_s": 0.0,
            "mode": "continuous_for_entire_rinse_step",
            "stop_after": True,
            "immersed_rotation_confirmed": True,
        },
        inter_cycle_pause_s=0.0,
        cycle_timeout_s=30.0,
        require_closed_paths=True,
    )


class PackagedRinseTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_axis_positions()

    def tearDown(self) -> None:
        reset_axis_positions()

    def test_rpm_and_arm_each_start_once_for_all_diamond_cycles(self) -> None:
        order: list[str] = []
        arm = FakeArmController(order)
        rpm_active = {"value": False}
        move_commands: list[tuple[int, int]] = []
        records: list[dict] = []

        def start_rpm(rpm: int) -> str:
            order.append(f"rpm:{rpm}")
            rpm_active["value"] = True
            return "ACK"

        def move(*, x_steps, z_steps, abort_event):
            self.assertTrue(rpm_active["value"])
            self.assertTrue(arm.commands)
            if not move_commands:
                self.assertTrue(arm.two_cycles.wait(1.0))
            order.append(f"diamond:{x_steps},{z_steps}")
            move_commands.append((x_steps, z_steps))
            return {"x_ack": "ACK", "z_ack": "ACK"}

        def stop(_error) -> None:
            order.append("rpm:stop")
            rpm_active["value"] = False

        result = execute_rinse(
            run_dir=".",
            label="Rinse",
            settings=settings(cycles=3),
            position_state={"x": 100, "z": 200},
            controller=arm,
            move_fn=move,
            send_rpm_fn=start_rpm,
            stop_rde_fn=stop,
            emergency_stop_motion_fn=lambda: None,
            emergency_stop_rotation_fn=lambda: None,
            external_abort_event=threading.Event(),
            record_fn=lambda _run_dir, record: records.append(dict(record)) or record,
            log_fn=lambda _run_dir, _message: None,
        )

        self.assertEqual(order[0], "rpm:300")
        self.assertTrue(order[1].startswith("arm:"))
        self.assertEqual(order[-1], "rpm:stop")
        self.assertEqual(len(move_commands), 15)
        self.assertEqual(result["diamond_cycles_completed"], 3)
        self.assertGreaterEqual(result["arm_oscillation_cycles_completed"], 2)
        self.assertEqual(len(arm.commands) % 3, 0)
        self.assertEqual(arm.offset, 0)
        self.assertTrue(result["rpm_started_once"])
        self.assertTrue(result["rpm_stopped_once"])
        self.assertEqual(result["final_net_x_steps"], 0)
        self.assertEqual(result["final_net_z_steps"], 0)
        self.assertEqual(result["final_net_arm_steps"], 0)
        self.assertEqual(result["final_rpm"], 0)
        self.assertFalse(result["disk_angular_origin_claimed"])
        self.assertEqual(records[-1]["status"], "completed")

    def test_component_failure_cancels_without_return_or_homing(self) -> None:
        order: list[str] = []
        arm = FakeArmController(order)
        commands = {"count": 0}
        records: list[dict] = []
        stops: list[str] = []

        def failing_move(*, x_steps, z_steps, abort_event):
            commands["count"] += 1
            if commands["count"] == 2:
                raise RuntimeError("diamond ACK failed")
            return {"x_ack": "ACK", "z_ack": "ACK"}

        with self.assertRaisesRegex(RuntimeError, "diamond ACK failed"):
            execute_rinse(
                run_dir=".",
                label="Rinse",
                settings=settings(cycles=3),
                position_state={"x": 0, "z": 0},
                controller=arm,
                move_fn=failing_move,
                send_rpm_fn=lambda _rpm: "ACK",
                stop_rde_fn=lambda _error: stops.append("rpm"),
                emergency_stop_motion_fn=lambda: stops.append("xz"),
                emergency_stop_rotation_fn=lambda: stops.append("arm"),
                external_abort_event=threading.Event(),
                record_fn=lambda _run_dir, record: records.append(dict(record)) or record,
                log_fn=lambda _run_dir, _message: None,
            )

        self.assertEqual(stops[:3], ["rpm", "xz", "arm"])
        self.assertEqual(commands["count"], 2)
        self.assertTrue(arm.marked_uncertain)
        self.assertEqual(get_axis_position_confidence("horizontal"), "uncertain")
        self.assertEqual(get_axis_position_confidence("linear"), "uncertain")
        self.assertEqual(records[-1]["status"], "failed")
        self.assertFalse(records[-1]["automatic_recovery_attempted"])
        self.assertFalse(records[-1]["homing_attempted"])
        self.assertFalse(records[-1]["legacy_rotation_zero_command_sent"])

    def test_missing_rpm_ack_still_sends_an_immediate_disk_stop(self) -> None:
        order: list[str] = []
        arm = FakeArmController(order)
        stops: list[str] = []

        def fail_rpm(_rpm: int) -> str:
            raise RuntimeError("missing RPM ACK")

        with self.assertRaisesRegex(RuntimeError, "missing RPM ACK"):
            execute_rinse(
                run_dir=".",
                label="Rinse",
                settings=settings(cycles=1),
                position_state={"x": 0, "z": 0},
                controller=arm,
                send_rpm_fn=fail_rpm,
                stop_rde_fn=lambda _error: stops.append("rpm"),
                emergency_stop_motion_fn=lambda: stops.append("xz"),
                emergency_stop_rotation_fn=lambda: stops.append("arm"),
                external_abort_event=threading.Event(),
                record_fn=lambda _run_dir, record: record,
                log_fn=lambda _run_dir, _message: None,
            )

        self.assertEqual(stops, ["rpm", "xz", "arm"])
        self.assertEqual(arm.commands, [])

    def test_group_runner_delegates_one_whole_packaged_step(self) -> None:
        step = {
            "action": "rinse",
            "name": "Concurrent rinse",
            "enabled": True,
            **settings(cycles=2),
        }
        group = {
            "group_id": "cleaning",
            "label": "Cleaning",
            "enabled": True,
            "steps": [step],
        }
        position_state = {"x": 10, "z": 20}
        calls: list[dict] = []

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "workflow.recipe_runner.create_sample_workspace",
                    return_value=Path("."),
                )
            )
            stack.enter_context(patch("workflow.recipe_runner.append_log"))
            stack.enter_context(
                patch("workflow.recipe_runner.set_automation_state")
            )
            stack.enter_context(patch("workflow.recipe_runner.check_abort"))
            stack.enter_context(
                patch(
                    "workflow.recipe_runner.execute_rinse",
                    side_effect=lambda **kwargs: calls.append(kwargs),
                )
            )
            run_group(
                run_dir=Path("."),
                group=group,
                group_index=1,
                repetition=1,
                repetitions=1,
                position_state=position_state,
            )

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["settings"], step)
        self.assertIs(calls[0]["position_state"], position_state)


if __name__ == "__main__":
    unittest.main()
