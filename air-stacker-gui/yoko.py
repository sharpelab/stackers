"""Yokogawa 7651 programmable DC source — thin protocol wrapper.

Reference: manuals/yokogawa-7651-user-manual.pdf §6 (Communication Functions).

The 7651 is pre-SCPI:

- No ``*IDN?``. Identify by enumerating GPIB resources.
- Commands are 1-3 ASCII letters with an optional numeric argument,
  terminated by ``;`` (or CR LF / LF — all three are accepted by the
  unit; we always send ``;``).
- Replies are terminated by **CR only** on the Air Stacker unit (the DL
  command selects CR LF / CR / LF / EOI; ours is configured CR-only and
  the setting is persistent). pyvisa's ``read_termination`` must match.
- Set-commands (function, range, output level, output enable) buffer
  in the unit; an ``E;`` trigger is required to apply them. Without
  the trigger nothing changes on the front panel.
- Most settings are write-only — there is no equivalent of ``F?`` or
  ``S?`` to read back the programmed value. The driver caches set
  values in software so the GUI can display them.
- ``OD;`` returns the live output reading in a header+mantissa+exponent
  format (see :class:`OutputReading`). This is the one always-on
  query; it does not perturb state.

**LOCAL / REMOTE caveat**: pressing the front-panel LOCAL key drops the
unit into LOCS, where setting commands (F, SA, O, RC) are silently
accepted on the bus but **not acted on** — the unit's relay never
clicks, OD keeps reading the quiescent ~0 V, and there's no error
returned. OD; still works, so the symptom looks like "the unit ignores
everything except reads." :meth:`open` asserts REN to transition the
unit back to REMS. If the talker also stops responding (OD; times out)
call :meth:`recover` — it sends a Device Clear (DCL) interface message
which flushes the I/O buffers. **DCL also turns the unit's OUTPUT OFF
on this 7651 firmware**, so don't call ``recover()`` while the piezo is
at non-zero voltage unless you re-ramp from 0 afterward.
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

# OC; reply parser — per IM 7651-01E §6.3(18). "STS1=<n>" where n in 0..255
# is a bitfield (bits numbered 1..8 in the manual; we expose them as named
# booleans on StatusByte).
_OC_RE = re.compile(r"^STS1=(?P<value>\d+)\s*$")


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


@dataclass(frozen=True)
class StatusByte:
    """Decoded OC; reply — per IM 7651-01E §6.3(18) Table.

    The unit returns ``STS1=<n>`` with ``<n>`` in 0..255. Bits are numbered
    1..8 in the manual (LSB-first); we expose each as a named boolean.
    The most useful one for debugging is ``last_cmd_err``: query OC; right
    after a write to verify the unit accepted it.
    """

    raw: int

    @property
    def program_setting(self) -> bool:
        return bool(self.raw & (1 << 0))  # bit 1

    @property
    def program_running(self) -> bool:
        return bool(self.raw & (1 << 1))  # bit 2

    @property
    def last_cmd_err(self) -> bool:
        return bool(self.raw & (1 << 2))  # bit 3

    @property
    def output_unstable(self) -> bool:
        return bool(self.raw & (1 << 3))  # bit 4

    @property
    def output_on(self) -> bool:
        return bool(self.raw & (1 << 4))  # bit 5

    @property
    def cal_mode(self) -> bool:
        return bool(self.raw & (1 << 5))  # bit 6

    @property
    def ic_card_in(self) -> bool:
        return bool(self.raw & (1 << 6))  # bit 7

    @property
    def cal_switch(self) -> bool:
        return bool(self.raw & (1 << 7))  # bit 8


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
        from pyvisa.constants import RENLineOperation
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
        # don't want pyvisa appending another. Reply terminator is CR
        # only on our unit — see the module docstring for the DL setting.
        inst.write_termination = ""
        inst.read_termination = "\r"
        # Assert REN + address-as-listener so a stray LOCAL key press
        # doesn't leave us in LOCS where writes silently no-op. Some
        # USB-GPIB adapters don't implement control_ren; we treat that
        # as best-effort. control_ren lives on GPIBInstrument, a
        # subclass of MessageBasedResource — use getattr so non-GPIB
        # resources (testing with serial loopback, etc.) don't break.
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

    def read_status_code(self) -> StatusByte:
        """Read the ``OC;`` status byte. Non-perturbing.

        ``StatusByte.last_cmd_err`` is the most useful field — query OC;
        right after a write to verify the unit parsed and accepted the
        previous command.
        """
        resp = self._query("OC")
        m = _OC_RE.match(resp)
        if not m:
            raise YokoError(f"unexpected OC reply: {resp!r}")
        return StatusByte(raw=int(m["value"]))

    def read_panel_settings(self) -> str:
        """Read the ``OS;`` panel-setting summary. Returns the raw reply.

        Per IM 7651-01E §6.3(15). Format isn't fully documented here —
        callers print/log it for visual inspection.
        """
        return self._query("OS")

    # --- set commands (write-only on bus; cached in software) ---------------
    #
    # Setting commands (function/range/output-data/output-on-off) buffer at
    # the unit and need an ``E;`` trigger to apply. We coalesce ``cmd;E`` into
    # a single bus write so a parallel ``OD;`` query from the poll thread
    # can't slip in between the queued setting and its trigger.

    def set_mode(self, mode: Literal["V", "A"]) -> None:
        self._write((FUNC_VOLTAGE if mode == "V" else FUNC_CURRENT) + ";E")
        self._mode_cache = mode

    def set_voltage(self, value: float) -> None:
        """Program output voltage (auto-range via ``SAm``)."""
        lo, hi = self._voltage_limits
        if not lo <= value <= hi:
            raise YokoError(f"voltage {value} V out of range {self._voltage_limits}")
        if self._mode_cache != "V":
            self.set_mode("V")
        self._write(f"SA{value:+.6f};E")
        self._voltage_cache = value

    def set_current(self, value: float) -> None:
        lo, hi = self._current_limits
        if not lo <= value <= hi:
            raise YokoError(f"current {value} A out of range {self._current_limits}")
        if self._mode_cache != "A":
            self.set_mode("A")
        self._write(f"SA{value:+.6f};E")
        self._current_cache = value

    def set_output(self, on: bool) -> None:
        self._write(("O1" if on else "O0") + ";E")
        self._output_cache = on

    def reset(self) -> None:
        """``RC;`` — full setting initialization. Invalidates all caches."""
        self._write("RC")
        self._mode_cache = None
        self._voltage_cache = None
        self._current_cache = None
        self._output_cache = None

    def recover(self) -> None:
        """Send a GPIB Device Clear (DCL) to flush stuck I/O buffers.

        Use only when ``OD;`` itself stops responding (the unit's talker
        queue is jammed). DCL is bus-level and reliable, but on this 7651
        firmware it **also turns the output OFF and clears the programmed
        level**, so all caches are invalidated. Don't call this while the
        piezo is at non-zero voltage unless you re-ramp from 0 afterward.
        """
        with self._lock:
            self._require().clear()
        self._mode_cache = None
        self._voltage_cache = None
        self._current_cache = None
        self._output_cache = False  # DCL drops the relay open

    def safe_disable(self, step: float = 0.1, delay: float = 0.05) -> None:
        """Shutdown protocol — ramp voltage to 0, then ``O0;``.

        Always use this in preference to a bare ``set_output(False)`` when
        the unit is at non-zero voltage and driving a piezo. Slamming
        ``O0`` while sitting at V≠0 V drops the full programmed voltage
        across the (capacitive) NPM140 in one step when the relay opens,
        which risks mechanical ringing (resonance at 670 Hz) and isn't
        kind to piezo life.

        Blocks until done (the ramp is synchronous). Call from a worker
        thread if you don't want to freeze the GUI.

        No-ops the ramp if the cache says we're already at ~0 V or in
        current mode; still always sends the final ``O0;``.
        """
        v = self._voltage_cache
        if self._mode_cache == "V" and v is not None and abs(v) > 1e-4:
            self.ramp_voltage(0.0, step=step, delay=delay)
        self.set_output(False)

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
