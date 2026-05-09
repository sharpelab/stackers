"""Newport SMC100CC ASCII driver over RS-232 (one controller per port, address 1).

Mirrors :mod:`conex.py` — same Newport ASCII command shape, same TS state /
error decoding (the bit assignments line up with the CONEX-CC documentation),
same query/response prefix-stripping rules. Differences from the CONEX-CC
driver:

- Default baud is 57600 (vs. 921600 on CONEX-CC).
- The CONEX-CC has its USB-CDC bridge built in; the SMC100 reaches the PC via
  RS-232 → USB-RS232 dongle, but the protocol on top is the same.
- ``open()`` validates the optional ``position_limits=`` kwarg lies inside the
  controller's ESP-loaded ``SL`` / ``SR`` envelope (parallel to
  :class:`yoko.Yoko7651`'s ``voltage_limits=`` sanity check).

Persistent / CONFIG-mode commands (``PW1`` / ``PW0`` / EEPROM saves) are
deliberately not exposed. Anyone needing those has to reach for ``send`` /
``query`` directly and explicitly authorize the action.

References:
- ``manuals/newport-smc100-user-manual.pdf`` §5 (state diagram, TS command).
- ``manuals/newport-smc100-command-interface.pdf`` (full command list).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import serial

DEFAULT_BAUD = 57600
DEFAULT_ADDRESS = 1
TERMINATOR = b"\r\n"

# Subset of the SMC100CC state byte (low 2 hex chars of the TS response) →
# human label. Verified against the user's manual §5.3 / TS command page.
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
    "1E": "HOMING (RS-232)",
    "1F": "HOMING (keypad)",
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


# 16-bit positioner error register from the TS response (high 4 hex chars).
# Bit assignments verified against the SMC100CC/PP user manual TS-command
# page. The manual's worked example `0x004C = Homing time out + RMS current
# limit + Peak current limit` only resolves with bit 6 = homing timeout, which
# pins down the rest of the table. Bits A–F (10–15) are documented as "Not
# used"; if any are set we surface as reserved.
ERROR_BITS: list[tuple[int, str]] = [
    (0x0001, "negative end of run"),
    (0x0002, "positive end of run"),
    (0x0004, "peak current limit"),
    (0x0008, "RMS current limit"),
    (0x0010, "short circuit detection"),
    (0x0020, "following error"),
    (0x0040, "homing timeout"),
    (0x0080, "wrong ESP stage"),
    (0x0100, "DC voltage too low"),
    (0x0200, "80 W output power exceeded"),
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


# State codes for which motion / home / enable commands are accepted. Mirrors
# the per-command Usage matrix in §5.5 of the user manual; consumers (e.g.
# the panel) use this to grey out buttons that would just bounce off the
# controller with an "Execution not allowed" error.
NOT_REFERENCED_STATES = frozenset({"0A", "0B", "0C", "0D", "0E", "0F", "10", "11"})
CONFIGURATION_STATES = frozenset({"14"})
HOMING_STATES = frozenset({"1E", "1F"})
MOVING_STATES = frozenset({"28"})
READY_STATES = frozenset({"32", "33", "34", "35"})
DISABLE_STATES = frozenset({"3C", "3D", "3E"})
JOGGING_STATES = frozenset({"46", "47"})


class SMC100Error(RuntimeError):
    pass


@dataclass
class StageInfo:
    raw_id: str
    state_code: str
    error_code: str
    position: float


class SMC100Axis:
    """A single SMC100CC controller on its own COM port (address 1 by default).

    ``position_limits=(lo, hi)`` is an optional software clamp on
    :meth:`move_absolute` and :meth:`move_relative` targets, parallel to
    :class:`yoko.Yoko7651`'s ``voltage_limits=`` kwarg. When set, ``open()``
    queries the controller's ESP-loaded ``SL`` / ``SR`` and verifies the
    supplied range lies inside that envelope, raising :class:`SMC100Error`
    otherwise. Targets are checked against the intersection of the kwarg and
    the controller's own SL/SR before being sent.
    """

    def __init__(
        self,
        port: str,
        *,
        baud: int = DEFAULT_BAUD,
        address: int = DEFAULT_ADDRESS,
        timeout: float = 0.5,
        position_limits: tuple[float, float] | None = None,
    ) -> None:
        if position_limits is not None and position_limits[0] >= position_limits[1]:
            raise SMC100Error(
                f"position_limits must satisfy lo < hi (got {position_limits})"
            )
        self.port = port
        self.baud = baud
        self.address = address
        self.timeout = timeout
        self._position_limits = position_limits
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        # Effective software clamp computed in open() = position_limits ∩ (SL, SR).
        # None means "no clamp configured" — caller sees raw controller behavior.
        self._effective_limits: tuple[float, float] | None = None

    def open(self) -> None:
        if self._serial is not None:
            return
        ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )
        self._serial = ser
        # Read the controller's ESP-loaded soft limits so we can validate
        # any caller-supplied range against them, and use them as the default
        # clamp when no kwarg was passed.
        try:
            sl, sr = self.software_limits()
        except SMC100Error:
            # Some states (e.g. CONFIGURATION) don't permit SL/SR; fall back
            # to no clamp. If position_limits was set we have nothing to
            # validate against, so honor it as-is.
            sl, sr = (-math.inf, math.inf)
        if self._position_limits is not None:
            lo, hi = self._position_limits
            if lo < sl or hi > sr:
                raise SMC100Error(
                    f"position_limits {self._position_limits} fall outside "
                    f"controller envelope ({sl}, {sr})"
                )
            self._effective_limits = (max(lo, sl), min(hi, sr))
        elif math.isfinite(sl) and math.isfinite(sr):
            self._effective_limits = (sl, sr)
        else:
            self._effective_limits = None

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

    @property
    def position_limits(self) -> tuple[float, float] | None:
        """The kwarg passed at construction (None if not set)."""
        return self._position_limits

    @property
    def effective_limits(self) -> tuple[float, float] | None:
        """``position_limits`` ∩ controller's ``(SL, SR)``. None if neither known."""
        return self._effective_limits

    # --- transport ----------------------------------------------------------

    def send(self, cmd: str) -> None:
        """Fire-and-forget command (no reply expected)."""
        if not self._serial:
            raise SMC100Error("serial port not open")
        line = f"{self.address}{cmd}".encode("ascii") + TERMINATOR
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(line)
            self._serial.flush()

    def query(self, cmd: str) -> str:
        """Send a query (e.g. ``TP?``) and return the value portion of the reply."""
        if not self._serial:
            raise SMC100Error("serial port not open")
        line = f"{self.address}{cmd}".encode("ascii") + TERMINATOR
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(line)
            self._serial.flush()
            raw = self._serial.read_until(TERMINATOR)
        text = raw.decode("ascii", errors="replace").strip()
        prefix = f"{self.address}{cmd.rstrip('?')}"
        if not text:
            raise SMC100Error(f"no response to {cmd!r}")
        if text.startswith(prefix):
            return text[len(prefix) :]
        return text

    # --- queries (safe) -----------------------------------------------------

    def identify(self) -> str:
        """``ID?`` — stage identification string (LTA-HS_PN:... on this rig)."""
        return self.query("ID?")

    def firmware(self) -> str:
        """``VE?`` — controller / firmware revision string."""
        return self.query("VE?")

    def position(self) -> float:
        """``TP?`` — current position in stage units (mm for LTA-HS)."""
        return float(self.query("TP?"))

    def setpoint(self) -> float:
        """``TH?`` — set-point position. Useful while in MOVING state."""
        return float(self.query("TH?"))

    def state(self) -> tuple[str, str]:
        """Returns (state_2hex, error_4hex) from the TS response.

        TS reply is ``1TSabcdef``: ``abcd`` is the 16-bit positioner error
        register, ``ef`` is the controller state. See user manual §5.3.

        NB: per the manual, TS *clears* the controller's error buffer when
        it reads it. Don't rely on multiple consecutive TS calls returning
        the same error bits; cache the first read if you need to inspect.
        """
        raw = self.query("TS")
        if len(raw) < 6:
            raise SMC100Error(f"unexpected TS response: {raw!r}")
        return raw[4:6], raw[:4]

    def velocity(self) -> float:
        """``VA?`` — current velocity setting (units/s)."""
        return float(self.query("VA?"))

    def accel(self) -> float:
        """``AC?`` — current acceleration setting (units/s²)."""
        return float(self.query("AC?"))

    def software_limits(self) -> tuple[float, float]:
        """``(SL, SR)`` — controller's ESP-loaded negative / positive limits."""
        sl = float(self.query("SL?"))
        sr = float(self.query("SR?"))
        return sl, sr

    # --- motion (mutating, but transient — no persistent writes) ------------

    def _check_target(self, target: float) -> None:
        if self._effective_limits is None:
            return
        lo, hi = self._effective_limits
        if not lo <= target <= hi:
            raise SMC100Error(f"target {target} outside effective limits ({lo}, {hi})")

    def move_absolute(self, target: float) -> None:
        """``PA<target>`` — move to absolute position."""
        self._check_target(target)
        self.send(f"PA{target}")

    def move_relative(self, delta: float) -> None:
        """``PR<delta>`` — move relative to current position.

        Validation is best-effort: if the current position can't be read we
        fall through and let the controller decide. The controller will
        bounce out-of-range PR commands on its own via SL/SR.
        """
        if self._effective_limits is not None:
            try:
                here = self.position()
            except SMC100Error:
                here = None
            if here is not None:
                self._check_target(here + delta)
        self.send(f"PR{delta}")

    def stop(self) -> None:
        """``ST`` — stop motion (controller decelerates to zero)."""
        self.send("ST")

    def home(self) -> None:
        """``OR`` — execute the home search sequence (NOT REFERENCED → READY)."""
        self.send("OR")

    def enable(self) -> None:
        """``MM1`` — leave DISABLE state, return to READY."""
        self.send("MM1")

    def disable(self) -> None:
        """``MM0`` — enter DISABLE state. Motor coils de-energize."""
        self.send("MM0")

    def reset(self) -> None:
        """``RS`` — reset controller. Returns to NOT REFERENCED (reset)."""
        self.send("RS")

    def disable_keypad(self) -> None:
        """``JM0`` — disable the external keypad input (transient).

        Outside of CONFIGURATION state this only applies for the current
        session — the controller reverts to the JM1 default on next boot.
        Useful for silencing a stuck/floating KEYPAD-connector signal so
        :meth:`leave_jog` can succeed.
        """
        self.send("JM0")

    def enable_keypad(self) -> None:
        """``JM1`` — re-enable the external keypad input (transient)."""
        self.send("JM1")

    def leave_jog(self) -> None:
        """``JD`` — leave JOGGING state (back to READY).

        Per the manual, JD only succeeds when no jog button is currently
        pressed and stage velocity is zero. Pair with :meth:`disable_keypad`
        first if a stuck/floating keypad signal is keeping the controller
        in JOGGING.
        """
        self.send("JD")

    def set_velocity(self, value: float) -> None:
        """``VA<value>`` — set max velocity (transient; not saved to flash)."""
        if value <= 0:
            raise SMC100Error(f"velocity must be > 0 (got {value})")
        self.send(f"VA{value}")

    def set_accel(self, value: float) -> None:
        """``AC<value>`` — set acceleration (transient; not saved to flash)."""
        if value <= 0:
            raise SMC100Error(f"acceleration must be > 0 (got {value})")
        self.send(f"AC{value}")
