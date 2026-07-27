from __future__ import annotations

import unittest
from unittest.mock import patch

from hardware.rde_controller import RDEController


class RDEControllerTest(unittest.TestCase):
    def test_first_set_rpm_starts_with_valid_pwm_before_enable(self) -> None:
        controller = RDEController()

        with patch.object(
            controller.device,
            "send_line_wait_for_response",
            return_value="ACK STARTED RPM 1600 DUTY 52/255 ENABLE 1 STOP 0",
        ) as send:
            response = controller.set_rpm(1600)

        self.assertEqual(
            response,
            "ACK STARTED RPM 1600 DUTY 52/255 ENABLE 1 STOP 0",
        )
        self.assertEqual(send.call_args.args[0], "START 1600")
        self.assertIn("ACK STARTED RPM 1600", send.call_args.kwargs["expected_prefixes"])

    def test_subsequent_set_rpm_does_not_interrupt_drive_with_another_rearm(self) -> None:
        controller = RDEController()

        with patch.object(
            controller.device,
            "send_line_wait_for_response",
            side_effect=[
                "ACK STARTED RPM 900 DUTY 40/255 ENABLE 1 STOP 0",
                "ACK RPM 1600 DUTY 52/255 ENABLE 1 STOP 0",
            ],
        ) as send:
            controller.set_rpm(900)
            response = controller.set_rpm(1600)

        self.assertEqual(
            response,
            "ACK RPM 1600 DUTY 52/255 ENABLE 1 STOP 0",
        )
        self.assertEqual(
            [entry.args[0] for entry in send.call_args_list],
            ["START 900", "1600"],
        )

    def test_stop_command_forces_next_run_to_use_safe_start(self) -> None:
        controller = RDEController()
        controller._drive_armed = True

        with patch.object(
            controller.device,
            "send_line_wait_for_response",
            return_value="ACK RPM 20 DUTY 25/255 ENABLE 1 STOP 0",
        ) as send:
            controller.stop()

        self.assertEqual(send.call_args_list[0].args[0], "20")
        self.assertFalse(controller._drive_armed)

    def test_legacy_rpm_response_remains_compatible(self) -> None:
        controller = RDEController()
        controller._drive_armed = True

        with patch.object(
            controller.device,
            "send_line_wait_for_response",
            return_value="RPM: 1600 -> Duty: 52",
        ) as send:
            response = controller.set_rpm(1600)

        self.assertEqual(response, "RPM: 1600 -> Duty: 52")
        self.assertIn("RPM: 1600", send.call_args.kwargs["expected_prefixes"])


if __name__ == "__main__":
    unittest.main()
