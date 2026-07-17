from __future__ import annotations

import threading
import time
import unittest
from typing import Callable

from hardware.motion_controller import MotionController
from workflow.state import reset_axis_positions


class CoordinatedFakeDevice:
    def __init__(
        self,
        all_devices_ready: Callable[[], bool],
        connect_delay_s: float = 0.0,
    ) -> None:
        self.all_devices_ready = all_devices_ready
        self.connect_delay_s = float(connect_delay_s)
        self.ready = False
        self.command: str | None = None

    def connect(self) -> None:
        time.sleep(self.connect_delay_s)
        self.ready = True

    def close(self) -> None:
        self.ready = False

    def send_line_wait_for_ack(self, text, timeout_s, abort_event=None) -> str:
        if not self.ready:
            self.connect()

        if not self.all_devices_ready():
            raise AssertionError("movement started before both axis devices were ready")

        self.command = str(text)
        return f"ACK {text}"

    def send_emergency_line_if_open(self, text="STOP") -> bool:
        return self.ready


class MotionControllerParallelTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_axis_positions()

    def tearDown(self) -> None:
        reset_axis_positions()

    def test_parallel_move_prepares_both_devices_before_start_barrier(self) -> None:
        controller = MotionController()
        devices: dict[str, CoordinatedFakeDevice] = {}

        def all_devices_ready() -> bool:
            return all(device.ready for device in devices.values())

        # The unequal delays model two USB controllers that take different
        # amounts of time to open after a power cycle.
        devices["horizontal"] = CoordinatedFakeDevice(
            all_devices_ready,
            connect_delay_s=0.03,
        )
        devices["linear"] = CoordinatedFakeDevice(all_devices_ready)
        controller.devices["horizontal"] = devices["horizontal"]
        controller.devices["linear"] = devices["linear"]

        result = controller.move_xz_steps_parallel(
            x_steps=1000,
            z_steps=700,
            abort_event=threading.Event(),
        )

        self.assertEqual(result, {"x_ack": "ACK 1000", "z_ack": "ACK 700"})
        self.assertEqual(devices["horizontal"].command, "1000")
        self.assertEqual(devices["linear"].command, "700")


if __name__ == "__main__":
    unittest.main()
