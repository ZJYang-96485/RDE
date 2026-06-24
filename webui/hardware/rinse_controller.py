from __future__ import annotations

from typing import Any

from hardware.motion_controller import move_to_xyz
from hardware.rde_controller import run_rpm_for_duration, stop_rde
from hardware.rotation_controller import send_rotation_text
from workflow.config_loader import get_safe_z, load_config
from workflow.safety import validate_rpm, validate_xyz_position
from workflow.state import AutomationAbortRequested, get_abort_event


class RinseControllerError(RuntimeError):
    pass


class RinseController:
    def rinse_config(self) -> dict[str, Any]:
        config = load_config()
        rinse = config.get("rinse", {})

        if not isinstance(rinse, dict):
            raise RinseControllerError("rinse config must be an object.")

        return rinse

    def enabled(self) -> bool:
        return bool(self.rinse_config().get("enabled", False))

    def position(self) -> dict[str, int]:
        rinse = self.rinse_config()
        position = rinse.get("position", {})

        if not isinstance(position, dict):
            raise RinseControllerError("rinse.position must be an object.")

        x = int(position.get("x", 0))
        y = int(position.get("y", 0))
        z = int(position.get("z", 0))

        validate_xyz_position(x, y, z)

        return {
            "x": x,
            "y": y,
            "z": z
        }

    def rpm(self) -> int:
        rpm = int(self.rinse_config().get("rpm", 1000))
        validate_rpm(rpm)
        return rpm

    def duration_s(self) -> float:
        duration = float(self.rinse_config().get("duration_s", 10))

        if duration <= 0:
            raise RinseControllerError("rinse.duration_s must be > 0.")

        return duration

    def rotation_command(self) -> str:
        return str(self.rinse_config().get("rotation_command", "") or "").strip()

    def return_to_safe_z_after(self) -> bool:
        return bool(self.rinse_config().get("return_to_safe_z_after", True))

    def run_cycle(self) -> dict[str, Any]:
        if not self.enabled():
            return {
                "ok": True,
                "enabled": False,
                "message": "Rinse skipped."
            }

        position = self.position()
        rpm = self.rpm()
        duration_s = self.duration_s()
        rotation_command = self.rotation_command()
        rotation_ack = None

        try:
            move_to_xyz(
                x=position["x"],
                y=position["y"],
                z=position["z"],
                abort_event=get_abort_event(),
            )

            if rotation_command:
                rotation_ack = send_rotation_text(rotation_command)

            run_rpm_for_duration(
                rpm=rpm,
                duration_seconds=duration_s,
                abort_event=get_abort_event(),
            )

            if self.return_to_safe_z_after():
                move_to_xyz(
                    x=position["x"],
                    y=position["y"],
                    z=get_safe_z(),
                    abort_event=get_abort_event(),
                )

            return {
                "ok": True,
                "enabled": True,
                "position": position,
                "rpm": rpm,
                "duration_s": duration_s,
                "rotation_command": rotation_command,
                "rotation_ack": rotation_ack
            }

        except AutomationAbortRequested:
            try:
                stop_rde("Automation aborted during rinse.")
            finally:
                raise

        except Exception as exc:
            try:
                stop_rde(str(exc))
            except Exception:
                pass

            raise RinseControllerError(f"rinse cycle failed: {exc}") from exc


_default_rinse_controller: RinseController | None = None


def get_rinse_controller() -> RinseController:
    global _default_rinse_controller

    if _default_rinse_controller is None:
        _default_rinse_controller = RinseController()

    return _default_rinse_controller


def run_rinse_cycle() -> dict[str, Any]:
    return get_rinse_controller().run_cycle()