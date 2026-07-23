from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass

from hardware.serial_base import SerialDevice
from workflow.config_loader import (
    get_baud_rate,
    get_rotation_config,
    get_serial_port,
    load_config,
)
from workflow.state import AutomationAbortRequested


class RotationControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RotationMoveResult:
    requested_steps: int
    executed_steps: int
    requested_angle_deg: float
    executed_angle_deg: float
    direction: str
    status: str
    raw_response: str
    angle_confidence: str


class RotationMoveInterrupted(AutomationAbortRequested):
    def __init__(self, message: str, result: RotationMoveResult) -> None:
        super().__init__(message)
        self.result = result


_RELATIVE_ACK_PATTERN = re.compile(
    r"^ACK REL requested=(?P<requested>[+-]?\d+) "
    r"executed=(?P<executed>[+-]?\d+) direction=(?P<direction>CCW|CW)$"
)
_RELATIVE_STOP_PATTERN = re.compile(
    r"^ACK STOP REL requested=(?P<requested>[+-]?\d+) "
    r"executed=(?P<executed>[+-]?\d+) direction=(?P<direction>CCW|CW)$"
)
_SIGNED_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")


def degrees_per_step(
    motor_full_steps_per_rev: int,
    microstep: int,
) -> float:
    motor_steps = int(motor_full_steps_per_rev)
    microsteps = int(microstep)
    if motor_steps <= 0 or microsteps <= 0:
        raise RotationControllerError(
            "motor_full_steps_per_rev and microstep must both be positive."
        )
    return 360.0 / float(motor_steps * microsteps)


def angle_to_steps(
    angle_deg: float,
    *,
    motor_full_steps_per_rev: int,
    microstep: int,
) -> int:
    try:
        angle = float(angle_deg)
    except (TypeError, ValueError) as exc:
        raise RotationControllerError("relative angle must be a finite number.") from exc

    if not math.isfinite(angle):
        raise RotationControllerError("relative angle must be a finite number.")

    step_angle = degrees_per_step(motor_full_steps_per_rev, microstep)
    return int(round(angle / step_angle))


def parse_relative_ack(
    raw_response: str,
    *,
    expected_requested_steps: int,
    step_angle_deg: float,
    requested_angle_deg: float | None = None,
) -> RotationMoveResult:
    response = str(raw_response or "").strip()
    match = _RELATIVE_ACK_PATTERN.fullmatch(response)
    stopped = False

    if match is None:
        match = _RELATIVE_STOP_PATTERN.fullmatch(response)
        stopped = match is not None

    if match is None:
        raise RotationControllerError(
            f"Malformed relative-rotation acknowledgement: {response or 'empty response'}"
        )

    requested = int(match.group("requested"))
    executed = int(match.group("executed"))
    direction = match.group("direction")
    expected = int(expected_requested_steps)
    expected_direction = "CCW" if expected > 0 else "CW"

    if requested != expected:
        raise RotationControllerError(
            f"Relative-rotation ACK requested {requested}, expected {expected}."
        )
    if direction != expected_direction:
        raise RotationControllerError(
            f"Relative-rotation ACK direction {direction}, expected {expected_direction}."
        )
    if executed != 0 and (executed > 0) != (expected > 0):
        raise RotationControllerError(
            "Relative-rotation ACK executed-step sign does not match the request."
        )
    if abs(executed) > abs(expected):
        raise RotationControllerError(
            "Relative-rotation ACK executed more steps than requested."
        )
    if not stopped and executed != expected:
        raise RotationControllerError(
            f"Relative-rotation ACK executed {executed}, expected {expected}."
        )

    requested_angle = (
        float(requested_angle_deg)
        if requested_angle_deg is not None
        else float(expected) * float(step_angle_deg)
    )
    return RotationMoveResult(
        requested_steps=expected,
        executed_steps=executed,
        requested_angle_deg=requested_angle,
        executed_angle_deg=float(executed) * float(step_angle_deg),
        direction=direction,
        status="aborted" if stopped else "completed",
        raw_response=response,
        angle_confidence="uncertain" if stopped else "tracked",
    )


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
        self.relative_state_lock = threading.Lock()
        self.expected_offset_steps = 0
        self.angle_confidence = "tracked"

    def rotation_config(self) -> dict:
        return get_rotation_config()

    def home_command(self) -> str:
        return str(self.rotation_config().get("home_command", "0"))

    def ccw_command(self) -> str:
        return str(self.rotation_config().get("ccw_command", "1"))

    def motor_full_steps_per_rev(self) -> int:
        return int(self.rotation_config()["motor_full_steps_per_rev"])

    def microstep(self) -> int:
        return int(self.rotation_config()["microstep"])

    def max_relative_steps(self) -> int:
        return int(self.rotation_config()["max_relative_steps"])

    def degrees_per_step(self) -> float:
        return degrees_per_step(
            self.motor_full_steps_per_rev(),
            self.microstep(),
        )

    def expected_relative_state(self) -> dict[str, int | str]:
        with self.relative_state_lock:
            return {
                "expected_offset_steps": int(self.expected_offset_steps),
                "angle_confidence": str(self.angle_confidence),
            }

    def _mark_angle_uncertain(self) -> None:
        with self.relative_state_lock:
            self.angle_confidence = "uncertain"

    def mark_angle_uncertain(self) -> None:
        """Invalidate software-only angle tracking without moving hardware."""

        self._mark_angle_uncertain()

    def _record_completed_relative_move(self, result: RotationMoveResult) -> None:
        with self.relative_state_lock:
            self.expected_offset_steps += int(result.executed_steps)
            self.angle_confidence = "tracked"

    def _validate_relative_steps(self, steps: int) -> int:
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise RotationControllerError("relative steps must be an integer.")

        requested = int(steps)
        if requested == 0:
            raise RotationControllerError("relative steps cannot be zero.")
        if abs(requested) > self.max_relative_steps():
            raise RotationControllerError(
                f"relative steps cannot exceed +/-{self.max_relative_steps()}."
            )
        return requested

    def relative_steps(
        self,
        steps: int,
        *,
        requested_angle_deg: float | None = None,
    ) -> RotationMoveResult:
        requested = self._validate_relative_steps(steps)
        step_angle = self.degrees_per_step()

        if not self.command_lock.acquire(blocking=False):
            raise RotationControllerError(
                "another rotation command is still in progress; "
                "this command was rejected and was not queued"
            )

        command = f"REL {requested}"
        try:
            try:
                response = self.device.send_line_wait_for_response(
                    command,
                    timeout_s=self.completion_timeout_s,
                    expected_prefixes=("ACK REL ", "ACK STOP REL "),
                )
                result = parse_relative_ack(
                    response,
                    expected_requested_steps=requested,
                    step_angle_deg=step_angle,
                    requested_angle_deg=requested_angle_deg,
                )
            except RotationMoveInterrupted:
                raise
            except Exception as exc:
                self._mark_angle_uncertain()
                self.device.close()
                if isinstance(exc, RotationControllerError):
                    raise
                raise RotationControllerError(
                    f"Unable to send relative rotation command '{command}': {exc}"
                ) from exc

            if result.status != "completed":
                self._mark_angle_uncertain()
                self.device.close()
                raise RotationMoveInterrupted(
                    (
                        f"Rotation arm relative movement was interrupted: "
                        f"{result.raw_response}"
                    ),
                    result,
                )

            self._record_completed_relative_move(result)
            return result
        finally:
            self.command_lock.release()

    def relative_angle(self, angle_deg: float) -> RotationMoveResult:
        try:
            requested_angle = float(angle_deg)
        except (TypeError, ValueError) as exc:
            raise RotationControllerError("relative angle must be a finite number.") from exc
        if not math.isfinite(requested_angle):
            raise RotationControllerError("relative angle must be a finite number.")

        steps = angle_to_steps(
            requested_angle,
            motor_full_steps_per_rev=self.motor_full_steps_per_rev(),
            microstep=self.microstep(),
        )
        if steps == 0:
            raise RotationControllerError(
                "relative angle rounds to zero motor steps."
            )
        return self.relative_steps(
            steps,
            requested_angle_deg=requested_angle,
        )

    def send_text(self, command: str) -> str | None:
        command = str(command).strip()

        if not command:
            raise RotationControllerError("rotation command cannot be empty.")

        if command.upper() == "REL" or command.upper().startswith("REL "):
            parts = command.split()
            if (
                len(parts) != 2
                or parts[0].upper() != "REL"
                or _SIGNED_INTEGER_PATTERN.fullmatch(parts[1]) is None
            ):
                raise RotationControllerError(
                    "relative rotation command must be REL <signed integer>."
                )
            return self.relative_steps(int(parts[1])).raw_response

        expected_prefixes: tuple[str, ...] = ()
        if command == self.ccw_command():
            expected_prefixes = (
                "Moved 180 deg CCW",
                "Already at 180 deg CCW position",
                "ACK STOP",
                f"ACK DONE {command}",
                f"ACK MOCK Rotation {command}",
            )
        elif command == self.home_command():
            expected_prefixes = (
                "Returned to home",
                "Already at home",
                "ACK STOP",
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
                response = self.device.send_line_wait_for_response(
                    command,
                    timeout_s=self.completion_timeout_s,
                    expected_prefixes=expected_prefixes,
                )
                if response.startswith("ACK STOP"):
                    self._mark_angle_uncertain()
                    raise AutomationAbortRequested(
                        f"Rotation arm stopped during command '{command}': {response}"
                    )
                return response
            except AutomationAbortRequested:
                self.device.close()
                raise
            except Exception as exc:
                self._mark_angle_uncertain()
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

    def emergency_stop(self) -> bool:
        sent = self.device.send_emergency_line_if_open("STOP")
        self._mark_angle_uncertain()
        return sent

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


def rotation_relative_steps(steps: int) -> RotationMoveResult:
    return get_rotation_controller().relative_steps(steps)


def rotation_relative_angle(angle_deg: float) -> RotationMoveResult:
    return get_rotation_controller().relative_angle(angle_deg)


def emergency_stop_rotation() -> bool:
    return get_rotation_controller().emergency_stop()
