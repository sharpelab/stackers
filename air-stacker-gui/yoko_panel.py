"""PySide6 panel for the Yokogawa 7651 driving the NPM140 fine-Z piezo
as a **constant-current source**, with a Keithley 617 voltmeter across
the piezo terminals as the voltage-feedback probe.

The Yoko sources a programmed current I; the piezo voltage evolves
linearly as dV/dt = I/C. The 617 reports the actual V (200 TΩ input,
parallel across the piezo). Software closes the loop: pick a target V,
the Move worker sources ±speed_a until V hits the target, then sets
I = 0 (current source at zero holds the piezo's V steady — no ramp-
back-to-0 needed on disable).

Why current-mode: prior V-mode panel stepped SA;E; in software at
~5 V/s, generating per-step charging transients on the piezo. A real
current source produces a smooth analog ramp by construction; the
617 gives us a real voltage trace decoupled from the Yoko's open-loop
set value. See docs/air-stacker-pc.md for the wiring and rationale.

Safety: the NPM140's −20 V hard floor has no hardware-side protection
(the Yoko can swing to −30 V). The 617 poll thread is the safety
watchdog — on every reading it checks against the voltage_limits and
asserts ``set_current(0)`` if breached, then latches a tripped state
that the operator has to acknowledge. The Move spinbox is clamped to
``voltage_limits ± safety_margin_v`` so normal operation can't reach
the trip wire. If the 617 stops answering (comm loss while output is
on), the watchdog also trips — without V feedback we have no safety
net, so we kill the source.

Expected ``cfg`` keys — see ``config.toml`` for the documented defaults.
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

import pyvisa.errors

from keithley617 import Keithley617, Keithley617Error, format_engineering
from yoko import VOLTAGE_RANGE_V, Yoko7651, YokoError

# Exception tuples for narrowing the hardware-resilience try/except blocks.
# yoko.py and keithley617.py let pyvisa errors propagate (they don't wrap),
# so callers must catch both their domain error AND pyvisa.errors.Error.
_YOKO_ERRORS = (YokoError, pyvisa.errors.Error)
_K617_ERRORS = (Keithley617Error, pyvisa.errors.Error)

log = logging.getLogger("airstacker.yoko")

# NPM140 datasheet: 140 µm open-loop over -20 → +130 V (150 V span)
# = 933.33 nm/V. See docs/air-stacker-pc.md.
DEFAULT_NM_PER_VOLT = 140_000.0 / 150.0  # ≈ 933.33

# Defaults — see config.toml for the documented rationale.
DEFAULT_CAPACITANCE_UF = 1.7
DEFAULT_SPEED_A = 5e-6
DEFAULT_MIN_SPEED_A = 100e-9
DEFAULT_MAX_SPEED_A = 20e-6
DEFAULT_JOG_STEP_V = 0.1
DEFAULT_TOLERANCE_V = 0.05
DEFAULT_SAFETY_MARGIN_V = 1.5
DEFAULT_STALE_MS = 1000
# Closed-loop under-damping factor. Worker aims to converge `error` to
# zero over `approach_factor` cycles instead of one. k > 1 gives the
# loop margin to absorb the 617's integrating-ADC bias + the NPM140's
# ±20 % C tolerance without overshoot. See config.toml.
DEFAULT_APPROACH_FACTOR = 2.0

# 617 analog-front-end integration window. Sets the floor on closed-
# loop period — anything faster is theatre. Quoted in the 617's quick-
# ref; effectively fixed.
KEITHLEY_CONVERSION_S = 0.333

# Mouse-down duration past which a +/− jog button switches from a
# single-step click to continuous travel at speed_a. Matches the
# SMC100 panel's value.
JOG_HOLD_MS = 250

# "Output is on" inference threshold on the Yoko's OD; reading in I
# mode. With the relay open the unit reads near 0 A regardless of the
# programmed current; anything above this magnitude is definitely
# sourcing into a load.
OUTPUT_ON_THRESHOLD_A = 10e-9  # 10 nA


class _LatestReading:
    """Thread-safe handoff of the most recent 617 sample.

    The Keithley poll thread writes; Move/Jog workers read. Reading is
    a snapshot — the workers don't block waiting for fresh data, they
    just check the timestamp and bail if it's stale.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: float | None = None
        self._function: str | None = None
        self._timestamp: float = 0.0

    def write(self, value: float, function: str) -> None:
        with self._lock:
            self._value = value
            self._function = function
            self._timestamp = time.monotonic()

    def read(self) -> tuple[float | None, str | None, float]:
        """Returns (value, function, age_seconds). Value is None if never written."""
        with self._lock:
            if self._value is None:
                return (None, None, float("inf"))
            return (self._value, self._function, time.monotonic() - self._timestamp)

    def invalidate(self) -> None:
        with self._lock:
            self._value = None
            self._function = None
            self._timestamp = 0.0


class _YokoPollWorker(QObject):
    """Yoko OD; polling — surfaces the I the source is presently delivering.

    In current mode OD; returns NDCA: the actually-sourced current, not
    just an echo of SA. Useful as a "source is alive" confirmation and
    surfaces compliance-clamp behavior (if the Yoko hits its ±30 V
    compliance ceiling, OD reads back the reduced I).
    """

    state_ready = Signal(object)
    finished = Signal()

    def __init__(self, yoko: Yoko7651, poll_interval_s: float) -> None:
        super().__init__()
        self._yoko = yoko
        self._interval = poll_interval_s
        self._stop_event = threading.Event()

    @Slot()
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                r = self._yoko.read_output()
                payload: dict = {
                    "value": r.value,
                    "function": r.function,
                    "status": r.status,
                }
            except _YOKO_ERRORS as e:
                log.warning("yoko OD read failed: %s", e)
                payload = {"od_err": str(e)}
            self.state_ready.emit(payload)
            self._stop_event.wait(self._interval)
        self.finished.emit()

    def stop(self) -> None:
        self._stop_event.set()


class _KeithleyWatchdogWorker(QObject):
    """617 polling + safety watchdog.

    Reads the 617 at ~3 Hz (the unit's native conversion rate), stashes
    each reading on a shared _LatestReading so Move/Jog workers can
    sample without lock contention, and asserts ``set_current(0)`` on
    hard-limit breach. The watchdog is the single thread that touches
    the 617 — Move/Jog never call ``keithley.read()`` directly.

    Latching is the panel's responsibility: this worker emits a
    ``tripped`` signal with the breach reason; the panel locks down
    controls and shows the operator a "TRIPPED" banner with an
    Acknowledge button.
    """

    state_ready = Signal(object)
    tripped = Signal(object)
    finished = Signal()

    def __init__(
        self,
        keithley: Keithley617,
        yoko: Yoko7651,
        latest: _LatestReading,
        hard_limits: tuple[float, float],
        poll_interval_s: float,
    ) -> None:
        super().__init__()
        self._k = keithley
        self._yoko = yoko
        self._latest = latest
        self._hard_lo, self._hard_hi = hard_limits
        self._interval = poll_interval_s
        self._stop_event = threading.Event()
        self._tripped = False

    @Slot()
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                r = self._k.read()
                payload: dict = {
                    "value": r.value,
                    "function": r.function,
                    "unit": r.unit,
                    "status": r.status,
                }
                # Stash _latest whenever the 617 is in DCV — we always
                # want workers to see fresh data the moment the user
                # Acknowledges a trip. Other functions (DCA from a
                # stale rig config, OHM, …) leave us blind to piezo
                # voltage; the panel's Enable button is gated on
                # function == DCV so we never source into an unknown V.
                # Trip-firing is separately gated on `not self._tripped`
                # so a latched trip doesn't re-fire on every reading
                # while V is still past the hard limit.
                if r.function == "DCV":
                    self._latest.write(r.value, r.function)
                    if (
                        not self._tripped
                        and (r.value < self._hard_lo or r.value > self._hard_hi)
                    ):
                        self._fire_trip(
                            f"V {r.value:+.3f} outside hard limits "
                            f"[{self._hard_lo:+.2f}, {self._hard_hi:+.2f}] V"
                        )
            except _K617_ERRORS as e:
                log.warning("keithley617 read failed: %s", e)
                payload = {"k617_err": str(e)}
            self.state_ready.emit(payload)
            self._stop_event.wait(self._interval)
        self.finished.emit()

    def _fire_trip(self, reason: str) -> None:
        """Latched emergency stop. Asserts I=0, signals the panel, latches
        so subsequent readings don't re-fire."""
        log.error("yoko watchdog TRIPPED: %s", reason)
        self._tripped = True
        try:
            self._yoko.set_current(0.0)
        except _YOKO_ERRORS as e:
            log.exception("yoko set_current(0) during trip failed: %s", e)
        self.tripped.emit({"reason": reason})

    def acknowledge(self) -> None:
        """Clear the latched-trip flag. Called from the GUI thread after
        the operator clicks Acknowledge."""
        self._tripped = False

    def stop(self) -> None:
        self._stop_event.set()


class _MoveWorker(QObject):
    """Closed-loop voltage Move: source ±I until 617 reports V at target.

    Algorithm — one-shot predictive stop per cycle:
        v_now = latest 617 sample
        error = target - v_now
        i_desired = error * C / dt           # the I that lands us in 1 cycle
        i_clipped = clip(i_desired, ±speed_a)  # respect speed ceiling
        set_current(i_clipped); wait dt
        loop

    On the final approach (|error| ≤ speed_a·dt/C), this scales I down
    automatically so we hit target in one cycle within ±20 % (C
    tolerance from the NPM140 datasheet). On the way there, it sources
    full speed_a in the direction of error.

    Bails out (set_current(0), emit err) on:
      - cancel via STOP
      - stale latest reading (no fresh 617 in `stale_s` seconds)
      - soft-limit breach (V outside voltage_limits ± safety_margin)
    """

    finished = Signal(object)
    # Per-iteration diagnostic emit — payload is a flat dict (see run()
    # for keys). Connected to the panel's _on_move_diag slot so the
    # operator can see what the loop is deciding in real time. Same
    # data is also logged at INFO so a post-mortem from the log file
    # is possible without screen-recording.
    diag = Signal(object)

    def __init__(
        self,
        yoko: Yoko7651,
        latest: _LatestReading,
        target_v: float,
        speed_a: float,
        tolerance_v: float,
        capacitance_f: float,
        soft_limits: tuple[float, float],
        stale_s: float,
        approach_factor: float = DEFAULT_APPROACH_FACTOR,
    ) -> None:
        super().__init__()
        self._yoko = yoko
        self._latest = latest
        self._target = target_v
        self._speed_a = abs(speed_a)
        self._tolerance = tolerance_v
        self._C = capacitance_f
        self._soft_lo, self._soft_hi = soft_limits
        self._stale_s = stale_s
        self._approach_factor = max(1.0, approach_factor)
        self._cancel = threading.Event()
        # Tracks the current we've commanded since the last 617 sample.
        # The piezo has been integrating this since the sample was
        # taken — without compensating for it, the predictive scale-
        # down systematically overshoots at the coarse→fine handoff.
        self._i_commanded = 0.0

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        v_last: float | None = None
        try:
            dt = KEITHLEY_CONVERSION_S
            iter_n = 0
            while not self._cancel.is_set():
                iter_n += 1
                v_now, func, age = self._latest.read()
                if v_now is None:
                    raise RuntimeError("no 617 reading available")
                if age > self._stale_s:
                    raise RuntimeError(
                        f"617 reading stale ({age * 1000:.0f} ms)"
                    )
                if func != "DCV":
                    raise RuntimeError(f"617 not in DCV (got {func})")
                # Where is V *right now*?
                # Two lag sources to compensate for:
                #   1. `age` — time since _latest got the reading from
                #      the 617's talker.
                #   2. dt/2 — the 617's integrating ADC reports V
                #      averaged over its conversion window, so the
                #      reading reflects V at the window's midpoint,
                #      not its end. That's an extra ~166 ms past `age`.
                # Both windows had _i_commanded flowing through the
                # piezo, so V has been changing at i_commanded/C through
                # the combined (age + dt/2) interval.
                effective_age = age + dt / 2.0
                v_real_now = v_now + self._i_commanded * effective_age / self._C
                if not self._soft_lo <= v_real_now <= self._soft_hi:
                    raise RuntimeError(
                        f"V {v_real_now:+.3f} outside soft limits "
                        f"[{self._soft_lo:+.2f}, {self._soft_hi:+.2f}]"
                    )
                v_last = v_real_now
                error = self._target - v_real_now
                if abs(error) < self._tolerance:
                    log.info(
                        "move iter %d: TARGET HIT  v_real=%+.4f  err=%+.4f  "
                        "(tolerance=%.3f)",
                        iter_n, v_real_now, error, self._tolerance,
                    )
                    self.diag.emit({
                        "iter": iter_n, "phase": "done",
                        "target": self._target, "v_latest": v_now, "age_ms": age * 1000,
                        "eff_age_ms": effective_age * 1000,
                        "i_prev_uA": self._i_commanded * 1e6,
                        "v_real": v_real_now, "error": error,
                        "i_des_uA": 0.0, "clipped": False, "v_pred": v_real_now,
                        "k": self._approach_factor,
                    })
                    break
                # Predictive under-damped: aim to drive V_real to
                # target over `approach_factor` cycles, not one. With
                # k=2.0 (default), each iteration commits half the
                # error's worth of current — gives the loop margin to
                # absorb both the C-tolerance error (~±20 %) and any
                # residual conversion-time bias. k=1.0 reverts to the
                # one-shot solver (more aggressive, more overshoot).
                # Clipped to ±speed_a so the user-set ceiling holds.
                i_unclipped = error * self._C / (self._approach_factor * dt)
                clipped = abs(i_unclipped) > self._speed_a
                if clipped:
                    i_desired = self._speed_a if error > 0 else -self._speed_a
                else:
                    i_desired = i_unclipped
                # Predictive soft-limit check: where will V_real be at
                # the next sample? Past i_commanded over effective_age
                # (already in flight) plus i_desired over dt + slack.
                slack = 0.1
                v_pred = v_now + (
                    self._i_commanded * effective_age + i_desired * (dt + slack)
                ) / self._C
                log.info(
                    "move iter %d: target=%+.3f  v_latest=%+.4f  age=%.0fms  "
                    "eff_age=%.0fms  i_prev=%+.3fµA  v_real=%+.4f  err=%+.4f  "
                    "i_des=%+.3fµA%s  v_pred=%+.4f  k=%.1f",
                    iter_n, self._target, v_now, age * 1000, effective_age * 1000,
                    self._i_commanded * 1e6, v_real_now, error,
                    i_desired * 1e6, " CLIP" if clipped else "", v_pred,
                    self._approach_factor,
                )
                self.diag.emit({
                    "iter": iter_n, "phase": "coarse" if clipped else "fine",
                    "target": self._target, "v_latest": v_now, "age_ms": age * 1000,
                    "eff_age_ms": effective_age * 1000,
                    "i_prev_uA": self._i_commanded * 1e6,
                    "v_real": v_real_now, "error": error,
                    "i_des_uA": i_desired * 1e6, "clipped": clipped,
                    "v_pred": v_pred,
                    "k": self._approach_factor,
                })
                if not self._soft_lo <= v_pred <= self._soft_hi:
                    self._yoko.set_current(0.0)
                    self._i_commanded = 0.0
                    self.finished.emit(
                        {"limit": True, "v_final": v_real_now, "v_pred": v_pred}
                    )
                    return
                self._yoko.set_current(i_desired)
                self._i_commanded = i_desired
                if self._cancel.wait(dt):
                    break
            # End of loop — success, cancel, or break. Source to 0 in
            # all cases (the finally also covers exceptions).
            self._yoko.set_current(0.0)
            self._i_commanded = 0.0
            if self._cancel.is_set():
                self.finished.emit({"cancelled": True, "v_final": v_last})
            else:
                self.finished.emit({"ok": True, "v_final": v_last})
        except Exception as e:  # noqa: BLE001 — narrowed by re-raising would lose context
            try:
                self._yoko.set_current(0.0)
                self._i_commanded = 0.0
            except _YOKO_ERRORS as e2:
                log.exception("move worker: set_current(0) on err failed: %s", e2)
            self.finished.emit({"err": str(e), "v_final": v_last})


class _JogWorker(QObject):
    """Continuous-jog worker: source ±speed_a until released or limit hit.

    Click-vs-hold is handled in the panel; this worker only runs when
    the operator has committed to continuous mode. On release, the
    panel calls cancel() and the worker drops I to 0.

    Same safety envelope as _MoveWorker — soft limits + stale-reading
    + 617-in-DCV checks every loop iteration.
    """

    finished = Signal(object)
    diag = Signal(object)

    def __init__(
        self,
        yoko: Yoko7651,
        latest: _LatestReading,
        direction: int,
        speed_a: float,
        capacitance_f: float,
        soft_limits: tuple[float, float],
        stale_s: float,
    ) -> None:
        super().__init__()
        self._yoko = yoko
        self._latest = latest
        self._direction = +1 if direction > 0 else -1
        self._speed_a = abs(speed_a)
        self._C = capacitance_f
        self._soft_lo, self._soft_hi = soft_limits
        self._stale_s = stale_s
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        v_last: float | None = None
        try:
            i_signed = self._direction * self._speed_a
            self._yoko.set_current(i_signed)
            log.info(
                "jog start: dir=%+d  speed=%+.3fµA  soft=[%+.2f, %+.2f]V",
                self._direction, i_signed * 1e6, self._soft_lo, self._soft_hi,
            )
            # Poll faster than the 617's native rate — the watchdog
            # writes _latest at ~3 Hz, but we want to respond to
            # release quickly. The cancel wait is the rate limiter.
            poll_s = 0.1
            iter_n = 0
            while not self._cancel.is_set():
                iter_n += 1
                v_now, func, age = self._latest.read()
                if v_now is None:
                    raise RuntimeError("no 617 reading available")
                if age > self._stale_s:
                    raise RuntimeError(
                        f"617 reading stale ({age * 1000:.0f} ms)"
                    )
                if func != "DCV":
                    raise RuntimeError(f"617 not in DCV (got {func})")
                v_last = v_now
                # Predictive soft-limit check — see _MoveWorker for
                # the rationale. `age` covers the staleness of v_now,
                # `poll_s` covers this loop's wait, +0.1 s slack.
                lookahead_s = age + poll_s + 0.1
                v_pred = v_now + i_signed * lookahead_s / self._C
                hit_hi = self._direction > 0 and (
                    v_now >= self._soft_hi or v_pred >= self._soft_hi
                )
                hit_lo = self._direction < 0 and (
                    v_now <= self._soft_lo or v_pred <= self._soft_lo
                )
                # Diag every ~3 iters (~300 ms) so the panel display
                # doesn't get hammered — jog runs at 10 Hz polling.
                if iter_n % 3 == 1 or hit_hi or hit_lo:
                    self.diag.emit({
                        "iter": iter_n, "phase": "jog",
                        "v_latest": v_now, "age_ms": age * 1000,
                        "i_des_uA": i_signed * 1e6, "v_pred": v_pred,
                        "stopping": hit_hi or hit_lo,
                    })
                if hit_hi or hit_lo:
                    log.info(
                        "jog soft-limit hit: dir=%+d  v_latest=%+.4f  "
                        "v_pred=%+.4f  age=%.0fms",
                        self._direction, v_now, v_pred, age * 1000,
                    )
                    break
                if self._cancel.wait(poll_s):
                    break
            self._yoko.set_current(0.0)
            if self._cancel.is_set():
                self.finished.emit({"cancelled": True, "v_final": v_last})
            else:
                self.finished.emit({"limit": True, "v_final": v_last})
        except Exception as e:  # noqa: BLE001
            try:
                self._yoko.set_current(0.0)
            except _YOKO_ERRORS as e2:
                log.exception("jog worker: set_current(0) on err failed: %s", e2)
            self.finished.emit({"err": str(e), "v_final": v_last})


class YokoPanel(QGroupBox):
    """Live readout + closed-loop CC control for the Yoko 7651 + NPM140."""

    DEFAULT_POLL_MS = 1000

    def __init__(self, cfg: dict) -> None:
        super().__init__("Fine Z (Yoko 7651 → NPM140) [CC]")

        # --- config ---------------------------------------------------------
        self._yoko_poll_s = (
            int(cfg.get("poll_interval_ms", self.DEFAULT_POLL_MS)) / 1000.0
        )
        self._nm_per_volt = float(cfg.get("nm_per_volt", DEFAULT_NM_PER_VOLT))
        self._C = float(cfg.get("piezo_capacitance_uf", DEFAULT_CAPACITANCE_UF)) * 1e-6
        self._default_speed_a = float(cfg.get("default_speed_a", DEFAULT_SPEED_A))
        self._min_speed_a = float(cfg.get("min_speed_a", DEFAULT_MIN_SPEED_A))
        self._max_speed_a = float(cfg.get("max_speed_a", DEFAULT_MAX_SPEED_A))
        self._jog_step_v = float(cfg.get("jog_step_v", DEFAULT_JOG_STEP_V))
        self._tolerance_v = float(cfg.get("move_tolerance_v", DEFAULT_TOLERANCE_V))
        self._margin_v = float(cfg.get("safety_margin_v", DEFAULT_SAFETY_MARGIN_V))
        self._stale_s = int(cfg.get("move_stale_ms", DEFAULT_STALE_MS)) / 1000.0
        self._approach_factor = float(
            cfg.get("approach_factor", DEFAULT_APPROACH_FACTOR)
        )

        v_raw = cfg.get("voltage_limits")
        if v_raw is None:
            voltage_limits = VOLTAGE_RANGE_V
        else:
            v_list = [float(x) for x in v_raw]
            if len(v_list) != 2:
                raise ValueError(f"voltage_limits must have 2 entries (got {v_raw!r})")
            voltage_limits = (v_list[0], v_list[1])
        self._hard_limits = voltage_limits
        self._soft_limits = (
            voltage_limits[0] + self._margin_v,
            voltage_limits[1] - self._margin_v,
        )

        c_raw = cfg.get("current_limits")
        current_limits: tuple[float, float] | None
        if c_raw is None:
            current_limits = None
        else:
            c_list = [float(x) for x in c_raw]
            if len(c_list) != 2:
                raise ValueError(f"current_limits must have 2 entries (got {c_raw!r})")
            current_limits = (c_list[0], c_list[1])

        # No fix_voltage_range — we're operating in current mode. The
        # 7651's current ranges are 1/10/100 mA; max_speed_a clamps
        # well inside the 1 mA range, so SA auto-ranging stays put.
        if current_limits is not None:
            self.yoko = Yoko7651(
                resource=cfg["resource"],
                voltage_limits=voltage_limits,
                current_limits=current_limits,
            )
        else:
            self.yoko = Yoko7651(
                resource=cfg["resource"],
                voltage_limits=voltage_limits,
            )

        # 617 is required for this CC panel — without V feedback we have
        # no closed loop and no safety net. If the config doesn't carry
        # a [yoko.keithley617] sub-block, we degrade to a display-only
        # panel: status shows the problem; Enable/Move/Jog stay disabled.
        k617_cfg = cfg.get("keithley617")
        if k617_cfg:
            self.keithley = Keithley617(resource=k617_cfg["resource"])
            # Default: poll as fast as the 617 emits (~3 Hz). Slower
            # polling causes the unit's talker buffer to fill, so each
            # read() returns an older reading — _latest goes stale and
            # workers bail. See config.toml comment.
            self._k617_poll_s = int(k617_cfg.get("poll_interval_ms", 0)) / 1000.0
        else:
            self.keithley = None
            self._k617_poll_s = 0.0

        # --- state ----------------------------------------------------------
        self._latest = _LatestReading()
        self._tripped: bool = False
        self._trip_reason: str | None = None
        self._cache_seeded = False  # for the Yoko I cache

        # Worker / thread handles. Move and Jog share a single "active
        # worker" slot — only one can run at a time.
        self._active_worker: _MoveWorker | _JogWorker | None = None
        self._active_thread: QThread | None = None
        # Set when Disable kicks off a Move-to-0; consumed by the
        # finished handler to chain safe_disable_current on success.
        self._disable_after_move = False

        self._yoko_poll_worker: _YokoPollWorker | None = None
        self._yoko_poll_thread: QThread | None = None
        self._k617_worker: _KeithleyWatchdogWorker | None = None
        self._k617_thread: QThread | None = None

        # Click-vs-hold jog state, matching smc100_panel.
        self._jog_direction: int = 0
        self._jog_continuous: bool = False
        self._jog_timer = QTimer(self)
        self._jog_timer.setSingleShot(True)
        self._jog_timer.setInterval(JOG_HOLD_MS)
        self._jog_timer.timeout.connect(self._on_jog_continuous_fire)

        # --- widgets --------------------------------------------------------
        self.status_label = QLabel("disconnected")
        self.trip_label = QLabel("")
        self.trip_label.setStyleSheet(
            "color: white; background-color: #c0392b; "
            "font-weight: bold; padding: 4px 8px;"
        )
        self.trip_label.setVisible(False)
        self.trip_ack_btn = QPushButton("Acknowledge")
        self.trip_ack_btn.setVisible(False)

        self.voltage_label = QLabel("—")
        v_font = QFont(self.voltage_label.font())
        v_font.setPointSize(v_font.pointSize() + 6)
        v_font.setStyleHint(QFont.StyleHint.Monospace)
        v_font.setFamily("monospace")
        self.voltage_label.setFont(v_font)
        self.travel_label = QLabel("")
        self.travel_label.setStyleSheet("color: #888;")

        # Yoko I sourcing readout — secondary, smaller, formatted A.
        self.current_label = QLabel("—")
        self.current_label.setStyleSheet("color: #888;")
        self.dvdt_label = QLabel("")
        self.dvdt_label.setStyleSheet("color: #888;")

        # 617 mode + status — surfaces a front-panel function-knob bump.
        self.k617_status_label = QLabel("")
        self.k617_status_label.setStyleSheet("color: #888;")

        # Move/Jog diagnostic line — updates each loop iteration from
        # the worker's diag signal. Shows what the closed-loop control
        # is actually deciding. Stays at the last value after the move
        # ends so post-mortem visual inspection is possible. Empty
        # until the first move runs.
        self.move_diag_label = QLabel("")
        diag_font = QFont(self.move_diag_label.font())
        diag_font.setStyleHint(QFont.StyleHint.Monospace)
        diag_font.setFamily("monospace")
        self.move_diag_label.setFont(diag_font)
        self.move_diag_label.setStyleSheet("color: #666; font-size: 10pt;")

        # Target spinbox + Move button.
        self.target_spin = QDoubleSpinBox()
        self.target_spin.setKeyboardTracking(False)
        self.target_spin.setDecimals(3)
        self.target_spin.setSingleStep(0.1)
        self.target_spin.setSuffix(" V")
        self.target_spin.setRange(self._soft_limits[0], self._soft_limits[1])
        self.move_btn = QPushButton("Move")

        # Step + Speed spinboxes.
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setKeyboardTracking(False)
        self.step_spin.setDecimals(3)
        self.step_spin.setSingleStep(0.01)
        self.step_spin.setSuffix(" V")
        self.step_spin.setRange(0.001, 10.0)
        self.step_spin.setValue(self._jog_step_v)
        # Speed in µA — friendlier than scientific notation. Converted
        # to A on read via _speed_a().
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setKeyboardTracking(False)
        self.speed_spin.setDecimals(3)
        self.speed_spin.setSingleStep(0.5)
        self.speed_spin.setSuffix(" µA")
        self.speed_spin.setRange(self._min_speed_a * 1e6, self._max_speed_a * 1e6)
        self.speed_spin.setValue(self._default_speed_a * 1e6)

        self.jog_minus_btn = QPushButton("−")
        self.jog_plus_btn = QPushButton("+")

        # STOP — top-right red panic button, matching smc100/rotation.
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-weight: bold; padding: 2px 10px; }"
            "QPushButton:pressed { background-color: #962d22; }"
        )

        self.enable_btn = QPushButton("Enable")
        self.disable_btn = QPushButton("Disable")
        self.disable_btn.setToolTip(
            "Stop sourcing (I→0) and open the relay. Piezo holds at its "
            "current voltage. Move to 0 V first if you want it discharged."
        )

        self._build_layout()
        self._wire_signals()

        # --- bring up the hardware -----------------------------------------
        try:
            self.yoko.open()
        except _YOKO_ERRORS as e:
            self.status_label.setText(f"yoko open failed: {e}")
            self._set_controls_enabled(False)
            return

        # Put the unit in current mode. set_mode("A") sends F5;E; — if
        # we were previously in V mode at non-zero V this WILL switch
        # the function, but the panel only allows this transition with
        # the output already off in normal operation. At startup the
        # OD; we just did would have told us if the relay was live;
        # we defer the safety call to the watchdog once 617 is up.
        try:
            self.yoko.set_mode("A")
        except _YOKO_ERRORS as e:
            log.warning("yoko set_mode('A') failed at open: %s", e)

        # Infer "is output on" from the first OD; reading. In I mode,
        # OD; reads the actual sourced current — anything above the
        # noise floor implies the relay is closed and we're delivering.
        # If OD failed entirely, leave the cache as None and let the
        # status label say "unknown".
        try:
            r = self.yoko.read_output()
            if r.function == "A":
                self.yoko.seed_output_cache(abs(r.value) > OUTPUT_ON_THRESHOLD_A)
                self._cache_seeded = True
            else:
                log.warning("yoko OD at startup reports %s mode, expected A", r.function)
        except _YOKO_ERRORS as e:
            log.warning("yoko OD at startup failed: %s", e)

        # --- threads --------------------------------------------------------
        # Yoko OD poll — informational, no safety responsibility.
        self._yoko_poll_thread = QThread()
        self._yoko_poll_worker = _YokoPollWorker(self.yoko, self._yoko_poll_s)
        self._yoko_poll_worker.moveToThread(self._yoko_poll_thread)
        self._yoko_poll_thread.started.connect(self._yoko_poll_worker.run)
        self._yoko_poll_worker.state_ready.connect(self._apply_yoko_state)
        self._yoko_poll_worker.finished.connect(self._yoko_poll_thread.quit)
        self._yoko_poll_thread.start()

        # 617 poll + watchdog. Drives the big V readout, stashes the
        # latest reading for Move/Jog, fires set_current(0) on trip.
        if self.keithley is not None:
            try:
                self.keithley.open()
            except _K617_ERRORS as e:
                log.warning("keithley617 open failed: %s", e)
                self.k617_status_label.setText(f"617 open failed: {e}")
                self.keithley = None

        if self.keithley is not None:
            self.k617_status_label.setText(f"617 @ {self.keithley.resource}")
            self._k617_thread = QThread()
            self._k617_worker = _KeithleyWatchdogWorker(
                self.keithley,
                self.yoko,
                self._latest,
                self._hard_limits,
                self._k617_poll_s,
            )
            self._k617_worker.moveToThread(self._k617_thread)
            self._k617_thread.started.connect(self._k617_worker.run)
            self._k617_worker.state_ready.connect(self._apply_k617_state)
            self._k617_worker.tripped.connect(self._on_tripped)
            self._k617_worker.finished.connect(self._k617_thread.quit)
            self._k617_thread.start()
        else:
            self.k617_status_label.setText(
                "no 617 configured — Enable/Move/Jog disabled (no V feedback)"
            )

        self._refresh_controls()
        self._refresh_status_label()

    # --- layout / wiring ----------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.stop_btn)
        outer.addLayout(status_row)

        trip_row = QHBoxLayout()
        trip_row.addWidget(self.trip_label, stretch=1)
        trip_row.addWidget(self.trip_ack_btn)
        outer.addLayout(trip_row)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep1)

        # Primary readout: piezo V from 617. Secondary: Yoko I + dV/dt.
        outer.addWidget(self.voltage_label)
        outer.addWidget(self.travel_label)
        outer.addWidget(self.current_label)
        outer.addWidget(self.dvdt_label)
        outer.addWidget(self.k617_status_label)
        outer.addWidget(self.move_diag_label)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep2)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Move to:"))
        target_row.addWidget(self.target_spin, stretch=1)
        target_row.addWidget(self.move_btn)
        outer.addLayout(target_row)

        step_speed_row = QHBoxLayout()
        step_speed_row.addWidget(QLabel("Step:"))
        step_speed_row.addWidget(self.step_spin, stretch=1)
        step_speed_row.addWidget(QLabel("Speed:"))
        step_speed_row.addWidget(self.speed_spin, stretch=1)
        outer.addLayout(step_speed_row)

        jog_row = QHBoxLayout()
        jog_row.addWidget(self.jog_minus_btn, stretch=1)
        jog_row.addWidget(self.jog_plus_btn, stretch=1)
        outer.addLayout(jog_row)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep3)

        action_row = QHBoxLayout()
        action_row.addWidget(self.enable_btn)
        action_row.addWidget(self.disable_btn)
        action_row.addStretch(1)
        outer.addLayout(action_row)

        outer.addStretch(1)

    def _wire_signals(self) -> None:
        self.move_btn.clicked.connect(self._on_move)
        self.stop_btn.clicked.connect(self._on_stop)
        self.enable_btn.clicked.connect(self._on_enable)
        self.disable_btn.clicked.connect(self._on_disable)
        self.trip_ack_btn.clicked.connect(self._on_trip_ack)
        # Click-vs-hold jog, matching smc100_panel.
        self.jog_minus_btn.pressed.connect(lambda: self._on_jog_pressed(-1))
        self.jog_minus_btn.released.connect(self._on_jog_released)
        self.jog_plus_btn.pressed.connect(lambda: self._on_jog_pressed(+1))
        self.jog_plus_btn.released.connect(self._on_jog_released)

    # --- helpers ------------------------------------------------------------

    def _speed_a(self) -> float:
        """Read the Speed spinbox in amperes."""
        return self.speed_spin.value() * 1e-6

    def _latest_v(self) -> float | None:
        """GUI-thread snapshot of the latest 617 V — used for jog-click target."""
        v, func, age = self._latest.read()
        if v is None or func != "DCV" or age > self._stale_s:
            return None
        return v

    def _ready_to_source(self) -> bool:
        """True iff we can safely command the source on (Enable/Move/Jog).

        Requires: not tripped, 617 present + in DCV + reading fresh and
        within hard limits.
        """
        if self._tripped:
            return False
        if self.keithley is None or self._k617_worker is None:
            return False
        v, func, age = self._latest.read()
        if v is None or func != "DCV" or age > self._stale_s:
            return False
        lo, hi = self._hard_limits
        return lo <= v <= hi

    # --- click handlers (GUI thread) ----------------------------------------

    def _on_enable(self) -> None:
        if not self._ready_to_source():
            self.status_label.setText(
                "can't enable — no fresh 617 DCV reading inside hard limits"
            )
            return
        self._safe(self.yoko.safe_enable_current)
        self._refresh_status_label()
        self._refresh_controls()

    def _on_disable(self) -> None:
        """Safe-shutdown protocol: leave the piezo at V ≈ 0 V, I = 0 A.

        Mirrors the V-mode Disable shape: ramp V down via a closed-loop
        Move-to-0 first, then open the relay. If V is already near 0 V
        we skip the move; if we have no 617 feedback we can't safely
        ramp, so just drop the relay (operator can manually recover at
        the rig if needed).
        """
        if self._active_thread is not None:
            return  # busy
        v = self._latest_v()
        if v is None:
            log.warning("disable without 617 feedback — opening relay at unknown V")
            self._safe(self.yoko.safe_disable_current)
            self._refresh_status_label()
            self._refresh_controls()
            return
        if abs(v) < 0.5:
            # Already at zero — skip the ramp, just open the relay.
            self._safe(self.yoko.safe_disable_current)
            self._refresh_status_label()
            self._refresh_controls()
            return
        # Non-zero V — Move to 0 first, then chain safe_disable_current
        # from the finished handler (gated on the Move succeeding).
        self._disable_after_move = True
        self._start_move(0.0, label=f"ramping {v:+.2f} → 0 V before disable…")

    def _on_move(self) -> None:
        if not self._ready_to_source():
            return
        target = self.target_spin.value()
        self._start_move(target, label=f"moving to {target:+.3f} V…")

    def _on_stop(self) -> None:
        if self._active_worker is not None:
            self._active_worker.cancel()

    def _on_jog_pressed(self, direction: int) -> None:
        if not self._ready_to_source() or self.yoko.cached_output_on is not True:
            return
        log.info("yoko: jog pressed dir=%+d (timer armed)", direction)
        self._jog_direction = direction
        self._jog_continuous = False
        self._jog_timer.start()

    def _on_jog_continuous_fire(self) -> None:
        if self._jog_direction == 0:
            return
        direction = self._jog_direction
        log.info("yoko: jog continuous-fire dir=%+d", direction)
        self._jog_continuous = True
        self._start_jog(direction)

    def _on_jog_released(self) -> None:
        if self._jog_direction == 0:
            return
        log.info(
            "yoko: jog released dir=%+d continuous=%s",
            self._jog_direction,
            self._jog_continuous,
        )
        if self._jog_continuous:
            # Continuous worker is running; cancel it (worker handles I=0).
            if self._active_worker is not None:
                self._active_worker.cancel()
        else:
            self._jog_timer.stop()
            v = self._latest_v()
            if v is None:
                self.status_label.setText("can't jog — no 617 reading")
            else:
                target = v + self._jog_direction * self.step_spin.value()
                # Clamp to soft limits — single-step jog at the edge of
                # the working range just stops at the limit.
                target = max(self._soft_limits[0], min(self._soft_limits[1], target))
                self._start_move(target, label=f"jog {self._jog_direction:+d}…")
        self._jog_direction = 0
        self._jog_continuous = False

    def _on_trip_ack(self) -> None:
        self._tripped = False
        self._trip_reason = None
        self.trip_label.setVisible(False)
        self.trip_ack_btn.setVisible(False)
        if self._k617_worker is not None:
            self._k617_worker.acknowledge()
        self._refresh_status_label()
        self._refresh_controls()

    # --- worker management --------------------------------------------------

    def _start_move(self, target_v: float, *, label: str) -> None:
        if self._active_thread is not None:
            return
        worker = _MoveWorker(
            self.yoko,
            self._latest,
            target_v,
            self._speed_a(),
            self._tolerance_v,
            self._C,
            self._soft_limits,
            self._stale_s,
            approach_factor=self._approach_factor,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_active_finished)
        worker.diag.connect(self._on_worker_diag)
        self._active_worker = worker
        self._active_thread = thread
        thread.start()
        self.status_label.setText(label)
        log.info(
            "move start: target=%+.3fV  speed=%+.3fµA  C=%.2fµF  "
            "tol=%.3fV  soft=[%+.2f, %+.2f]  k=%.1f",
            target_v, self._speed_a() * 1e6, self._C * 1e6,
            self._tolerance_v, *self._soft_limits, self._approach_factor,
        )
        self._refresh_controls()

    def _start_jog(self, direction: int) -> None:
        if self._active_thread is not None:
            return
        worker = _JogWorker(
            self.yoko,
            self._latest,
            direction,
            self._speed_a(),
            self._C,
            self._soft_limits,
            self._stale_s,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_active_finished)
        worker.diag.connect(self._on_worker_diag)
        self._active_worker = worker
        self._active_thread = thread
        thread.start()
        self.status_label.setText(f"jog continuous {direction:+d}…")
        self._refresh_controls()

    @Slot(object)
    def _on_worker_diag(self, info: dict) -> None:
        """Render the worker's per-iteration diagnostic info on the panel.

        Stays at the last reported state after the worker finishes, so
        the operator can scroll back and read the final iteration's
        numbers without needing the log file.
        """
        phase = info.get("phase", "?")
        v_real = info.get("v_real")
        target = info.get("target")
        err = info.get("error")
        i_des = info.get("i_des_uA")
        age = info.get("age_ms")
        clipped = info.get("clipped")
        if phase == "jog":
            self.move_diag_label.setText(
                f"jog #{info['iter']:>3}  v={info['v_latest']:+.4f}V  "
                f"age={age:>4.0f}ms  i={i_des:+.2f}µA  "
                f"v_pred={info['v_pred']:+.3f}"
                + ("  STOPPING" if info.get("stopping") else "")
            )
        else:
            clip_marker = " CLIP" if clipped else ""
            self.move_diag_label.setText(
                f"move #{info['iter']:>3} [{phase}]  v_real={v_real:+.4f}V  "
                f"target={target:+.3f}  err={err:+.4f}V  "
                f"age={age:>4.0f}ms  i_prev={info['i_prev_uA']:+.2f}µA  "
                f"i_des={i_des:+.2f}µA{clip_marker}  v_pred={info['v_pred']:+.3f}"
            )

    @Slot(object)
    def _on_active_finished(self, payload: dict) -> None:
        if self._active_thread is not None:
            self._active_thread.quit()
            self._active_thread.wait(1000)
            self._active_thread = None
        self._active_worker = None

        do_disable = self._disable_after_move
        self._disable_after_move = False

        transient: str | None = None
        if payload.get("ok"):
            v_final = payload.get("v_final")
            if v_final is not None:
                transient = f"at {v_final:+.3f} V"
            if do_disable:
                self._safe(self.yoko.safe_disable_current)
        elif payload.get("cancelled"):
            v_final = payload.get("v_final")
            transient = (
                f"cancelled at {v_final:+.3f} V"
                if v_final is not None
                else "cancelled"
            )
        elif payload.get("limit"):
            v_final = payload.get("v_final")
            transient = (
                f"hit soft limit at {v_final:+.3f} V"
                if v_final is not None
                else "hit soft limit"
            )
        elif "err" in payload:
            transient = f"err: {payload['err']}"

        self._refresh_controls()
        if transient is not None:
            self.status_label.setText(transient)
        else:
            self._refresh_status_label()

    @Slot(object)
    def _on_tripped(self, payload: dict) -> None:
        reason = payload.get("reason", "unknown")
        log.error("yoko panel tripped: %s", reason)
        self._tripped = True
        self._trip_reason = reason
        # Cancel any active worker — the watchdog has already set I=0
        # via the Yoko driver lock, but we want the worker thread to
        # exit cleanly so the action buttons re-enable.
        if self._active_worker is not None:
            self._active_worker.cancel()
        self.trip_label.setText(f"TRIPPED — {reason}")
        self.trip_label.setVisible(True)
        self.trip_ack_btn.setVisible(True)
        self._refresh_controls()

    # --- state refresh ------------------------------------------------------

    def _refresh_status_label(self) -> None:
        if self._tripped:
            self.status_label.setText("TRIPPED — see banner")
            return
        if self.keithley is None:
            self.status_label.setText("no 617 — display only")
            return
        state = self.yoko.cached_output_on
        if state is None:
            self.status_label.setText("output state unknown")
        elif state:
            self.status_label.setText("OUTPUT ON")
        else:
            self.status_label.setText("OUTPUT OFF")

    def _refresh_controls(self) -> None:
        """Sync Enable / Disable / Move / Jog enabled state."""
        # While tripped or while a worker is running, action buttons go
        # cold. STOP and Acknowledge stay live.
        if self._tripped:
            for btn in (
                self.enable_btn,
                self.disable_btn,
                self.move_btn,
                self.jog_minus_btn,
                self.jog_plus_btn,
            ):
                btn.setEnabled(False)
            return

        if self._active_thread is not None:
            # Workers stomp all action buttons except STOP. Jog buttons
            # also get disabled but we skip toggling them mid-press to
            # avoid Qt firing released() on a disabled-mid-press button.
            self.enable_btn.setEnabled(False)
            self.disable_btn.setEnabled(False)
            self.move_btn.setEnabled(False)
            if self._jog_direction == 0:
                self.jog_minus_btn.setEnabled(False)
                self.jog_plus_btn.setEnabled(False)
            return

        ready = self._ready_to_source()
        state = self.yoko.cached_output_on

        if not ready or state is None:
            # 617 not configured / not in DCV / stale → can't enable, can't move.
            self.enable_btn.setEnabled(False)
            self.disable_btn.setEnabled(state is True)
            self.move_btn.setEnabled(False)
            self.jog_minus_btn.setEnabled(False)
            self.jog_plus_btn.setEnabled(False)
            return

        if state:
            self.enable_btn.setEnabled(False)
            self.disable_btn.setEnabled(True)
            self.move_btn.setEnabled(True)
            self.jog_minus_btn.setEnabled(True)
            self.jog_plus_btn.setEnabled(True)
        else:
            self.enable_btn.setEnabled(True)
            self.disable_btn.setEnabled(False)
            # Move/Jog require output on — when the relay is open,
            # sourcing into nothing just runs the unit into compliance.
            self.move_btn.setEnabled(False)
            self.jog_minus_btn.setEnabled(False)
            self.jog_plus_btn.setEnabled(False)

    def _safe(self, fn, *args) -> None:
        name = getattr(fn, "__name__", repr(fn))
        log.info("yoko: %s(%s)", name, args if args else "")
        try:
            fn(*args)
        except _YOKO_ERRORS as e:
            log.exception("yoko: %s failed", name)
            self.status_label.setText(f"err: {e}")

    def _set_controls_enabled(self, enabled: bool) -> None:
        """Used at startup when open() failed."""
        for w in (
            self.target_spin,
            self.step_spin,
            self.speed_spin,
            self.move_btn,
            self.jog_minus_btn,
            self.jog_plus_btn,
            self.enable_btn,
            self.disable_btn,
        ):
            w.setEnabled(enabled)

    # --- poll-thread payload handlers ---------------------------------------

    @Slot(object)
    def _apply_yoko_state(self, payload: dict) -> None:
        if "_worker_err" in payload:
            self.current_label.setText(f"yoko poll err: {payload['_worker_err']}")
            return
        if "od_err" in payload:
            self.current_label.setText(f"yoko OD err: {payload['od_err']}")
            return

        value = payload["value"]
        function = payload["function"]
        status = payload["status"]

        if function == "A":
            self.current_label.setText(
                f"Yoko sourcing {format_engineering(value, 'A')}"
            )
            # Implied piezo dV/dt = I / C. Small enough that the
            # operator can sanity-check the speed setting visually.
            dvdt = value / self._C
            self.dvdt_label.setText(f"≈ {dvdt:+.3f} V/s on {self._C * 1e6:.1f} µF")
            # Seed output_cache once we've got a first OD reading.
            if not self._cache_seeded:
                self.yoko.seed_output_cache(abs(value) > OUTPUT_ON_THRESHOLD_A)
                self._cache_seeded = True
                self._refresh_controls()
                self._refresh_status_label()
        else:
            # Yoko in V mode unexpectedly (e.g. someone clicked the
            # front-panel function key). Surface but don't crash.
            self.current_label.setText(
                f"Yoko in {function} mode (expected A): {value:+.3f}"
            )
            self.dvdt_label.setText("")

        if status == "E":
            # Overload — surface in the status row but no widget-level
            # banner (current is informational, not life-safety here).
            self.current_label.setText(self.current_label.text() + " · OVERLOAD")

    @Slot(object)
    def _apply_k617_state(self, payload: dict) -> None:
        if "_worker_err" in payload:
            self.k617_status_label.setText(f"617 poll err: {payload['_worker_err']}")
            return
        if "k617_err" in payload:
            self.voltage_label.setText("—")
            self.k617_status_label.setText(f"617 err: {payload['k617_err']}")
            self._refresh_controls()
            return

        value = payload["value"]
        unit = payload["unit"]
        function = payload["function"]
        status = payload["status"]

        if function == "DCV":
            self.voltage_label.setText(format_engineering(value, "V"))
            travel_um = value * self._nm_per_volt / 1000.0
            self.travel_label.setText(f"≈ {travel_um:+.2f} µm extension")
            suffix = f"617 · {function}"
        else:
            # Wrong-mode display: still show the reading but with the
            # function suffix so it's obvious why Move/Jog are greyed.
            self.voltage_label.setText(format_engineering(value, unit))
            self.travel_label.setText("")
            suffix = f"617 · {function} (need DCV for control)"

        if status != "N":
            suffix += " · OVERFLOW"
        self.k617_status_label.setText(suffix)
        # Function-mode flip from DCV could re-enable / disable the
        # control buttons — refresh.
        self._refresh_controls()

    # --- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Safe-shutdown protocol: leave the piezo at V ≈ 0 V, I = 0 A, relay open.

        Order matters — the inline ramp-to-0 needs the watchdog thread
        running so _latest stays fresh. Sequence:
          1. Cancel any active Move/Jog worker (it drops I to 0).
          2. Block here ramping V → 0 using the watchdog's _latest.
          3. safe_disable_current — SA0 + O0.
          4. Stop watchdog + Yoko-poll threads, close instruments.

        Blocking the GUI thread is fine — the app is tearing down.
        Worst case the ramp takes ~30 s from full deflection at 1 µA;
        typically the operator has already clicked Disable first, in
        which case cached_output_on is False and we skip the ramp.
        """
        # 1. Cancel any active worker.
        if self._active_worker is not None:
            self._active_worker.cancel()
        if self._active_thread is not None:
            self._active_thread.quit()
            self._active_thread.wait(2000)
        self._active_thread = None
        self._active_worker = None

        # 2. Ramp to 0 V if needed and we have 617 feedback. Skipped
        # when output is already off, when we're tripped (operator
        # acknowledges to recover), or when 617 isn't usable.
        if (
            self.yoko.is_open
            and self.yoko.cached_output_on
            and not self._tripped
            and self._k617_worker is not None
        ):
            try:
                self._shutdown_ramp_to_zero(deadline_s=60.0)
            except Exception as e:  # noqa: BLE001
                log.warning("shutdown ramp-to-zero failed: %s", e)

        # 3. Drop I to 0 and open the relay. Always do this if the
        # output is still on — leaves the unit in a known safe state
        # even if the ramp couldn't complete.
        if self.yoko.is_open and self.yoko.cached_output_on:
            try:
                self.yoko.safe_disable_current()
            except _YOKO_ERRORS as e:
                log.warning("yoko safe_disable_current during shutdown: %s", e)

        # 4. Stop poll threads + close instruments.
        if self._yoko_poll_worker is not None and self._yoko_poll_thread is not None:
            self._yoko_poll_worker.stop()
            self._yoko_poll_thread.quit()
            if not self._yoko_poll_thread.wait(2000):
                log.warning("yoko poll thread did not exit cleanly")
        if self._k617_worker is not None and self._k617_thread is not None:
            self._k617_worker.stop()
            self._k617_thread.quit()
            if not self._k617_thread.wait(2000):
                log.warning("keithley617 poll thread did not exit cleanly")
        if self.keithley is not None:
            try:
                self.keithley.close()
            except _K617_ERRORS as e:
                log.warning("keithley617 close failed: %s", e)
        self.yoko.close()

    def _shutdown_ramp_to_zero(self, *, deadline_s: float) -> None:
        """Block-the-GUI-thread ramp to V≈0 using the watchdog's _latest.

        Uses the same predictive scale-down algorithm as _MoveWorker
        but inline on the GUI thread. The watchdog thread continues
        writing to _latest at ~3 Hz throughout. Bails out and returns
        on:
          - deadline_s elapsed (something's wrong; let safe_disable
            finish the job at whatever V we got to)
          - stale or non-DCV 617 reading (no feedback → can't ramp
            safely; safe_disable will open the relay)
          - V within move_tolerance of 0 (success)
        """
        deadline = time.monotonic() + deadline_s
        target = 0.0
        # Use the default speed for shutdown, not the spinbox value —
        # operator may have left it at the max, and shutdown isn't a
        # context to be cute about speed.
        speed_a = self._default_speed_a
        dt = KEITHLEY_CONVERSION_S
        i_commanded = 0.0  # parallel to _MoveWorker._i_commanded
        soft_lo, soft_hi = self._soft_limits
        while time.monotonic() < deadline:
            v_now, func, age = self._latest.read()
            if v_now is None or func != "DCV" or age > self._stale_s:
                log.warning(
                    "shutdown ramp: stale/missing 617 (age=%s, func=%s) — aborting",
                    age, func,
                )
                break
            # Compensate sample staleness + 617 integration midpoint
            # (mirrors _MoveWorker; without these the final approach
            # overshoots by ~i·(age + dt/2)/C).
            effective_age = age + dt / 2.0
            v_real_now = v_now + i_commanded * effective_age / self._C
            error = target - v_real_now
            if abs(error) < self._tolerance_v:
                break
            # Under-damped: same approach_factor as Move so shutdown
            # has the same gentle settling behavior.
            i_desired = error * self._C / (self._approach_factor * dt)
            if abs(i_desired) > speed_a:
                i_desired = speed_a if error > 0 else -speed_a
            slack = 0.1
            v_pred = v_now + (
                i_commanded * effective_age + i_desired * (dt + slack)
            ) / self._C
            if not soft_lo <= v_pred <= soft_hi:
                log.warning(
                    "shutdown ramp: predicted V_pred=%+.3f outside soft limits — bailing",
                    v_pred,
                )
                break
            try:
                self.yoko.set_current(i_desired)
            except _YOKO_ERRORS as e:
                log.warning("shutdown ramp: set_current failed: %s", e)
                break
            i_commanded = i_desired
            time.sleep(dt)
        # Always end at I=0, regardless of how the loop exited.
        try:
            self.yoko.set_current(0.0)
        except _YOKO_ERRORS as e:
            log.warning("shutdown ramp: final set_current(0) failed: %s", e)
