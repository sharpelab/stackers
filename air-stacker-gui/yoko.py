"""Yokogawa 7651 programmable DC source — thin protocol wrapper.

Reference: manuals/yokogawa-7651-user-manual.pdf §6 (Communication Functions).

The 7651 is pre-SCPI:

- No ``*IDN?``. Identify by enumerating GPIB resources.
- Commands are 1-3 ASCII letters with an optional numeric argument,
  terminated by ``;`` (or CR LF / LF — all three are accepted by the
  unit; we always send ``;``).
- Replies are CR LF-terminated by default.
- Set-commands (function, range, output level, output enable) buffer
  in the unit; an ``E;`` trigger is required to apply them. Without
  the trigger nothing changes on the front panel.
- Most settings are write-only — there is no equivalent of ``F?`` or
  ``S?`` to read back the programmed value. The driver caches set
  values in software so the GUI can display them.
- ``OD;`` returns the live output reading in a header+mantissa+exponent
  format (see :class:`OutputReading`). This is the one always-on
  query; it does not perturb state.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Literal

# OD; reply header — per IM 7651-01E §6.2.4 Table 6.10:
#   a1   = 'N' (Normal) | 'E' (Overload)
#   a2a3 = 'DC'
#   a4   = 'V' (Voltage) | 'A' (Current)
STATUS_LABEL: dict[str, str] = {"N": "normal", "E": "overload"}

# Function codes (Fm1) and range codes (Rm2) — used as raw command strings.
FUNC_VOLTAGE = "F1"
FUNC_CURRENT = "F5"

# Operating range — per IM 7651-01E §1.1.1 / §8 Specifications.
VOLTAGE_RANGE_V: tuple[float, float] = (-30.0, 30.0)
CURRENT_RANGE_A: tuple[float, float] = (-0.120, 0.120)

# OD; reply parser. Manual specifies header `a1a2a3a4` (4 alphabets) +
# data `mantissa E sign exponent`. Mantissa may carry an explicit sign.
_OD_RE = re.compile(
    r"^(?P<status>[NE])DC(?P<func>[VA])(?P<value>[+-]?\d+\.?\d*E[+-]?\d+)\s*$"
)


class YokoError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutputReading:
    """Decoded OD; reply — live output as read from the talker."""

    status: str  # 'N' (normal) | 'E' (overload)
    function: Literal["V", "A"]
    value: float

    @property
    def is_normal(self) -> bool:
        return self.status == "N"

    @property
    def is_overload(self) -> bool:
        return self.status == "E"

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)


class Yoko7651:
    """Single-instrument wrapper around pyvisa for the 7651.

    Set commands queue at the unit and require an ``E;`` trigger to take
    effect; the helpers here send the trigger automatically. Use
    :meth:`read_output` for the live output value (always queryable),
    and the cache properties for the *programmed* values (which the bus
    cannot read back).
    """

    def __init__(
        self,
        resource: str,
        *,
        voltage_limits: tuple[float, float] = VOLTAGE_RANGE_V,
        current_limits: tuple[float, float] = CURRENT_RANGE_A,
    ) -> None:
        """Construct a 7651 driver.

        ``voltage_limits`` and ``current_limits`` default to the 7651's full
        hardware envelope (±30 V, ±0.12 A). Pass tighter bounds when driving
        a load with a narrower safe range — e.g. the NPM140 piezo wants a
        floor of −20 V and won't see anything above the Yoko's +30 V ceiling
        anyway, so a piezo caller passes ``voltage_limits=(-20.0, 30.0)``.
        """
        if not voltage_limits[0] >= VOLTAGE_RANGE_V[0]:
            raise YokoError(
                f"voltage_limits low {voltage_limits[0]} below 7651 floor "
                f"{VOLTAGE_RANGE_V[0]}"
            )
        if not voltage_limits[1] <= VOLTAGE_RANGE_V[1]:
            raise YokoError(
                f"voltage_limits high {voltage_limits[1]} above 7651 ceiling "
                f"{VOLTAGE_RANGE_V[1]}"
            )
        self.resource = resource
        self._voltage_limits = voltage_limits
        self._current_limits = current_limits
        self._inst = None  # pyvisa MessageBasedResource once open()'d
        self._lock = threading.Lock()
        # Software caches — protocol does not allow reading these back.
        self._mode_cache: Literal["V", "A"] | None = None
        self._voltage_cache: float | None = None
        self._current_cache: float | None = None
        self._output_cache: bool | None = None

    def open(self) -> None:
        if self._inst is not None:
            return
        # Lazy import so non-instrument code (tests, docs builds) can import
        # this module without pyvisa or a VISA backend installed.
        import pyvisa
        from pyvisa.resources import MessageBasedResource

        rm = pyvisa.ResourceManager()
        inst = rm.open_resource(self.resource)
        if not isinstance(inst, MessageBasedResource):
            inst.close()
            raise YokoError(
                f"resource {self.resource!r} is not message-based "
                f"(got {type(inst).__name__})"
            )
        inst.timeout = 1500
        # The 7651 carries its own ';' terminator on each command; we
        # don't want pyvisa appending another. Replies are CR LF.
        inst.write_termination = ""
        inst.read_termination = "\n"
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

    # --- low-level helpers --------------------------------------------------

    def _require(self):
        if self._inst is None:
            raise YokoError("instrument not open")
        return self._inst

    def _write(self, cmd: str) -> None:
        with self._lock:
            self._require().write(cmd + ";")

    def _query(self, cmd: str) -> str:
        with self._lock:
            return self._require().query(cmd + ";").strip("\r\n ")

    def trigger(self) -> None:
        """Send ``E;`` so queued settings become effective on the unit."""
        self._write("E")

    # --- queries (safe) -----------------------------------------------------

    def read_output(self) -> OutputReading:
        """Read the live output value via ``OD;``. Non-perturbing."""
        resp = self._query("OD")
        m = _OD_RE.match(resp)
        if not m:
            raise YokoError(f"unexpected OD reply: {resp!r}")
        return OutputReading(
            status=m["status"],
            function="V" if m["func"] == "V" else "A",
            value=float(m["value"]),
        )

    # --- set commands (write-only on bus; cached in software) ---------------

    def set_mode(self, mode: Literal["V", "A"]) -> None:
        self._write(FUNC_VOLTAGE if mode == "V" else FUNC_CURRENT)
        self.trigger()
        self._mode_cache = mode

    def set_voltage(self, value: float) -> None:
        """Program output voltage (auto-range via ``SAm``)."""
        lo, hi = self._voltage_limits
        if not lo <= value <= hi:
            raise YokoError(f"voltage {value} V out of range {self._voltage_limits}")
        if self._mode_cache != "V":
            self.set_mode("V")
        self._write(f"SA{value:+.6f}")
        self.trigger()
        self._voltage_cache = value

    def set_current(self, value: float) -> None:
        lo, hi = self._current_limits
        if not lo <= value <= hi:
            raise YokoError(f"current {value} A out of range {self._current_limits}")
        if self._mode_cache != "A":
            self.set_mode("A")
        self._write(f"SA{value:+.6f}")
        self.trigger()
        self._current_cache = value

    def set_output(self, on: bool) -> None:
        self._write("O1" if on else "O0")
        self.trigger()
        self._output_cache = on

    def reset(self) -> None:
        """``RC;`` — full setting initialization. Invalidates all caches."""
        self._write("RC")
        self._mode_cache = None
        self._voltage_cache = None
        self._current_cache = None
        self._output_cache = None

    # --- caches -------------------------------------------------------------

    @property
    def cached_mode(self) -> Literal["V", "A"] | None:
        return self._mode_cache

    @property
    def cached_voltage(self) -> float | None:
        return self._voltage_cache

    @property
    def cached_current(self) -> float | None:
        return self._current_cache

    @property
    def cached_output_on(self) -> bool | None:
        return self._output_cache

    def seed_voltage_cache(self, value: float) -> None:
        """Seed the voltage cache after a kernel/app restart.

        Useful when the GUI starts and we want to give :meth:`ramp_voltage`
        a known starting point without first issuing ``OD;``.
        """
        self._voltage_cache = value

    # --- safety: software ramp ---------------------------------------------

    def ramp_voltage(
        self,
        target: float,
        step: float = 0.05,
        delay: float = 0.05,
        start: float | None = None,
    ) -> None:
        """Step from *start* (or cache, or live OD) to *target* in voltage mode.

        Piezos don't love sudden voltage steps; always ramp for non-trivial
        moves. Defaults: 0.05 V steps every 50 ms = 1 V/s.
        """
        if step <= 0:
            raise YokoError("step must be > 0")
        if start is not None:
            v = start
            self._voltage_cache = start
        elif self._voltage_cache is not None:
            v = self._voltage_cache
        else:
            r = self.read_output()
            if r.function != "V":
                raise YokoError(
                    "can't ramp voltage — instrument is currently in current mode"
                )
            v = r.value
            self._voltage_cache = v

        direction = 1 if target >= v else -1
        step_signed = step * direction
        while (direction > 0 and v < target) or (direction < 0 and v > target):
            v_next = v + step_signed
            if (direction > 0 and v_next > target) or (
                direction < 0 and v_next < target
            ):
                v_next = target
            self.set_voltage(v_next)
            time.sleep(delay)
            v = v_next
