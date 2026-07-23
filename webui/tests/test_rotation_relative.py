from __future__ import annotations

import unittest
from unittest.mock import patch

from hardware.rotation_controller import (
    RotationController,
    RotationControllerError,
    RotationMoveInterrupted,
    parse_relative_ack,
)
from hardware.serial_base import SerialConnectionError


class RelativeRotationTests(unittest.TestCase):
    def test_strict_complete_ack_is_parsed(self) -> None:
        result = parse_relative_ack(
            "ACK REL requested=9 executed=9 direction=CCW",
            expected_requested_steps=9,
            step_angle_deg=0.225,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.executed_steps, 9)
        self.assertAlmostEqual(result.executed_angle_deg, 2.025)

    def test_malformed_and_mismatched_acks_are_rejected(self) -> None:
        bad_responses = (
            "ACK REL 9",
            "ACK REL requested=9 executed=8 direction=CCW",
            "ACK REL requested=10 executed=10 direction=CCW",
            "ACK REL requested=9 executed=9 direction=CW",
        )
        for response in bad_responses:
            with self.subTest(response=response):
                with self.assertRaises(RotationControllerError):
                    parse_relative_ack(
                        response,
                        expected_requested_steps=9,
                        step_angle_deg=0.225,
                    )

    def test_controller_sends_exact_signed_relative_commands(self) -> None:
        controller = RotationController()
        responses = (
            "ACK REL requested=9 executed=9 direction=CCW",
            "ACK REL requested=-18 executed=-18 direction=CW",
        )
        with patch.object(
            controller.device,
            "send_line_wait_for_response",
            side_effect=responses,
        ) as send:
            controller.relative_steps(9)
            controller.relative_steps(-18)

        self.assertEqual(send.call_args_list[0].args[0], "REL 9")
        self.assertEqual(send.call_args_list[1].args[0], "REL -18")
        self.assertEqual(controller.expected_relative_state()["expected_offset_steps"], -9)

    def test_partial_stop_marks_angle_uncertain_and_is_not_counted_complete(self) -> None:
        controller = RotationController()
        with (
            patch.object(
                controller.device,
                "send_line_wait_for_response",
                return_value=(
                    "ACK STOP REL requested=44 executed=17 direction=CCW"
                ),
            ),
            patch.object(controller.device, "close") as close,
        ):
            with self.assertRaises(RotationMoveInterrupted) as captured:
                controller.relative_steps(44)

        self.assertEqual(captured.exception.result.executed_steps, 17)
        self.assertEqual(controller.expected_relative_state()["expected_offset_steps"], 0)
        self.assertEqual(controller.expected_relative_state()["angle_confidence"], "uncertain")
        close.assert_called_once_with()

    def test_timeout_closes_connection_and_marks_angle_uncertain(self) -> None:
        controller = RotationController()
        with (
            patch.object(
                controller.device,
                "send_line_wait_for_response",
                side_effect=SerialConnectionError("timeout"),
            ),
            patch.object(controller.device, "close") as close,
        ):
            with self.assertRaisesRegex(RotationControllerError, "timeout"):
                controller.relative_steps(9)

        self.assertEqual(controller.expected_relative_state()["angle_confidence"], "uncertain")
        diagnostic = controller.relative_diagnostic_state()
        self.assertEqual(diagnostic["last_relative_command"], "REL 9")
        self.assertEqual(diagnostic["last_relative_error"], "timeout")
        close.assert_called_once_with()

    def test_operator_inspection_reset_changes_software_state_without_serial_io(self) -> None:
        controller = RotationController()
        controller.expected_offset_steps = 9
        controller.mark_angle_uncertain("missing relative ACK")

        with (
            patch.object(controller.device, "connect") as connect,
            patch.object(controller.device, "write_line") as write_line,
        ):
            reset = controller.confirm_operator_inspection()

        self.assertEqual(reset["previous_relative_error"], "missing relative ACK")
        self.assertEqual(reset["angle_confidence"], "tracked")
        self.assertEqual(
            controller.relative_diagnostic_state()["expected_offset_steps"],
            0,
        )
        self.assertIsNone(
            controller.relative_diagnostic_state()["last_relative_error"]
        )
        connect.assert_not_called()
        write_line.assert_not_called()

    def test_firmware_capability_check_uses_help_without_changing_confidence(self) -> None:
        controller = RotationController()
        controller.mark_angle_uncertain("earlier relative timeout")

        with patch.object(
            controller.device,
            "send_line_wait_for_response",
            return_value=(
                "Rotation commands: 1, 0, REL <signed_steps>, STOP, PING, "
                "STATUS, HELP"
            ),
        ) as send:
            capability = controller.check_relative_firmware_support()

        self.assertTrue(capability["supported"])
        self.assertFalse(capability["motion_command_sent"])
        self.assertEqual(send.call_args.args[0], "HELP")
        self.assertEqual(
            controller.expected_relative_state()["angle_confidence"],
            "uncertain",
        )
        self.assertEqual(
            controller.relative_diagnostic_state()["last_relative_error"],
            "earlier relative timeout",
        )

    def test_relative_command_is_rejected_instead_of_queued(self) -> None:
        controller = RotationController()
        controller.command_lock.acquire()
        try:
            with self.assertRaisesRegex(RotationControllerError, "rejected and was not queued"):
                controller.relative_steps(9)
        finally:
            controller.command_lock.release()

    def test_relative_angle_rejects_nonfinite_and_zero_step_values(self) -> None:
        controller = RotationController()
        for value in (float("nan"), float("inf"), -float("inf"), 0.01):
            with self.subTest(value=value):
                with self.assertRaises(RotationControllerError):
                    controller.relative_angle(value)

    def test_emergency_stop_invalidates_angle_confidence(self) -> None:
        controller = RotationController()
        with patch.object(
            controller.device,
            "send_emergency_line_if_open",
            return_value=True,
        ):
            self.assertTrue(controller.emergency_stop())

        self.assertEqual(
            controller.expected_relative_state()["angle_confidence"],
            "uncertain",
        )


if __name__ == "__main__":
    unittest.main()
