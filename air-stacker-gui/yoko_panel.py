"""PySide6 panel for the Yokogawa 7651 driving the NPM140 fine-Z piezo
as an operator-driven constant-current source.

Operator workflow: type a signed current in the Current spinbox, press
Start to source it, watch the live V trace from the Keithley 617, press
Stop to drop I to 0. There is no closed-loop "move to target voltage"
— the operator IS the loop. This sidesteps the Yoko's ~0.6 s output
settling lag and the NPM140's nonlinear V-dependent leakage, both of
which made the prior auto-Move unstable. See
YOKO_CONTROL_INVESTIGATION.md for the diagnostic series.

Safety: the NPM140's −20 V hard floor has no hardware-side protection
(the Yoko can swing to −30 V). Two safety layers run on the 617 poll
thread:
  - **Hard trip** (reactive): V already outside [hard_lo, hard_hi].
    Always armed.
  - **Predictive trip** (proactive): only fires while sourcing — if
    ``v + i_commanded · (dt + slack) / C`` would cross the soft
    floor, kill the source one cycle early. This is the working
    envelope; the operator hits it before the hard trip ever fires.
Both call ``set_current(0)`` and latch a "TRIPPED" state the operator
must Acknowledge. Start is gated on a fresh DCV reading inside hard
limits, so a 617 comm loss can't trick us into sourcing blind.

Expected ``cfg`` keys — see ``config.toml`` for the documented defaults.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
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
DEFAULT_CAPACITANCE_UF = 1.7
DEFAULT_SPEED_A = 5e-6
DEFAULT_MAX_SPEED_A = 20e-6
DEFAULT_SAFETY_MARGIN_V = 1.5
DEFAULT_STALE_MS = 1000

# 617 analog-front-end integration window. Sets the lookahead floor
# for the predictive trip (v_pred = v + i · (dt + slack) / C).
KEITHLEY_CONVERSION_S = 0.333

# Headroom on top of one 617 conversion cycle for the predictive trip.
# Covers _latest staleness + scheduling jitter between the watchdog
# read and a GUI-thread Start.
PREDICT_SLACK_S = 0.1

# "Output is on" inference threshold on the Yoko's OD; reading in I
# mode. With the relay open the unit reads near 0 A regardless of the
# programmed current; anything above this magnitude is definitely
# sourcing into a load.
OUTPUT_ON_THRESHOLD_A = 10e-9  # 10 nA


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
        get_source_state: Callable[[], tuple[bool, float]],
    ) -> None:
        super().__init__()
        self._k = keithley
        self._yoko = yoko
        self._latest = latest
        self._hard_lo, self._hard_hi = hard_limits
        self._soft_floor = soft_floor
        self._C = capacitance_f
        self._interval = poll_interval_s
        self._get_source_state = get_source_state
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
                            sourcing, i_cmd = self._get_source_state()
                            if sourcing and i_cmd < 0:
                                lookahead = KEITHLEY_CONVERSION_S + PREDICT_SLACK_S
                                v_pred = r.value + i_cmd * lookahead / self._C
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
        # Sourcing state — read from the watchdog thread by
        # _get_source_state for the predictive trip. Plain attribute
        # reads are atomic under the GIL; the watchdog could observe a
        # one-cycle-old (sourcing, i) pair across a GUI-thread update,
        # which is fine because the hard trip is the backstop.
        self._sourcing: bool = False
        self._i_commanded: float = 0.0
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


        # Unsigned current magnitude in µA. Sign comes from which
        # direction button the operator presses (+ vs −); the spinbox
        # itself never goes negative. Default 0 so a stray + or −
        # click at panel-open is a noop.
        self.current_spin = QDoubleSpinBox()
        self.current_spin.setKeyboardTracking(False)
        # ClickFocus — typeable when explicitly clicked, but stays out
        # of tab order so Qt's auto-focus-routing (e.g. when another
        # focused widget elsewhere is dismissed) doesn't land here.
        self.current_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.current_spin.setDecimals(3)
        self.current_spin.setSingleStep(0.5)
        self.current_spin.setSuffix(" µA")
        self.current_spin.setRange(0.0, self._max_speed_a * 1e6)
        self.current_spin.setValue(0.0)

        # Direction buttons: + sources +I (V rises), − sources −I (V
        # falls). Clicking the opposite direction while sourcing flips
        # I without needing to Stop first — live reversal.
        self.up_btn = action_button("+")
        self.down_btn = action_button("−")

        # Stop — red, always-live halt. set_current(0) on click; works
        # even when not nominally sourcing (idempotent safety net).
        self.stop_btn = action_button("Stop")
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-weight: bold; padding: 2px 10px; }"
            "QPushButton:pressed { background-color: #962d22; }"
        )

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
                self._get_source_state,
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
        outer.addWidget(self.travel_label)
        outer.addWidget(self.dvdt_label)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep2)

        current_row = QHBoxLayout()
        current_row.addWidget(QLabel("Current:"))
        current_row.addWidget(self.current_spin, stretch=1)
        outer.addLayout(current_row)

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

    def _get_source_state(self) -> tuple[bool, float]:
        """Snapshot for the watchdog's predictive trip. Called from the
        watchdog thread, so keep it cheap and lock-free."""
        return (self._sourcing, self._i_commanded)

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
        """Apply current at the spinbox magnitude with the given sign.

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
        magnitude_a = self.current_spin.value() * 1e-6
        i_a = direction * magnitude_a
        # Refuse up-front if the requested I would immediately trip
        # the predictive floor — better UX than letting the watchdog
        # fire on the next cycle and forcing an Acknowledge.
        v = self._latest_v()
        if v is not None and i_a < 0:
            lookahead = KEITHLEY_CONVERSION_S + PREDICT_SLACK_S
            v_pred = v + i_a * lookahead / self._C
            if v_pred < self._soft_limits[0]:
                self.status_label.setText(
                    f"refused: −{magnitude_a*1e6:.2f}µA from V={v:+.3f} would "
                    f"breach floor (v_pred={v_pred:+.3f}); pick a smaller |I|"
                )
                return
        self._safe(self.yoko.set_current, i_a)
        self._sourcing = True
        self._i_commanded = i_a
        if magnitude_a == 0.0:
            self.status_label.setText("sourcing 0 µA (idle)")
        else:
            self.status_label.setText(f"sourcing {i_a*1e6:+.3f} µA")
        log.info("yoko: source I=%+.3fµA  V=%s", i_a * 1e6,
                 f"{v:+.3f}" if v is not None else "?")
        self._refresh_controls()

    def _on_stop(self) -> None:
        # Always send set_current(0), regardless of _sourcing — Stop
        # is the universal halt and should be effective even if the
        # cached state has drifted out of sync.
        self._safe(self.yoko.set_current, 0.0)
        self._sourcing = False
        self._i_commanded = 0.0
        log.info("yoko: stop (I=0)")
        self._refresh_status_label()
        self._refresh_controls()

    def _on_trip_ack(self) -> None:
        self._tripped = False
        self._trip_reason = None
        self._sourcing = False
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
        self._sourcing = False
        self._i_commanded = 0.0
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
        if self._sourcing and self._i_commanded != 0.0:
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
        """Sync Enable / +/− / Stop enabled state. Stop is live iff sourcing.
        Acknowledge always live (visibility controlled separately)."""
        # Stop is only meaningful while sourcing — when idle it has
        # nothing to halt. Watchdog already cleared _sourcing on trip,
        # so this also disables Stop in the tripped state (Acknowledge
        # is the action there).
        self.stop_btn.setEnabled(self._sourcing)

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
            self.current_spin,
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
            # operator can sanity-check the spinbox value visually.
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

    @Slot(object)
    def _apply_k617_state(self, payload: dict) -> None:
        if "_worker_err" in payload:
            self._k617_issue = f"617 poll err: {payload['_worker_err']}"
            self._refresh_status_label()
            return
        if "k617_err" in payload:
            self.voltage_label.setText("—")
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
            issue: str | None = None
        else:
            # Wrong mode — still show reading but flag the problem so
            # the operator knows why +/− are greyed.
            self.voltage_label.setText(format_engineering(value, unit))
            self.travel_label.setText("")
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
          1. If sourcing, drop I to 0 so the wind-down ramp starts
             from a known state.
          2. Block here ramping V → 0 using the watchdog's _latest, if
             V is well off zero and we have 617 feedback.
          3. safe_disable_current — SA0 + O0.
          4. Stop watchdog + Yoko-poll threads, close instruments.

        Blocking the GUI thread is fine — the app is tearing down.
        """
        # 1. Stop sourcing if we were.
        if self._sourcing:
            try:
                self.yoko.set_current(0.0)
            except _YOKO_ERRORS as e:
                log.warning("shutdown: set_current(0) failed: %s", e)
        self._sourcing = False
        self._i_commanded = 0.0

        # 2. Ramp V → 0 if needed and we have 617 feedback. Skipped
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
        """Block-the-GUI-thread open-loop ramp to V ≈ 0.

        Sources ``default_speed_a`` opposite-sign of the current V until
        V crosses zero (or limit-breaches, or deadline elapses). No
        predictive scale-down, no model. Settling lag + leakage cause
        some overshoot past zero, but the watchdog's hard trip + the
        following safe_disable_current both serve as backstops.
        """
        v_start = self._latest_v()
        if v_start is None:
            log.warning("shutdown ramp: no 617 reading — skipping ramp")
            return
        if abs(v_start) < 0.5:
            return  # already at zero, nothing to do
        deadline = time.monotonic() + deadline_s
        soft_lo, soft_hi = self._soft_limits
        direction = -1 if v_start > 0 else +1
        i_signed = direction * self._default_speed_a
        try:
            self.yoko.set_current(i_signed)
        except _YOKO_ERRORS as e:
            log.warning("shutdown ramp: set_current failed: %s", e)
            return
        try:
            while time.monotonic() < deadline:
                v, func, age = self._latest.read()
                if v is None or func != "DCV" or age > self._stale_s:
                    log.warning(
                        "shutdown ramp: stale/missing 617 (age=%s, func=%s) — bailing",
                        age, func,
                    )
                    break
                # Predictive bail on soft limits so we don't overshoot
                # into the predictive-trip zone during shutdown.
                lookahead = KEITHLEY_CONVERSION_S + PREDICT_SLACK_S
                v_pred = v + i_signed * lookahead / self._C
                if v_pred < soft_lo or v_pred > soft_hi:
                    log.info(
                        "shutdown ramp: predicted V_pred=%+.3f outside soft "
                        "limits — bailing", v_pred,
                    )
                    break
                # Crossed zero? Stop. Sign-sensitive so we don't bail
                # immediately if shutdown launched near zero already.
                if direction > 0 and v >= 0:
                    break
                if direction < 0 and v <= 0:
                    break
                time.sleep(KEITHLEY_CONVERSION_S)
        finally:
            try:
                self.yoko.set_current(0.0)
            except _YOKO_ERRORS as e:
                log.warning("shutdown ramp: final set_current(0) failed: %s", e)
