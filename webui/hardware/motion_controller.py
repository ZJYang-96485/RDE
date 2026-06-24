from __future__ import annotations

import threading

from hardware.rotation_controller import rotation_home
from hardware.serial_base import SerialDevice
from workflow.config_loader import get_baud_rate, get_safe_z, get_serial_port, load_config, user_axis_to_internal_axis
from workflow.safety import axis_ack_timeout_seconds, validate_axis_move, validate_xyz_position
from workflow.state import (
    AutomationAbortRequested,
    add_axis_delta,
    get_abort_event,
    get_axis_position,
    get_axis_positions,
    reset_axis_positions,
)


class MotionControllerError(RuntimeError):
    pass


class MotionController:
    def __init__(self) -> None:
        config = load_config()
        timeouts = config["serial"]["timeouts"]

        self.devices = {
            "linear": SerialDevice(
                name="Linear/Z",
                port=get_serial_port("linear"),
                baud_rate=get_baud_rate(),
                timeout_s=float(timeouts.get("axis_s", 0.4)),
                write_timeout_s=float(timeouts.get("write_s", 1.0)),
                startup_delay_s=float(timeouts.get("startup_delay_s", 2.0)),
            ),
            "horizontal": SerialDevice(
                name="Horizontal/X",
                port=get_serial_port("horizontal"),
                baud_rate=get_baud_rate(),
                timeout_s=float(timeouts.get("axis_s", 0.4)),
                write_timeout_s=float(timeouts.get("write_s", 1.0)),
                startup_delay_s=float(timeouts.get("startup_delay_s", 2.0)),
            ),
            "vertical": SerialDevice(
                name="Vertical/Y",
                port=get_serial_port("vertical"),
                baud_rate=get_baud_rate(),
                timeout_s=float(timeouts.get("axis_s", 0.4)),
                write_timeout_s=float(timeouts.get("write_s", 1.0)),
                startup_delay_s=float(timeouts.get("startup_delay_s", 2.0)),
            ),
        }

    def device_for_axis(self, internal_axis: str) -> SerialDevice:
        if internal_axis not in self.devices:
            raise MotionControllerError(f"unknown motion axis: {internal_axis}")

        return self.devices[internal_axis]

    def move_axis_steps(
        self,
        internal_axis: str,
        steps: int,
        abort_event: threading.Event | None = None,
    ) -> str | None:
        steps = int(steps)

        if steps == 0:
            return None

        validate_axis_move(internal_axis, steps)

        if abort_event is None:
            abort_event = get_abort_event()

        timeout_s = axis_ack_timeout_seconds(steps)
        device = self.device_for_axis(internal_axis)

        try:
            ack = device.send_line_wait_for_ack(
                str(steps),
                timeout_s=timeout_s,
                abort_event=abort_event,
            )
        except Exception as exc:
            if abort_event is not None and abort_event.is_set():
                raise AutomationAbortRequested("Abort requested during axis movement.") from exc

            raise MotionControllerError(f"Unable to move {internal_axis} by {steps} steps: {exc}") from exc

        add_axis_delta(internal_axis, steps)
        return ack

    def move_user_axis_steps(
        self,
        user_axis: str,
        steps: int,
        abort_event: threading.Event | None = None,
    ) -> str | None:
        internal_axis = user_axis_to_internal_axis(user_axis)

        return self.move_axis_steps(
            internal_axis=internal_axis,
            steps=int(steps),
            abort_event=abort_event,
        )

    def move_linear_steps(
        self,
        steps: int,
        abort_event: threading.Event | None = None,
    ) -> str | None:
        return self.move_axis_steps("linear", steps, abort_event=abort_event)

    def move_horizontal_steps(
        self,
        steps: int,
        abort_event: threading.Event | None = None,
    ) -> str | None:
        return self.move_axis_steps("horizontal", steps, abort_event=abort_event)

    def move_vertical_steps(
        self,
        steps: int,
        abort_event: threading.Event | None = None,
    ) -> str | None:
        return self.move_axis_steps("vertical", steps, abort_event=abort_event)

    def move_to_safe_z(self, abort_event: threading.Event | None = None) -> str | None:
        safe_z = get_safe_z()
        current_z = get_axis_position("linear")
        delta = int(safe_z) - int(current_z)

        return self.move_linear_steps(delta, abort_event=abort_event)

    def move_to_xyz(
        self,
        x: int,
        y: int,
        z: int,
        abort_event: threading.Event | None = None,
    ) -> dict[str, str | None]:
        validate_xyz_position(int(x), int(y), int(z))

        if abort_event is None:
            abort_event = get_abort_event()

        result = {
            "safe_z_ack": None,
            "horizontal_ack": None,
            "vertical_ack": None,
            "linear_ack": None,
        }

        result["safe_z_ack"] = self.move_to_safe_z(abort_event=abort_event)

        positions = get_axis_positions()

        x_delta = int(x) - int(positions["horizontal"])
        y_delta = int(y) - int(positions["vertical"])

        if x_delta != 0:
            result["horizontal_ack"] = self.move_horizontal_steps(x_delta, abort_event=abort_event)

        if y_delta != 0:
            result["vertical_ack"] = self.move_vertical_steps(y_delta, abort_event=abort_event)

        current_z = get_axis_position("linear")
        z_delta = int(z) - int(current_z)

        if z_delta != 0:
            result["linear_ack"] = self.move_linear_steps(z_delta, abort_event=abort_event)

        return result

    def home_axes(
        self,
        abort_event: threading.Event | None = None,
    ) -> dict[str, int | str | None]:
        if abort_event is None:
            abort_event = get_abort_event()

        positions = get_axis_positions()

        linear_command = -int(positions["linear"])
        horizontal_command = -int(positions["horizontal"])
        vertical_command = -int(positions["vertical"])

        linear_ack = None
        horizontal_ack = None
        vertical_ack = None
        rotation_ack = None

        if linear_command != 0:
            linear_ack = self.move_linear_steps(linear_command, abort_event=abort_event)

        rotation_ack = rotation_home()

        if horizontal_command != 0:
            horizontal_ack = self.move_horizontal_steps(horizontal_command, abort_event=abort_event)

        if vertical_command != 0:
            vertical_ack = self.move_vertical_steps(vertical_command, abort_event=abort_event)

        reset_axis_positions()

        return {
            "linear_command": linear_command,
            "horizontal_command": horizontal_command,
            "vertical_command": vertical_command,
            "rotation_command": "0",
            "linear_ack": linear_ack,
            "horizontal_ack": horizontal_ack,
            "vertical_ack": vertical_ack,
            "rotation_ack": rotation_ack,
        }

    def close(self) -> None:
        for device in self.devices.values():
            device.close()


_default_motion_controller: MotionController | None = None


def get_motion_controller() -> MotionController:
    global _default_motion_controller

    if _default_motion_controller is None:
        _default_motion_controller = MotionController()

    return _default_motion_controller


def move_linear_steps(
    steps: int,
    abort_event: threading.Event | None = None,
) -> str | None:
    return get_motion_controller().move_linear_steps(steps, abort_event=abort_event)


def move_horizontal_steps(
    steps: int,
    abort_event: threading.Event | None = None,
) -> str | None:
    return get_motion_controller().move_horizontal_steps(steps, abort_event=abort_event)


def move_vertical_steps(
    steps: int,
    abort_event: threading.Event | None = None,
) -> str | None:
    return get_motion_controller().move_vertical_steps(steps, abort_event=abort_event)


def move_to_xyz(
    x: int,
    y: int,
    z: int,
    abort_event: threading.Event | None = None,
) -> dict[str, str | None]:
    return get_motion_controller().move_to_xyz(
        x=int(x),
        y=int(y),
        z=int(z),
        abort_event=abort_event,
    )


def home_axes_internal(
    abort_event: threading.Event | None = None,
) -> dict[str, int | str | None]:
    return get_motion_controller().home_axes(abort_event=abort_event)