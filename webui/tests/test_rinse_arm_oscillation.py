from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from hardware.rotation_controller import RotationMoveInterrupted, RotationMoveResult
from workflow.rinse_arm_oscillation import execute_rinse_arm_oscillation


def completed_result(steps: int) -> RotationMoveResult:
    return RotationMoveResult(
        requested_steps=steps,
        executed_steps=steps,
        requested_angle_deg=steps * 0.225,
        executed_angle_deg=steps * 0.225,
        direction="CCW" if steps > 0 else "CW",
        status="completed",
        raw_response=(
            f"ACK REL requested={steps} executed={steps} "
            f"direction={'CCW' if steps > 0 else 'CW'}"
        ),
        angle_confidence="tracked",
    )


class FakeRotationController:
    def __init__(self, failure: Exception | None = None) -> None:
        self.offset = 0
        self.confidence = "tracked"
        self.commands: list[int] = []
        self.failure = failure

    def expected_relative_state(self) -> dict[str, int | str]:
        return {
            "expected_offset_steps": self.offset,
            "angle_confidence": self.confidence,
        }

    def max_relative_steps(self) -> int:
        return 44

    def relative_steps(self, steps: int) -> RotationMoveResult:
        self.commands.append(steps)
        if self.failure is not None and len(self.commands) == 2:
            self.confidence = "uncertain"
            raise self.failure
        result = completed_result(steps)
        self.offset += steps
        return result


class RinseArmExecutorTests(unittest.TestCase):
    def test_success_runs_only_symmetric_relative_commands_and_logs_result(self) -> None:
        controller = FakeRotationController()
        records: list[dict] = []
        pauses: list[float] = []
        with patch(
            "workflow.rinse_arm_oscillation.check_abort"
        ):
            result = execute_rinse_arm_oscillation(
                run_dir=Path("."),
                label="Sample 1/Rinse",
                amplitude_deg=2,
                amplitude_steps=9,
                cycles=1,
                pause_between_moves_s=0.2,
                controller=controller,
                pause_fn=pauses.append,
                record_fn=lambda _run_dir, record: records.append(dict(record)) or record,
                log_fn=lambda _run_dir, _message: None,
            )

        self.assertEqual(controller.commands, [9, -18, 9])
        self.assertNotIn(0, controller.commands)
        self.assertEqual(pauses, [0.2, 0.2])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["net_commanded_steps"], 0)
        self.assertEqual(result["ending_expected_offset_steps"], 0)
        self.assertFalse(result["automatic_recovery_attempted"])
        self.assertEqual(records[-1]["status"], "completed")

    def test_failure_stops_immediately_without_reverse_or_home(self) -> None:
        controller = FakeRotationController(failure=RuntimeError("malformed ACK"))
        records: list[dict] = []
        with patch(
            "workflow.rinse_arm_oscillation.check_abort"
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed ACK"):
                execute_rinse_arm_oscillation(
                    run_dir=Path("."),
                    label="Rinse",
                    amplitude_deg=2,
                    amplitude_steps=9,
                    cycles=3,
                    pause_between_moves_s=0,
                    controller=controller,
                    record_fn=lambda _run_dir, record: records.append(dict(record)) or record,
                    log_fn=lambda _run_dir, _message: None,
                )

        self.assertEqual(controller.commands, [9, -18])
        self.assertNotIn(0, controller.commands)
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["angle_confidence"], "uncertain")
        self.assertFalse(records[-1]["automatic_recovery_attempted"])

    def test_partial_stop_is_recorded_and_no_more_commands_are_sent(self) -> None:
        partial = completed_result(-18)
        partial = RotationMoveResult(
            **{
                **partial.__dict__,
                "executed_steps": -4,
                "executed_angle_deg": -0.9,
                "status": "aborted",
                "raw_response": "ACK STOP REL requested=-18 executed=-4 direction=CW",
                "angle_confidence": "uncertain",
            }
        )
        controller = FakeRotationController(
            failure=RotationMoveInterrupted("stopped", partial)
        )
        records: list[dict] = []
        with patch(
            "workflow.rinse_arm_oscillation.check_abort"
        ):
            with self.assertRaises(RotationMoveInterrupted):
                execute_rinse_arm_oscillation(
                    run_dir=Path("."),
                    label="Rinse",
                    amplitude_deg=2,
                    amplitude_steps=9,
                    cycles=1,
                    pause_between_moves_s=0,
                    controller=controller,
                    record_fn=lambda _run_dir, record: records.append(dict(record)) or record,
                    log_fn=lambda _run_dir, _message: None,
                )

        self.assertEqual(controller.commands, [9, -18])
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["segments"][-1]["executed_steps"], -4)
        self.assertFalse(records[-1]["automatic_recovery_attempted"])

    def test_uncertain_start_refuses_to_send_any_command(self) -> None:
        controller = FakeRotationController()
        controller.confidence = "uncertain"
        records: list[dict] = []
        with self.assertRaisesRegex(RuntimeError, "no rinse-arm commands were sent"):
            execute_rinse_arm_oscillation(
                run_dir=Path("."),
                label="Rinse",
                amplitude_deg=2,
                amplitude_steps=9,
                cycles=1,
                pause_between_moves_s=0,
                controller=controller,
                record_fn=lambda _run_dir, record: records.append(dict(record)) or record,
                log_fn=lambda _run_dir, _message: None,
            )

        self.assertEqual(controller.commands, [])
        self.assertEqual(records[-1]["status"], "failed")

    def test_confidence_change_during_pause_prevents_next_segment(self) -> None:
        controller = FakeRotationController()
        records: list[dict] = []

        def interrupt_during_pause(_seconds: float) -> None:
            controller.confidence = "uncertain"

        with patch("workflow.rinse_arm_oscillation.check_abort"):
            with self.assertRaisesRegex(RuntimeError, "no further"):
                execute_rinse_arm_oscillation(
                    run_dir=Path("."),
                    label="Rinse",
                    amplitude_deg=2,
                    amplitude_steps=9,
                    cycles=1,
                    pause_between_moves_s=0.2,
                    controller=controller,
                    pause_fn=interrupt_during_pause,
                    record_fn=lambda _run_dir, record: records.append(dict(record)) or record,
                    log_fn=lambda _run_dir, _message: None,
                )

        self.assertEqual(controller.commands, [9])
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["angle_confidence"], "uncertain")


if __name__ == "__main__":
    unittest.main()
