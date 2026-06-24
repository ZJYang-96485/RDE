from __future__ import annotations

import threading
import time

from hardware.serial_base import SerialDevice
from workflow.config_loader import get_baud_rate, get_rde_limits, get_serial_port, get_timeout, load_config
from workflow.safety import validate_duration_seconds, validate_rpm
from workflow.state import AutomationAbortRequested, get_abort_event, start_rde_run, stop_rde_run


class RDEControllerError(RuntimeError):
    pass


class RDEController:
    def __init__(self) -> None:
        config = load_config()
        timeouts = config["serial"]["timeouts"]

        self.device = SerialDevice(
            name="RDE",
            port=get_serial_port("rde"),
            baud_rate=get_baud_rate(),
            timeout_s=float(timeouts.get("rde_s", 1.0)),
            write_timeout_s=float(timeouts.get("write_s", 1.0)),
            startup_delay_s=float(timeouts.get("startup_delay_s", 2.0)),
        )

    def limits(self) -> dict[str, int]:
        return get_rde_limits()

    def stop_rpm(self) -> int:
        return int(self.limits()["stop_rpm"])

    def send_raw_rpm(self, rpm: int) -> None:
        self.device.send_line(str(int(rpm)))

    def set_rpm(self, rpm: int) -> None:
        validate_rpm(int(rpm))
        self.send_raw_rpm(int(rpm))

    def stop(self, error: str | None = None) -> None:
        try:
            self.send_raw_rpm(self.stop_rpm())
            stop_rde_run(error)
        except Exception as exc:
            stop_rde_run(str(exc))
            raise RDEControllerError(f"Unable to stop RDE: {exc}") from exc

    def run_for_duration(
        self,
        rpm: int,
        duration_seconds: int | float,
        abort_event: threading.Event | None = None,
    ) -> None:
        validate_rpm(int(rpm))
        validate_duration_seconds(duration_seconds)

        if abort_event is None:
            abort_event = get_abort_event()

        duration_seconds = float(duration_seconds)

        try:
            self.set_rpm(int(rpm))
            start_rde_run(int(rpm), int(duration_seconds))

            deadline = time.monotonic() + duration_seconds
            poll_interval_s = float(load_config()["automation"].get("poll_interval_s", 0.1))

            while time.monotonic() < deadline:
                if abort_event.is_set():
                    raise AutomationAbortRequested("Abort requested during RDE run.")

                time.sleep(max(0.01, poll_interval_s))

            self.stop(None)

        except AutomationAbortRequested:
            try:
                self.stop("Automation aborted.")
            finally:
                raise

        except Exception as exc:
            try:
                self.stop(str(exc))
            finally:
                raise RDEControllerError(f"RDE run failed: {exc}") from exc

    def close(self) -> None:
        self.device.close()


_default_rde_controller: RDEController | None = None


def get_rde_controller() -> RDEController:
    global _default_rde_controller

    if _default_rde_controller is None:
        _default_rde_controller = RDEController()

    return _default_rde_controller


def send_rpm(rpm: int) -> None:
    get_rde_controller().set_rpm(int(rpm))


def stop_rde(error: str | None = None) -> None:
    get_rde_controller().stop(error)


def run_rpm_for_duration(
    rpm: int,
    duration_seconds: int | float,
    abort_event: threading.Event | None = None,
) -> None:
    get_rde_controller().run_for_duration(
        rpm=int(rpm),
        duration_seconds=duration_seconds,
        abort_event=abort_event,
    )