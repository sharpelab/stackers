"""Keithley 617 programmable electrometer — thin GPIB protocol wrapper.

Reference: ``manuals/keithley-617-quick-reference.pdf`` (Doc 617-903-01
Rev A) and the full ``keithley-617-instruction-manual.pdf``.

The 617 is pre-SCPI like the Yokogawa 7651:

- No ``*IDN?``. Sending one raises an IDDC (Invalid Device-Dependent
  Command) error on the unit; identify by enumerating GPIB resources.
- Commands are short letter codes with optional numeric argument.
  **``X`` is the execute command** (like ``E;`` on the 7651); the unit
  buffers settings until ``X`` applies them. Concatenate freely:
  ``F1R0X`` = Amps + Autorange + execute.
- Default reply terminator is **CR LF** (``Y0``). Configure pyvisa with
  ``read_termination='\\r\\n'``.
- The talker's default output is the live measurement at the unit's
  conversion rate (~3 Hz). Each ``read()`` dequeues the next one;
  reading format is ``NDCA+0.12345E-09`` (see :class:`Reading`).

This v1 driver is read-mostly:

- Connect / identify / poll the current measurement.
- Bus-driven Zero Check and Zero Correct toggles + a composite
  :meth:`zero` that runs the canonical zero-verification sequence.
- Does **not** change function / range / trigger / V-Source — those
  stay at whatever the operator dialed on the front panel.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("airstacker.keithley617")

# Reading reply format per quick-ref §DATA FORMAT (p38):
#   *NDCA+0.12345E+06,nnn
#   * = N (Normal) | O (Overflow)
#   DCA = function: DCA Amps · DCV Volts · OHM Ohms · DCC Coul ·
#                   DCX External Feedback · VSCR (V-Source readback)
#   mantissa = 5½ digit signed
#   E± exponent
#   ,nnn = optional buffer address (only on Data Store reads, B1X)
_READING_RE = re.compile(
    r"^(?P<status>[NO])"
    r"(?P<func>DCA|DCV|OHM|DCC|DCX|VSCR)"
    r"(?P<value>[+-]?\d+\.?\d*E[+-]?\d+)"
    r"(?:,\d+)?"
    r"\s*$"
)

STATUS_LABEL: dict[str, str] = {"N": "normal", "O": "overflow"}

# Function code → physical unit suffix. Empty for external feedback
# (no canonical unit — depends on the external element).
FUNC_UNIT: dict[str, str] = {
    "DCA": "A",
    "DCV": "V",
    "OHM": "Ω",
    "DCC": "C",
    "DCX": "",
    "VSCR": "V",
}


class Keithley617Error(RuntimeError):
    pass


@dataclass(frozen=True)
class Reading:
    """One parsed measurement off the talker."""

    status: str  # 'N' (normal) | 'O' (overflow)
    function: str  # 'DCA' | 'DCV' | 'OHM' | 'DCC' | 'DCX' | 'VSCR'
    value: float

    @property
    def is_normal(self) -> bool:
        return self.status == "N"

    @property
    def is_overflow(self) -> bool:
        return self.status == "O"

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)

    @property
    def unit(self) -> str:
        return FUNC_UNIT.get(self.function, "")


def format_engineering(value: float, unit: str) -> str:
    """Return a human-readable string like ``+0.913 pA``.

    Picks the appropriate metric prefix based on magnitude so a 0.9 pA
    reading doesn't display as ``+0.0000000009 A``.
    """
    if value == 0.0:
        return f"+0.000 {unit}"
    abs_v = abs(value)
    if abs_v < 1e-12:
        return f"{value * 1e15:+.3f} f{unit}"
    if abs_v < 1e-9:
        return f"{value * 1e12:+.3f} p{unit}"
    if abs_v < 1e-6:
        return f"{value * 1e9:+.3f} n{unit}"
    if abs_v < 1e-3:
        return f"{value * 1e6:+.3f} μ{unit}"
    if abs_v < 1.0:
        return f"{value * 1e3:+.3f} m{unit}"
    if abs_v < 1e3:
        return f"{value:+.3f} {unit}"
    if abs_v < 1e6:
        return f"{value * 1e-3:+.3f} k{unit}"
    return f"{value:+.3e} {unit}"


class Keithley617:
    """Single-instrument wrapper around pyvisa for the 617."""

    def __init__(self, resource: str) -> None:
        self.resource = resource
        self._inst = None  # pyvisa MessageBasedResource once open()'d
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._inst is not None:
            return
        import pyvisa
        from pyvisa.constants import RENLineOperation
        from pyvisa.resources import MessageBasedResource

        rm = pyvisa.ResourceManager()
        inst = rm.open_resource(self.resource)
        if not isinstance(inst, MessageBasedResource):
            inst.close()
            raise Keithley617Error(
                f"resource {self.resource!r} is not message-based "
                f"(got {type(inst).__name__})"
            )
        # 617 conversion time is 330 ms; give the read margin to avoid
        # spurious timeouts during slow updates.
        inst.timeout = 3000
        inst.write_termination = ""
        inst.read_termination = "\r\n"
        # Assert REN + address-as-listener so a stray LOCAL key press
        # doesn't leave us in LOCS. control_ren lives on GPIBInstrument
        # — use getattr so non-GPIB resources don't break.
        control_ren = getattr(inst, "control_ren", None)
        if control_ren is not None:
            try:
                control_ren(RENLineOperation.asrt_address)
            except Exception:
                pass
        self._inst = inst

    def close(self) -> None:
        with self._lock:
            if self._inst is not None:
                try:
                    self._inst.close()
                finally:
                    self._inst = None

    @property
    def is_open(self) -> bool:
        return self._inst is not None

    def _require(self):
        if self._inst is None:
            raise Keithley617Error("instrument not open")
        return self._inst

    def _write(self, cmd: str) -> None:
        with self._lock:
            self._require().write(cmd)

    def _read(self) -> str:
        with self._lock:
            return self._require().read().strip()

    def read(self) -> Reading:
        """Read the next available measurement from the talker.

        The 617 produces readings at its conversion rate (~3 Hz at
        default integration). Each call to :meth:`read` consumes one.
        The returned :class:`Reading` carries whatever function the
        unit is currently in (see :attr:`Reading.function`).
        """
        resp = self._read()
        m = _READING_RE.match(resp)
        if not m:
            raise Keithley617Error(f"unexpected reading reply: {resp!r}")
        return Reading(
            status=m["status"],
            function=m["func"],
            value=float(m["value"]),
        )

    def read_machine_status(self) -> str:
        """Send ``U0X`` and read the machine status word.

        Reply format (after the ``617`` prefix): ``F RR C Z N T O B G
        D Q MM K YY``. See the quick-ref §STATUS WORDS table or
        :func:`decode_machine_status` for field meanings.
        """
        self._write("U0X")
        return self._read()

    def read_error_status(self) -> str:
        """Send ``U1X`` and read the error status word.

        Reading this clears the Error bit in the status byte. Reply
        format (after ``617`` prefix): ``IDDC IDDCO NO_REMOTE
        TRIGGER_OVERRUN NUMBER`` — each 0 or 1.
        """
        self._write("U1X")
        return self._read()

    def read_data_status(self) -> str:
        """Send ``U2X`` — buffer / cal / V-Source overload flags."""
        self._write("U2X")
        return self._read()

    def set_zero_check(self, on: bool) -> None:
        """``C1X`` / ``C0X`` — Zero Check on/off.

        When on, the input is internally shorted; impedance changes per
        the table on p10 of the quick-ref. Front-panel safety label
        says to enable Zero Check before connecting / disconnecting
        cables, so do the same here when manipulating the triax.
        """
        self._write("C1X" if on else "C0X")

    def set_zero_correct(self, on: bool) -> None:
        """``Z1X`` / ``Z0X`` — Zero Correct on/off.

        ``Z1X`` captures the present input as the offset reference and
        subtracts it from subsequent readings. The unit should be in
        Zero Check when Z1X is sent so the captured value is the unit's
        own offset, not a real external signal.
        """
        self._write("Z1X" if on else "Z0X")

    def zero(self, settle_s: float = 1.0) -> None:
        """Run the canonical zero-verification sequence remotely.

        Steps (per quick-ref p11):
          1. Zero Check ON  (``C1X``) — input internally shorted
          2. Wait for the reading to settle
          3. Zero Correct ON (``Z1X``) — capture present reading as the
             offset reference
          4. Wait briefly for the correction to apply
          5. Zero Check OFF (``C0X``) — out of zero check; unit now
             measures the live signal with offset subtracted

        ``settle_s`` is how long to wait between each step. The 617's
        conversion time is 330 ms so 1 s captures several conversions.
        """
        self._write("C1X")
        time.sleep(settle_s)
        # Drain any pre-zero-check reading still in the talker so the
        # next conversion is the one we use to learn the offset.
        try:
            self.read()
        except Exception:
            pass
        time.sleep(settle_s)
        self._write("Z1X")
        time.sleep(settle_s)
        self._write("C0X")
