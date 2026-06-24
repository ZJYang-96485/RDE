from __future__ import annotations

import threading
import time
from typing import Any

try:
    import serial
except ImportError:
    serial = None


class SerialConnectionError(RuntimeError):
    pass


class SerialDevice:
    def __init__(
        self,
        name: str,
        port: str,
        baud_rate: int,
        timeout_s: float = 0.4,
        write_timeout_s: float = 1.0,
        startup_delay_s: float = 2.0,
    ) -> None:
        self.name = name
        self.port = port
        self.baud_rate = int(baud_rate)
        self.timeout_s = float(timeout_s)
        self.write_timeout_s = float(write_timeout_s)
        self.startup_delay_s = float(startup_delay_s)
        self.conn: Any = None
        self.lock = threading.Lock()

    def ensure_available(self) -> None:
        if serial is None:
            raise SerialConnectionError("pyserial is not installed. Run: pip install -r requirements.txt")

    def is_open(self) -> bool:
        return bool(self.conn and self.conn.is_open)

    def connect(self) -> None:
        self.ensure_available()

        if self.is_open():
            return

        self.conn = serial.Serial(
            self.port,
            self.baud_rate,
            timeout=self.timeout_s,
            write_timeout=self.write_timeout_s,
        )

        time.sleep(self.startup_delay_s)

        try:
            self.conn.reset_input_buffer()
            self.conn.reset_output_buffer()
        except Exception:
            pass

    def close(self) -> None:
        if self.conn and self.conn.is_open:
            self.conn.close()

        self.conn = None

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def write_line(self, text: str) -> None:
        self.connect()

        payload = f"{text}\n".encode("ascii")
        self.conn.write(payload)
        self.conn.flush()

    def read_line(self) -> str:
        self.connect()

        return self.conn.readline().decode("utf-8", errors="replace").strip()

    def send_line(self, text: str) -> None:
        with self.lock:
            try:
                self.write_line(text)
            except Exception:
                self.reconnect()
                self.write_line(text)

    def send_line_read_first_response(self, text: str, attempts: int = 4) -> str | None:
        with self.lock:
            try:
                self.write_line(text)
            except Exception:
                self.reconnect()
                self.write_line(text)

            response = None

            for _ in range(attempts):
                line = self.read_line()
                if line:
                    response = line
                    break

            return response

    def send_line_wait_for_ack(
        self,
        text: str,
        timeout_s: float,
        abort_event: threading.Event | None = None,
    ) -> str:
        with self.lock:
            try:
                self.write_line(text)
            except Exception:
                self.reconnect()
                self.write_line(text)

            return self.wait_for_ack(timeout_s, abort_event=abort_event)

    def wait_for_ack(
        self,
        timeout_s: float,
        abort_event: threading.Event | None = None,
    ) -> str:
        deadline = time.monotonic() + float(timeout_s)
        last_line = None

        while time.monotonic() < deadline:
            if abort_event is not None and abort_event.is_set():
                raise SerialConnectionError(f"{self.name}: abort requested while waiting for ACK.")

            line = self.read_line()

            if not line:
                continue

            last_line = line

            if line.startswith("ACK"):
                return line

            if line.startswith("ERR"):
                raise SerialConnectionError(f"{self.name} reported error: {line}")

        detail = f" Last line from board: {last_line}" if last_line else ""
        raise SerialConnectionError(f"Timeout waiting for ACK from {self.name} on {self.port}.{detail}")


def make_serial_device(
    name: str,
    port: str,
    baud_rate: int,
    timeout_s: float = 0.4,
    write_timeout_s: float = 1.0,
    startup_delay_s: float = 2.0,
) -> SerialDevice:
    return SerialDevice(
        name=name,
        port=port,
        baud_rate=baud_rate,
        timeout_s=timeout_s,
        write_timeout_s=write_timeout_s,
        startup_delay_s=startup_delay_s,
    )