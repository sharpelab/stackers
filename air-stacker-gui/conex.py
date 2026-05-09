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


# 16-bit positioner error register from the TS response, decoded per the
# CONEX-CC Controller Documentation (manuals/newport-conex-cc-controller.pdf §TS).
# Bits A-F are documented as "Not used"; if any are set we surface as reserved.
ERROR_BITS: list[tuple[int, str]] = [
    (0x0001, "negative end of run"),
    (0x0002, "positive end of run"),
    (0x0004, "peak current limit"),
    (0x0008, "RMS current limit"),
    (0x0010, "short circuit"),
    (0x0020, "following error"),
    (0x0040, "homing timeout"),
    (0x0080, "wrong ESP stage"),
    (0x0100, "DC voltage too low"),
    (0x0200, "80W output power exceeded"),
]


def error_label(code: str) -> str:
    """Decode the 4-hex-char positioner error register from a TS response."""
    try:
        bits = int(code, 16)
    except ValueError:
        return f"invalid ({code})"
    if bits == 0:
        return "no error"
    names = [name for mask, name in ERROR_BITS if bits & mask]
    reserved = bits & 0xFC00
    if reserved:
        names.append(f"reserved=0x{reserved:04x}")
    return ", ".join(names) if names else f"reserved=0x{bits:04x}"


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
        """Returns (controller_state_2hex, error_register_4hex).

        TS response is `1TSabcdef`: abcd is the 16-bit positioner error
        register, ef is the controller state. See CONEX-CC controller doc §TS.
        """
        raw = self.query("TS?")
        if len(raw) < 6:
            raise ConexError(f"unexpected TS response: {raw!r}")
        return raw[4:6], raw[:4]

    def negative_limit(self) -> float:
        return float(self.query("SL?"))

    def positive_limit(self) -> float:
        return float(self.query("SR?"))

    def velocity(self) -> float:
        return float(self.query("VA?"))

    # --- transient setters ---
    def set_velocity(self, v: float) -> None:
        # VA without a PW1/PW0 wrap is RAM-only — does not touch flash.
        self.send(f"VA{v}")

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
