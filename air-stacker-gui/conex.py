"""Newport CONEX-CC ASCII driver over USB-CDC serial (one controller per port, address 1)."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import serial

DEFAULT_BAUD = 921600
DEFAULT_ADDRESS = 1
TERMINATOR = b"\r\n"

# Subset of the CONEX-CC state byte (first 4 hex chars of TS response) → human label.
STATE_LABELS: dict[str, str] = {
    "0A": "NOT REFERENCED (reset)",
    "0B": "NOT REFERENCED (homing)",
    "0C": "NOT REFERENCED (configuration)",
    "0D": "NOT REFERENCED (disable)",
    "0E": "NOT REFERENCED (ready)",
    "0F": "NOT REFERENCED (moving)",
    "10": "NOT REFERENCED (ESP stage err)",
    "11": "NOT REFERENCED (jogging)",
    "14": "CONFIGURATION",
    "1E": "HOMING (reset)",
    "1F": "HOMING (configuration)",
    "28": "MOVING",
    "32": "READY (from homing)",
    "33": "READY (from moving)",
    "34": "READY (from disable)",
    "35": "READY (from jogging)",
    "3C": "DISABLE (from ready)",
    "3D": "DISABLE (from moving)",
    "3E": "DISABLE (from jogging)",
    "46": "JOGGING (from ready)",
    "47": "JOGGING (from disable)",
}


def state_label(code: str) -> str:
    return STATE_LABELS.get(code.upper(), f"UNKNOWN ({code})")


class ConexError(RuntimeError):
    pass


@dataclass
class StageInfo:
    raw_id: str
    state_code: str
    error_code: str
    position: float


class ConexAxis:
    """A single CONEX-CC controller on its own COM port (address 1 by convention)."""

    def __init__(
        self,
        port: str,
        baud: int = DEFAULT_BAUD,
        address: int = DEFAULT_ADDRESS,
        timeout: float = 0.5,
    ) -> None:
        self.port = port
        self.baud = baud
        self.address = address
        self.timeout = timeout
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._serial is not None:
            return
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                finally:
                    self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def send(self, cmd: str) -> None:
        """Fire-and-forget command (no reply expected)."""
        if not self._serial:
            raise ConexError("serial port not open")
        line = f"{self.address}{cmd}".encode("ascii") + TERMINATOR
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(line)
            self._serial.flush()

    def query(self, cmd: str) -> str:
        """Send a query (e.g. 'TP?') and return the value portion of the response."""
        if not self._serial:
            raise ConexError("serial port not open")
        line = f"{self.address}{cmd}".encode("ascii") + TERMINATOR
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(line)
            self._serial.flush()
            raw = self._serial.read_until(TERMINATOR)
        text = raw.decode("ascii", errors="replace").strip()
        prefix = f"{self.address}{cmd.rstrip('?')}"
        if not text:
            raise ConexError(f"no response to {cmd!r}")
        if text.startswith(prefix):
            return text[len(prefix):]
        return text

    # --- queries (safe) ---
    def identify(self) -> str:
        return self.query("ID?")

    def position(self) -> float:
        return float(self.query("TP?"))

    def state(self) -> tuple[str, str]:
        """Returns (state_code_hex, error_code_hex)."""
        raw = self.query("TS?")
        if len(raw) < 6:
            raise ConexError(f"unexpected TS response: {raw!r}")
        return raw[:4][-2:], raw[4:6]

    def negative_limit(self) -> float:
        return float(self.query("SL?"))

    def positive_limit(self) -> float:
        return float(self.query("SR?"))

    # --- motion (do not call until intended) ---
    def move_absolute(self, target: float) -> None:
        self.send(f"PA{target}")

    def move_relative(self, delta: float) -> None:
        self.send(f"PR{delta}")

    def stop(self) -> None:
        self.send("ST")

    def home(self) -> None:
        self.send("OR")

    def enable(self) -> None:
        self.send("MM1")

    def disable(self) -> None:
        self.send("MM0")

    def reset(self) -> None:
        self.send("RS")
