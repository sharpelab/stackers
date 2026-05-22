"""PySide6 panel for the Yokogawa 7651 driving the NPM140 fine-Z piezo.

Two operator modes share the same hardware path:

  * **Constant-I** (+/− buttons): pick a magnitude on the Current
    slider, press + or − to source ±I. Fire-and-forget; Stop drops I
    to 0. Operator-IS-the-loop pattern — no closed loop, no overshoot
    surprises.

  * **Target-tracking** (drag on travel bar): drag the marker to set
    a target voltage. A GUI-thread P-law controller runs every fresh
    617 reading: ``i_cmd = clip(K · err, ±i_slider)`` with
    ``K = C / τ`` so an unclipped command would zero the error in one
    Yoko settling time. Target persists past drag-release into a
    Holding state where the same controller corrects against drift
    (steady-state ``|err| ≈ I_leak / K``). Slider value acts as a
    max-speed cap in both Tracking and Holding. Quantizes i_cmd to
    the Yoko's 100 nA resolution — sub-step values round to 0, giving
    a ~15 mV deadband for free.

The two modes are mutually exclusive: +/− cancels any active target;
dragging on the bar cancels constant-I. Stop tears down whichever is
active. Compared to the prior closed-loop auto-Move that was ripped
out in `61f785b`, the K = C/τ gain deliberately under-damps the
high-bandwidth tracking that oscillated against V-dependent leakage —
see YOKO_CONTROL_INVESTIGATION.md for the diagnostic series.

Safety: the NPM140's −20 V hard floor has no hardware-side protection
(the Yoko can swing to −30 V); the +30 V ceiling is hardware-bounded
by the Yoko's compliance. Two safety layers run on the 617 poll
thread:
  - **Hard trip** (reactive): V already outside [hard_lo, hard_hi].
    Always armed; symmetric on both ends.
  - **Predictive trip** (proactive, floor-side only): if
    ``v + i_commanded · (dt + slack) / C`` would cross the soft
    floor while sourcing negative, kill the source one cycle early.
    No ceiling-side predictor — the Yoko's compliance clamps before
    NPM140 damage.
Both call ``set_current(0)`` and latch a "TRIPPED" state the operator
must Acknowledge. Start is gated on a fresh DCV reading inside hard
limits, so a 617 comm loss can't trick us into sourcing blind.

Expected ``cfg`` keys — see ``config.toml`` for the documented defaults.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

from PySide6.QtCore import (
    QEventLoop,
    QObject,
    QPointF,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressDialog,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import pyvisa.errors

from keithley617 import Keithley617, Keithley617Error, format_engineering
from widgets import action_button
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
# DEFAULT_CAPACITANCE_UF is the measured NPM140 value (Probe 2 in
# YOKO_CONTROL_INVESTIGATION.md, 2026-05-11), not the datasheet 1.7 µF.
# Sets the controller gain K = C / τ; an off-by-X% here biases settling.
DEFAULT_CAPACITANCE_UF = 2.0
DEFAULT_SPEED_A = 2.5e-6
DEFAULT_MAX_SPEED_A = 5e-6
DEFAULT_SAFETY_MARGIN_V = 1.5
DEFAULT_STALE_MS = 1000

# 617 analog-front-end integration window. Sets the lookahead floor
# for the predictive trip (v_pred = v + i · (dt + slack) / C).
KEITHLEY_CONVERSION_S = 0.333

# Headroom on top of one 617 conversion cycle for the predictive trip.
# Covers _latest staleness + scheduling jitter between the watchdog
# read and a GUI-thread Start.
PREDICT_SLACK_S = 0.1

# Yoko 7651 output settling lag — time between sending set_current(X)
# and I actually reaching X on the unit. From Probe 3b in
# YOKO_CONTROL_INVESTIGATION.md (~0.6 s first-order). Two roles:
#   * Controller gain: K = C / YOKO_SETTLE_S so an unclipped P command
#     would zero the error in one τ.
#   * Shutdown overshoot prediction: V keeps moving at the old i for
#     ~τ after set_current(0), so v_after_stop = v + i · τ / C.
YOKO_SETTLE_S = 0.6

# "Output is on" inference threshold on the Yoko's OD; reading in I
# mode. With the relay open the unit reads near 0 A regardless of the
# programmed current; anything above this magnitude is definitely
# sourcing into a load.
OUTPUT_ON_THRESHOLD_A = 10e-9  # 10 nA

# Yoko 7651 1 mA range programming resolution. The target-mode
# controller quantizes i_cmd to this step to skip bus traffic for
# sub-resolution changes (the Yoko would round anyway). Also acts as
# a natural deadband: at K = C/τ ≈ 3.3 µA/V into 2 µF, |err| < ~15 mV
# rounds to i_cmd = 0.
YOKO_I_RESOLUTION_A = 100e-9


def _predict_v_one_cycle(v: float, i_a: float, capacitance_f: float) -> float:
    """Project V one 617 conversion + slack ahead at constant i_a.

    Single source of truth for the soft-floor predictive trip: the
    watchdog (worker thread), +/− refusal (GUI thread), and target
    controller (GUI thread) all use this same lookahead. The window
    covers one 617 conversion (333 ms) plus PREDICT_SLACK_S for
    _latest staleness + GUI scheduling jitter.
    """
    return v + i_a * (KEITHLEY_CONVERSION_S + PREDICT_SLACK_S) / capacitance_f


class _LatestReading:
    """Thread-safe handoff of the most recent 617 sample.

    Watchdog writes; GUI thread reads on Start to gate on fresh feedback.
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
    """617 polling + safety watchdog (hard + predictive trips).

    Reads the 617 at ~3 Hz (the unit's native conversion rate), stashes
    each reading on a shared _LatestReading, and asserts
    ``set_current(0)`` on either:
      - **hard trip**: V already outside [hard_lo, hard_hi]. Always
        armed, regardless of whether we're sourcing.
      - **predictive trip**: while ``get_source_state`` reports
        sourcing with I<0, ``v_pred = v + i·(dt+slack)/C`` would cross
        the soft floor.
    Both share the same latch + emit path, so the panel's Acknowledge
    UI works for either.

    The watchdog is the single thread that touches the 617.
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
        soft_floor: float,
        capacitance_f: float,
        poll_interval_s: float,
        get_commanded_current: Callable[[], float],
    ) -> None:
        super().__init__()
        self._k = keithley
        self._yoko = yoko
        self._latest = latest
        self._hard_lo, self._hard_hi = hard_limits
        self._soft_floor = soft_floor
        self._C = capacitance_f
        self._interval = poll_interval_s
        self._get_commanded_current = get_commanded_current
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
                if r.function == "DCV":
                    self._latest.write(r.value, r.function)
                    if not self._tripped:
                        # Hard trip — V already past the damage
                        # threshold trumps the predictive check.
                        if r.value < self._hard_lo or r.value > self._hard_hi:
                            self._fire_trip(
                                f"V {r.value:+.3f} outside hard limits "
                                f"[{self._hard_lo:+.2f}, {self._hard_hi:+.2f}] V"
                            )
                        else:
                            # Predictive floor trip — only while
                            # sourcing toward the floor. NPM140 −20 V
                            # is the damage threshold; the Yoko's
                            # +30 V ceiling is hardware-bounded so no
                            # ceiling-side predictor.
                            i_cmd = self._get_commanded_current()
                            if i_cmd < 0:
                                v_pred = _predict_v_one_cycle(
                                    r.value, i_cmd, self._C,
                                )
                                if v_pred < self._soft_floor:
                                    self._fire_trip(
                                        f"v_pred {v_pred:+.3f} V would breach "
                                        f"floor {self._soft_floor:+.2f} V at "
                                        f"I={i_cmd*1e6:+.2f}µA "
                                        f"(v={r.value:+.3f})"
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


class _TravelBar(QWidget):
    """Horizontal scale showing piezo V within the hard limits.

    Three labeled ticks (hard_lo, 0, hard_hi — typically −20, 0, +30)
    with a filled circle at the current V. Marker is blue in the safe
    zone, red when within ``safety_margin`` of either hard limit —
    visual preview of the predictive trip zone.

    Click-and-drag on the bar to set a target voltage; the bar paints a
    hollow ring at the target. ``target_changed`` fires continuously
    during the drag and the target persists past mouse release (so the
    panel's P-law controller can hold there). ``target_cleared`` fires
    when an external caller (Stop / +-/− / trip) clears the target.
    Targets are clamped to the soft limits so the operator can't aim
    into the predictive-trip zone.
    """

    _AXIS = "#888"
    _MARKER = "#3498db"
    _MARKER_WARN = "#c0392b"
    _TARGET = "#2c3e50"
    _TARGET_DRAG = "#5dade2"

    target_changed = Signal(float)
    target_cleared = Signal()
    drag_released = Signal()

    def __init__(
        self,
        hard_lo: float,
        hard_hi: float,
        safety_margin: float,
    ) -> None:
        super().__init__()
        self._lo = hard_lo
        self._hi = hard_hi
        self._margin = safety_margin
        self._v: float | None = None
        self._target: float | None = None
        self._dragging: bool = False
        self.setMinimumHeight(34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_voltage(self, v: float | None) -> None:
        if v != self._v:
            self._v = v
            self.update()

    def set_target(self, v: float | None) -> None:
        """External setter — Stop / +/− / trip use this to clear the
        target (passing None). Emits ``target_cleared`` on None,
        ``target_changed`` otherwise. Idempotent."""
        if v == self._target:
            return
        self._target = v
        self.update()
        if v is None:
            self.target_cleared.emit()
        else:
            self.target_changed.emit(v)

    @property
    def target(self) -> float | None:
        return self._target

    @property
    def is_dragging(self) -> bool:
        return self._dragging

    def _x_to_v(self, x: float) -> float:
        pad_x = 18.0
        x_lo = pad_x
        x_hi = self.width() - pad_x
        span = self._hi - self._lo
        if x_hi <= x_lo:
            return self._lo
        return self._lo + (x - x_lo) / (x_hi - x_lo) * span

    def _clamp_target(self, v: float) -> float:
        # Clamp to soft limits — predictive trip would catch anything
        # past, so don't let the operator command into that zone.
        soft_lo = self._lo + self._margin
        soft_hi = self._hi - self._margin
        return max(soft_lo, min(soft_hi, v))

    def mousePressEvent(self, ev) -> None:  # noqa: N802 — Qt override
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = True
        v = self._clamp_target(self._x_to_v(ev.position().x()))
        self._target = v
        self.target_changed.emit(v)
        self.update()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802 — Qt override
        if not self._dragging:
            return
        v = self._clamp_target(self._x_to_v(ev.position().x()))
        if v != self._target:
            self._target = v
            self.target_changed.emit(v)
            self.update()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802 — Qt override
        if not self._dragging or ev.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = False
        self.drag_released.emit()
        self.update()  # repaint to switch ghost → locked target style

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        pad_x = 18.0
        bar_y = h * 0.40
        x_lo = pad_x
        x_hi = w - pad_x
        span = self._hi - self._lo

        def v_to_x(v: float) -> float:
            return x_lo + (v - self._lo) / span * (x_hi - x_lo)

        # Axis line
        p.setPen(QPen(QColor(self._AXIS), 1))
        p.drawLine(QPointF(x_lo, bar_y), QPointF(x_hi, bar_y))

        # Ticks + labels at lo, 0, hi
        font = QFont(self.font())
        font.setPointSize(8)
        p.setFont(font)
        fm = QFontMetrics(font)
        tick_half = 4.0
        for v in (self._lo, 0.0, self._hi):
            x = v_to_x(v)
            p.drawLine(
                QPointF(x, bar_y - tick_half),
                QPointF(x, bar_y + tick_half),
            )
            label = f"{v:+.0f} V" if v != 0 else "0 V"
            tw = fm.horizontalAdvance(label)
            p.drawText(QPointF(x - tw / 2, bar_y + 18), label)

        # Target marker (hollow ring) — paint under the V marker so the
        # current position is always on top when they overlap.
        if self._target is not None:
            x = v_to_x(self._target)
            color = QColor(self._TARGET_DRAG if self._dragging else self._TARGET)
            p.setPen(QPen(color, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(x, bar_y), 7.0, 7.0)

        # Position marker (filled, current V)
        if self._v is not None:
            v_clamped = max(self._lo, min(self._hi, self._v))
            x = v_to_x(v_clamped)
            soft_lo = self._lo + self._margin
            soft_hi = self._hi - self._margin
            color = QColor(
                self._MARKER_WARN if (v_clamped < soft_lo or v_clamped > soft_hi)
                else self._MARKER
            )
            p.setBrush(color)
            p.setPen(QPen(color))
            p.drawEllipse(QPointF(x, bar_y), 5.0, 5.0)


class _CurrentTickStrip(QWidget):
    """Tick-mark strip painted under the current slider (log axis).

    "0" label at the far left where raw 0 == 0 µA, then the operator-
    preferred values (0.5, 1.0, 2.5, 5.0 µA) at their log positions.
    Padding matches the QSlider handle inset so tick centers line up
    with handle positions.
    """

    _AXIS = "#888"

    def __init__(
        self, ticks: tuple[float, ...], min_ua: float, max_ua: float,
    ) -> None:
        super().__init__()
        self._ticks = ticks
        self._log_min = math.log10(min_ua)
        self._log_max = math.log10(max_ua)
        self._log_range = self._log_max - self._log_min
        self.setMinimumHeight(16)

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        # QSlider default handle ≈ 18 px wide; the groove insets by half
        # the handle on each side. 9 px matches the handle center range
        # so tick labels sit under the values the handle can actually
        # reach. If a custom QStyle changes the handle width this drifts
        # by a few px — visually fine.
        pad = 9.0
        x_lo = pad
        x_hi = w - pad
        font = QFont(self.font())
        font.setPointSize(8)
        p.setFont(font)
        fm = QFontMetrics(font)
        p.setPen(QPen(QColor(self._AXIS), 1))
        # "0" label at the far left — raw 0 is special-cased in
        # _CurrentSlider as the off-state position.
        p.drawLine(QPointF(x_lo, 0), QPointF(x_lo, 4))
        p.drawText(QPointF(x_lo - fm.horizontalAdvance("0") / 2, 14), "0")
        for tick in self._ticks:
            frac = (math.log10(tick) - self._log_min) / self._log_range
            x = x_lo + frac * (x_hi - x_lo)
            p.drawLine(QPointF(x, 0), QPointF(x, 4))
            label = f"{tick:g}"
            tw = fm.horizontalAdvance(label)
            p.drawText(QPointF(x - tw / 2, 14), label)


class _CurrentSlider(QWidget):
    """Horizontal log-scale slider over [0, max_ua] with sticky ticks.

    Raw range [0, _STEPS]:
      - raw 0 → 0 µA (special "off" position; matches the original
        spinbox-at-0 startup behavior — +/− with the slider here is a
        noop).
      - raw 1..N → log10-spaced over [MIN_NONZERO_UA, max_ua].

    Snap-on-drag at the named ticks (0.5/1.0/2.5/5.0 µA) within
    SNAP_TOLERANCE_DECADES on the log axis (≈ ±10 % of the tick value,
    symmetric in log space). Snap windows don't overlap between ticks.
    """

    TICKS_UA: tuple[float, ...] = (0.5, 1.0, 2.5, 5.0)
    MIN_NONZERO_UA = 0.05  # left edge of log axis (raw 1)
    SNAP_TOLERANCE_DECADES = 0.04  # ≈ ±10 % of tick value
    _STEPS = 1000

    valueChanged = Signal(float)

    def __init__(self, max_ua: float) -> None:
        super().__init__()
        self._max_ua = max_ua
        self._log_min = math.log10(self.MIN_NONZERO_UA)
        self._log_max = math.log10(max_ua)
        self._log_range = self._log_max - self._log_min
        ticks_in_range = tuple(t for t in self.TICKS_UA if t <= max_ua)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, self._STEPS)
        self._slider.setSingleStep(1)
        self._slider.setPageStep(50)
        # ClickFocus — same rationale as the prior spinbox: keyboard
        # adjustments require an explicit click; auto-focus-routing
        # from elsewhere won't land here and start eating arrow keys.
        self._slider.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._slider.valueChanged.connect(self._on_raw_changed)

        self._tick_strip = _CurrentTickStrip(
            ticks_in_range, self.MIN_NONZERO_UA, max_ua,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._slider)
        layout.addWidget(self._tick_strip)

    def _raw_to_ua(self, raw: int) -> float:
        if raw <= 0:
            return 0.0
        frac = (raw - 1) / (self._STEPS - 1)
        return 10 ** (self._log_min + frac * self._log_range)

    def _ua_to_raw(self, ua: float) -> int:
        if ua <= 0:
            return 0
        ua_c = max(self.MIN_NONZERO_UA, min(self._max_ua, ua))
        frac = (math.log10(ua_c) - self._log_min) / self._log_range
        return 1 + int(round(frac * (self._STEPS - 1)))

    def _on_raw_changed(self, raw: int) -> None:
        ua = self._raw_to_ua(raw)
        if ua > 0:
            log_ua = math.log10(ua)
            for tick in self.TICKS_UA:
                if abs(log_ua - math.log10(tick)) <= self.SNAP_TOLERANCE_DECADES:
                    snapped_raw = self._ua_to_raw(tick)
                    if snapped_raw != raw:
                        self._slider.blockSignals(True)
                        self._slider.setValue(snapped_raw)
                        self._slider.blockSignals(False)
                    ua = tick
                    break
        self.valueChanged.emit(ua)

    def value(self) -> float:
        return self._raw_to_ua(self._slider.value())

    def setValue(self, ua: float) -> None:  # noqa: N802 — Qt naming
        self._slider.setValue(self._ua_to_raw(ua))


class YokoPanel(QGroupBox):
    """Live readout + operator-driven CC source for the Yoko 7651 + NPM140."""

    DEFAULT_POLL_MS = 1000

    def __init__(self, cfg: dict) -> None:
        super().__init__("Yoko (fine Z)")

        # --- config ---------------------------------------------------------
        self._yoko_poll_s = (
            int(cfg.get("poll_interval_ms", self.DEFAULT_POLL_MS)) / 1000.0
        )
        self._nm_per_volt = float(cfg.get("nm_per_volt", DEFAULT_NM_PER_VOLT))
        self._C = float(cfg.get("piezo_capacitance_uf", DEFAULT_CAPACITANCE_UF)) * 1e-6
        self._default_speed_a = float(cfg.get("default_speed_a", DEFAULT_SPEED_A))
        self._max_speed_a = float(cfg.get("max_speed_a", DEFAULT_MAX_SPEED_A))
        self._margin_v = float(cfg.get("safety_margin_v", DEFAULT_SAFETY_MARGIN_V))
        self._stale_s = int(cfg.get("move_stale_ms", DEFAULT_STALE_MS)) / 1000.0

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

        # 617 is required for this CC panel — without V feedback there's
        # no predictive safety. If the config doesn't carry a
        # [yoko.keithley617] sub-block we degrade to display-only.
        k617_cfg = cfg.get("keithley617")
        if k617_cfg:
            self.keithley = Keithley617(resource=k617_cfg["resource"])
            self._k617_poll_s = int(k617_cfg.get("poll_interval_ms", 0)) / 1000.0
        else:
            self.keithley = None
            self._k617_poll_s = 0.0

        # --- state ----------------------------------------------------------
        self._latest = _LatestReading()
        self._tripped: bool = False
        self._trip_reason: str | None = None
        self._cache_seeded = False  # for the Yoko I cache
        # Last commanded current. Read from the watchdog thread by
        # _get_commanded_current for the predictive trip. Plain float
        # reads are atomic under the GIL; the watchdog could observe
        # a one-cycle-old value across a GUI-thread update, which is
        # fine because the hard trip is the backstop. ``!= 0`` is the
        # canonical "we're asking the Yoko to source" check (Stop
        # button enable, status label, shutdown step 1).
        self._i_commanded: float = 0.0
        # Target-mode setpoint. While not None, the controller (see
        # _controller_tick) drives V toward this value at every fresh
        # 617 reading. Set by drag on travel_bar; cleared by Stop /
        # +/− / trip / shutdown.
        self._target_v: float | None = None
        # During shutdown, _drive_to_zero_for_shutdown reuses the same
        # target controller but overrides the slider cap with
        # default_speed_a (predictable wind-down speed regardless of
        # whatever the slider was last set to).
        self._shutdown_active: bool = False
        # Surface instrument problems (wrong mode, comm error, overflow)
        # in the main status_label rather than a dedicated row. None = OK.
        self._k617_issue: str | None = None
        self._yoko_issue: str | None = None

        self._yoko_poll_worker: _YokoPollWorker | None = None
        self._yoko_poll_thread: QThread | None = None
        self._k617_worker: _KeithleyWatchdogWorker | None = None
        self._k617_thread: QThread | None = None

        # --- widgets --------------------------------------------------------
        self.status_label = QLabel("disconnected")
        self.trip_label = QLabel("")
        self.trip_label.setStyleSheet(
            "color: white; background-color: #c0392b; "
            "font-weight: bold; padding: 4px 8px;"
        )
        self.trip_label.setVisible(False)
        self.trip_ack_btn = action_button("Acknowledge")
        self.trip_ack_btn.setVisible(False)

        self.voltage_label = QLabel("—")
        v_font = QFont(self.voltage_label.font())
        v_font.setPointSize(v_font.pointSize() + 6)
        v_font.setStyleHint(QFont.StyleHint.Monospace)
        v_font.setFamily("monospace")
        self.voltage_label.setFont(v_font)
        self.travel_label = QLabel("")
        self.travel_label.setStyleSheet("color: #888; font-size: 8pt;")

        # Implied dV/dt from the Yoko OD poll. Doubles as a "source is
        # alive" indicator — empty when output is off.
        self.dvdt_label = QLabel("")
        self.dvdt_label.setStyleSheet("color: #888;")

        # Horizontal travel scale: -20 V / 0 / +30 V (or whatever
        # voltage_limits says) with a marker at the live V.
        self.travel_bar = _TravelBar(
            self._hard_limits[0], self._hard_limits[1], self._margin_v
        )


        # Unsigned current magnitude in µA. Sign comes from which
        # direction button the operator presses (+ vs −); the slider
        # itself never goes negative. Default 0 so a stray + or −
        # click at panel-open is a noop.
        self.current_slider = _CurrentSlider(max_ua=self._max_speed_a * 1e6)
        self.current_readout = QLabel("0.000 µA")
        readout_font = QFont(self.current_readout.font())
        readout_font.setStyleHint(QFont.StyleHint.Monospace)
        readout_font.setFamily("monospace")
        self.current_readout.setFont(readout_font)
        self.current_slider.valueChanged.connect(
            lambda ua: self.current_readout.setText(f"{ua:.3f} µA")
        )

        # Direction buttons: + sources +I (V rises), − sources −I (V
        # falls). Clicking the opposite direction while sourcing flips
        # I without needing to Stop first — live reversal.
        self.up_btn = action_button("+")
        self.down_btn = action_button("−")

        # Stop — set_current(0). Live only while sourcing; greyed
        # otherwise (see _refresh_controls).
        self.stop_btn = action_button("Stop")

        self.enable_btn = action_button("Enable")

        self._build_layout()
        self._wire_signals()

        # --- bring up the hardware -----------------------------------------
        try:
            self.yoko.open()
        except _YOKO_ERRORS as e:
            self.status_label.setText(f"yoko open failed: {e}")
            self._set_controls_enabled(False)
            return

        # Put the unit in current mode. set_mode("A") sends F5;E;. The
        # panel only allows this transition with the output already off
        # in normal operation; at startup we defer the safety call to
        # the watchdog once 617 is up.
        try:
            self.yoko.set_mode("A")
        except _YOKO_ERRORS as e:
            log.warning("yoko set_mode('A') failed at open: %s", e)

        # Infer "is output on" from the first OD; reading. In I mode,
        # OD; reads the actual sourced current — anything above the
        # noise floor implies the relay is closed.
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

        # 617 poll + watchdog.
        if self.keithley is not None:
            try:
                self.keithley.open()
            except _K617_ERRORS as e:
                log.warning("keithley617 open failed: %s", e)
                self._k617_issue = f"617 open failed: {e}"
                self.keithley = None

        if self.keithley is not None:
            self._k617_thread = QThread()
            self._k617_worker = _KeithleyWatchdogWorker(
                self.keithley,
                self.yoko,
                self._latest,
                self._hard_limits,
                self._soft_limits[0],
                self._C,
                self._k617_poll_s,
                self._get_commanded_current,
            )
            self._k617_worker.moveToThread(self._k617_thread)
            self._k617_thread.started.connect(self._k617_worker.run)
            self._k617_worker.state_ready.connect(self._apply_k617_state)
            self._k617_worker.tripped.connect(self._on_tripped)
            self._k617_worker.finished.connect(self._k617_thread.quit)
            self._k617_thread.start()

        self._refresh_controls()
        self._refresh_status_label()

    # --- layout / wiring ----------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.enable_btn)
        outer.addLayout(status_row)

        trip_row = QHBoxLayout()
        trip_row.addWidget(self.trip_label, stretch=1)
        trip_row.addWidget(self.trip_ack_btn)
        outer.addLayout(trip_row)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep1)

        outer.addWidget(self.voltage_label)
        sub_row = QHBoxLayout()
        sub_row.addWidget(self.travel_label)
        sub_row.addStretch(1)
        sub_row.addWidget(self.dvdt_label)
        outer.addLayout(sub_row)
        outer.addWidget(self.travel_bar)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep2)

        current_header = QHBoxLayout()
        current_header.addWidget(QLabel("Current:"))
        current_header.addStretch(1)
        current_header.addWidget(self.current_readout)
        outer.addLayout(current_header)
        outer.addWidget(self.current_slider)

        button_row = QHBoxLayout()
        button_row.addWidget(self.up_btn, stretch=1)
        button_row.addWidget(self.down_btn, stretch=1)
        button_row.addWidget(self.stop_btn, stretch=1)
        outer.addLayout(button_row)

        outer.addStretch(1)

    def _wire_signals(self) -> None:
        self.up_btn.clicked.connect(lambda: self._on_source(+1))
        self.down_btn.clicked.connect(lambda: self._on_source(-1))
        self.stop_btn.clicked.connect(self._on_stop)
        self.enable_btn.clicked.connect(self._on_enable)
        self.trip_ack_btn.clicked.connect(self._on_trip_ack)
        self.travel_bar.target_changed.connect(self._on_target_changed)
        self.travel_bar.target_cleared.connect(self._on_target_cleared)
        self.travel_bar.drag_released.connect(self._on_drag_released)

    # --- helpers ------------------------------------------------------------

    def _latest_v(self) -> float | None:
        """GUI-thread snapshot of the latest 617 V."""
        v, func, age = self._latest.read()
        if v is None or func != "DCV" or age > self._stale_s:
            return None
        return v

    def _ready_to_source(self) -> bool:
        """True iff we can safely command the source on (Enable/Start).

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

    def _get_commanded_current(self) -> float:
        """Snapshot of the last-commanded I for the watchdog's predictive
        trip. Called from the watchdog thread, so keep it cheap and
        lock-free — plain attribute read is atomic under the GIL."""
        return self._i_commanded

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

    def _on_source(self, direction: int) -> None:
        """Apply current at the slider's magnitude with the given sign.

        Called by the + and − buttons. Clicking the opposite direction
        while sourcing flips I without needing Stop first.
        """
        if not self._ready_to_source():
            self.status_label.setText(
                "can't source — no fresh 617 DCV reading inside hard limits"
            )
            return
        if self.yoko.cached_output_on is not True:
            self.status_label.setText("can't source — output is OFF (Enable first)")
            return
        magnitude_a = self.current_slider.value() * 1e-6
        # Refuse zero magnitude — sourcing 0 µA is a no-op that would
        # leave _i_commanded == 0 (i.e., "not sourcing") with the UI
        # claiming we are. Use Stop for the explicit-halt semantic.
        if magnitude_a == 0.0:
            self.status_label.setText(
                "refused: set a magnitude on the slider first"
            )
            return
        i_a = direction * magnitude_a
        # Refuse up-front if the requested I would immediately trip
        # the predictive floor — better UX than letting the watchdog
        # fire on the next cycle and forcing an Acknowledge.
        v = self._latest_v()
        if v is not None and i_a < 0:
            v_pred = _predict_v_one_cycle(v, i_a, self._C)
            if v_pred < self._soft_limits[0]:
                self.status_label.setText(
                    f"refused: −{magnitude_a*1e6:.2f}µA from V={v:+.3f} would "
                    f"breach floor (v_pred={v_pred:+.3f}); pick a smaller |I|"
                )
                return
        # Cancel any active target controller — +/− is fire-and-forget
        # constant-I. Clearing first so a 617 tick between here and
        # set_current can't re-fight the new direction.
        if self._target_v is not None:
            self.travel_bar.set_target(None)
        self._safe(self.yoko.set_current, i_a)
        self._i_commanded = i_a
        self.status_label.setText(f"sourcing {i_a*1e6:+.3f} µA")
        log.info("yoko: source I=%+.3fµA  V=%s", i_a * 1e6,
                 f"{v:+.3f}" if v is not None else "?")
        self._refresh_controls()

    def _on_stop(self) -> None:
        # Always send set_current(0), regardless of _i_commanded — Stop
        # is the universal halt and should be effective even if the
        # cached state has drifted out of sync.
        self._safe(self.yoko.set_current, 0.0)
        self._i_commanded = 0.0
        # Stop also tears down any active target controller.
        if self._target_v is not None:
            self.travel_bar.set_target(None)
        log.info("yoko: stop (I=0)")
        self._refresh_status_label()
        self._refresh_controls()

    def _on_trip_ack(self) -> None:
        self._tripped = False
        self._trip_reason = None
        self._i_commanded = 0.0
        self.trip_label.setVisible(False)
        self.trip_ack_btn.setVisible(False)
        if self._k617_worker is not None:
            self._k617_worker.acknowledge()
        self._refresh_status_label()
        self._refresh_controls()

    @Slot(object)
    def _on_tripped(self, payload: dict) -> None:
        reason = payload.get("reason", "unknown")
        log.error("yoko panel tripped: %s", reason)
        self._tripped = True
        self._trip_reason = reason
        # Watchdog already set I=0 on the bus; mirror in cached state
        # so a post-Acknowledge Start begins from a clean slate.
        self._i_commanded = 0.0
        # Drop any active target so the post-Ack controller doesn't
        # immediately re-engage on whatever the operator was aiming at.
        if self._target_v is not None:
            self.travel_bar.set_target(None)
        self.trip_label.setText(f"TRIPPED — {reason}")
        self.trip_label.setVisible(True)
        self.trip_ack_btn.setVisible(True)
        self._refresh_controls()

    # --- travel-bar drag handlers ------------------------------------------

    def _on_target_changed(self, v: float) -> None:
        """Operator dragged on the travel bar or external set_target.

        Engages the controller on the next 617 tick. Same handler whether
        the drag is in flight (Tracking) or already released (set_target
        from code) — controller doesn't care.
        """
        self._target_v = v
        self._refresh_status_label()
        self._refresh_controls()

    def _on_target_cleared(self) -> None:
        """Travel bar's target was cleared (by external set_target(None)).

        The matching set_current(0) is the caller's responsibility — this
        slot only tears down the controller's setpoint.
        """
        self._target_v = None
        self._refresh_status_label()
        self._refresh_controls()

    def _on_drag_released(self) -> None:
        """Mouse-up after a drag. Target persists into Holding state —
        controller keeps running. Just a status refresh to flip the
        verb from 'Tracking' to 'Holding'.
        """
        self._refresh_status_label()

    # --- state refresh ------------------------------------------------------

    def _refresh_status_label(self) -> None:
        if self._tripped:
            self.status_label.setText("TRIPPED — see banner")
            return
        if self.keithley is None:
            self.status_label.setText(
                self._k617_issue or self._yoko_issue or "no 617 — display only"
            )
            return
        # Instrument problems take precedence over routine state — if
        # the 617 is in DCA or the Yoko is in V mode, surface that
        # rather than the "sourcing X µA" line. Pipe-separate when both.
        if self._k617_issue or self._yoko_issue:
            parts = [s for s in (self._k617_issue, self._yoko_issue) if s]
            self.status_label.setText(" · ".join(parts))
            return
        # Target mode (Tracking/Holding) outranks constant-I — the
        # controller is in charge of i_commanded in this mode.
        if self._target_v is not None:
            v_now = self._latest_v()
            err_txt = (
                f"err {v_now - self._target_v:+.2f} V"
                if v_now is not None
                else "err ?"
            )
            ready = self._ready_to_source() and self.yoko.cached_output_on is True
            if not ready:
                self.status_label.setText(
                    f"target {self._target_v:+.2f} V — Enable to engage"
                )
                return
            if self.current_slider.value() <= 0:
                self.status_label.setText(
                    f"target {self._target_v:+.2f} V — set max speed on slider"
                )
                return
            verb = "Tracking" if self.travel_bar.is_dragging else "Holding"
            self.status_label.setText(
                f"{verb} {self._target_v:+.2f} V · {err_txt}"
            )
            return
        if self._i_commanded != 0.0:
            self.status_label.setText(
                f"sourcing {self._i_commanded*1e6:+.3f} µA"
            )
            return
        state = self.yoko.cached_output_on
        if state is None:
            self.status_label.setText("output state unknown")
        elif state:
            self.status_label.setText("OUTPUT ON (idle)")
        else:
            self.status_label.setText("OUTPUT OFF")

    def _refresh_controls(self) -> None:
        """Sync Enable / +/− / Stop enabled state. Stop is live iff there's
        something to halt — either constant-I sourcing or an active
        target controller. Acknowledge always live (visibility controlled
        separately)."""
        # Stop is only meaningful when there's something to halt:
        # either we're sourcing a non-zero I, or a target controller
        # is engaged (which would otherwise keep ticking on its own).
        # _on_tripped clears both _i_commanded and _target_v, so this
        # also disables Stop in the tripped state.
        self.stop_btn.setEnabled(
            self._i_commanded != 0.0 or self._target_v is not None
        )

        if self._tripped:
            self.enable_btn.setEnabled(False)
            self.up_btn.setEnabled(False)
            self.down_btn.setEnabled(False)
            return

        ready = self._ready_to_source()
        state = self.yoko.cached_output_on

        if not ready or state is None:
            # 617 missing / not in DCV / stale → can't enable, can't source.
            self.enable_btn.setEnabled(False)
            self.up_btn.setEnabled(False)
            self.down_btn.setEnabled(False)
            return

        if state:
            # Output on. Enable redundant; both direction buttons live
            # so the operator can re-apply with a new magnitude or
            # flip direction without going through Stop first.
            self.enable_btn.setEnabled(False)
            self.up_btn.setEnabled(True)
            self.down_btn.setEnabled(True)
        else:
            # Output off. Need Enable before sourcing makes sense.
            self.enable_btn.setEnabled(True)
            self.up_btn.setEnabled(False)
            self.down_btn.setEnabled(False)

    def _safe(self, fn, *args) -> None:
        name = fn.__name__
        log.info("yoko: %s(%s)", name, args if args else "")
        try:
            fn(*args)
        except _YOKO_ERRORS as e:
            log.exception("yoko: %s failed", name)
            self.status_label.setText(f"err: {e}")

    def _set_controls_enabled(self, enabled: bool) -> None:
        """Used at startup when open() failed."""
        for w in (
            self.current_slider,
            self.up_btn,
            self.down_btn,
            self.enable_btn,
        ):
            w.setEnabled(enabled)

    # --- poll-thread payload handlers ---------------------------------------

    @Slot(object)
    def _apply_yoko_state(self, payload: dict) -> None:
        if "_worker_err" in payload:
            self._yoko_issue = f"yoko poll err: {payload['_worker_err']}"
            self._refresh_status_label()
            return
        if "od_err" in payload:
            self._yoko_issue = f"yoko OD err: {payload['od_err']}"
            self._refresh_status_label()
            return

        value = payload["value"]
        function = payload["function"]
        status = payload["status"]

        issue: str | None = None
        if function == "A":
            # Implied piezo dV/dt = I / C. Small enough that the
            # operator can sanity-check the slider value visually.
            dvdt = value / self._C
            self.dvdt_label.setText(f"≈ {dvdt:+.3f} V/s on {self._C * 1e6:.1f} µF")
            if not self._cache_seeded:
                self.yoko.seed_output_cache(abs(value) > OUTPUT_ON_THRESHOLD_A)
                self._cache_seeded = True
                self._refresh_controls()
        else:
            # Yoko in V mode unexpectedly (e.g. someone hit the
            # front-panel function key). Flag it so +/− greying is
            # explained in the status line.
            self.dvdt_label.setText("")
            issue = f"yoko in {function} mode (expected A)"

        if status == "E":
            issue = (issue + " · OVERLOAD") if issue else "yoko OVERLOAD"

        self._yoko_issue = issue
        self._refresh_status_label()

    def _controller_tick(self, v: float) -> None:
        """Run one cycle of the target-mode P-law controller.

        Called from _apply_k617_state on every fresh DCV reading. The
        controller is the same in Tracking and Holding states — in
        Tracking, _target_v is being updated live by the bar drag; in
        Holding, _target_v is fixed and the controller drives drift
        correction. Gain K = C / τ — picked so an unclipped command
        would zero the error in one Yoko settling time. The slider
        value caps |i_cmd|; the cap applies in both states.

        Quantizes i_cmd to YOKO_I_RESOLUTION_A — sub-step values round
        to 0, giving a natural deadband (~15 mV of |err| at K = C/τ
        into 2 µF). Also gates on the same predictive-floor check
        +/− uses, so the controller can't deepen toward the floor.
        """
        if self._tripped or self._target_v is None:
            return
        if not self._ready_to_source():
            return
        if self.yoko.cached_output_on is not True:
            return  # output off — controller has no authority

        error = self._target_v - v
        K = self._C / YOKO_SETTLE_S
        i_desired = K * error
        if self._shutdown_active:
            # Shutdown override — predictable wind-down speed
            # regardless of slider position. The slider can't disable
            # the shutdown ramp.
            i_max = self._default_speed_a
        else:
            i_max = self.current_slider.value() * 1e-6
        if i_max <= 0:
            # Operator hasn't picked a max speed. Status label still
            # shows the target; controller idles until the slider
            # moves off zero.
            i_cmd = 0.0
        else:
            i_cmd = max(-i_max, min(i_max, i_desired))
        # Predictive-floor prophylactic — same check +/− does.
        if i_cmd < 0 and _predict_v_one_cycle(v, i_cmd, self._C) < self._soft_limits[0]:
            i_cmd = 0.0
        # Quantize to Yoko resolution — keeps sub-step changes off
        # the bus and gives a natural deadband from the floor.
        i_cmd = round(i_cmd / YOKO_I_RESOLUTION_A) * YOKO_I_RESOLUTION_A
        if i_cmd == self._i_commanded:
            return
        self._safe(self.yoko.set_current, i_cmd)
        self._i_commanded = i_cmd

    @Slot(object)
    def _apply_k617_state(self, payload: dict) -> None:
        if "_worker_err" in payload:
            self._k617_issue = f"617 poll err: {payload['_worker_err']}"
            self.travel_bar.set_voltage(None)
            self._refresh_status_label()
            return
        if "k617_err" in payload:
            self.voltage_label.setText("—")
            self.travel_bar.set_voltage(None)
            self._k617_issue = f"617 err: {payload['k617_err']}"
            self._refresh_status_label()
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
            self.travel_bar.set_voltage(value)
            # Drive the controller off the freshest reading we have.
            # Runs on GUI thread; Yoko._lock serializes any conflict
            # with the watchdog's set_current(0) on trip.
            self._controller_tick(value)
            issue: str | None = None
        else:
            # Wrong mode — still show reading but flag the problem so
            # the operator knows why +/− are greyed.
            self.voltage_label.setText(format_engineering(value, unit))
            self.travel_label.setText("")
            self.travel_bar.set_voltage(None)
            issue = f"617 in {function} (need DCV for control)"

        if status != "N":
            issue = (issue + " · OVERFLOW") if issue else "617 · OVERFLOW"

        self._k617_issue = issue
        self._refresh_status_label()
        self._refresh_controls()

    # --- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Safe-shutdown protocol: leave the piezo at V ≈ 0 V, I = 0 A, relay open.

        Sequence:
          1. If sourcing constant-I, drop I to 0.
          2. Drive V → 0 by the target controller (same K = C/τ law as
             runtime target-mode), with the slider cap overridden by
             default_speed_a for a predictable wind-down. Skipped if V
             is already near 0 or we don't have 617 feedback.
          3. safe_disable_current — SA0 + O0.
          4. Stop watchdog + Yoko-poll threads, close instruments.

        Returns synchronously: the GUI thread blocks on a nested
        QEventLoop in step 2, but events keep flowing — the controller
        runs off 617 ticks as usual and a QTimer polls the settle/dialog.
        """
        # 1. Stop sourcing if we were.
        if self._i_commanded != 0.0:
            try:
                self.yoko.set_current(0.0)
            except _YOKO_ERRORS as e:
                log.warning("shutdown: set_current(0) failed: %s", e)
        self._i_commanded = 0.0

        # 2. Drive V → 0 via the controller. Skipped when output is
        # already off, when we're tripped (operator acknowledges to
        # recover), or when 617 isn't usable. The modal dialog has no
        # cancel button: aborting mid-ramp leaves the piezo at a
        # random V which is exactly what we're trying to avoid.
        if (
            self.yoko.is_open
            and self.yoko.cached_output_on
            and not self._tripped
            and self._k617_worker is not None
        ):
            v_start = self._latest_v()
            if v_start is not None and abs(v_start) >= 0.5:
                self._drive_to_zero_for_shutdown(v_start)

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

    def _drive_to_zero_for_shutdown(self, v_start: float) -> None:
        """Drive V → 0 via the target controller, with a modal dialog.

        Reuses the runtime target controller — same K = C/τ proportional
        law, same predictive-floor check, same 617-tick cadence. The
        only differences vs operator-driven target mode:

          * ``_shutdown_active`` makes the controller use
            ``default_speed_a`` as i_max instead of the slider.
          * A QTimer polls the settle condition (``|v| < 50 mV`` AND
            ``i_commanded == 0``) and updates the dialog. The poll
            quits the nested event loop on settle or deadline.

        Belt-and-suspenders cleanup: forces I=0 + clears the target
        after the loop exits, regardless of how it exited, so step 3
        of ``shutdown`` opens the relay onto a quiescent source.
        """
        log.info(
            "shutdown: drive V=%+.3f → 0  i_max=%.2fµA  K=C/τ=%.2fµA/V",
            v_start, self._default_speed_a * 1e6,
            (self._C / YOKO_SETTLE_S) * 1e6,
        )

        self._shutdown_active = True
        # set_target() emits target_changed → _on_target_changed sets
        # self._target_v = 0.0, engaging the controller on the next
        # 617 tick. Overwrites any operator-set target (intentional —
        # shutdown means go to 0 regardless of prior intent).
        self.travel_bar.set_target(0.0)

        dlg = QProgressDialog(
            f"Ramping V from {v_start:+.2f} V → 0 V…\n"
            "(Yoko 7651 + NPM140 — please wait)",
            "",  # cancel text (suppressed via setCancelButton below)
            0, 100,
            self.window(),
        )
        dlg.setWindowTitle("Shutting down Yoko")
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumDuration(0)  # show immediately
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setCancelButton(None)
        dlg.setValue(0)
        dlg.show()

        loop = QEventLoop()
        deadline = time.monotonic() + 60.0
        tolerance_v = 0.05
        v_start_abs = abs(v_start)

        def _poll() -> None:
            v = self._latest_v()
            if v is not None:
                progress = max(0, min(100, int(
                    100 * (1.0 - abs(v) / v_start_abs)
                )))
                dlg.setValue(progress)
                dlg.setLabelText(
                    f"Ramping V → 0  ·  now {v:+.3f} V  "
                    f"(started {v_start:+.2f} V)"
                )
            if (
                v is not None
                and abs(v) < tolerance_v
                and self._i_commanded == 0.0
            ):
                log.info("shutdown: settled at v=%+.3f V", v)
                loop.quit()
                return
            if time.monotonic() > deadline:
                log.warning(
                    "shutdown: deadline reached at v=%s",
                    f"{v:+.3f}" if v is not None else "?",
                )
                loop.quit()
                return

        poll_timer = QTimer()
        poll_timer.setInterval(200)
        poll_timer.timeout.connect(_poll)
        poll_timer.start()
        loop.exec()
        poll_timer.stop()

        # Teardown. Clear the controller target + force I=0 so step 3
        # opens the relay onto a quiescent source (the controller
        # could otherwise have just commanded a small non-zero I).
        self.travel_bar.set_target(None)
        self._shutdown_active = False
        try:
            self.yoko.set_current(0.0)
        except _YOKO_ERRORS as e:
            log.warning("shutdown: final set_current(0) failed: %s", e)
        self._i_commanded = 0.0
        dlg.close()
