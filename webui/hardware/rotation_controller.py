from __future__ import annotations

import threading

from hardware.serial_base import SerialDevice
from workflow.config_loader import get_baud_rate, get_serial_port, load_config


class RotationControllerError(RuntimeError):
    pass


class RotationController:
    def __init__(self) -> None:
        config = load_config()
        timeouts = config["serial"]["timeouts"]

        self.device = SerialDevice(
            name="Rotation",
            port=get_serial_port("rotation"),
            baud_rate=get_baud_rate(),
            timeout_s=float(timeouts.get("rotation_s", 0.4)),
            write_timeout_s=float(timeouts.get("write_s", 1.0)),
            startup_delay_s=float(timeouts.get("startup_delay_s", 2.0)),
        )
        self.completion_timeout_s = float(timeouts.get("rotation_ack_s", 10.0))
        # Reject concurrent callers instead of letting Flask threads queue
        # commands that could move the stage much later.
        self.command_lock = threading.Lock()

    def rotation_config(self) -> dict:
        return load_config()["rotation"]

    def home_command(self) -> str:
        return str(self.rotation_config().get("home_command", "0"))

    def ccw_command(self) -> str:
        return str(self.rotation_config().get("ccw_command", "1"))

    def send_text(self, command: str) -> str | None:
        command = str(command).strip()

        if not command:
            raise RotationControllerError("rotation command cannot be empty.")

        expected_prefixes: tuple[str, ...] = ()
        if command == self.ccw_command():
            expected_prefixes = (
                "Moved 180 deg CCW",
                "Already at 180 deg CCW position",
                f"ACK DONE {command}",
                f"ACK MOCK Rotation {command}",
            )
        elif command == self.home_command():
            expected_prefixes = (
                "Returned to home",
                "Already at home",
                f"ACK DONE {command}",
                f"ACK MOCK Rotation {command}",
            )

        if not self.command_lock.acquire(blocking=False):
            raise RotationControllerError(
                "another rotation command is still in progress; "
                "this command was rejected and was not queued"
            )

        try:
            try:
                return self.device.send_line_wait_for_response(
                    command,
                    timeout_s=self.completion_timeout_s,
                    expected_prefixes=expected_prefixes,
                )
            except Exception as exc:
                # A failed transaction must not keep a serial connection with
                # stale input/output around for the next request.
                self.device.close()
                raise RotationControllerError(
                    f"Unable to send rotation command '{command}': {exc}"
                ) from exc
        finally:
            self.command_lock.release()

    def send_command(self, value: int | str) -> str | None:
        return self.send_text(str(value))

    def home(self) -> str | None:
        return self.send_text(self.home_command())

    def ccw(self) -> str | None:
        return self.send_text(self.ccw_command())

    def close(self) -> None:
        self.device.close()


_default_rotation_controller: RotationController | None = None
_default_rotation_controller_lock = threading.Lock()


def get_rotation_controller() -> RotationController:
    global _default_rotation_controller

    if _default_rotation_controller is None:
        with _default_rotation_controller_lock:
            if _default_rotation_controller is None:
                _default_rotation_controller = RotationController()

    return _default_rotation_controller


def send_rotation_text(command: str) -> str | None:
    return get_rotation_controller().send_text(command)


def send_rotation_command(value: int | str) -> str | None:
    return get_rotation_controller().send_command(value)


def rotation_home() -> str | None:
    return get_rotation_controller().home()


def rotation_ccw() -> str | None:
    return get_rotation_controller().ccw()
