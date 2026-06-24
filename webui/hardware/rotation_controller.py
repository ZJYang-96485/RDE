from __future__ import annotations

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

        try:
            return self.device.send_line_read_first_response(command, attempts=4)
        except Exception as exc:
            raise RotationControllerError(f"Unable to send rotation command '{command}': {exc}") from exc

    def send_command(self, value: int | str) -> str | None:
        return self.send_text(str(value))

    def home(self) -> str | None:
        return self.send_text(self.home_command())

    def ccw(self) -> str | None:
        return self.send_text(self.ccw_command())

    def close(self) -> None:
        self.device.close()


_default_rotation_controller: RotationController | None = None


def get_rotation_controller() -> RotationController:
    global _default_rotation_controller

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