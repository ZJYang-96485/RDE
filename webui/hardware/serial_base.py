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


class SerialAbortError(SerialConnectionError):
    """Raised after an active serial operation receives an abort request."""

    def __init__(self, message: str, response: str | None = None) -> None:
        super().__init__(message)
        self.response = response


class MockSerialConnection:
    def __init__(self, name: str, port: str) -> None:
        self.name = name
        self.port = port
        self.is_open = True
        self.responses: list[bytes] = []

    def close(self) -> None:
        self.is_open = False

    def reset_input_buffer(self) -> None:
        self.responses.clear()

    def reset_output_buffer(self) -> None:
        pass

    def write(self, payload: bytes) -> int:
        text = payload.decode("ascii", errors="replace").strip()

        if text.upper() in {"STOP", "ABORT", "CANCEL"}:
            response = f"ACK STOP MOCK {self.name}\n"
        else:
            response = f"ACK MOCK {self.name} {text}\n"

        self.responses.append(response.encode("utf-8"))
        return len(payload)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        if not self.responses:
            return b""
        return self.responses.pop(0)


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

        # Main lock serializes normal request/response transactions.
        self.lock = threading.Lock()

        # Separate write lock lets the HTTP abort route send STOP while the
        # motion thread is blocked waiting for the Arduino ACK.
        self.write_lock = threading.Lock()

        # Prevent duplicate STOP writes from the HTTP abort route and the
        # waiting motion thread. Duplicate STOP commands can leave a stale
        # "ACK STOP IDLE" in the serial buffer and corrupt the next move.
        self.stop_command_sent = threading.Event()

    def ensure_available(self) -> None:
        if serial is None:
            raise SerialConnectionError(
                "pyserial is not installed. Run: pip install -r requirements.txt"
            )

    def mock_serial_enabled(self) -> bool:
        try:
            from workflow.config_loader import load_config
        except ModuleNotFoundError:
            return False

        serial_config = load_config().get("serial", {})
        hardware_config = serial_config.get("hardware", {})

        if not isinstance(hardware_config, dict):
            return False

        return bool(hardware_config.get("mock_serial", False))

    def is_open(self) -> bool:
        return bool(self.conn and self.conn.is_open)

    def connect(self) -> None:
        if self.is_open():
            return

        if self.mock_serial_enabled():
            self.conn = MockSerialConnection(self.name, self.port)
            return

        self.ensure_available()

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

    def _write_payload(self, payload: bytes) -> None:
        self.connect()

        with self.write_lock:
            self.conn.write(payload)
            self.conn.flush()

    def write_line(self, text: str) -> None:
        self._write_payload(f"{text}\n".encode("ascii"))

    def read_line(self) -> str:
        self.connect()
        return self.conn.readline().decode("utf-8", errors="replace").strip()

    def send_emergency_line_if_open(self, text: str = "STOP") -> bool:
        """
        Write an emergency command without waiting for the normal transaction
        lock. This is only used for STOP/ABORT while another thread is waiting
        for the movement ACK.
        """
        if not self.is_open():
            return False

        normalized = str(text).strip().upper()
        is_stop = normalized in {"STOP", "ABORT", "CANCEL"}

        if is_stop and self.stop_command_sent.is_set():
            return True

        payload = f"{text}\n".encode("ascii")

        try:
            if is_stop:
                self.stop_command_sent.set()

            with self.write_lock:
                self.conn.write(payload)
                self.conn.flush()

            return True
        except Exception:
            if is_stop:
                self.stop_command_sent.clear()
            return False

    def discard_stale_input(self) -> None:
        """
        Clear delayed ACK/boot text before a new request. In particular, this
        removes an idle STOP acknowledgement left after an emergency command.
        """
        self.connect()

        try:
            self.conn.reset_input_buffer()
        except Exception:
            while True:
                line = self.conn.readline()
                if not line:
                    break

    def send_line(self, text: str) -> None:
        with self.lock:
            try:
                self.write_line(text)
            except Exception:
                self.reconnect()
                self.write_line(text)

    def send_line_read_first_response(
        self,
        text: str,
        attempts: int = 4,
    ) -> str | None:
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

    def send_line_wait_for_response(
        self,
        text: str,
        timeout_s: float,
        expected_prefixes: tuple[str, ...] = (),
    ) -> str:
        """Send one command and wait for its matching completion response."""
        expected = tuple(str(prefix) for prefix in expected_prefixes if str(prefix))

        with self.lock:
            # Rotation responses can arrive after an older short timeout. Never
            # let one of those stale lines acknowledge the next command.
            self.discard_stale_input()

            try:
                self.write_line(text)
            except Exception:
                self.reconnect()
                self.discard_stale_input()
                self.write_line(text)

            deadline = time.monotonic() + float(timeout_s)
            received: list[str] = []

            while time.monotonic() < deadline:
                line = self.read_line()
                if not line:
                    continue

                received.append(line)

                if line.startswith("ERR"):
                    raise SerialConnectionError(
                        f"{self.name} reported error: {line}"
                    )

                if not expected or any(line.startswith(prefix) for prefix in expected):
                    return line

            detail = f" Received: {received}" if received else " No response was received."
            expectation = f" Expected one of: {expected}." if expected else ""
            raise SerialConnectionError(
                f"Timeout waiting for completion response from {self.name} on {self.port}."
                f"{expectation}{detail}"
            )

    def send_line_wait_for_ack(
        self,
        text: str,
        timeout_s: float,
        abort_event: threading.Event | None = None,
    ) -> str:
        # Never issue a new motion command after the abort flag is already set.
        if abort_event is not None and abort_event.is_set():
            raise SerialAbortError(
                f"{self.name}: abort was already requested before command send."
            )

        with self.lock:
            # A STOP sent while idle may have left an ACK STOP IDLE response.
            self.discard_stale_input()
            self.stop_command_sent.clear()

            # Check again after acquiring the transaction lock and after any
            # serial startup delay.
            if abort_event is not None and abort_event.is_set():
                raise SerialAbortError(
                    f"{self.name}: abort requested before command write."
                )

            try:
                self.write_line(text)
            except Exception:
                self.reconnect()
                self.discard_stale_input()

                if abort_event is not None and abort_event.is_set():
                    raise SerialAbortError(
                        f"{self.name}: abort requested before retry write."
                    )

                self.write_line(text)

            return self.wait_for_ack(timeout_s, abort_event=abort_event)

    def wait_for_ack(
        self,
        timeout_s: float,
        abort_event: threading.Event | None = None,
    ) -> str:
        deadline = time.monotonic() + float(timeout_s)
        last_line = None
        stop_sent = False
        stop_deadline: float | None = None

        while time.monotonic() < deadline:
            if abort_event is not None and abort_event.is_set() and not stop_sent:
                # The axis firmware must support STOP while moving.
                self.send_emergency_line_if_open("STOP")
                stop_sent = True
                stop_deadline = time.monotonic() + 5.0

            line = self.read_line()

            if line:
                last_line = line

                if line.startswith("ACK STOP"):
                    self.stop_command_sent.clear()
                    raise SerialAbortError(
                        f"{self.name}: movement stopped after abort request.",
                        response=line,
                    )

                if line.startswith("ACK"):
                    if abort_event is not None and abort_event.is_set():
                        # Movement may have completed at the same moment the
                        # abort was requested. Propagate abort before any next
                        # run-plan step can start.
                        raise SerialAbortError(
                            f"{self.name}: abort requested as movement completed.",
                            response=line,
                        )
                    self.stop_command_sent.clear()
                    return line

                if line.startswith("ERR"):
                    raise SerialConnectionError(
                        f"{self.name} reported error: {line}"
                    )

            if stop_sent and stop_deadline is not None:
                if time.monotonic() >= stop_deadline:
                    self.stop_command_sent.clear()
                    raise SerialAbortError(
                        f"{self.name}: STOP was sent but no stop ACK was received.",
                        response=last_line,
                    )

        self.stop_command_sent.clear()
        detail = f" Last line from board: {last_line}" if last_line else ""
        raise SerialConnectionError(
            f"Timeout waiting for ACK from {self.name} on {self.port}.{detail}"
        )


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
