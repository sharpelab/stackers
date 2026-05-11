"""PySide6 panel for a single Newport SMC100CC axis.

Self-contained: not yet imported from ``main.py``. When the master session
wires it up, the integration call site will look like::

    from smc100_panel import SMC100Panel
    smc_cfg = config.get("smc100")
    if smc_cfg:
        self.smc100_panel = SMC100Panel(smc_cfg)

The panel matches :class:`HeaterPanel` in shape — single ``cfg: dict``
constructor, polls the device on a worker thread, drives state-aware button
enables on the GUI thread.

Expected ``cfg`` keys (parallel to the ``[heater]`` and ``[[axis]]`` blocks
in ``config.toml``):

  - ``port`` (str, required)
  - ``baud`` (int, default 57600)
  - ``units`` (str, default ``"mm"``)
  - ``step`` (float, default 0.1) — initial step-size for ± buttons
  - ``poll_interval_ms`` (int, default 100)
  - ``position_limits`` (list of two floats, optional) — operational software
    cap; passed through to :class:`SMC100Axis`. The air-stacker rig sets
    ``[0.0, 30.0]``.
  - ``default_velocity`` (float, optional) — pushed to ``VA`` on connect

Persistent CONFIG-mode commands (``PW0``/``PW1``/EEPROM saves) are not
exposed by either the driver or this panel.
"""

from __future__ import annotations

import logging
import threading

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

from smc100 import (
    DISABLE_STATES,
    JOGGING_STATES,
    MOVING_STATES,
    NOT_REFERENCED_STATES,
    READY_STATES,
    SMC100Axis,
    SMC100Error,
    error_label,
    state_label,
)

log = logging.getLogger("airstacker.smc100")

# Bits that should turn the status line red. Anything except a clean 0x0000
# reads as a fault we want the operator to notice; specifically, a hit on
# the end-of-run bits or on the protective trips matters most.
_FAULT_STATES = NOT_REFERENCED_STATES  # plus state code "10" (ESP err) included


class _PollWorker(QObject):
    """Single-axis polling worker — gated mailbox via Qt's queued signal.

    Identical in spirit to :class:`main.PollWorker`. Inlined here so the
    module stays self-contained (no import from ``main`` while the master
    session is editing it). When integration lands we can collapse this
    onto the shared ``PollWorker`` if it's still appropriate.
    """

    state_ready = Signal(object)
    finished = Signal()

    def __init__(self, read_fn, poll_interval_s: float) -> None:
        super().__init__()
        self._read = read_fn
        self._interval = poll_interval_s
        self._stop_event = threading.Event()

    @Slot()
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self._read()
            except Exception as e:  # noqa: BLE001 — surface and continue
                payload = {"_worker_err": str(e)}
            self.state_ready.emit(payload)
            self._stop_event.wait(self._interval)
        self.finished.emit()

    def stop(self) -> None:
        self._stop_event.set()


class SMC100Panel(QGroupBox):
    """Live status + control surface for a single SMC100CC axis."""

    DEFAULT_POLL_MS = 100
    # Mouse-down duration past which a jog button switches from step to
    # continuous mode. 250 ms is comfortable for an intentional click and
    # short enough that a hold feels responsive.
    JOG_HOLD_MS = 250

    def __init__(self, cfg: dict) -> None:
        super().__init__("Z stage (SMC100)")
        self.units = cfg.get("units", "mm")
        self._step_default = float(cfg.get("step", 0.1))
        self._poll_interval_s = (
            int(cfg.get("poll_interval_ms", self.DEFAULT_POLL_MS)) / 1000.0
        )
        self._default_velocity = cfg.get("default_velocity")

        # Optional position_limits from TOML, e.g. position_limits = [0.0, 30.0].
        # tomlkit hands us a list-like; coerce to a plain tuple of floats.
        raw_limits = cfg.get("position_limits")
        position_limits: tuple[float, float] | None
        if raw_limits is None:
            position_limits = None
        else:
            limits_list = [float(x) for x in raw_limits]
            if len(limits_list) != 2:
                raise ValueError(
                    f"position_limits must have exactly 2 entries (got {raw_limits!r})"
                )
            position_limits = (limits_list[0], limits_list[1])

        self.axis = SMC100Axis(
            port=cfg["port"],
            baud=int(cfg.get("baud", 57600)),
            position_limits=position_limits,
        )
        self._configured_limits = position_limits

        # --- widgets --------------------------------------------------------
        self.status_label = QLabel("disconnected")
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #b04040;")
        self.id_label = QLabel("")
        self.id_label.setStyleSheet("color: #888;")
        self.id_label.setVisible(False)

        self.position_label = QLabel("—")
        pos_font = QFont(self.position_label.font())
        pos_font.setPointSize(pos_font.pointSize() + 6)
        pos_font.setStyleHint(QFont.StyleHint.Monospace)
        pos_font.setFamily("monospace")
        self.position_label.setFont(pos_font)

        self.target_spin = QDoubleSpinBox()
        self.target_spin.setKeyboardTracking(False)
        self.target_spin.setDecimals(3)
        self.target_spin.setSingleStep(0.1)
        self.target_spin.setSuffix(f" {self.units}")
        self.target_spin.setRange(-1e6, 1e6)  # tightened in _apply_limits

        self.go_btn = QPushButton("Go")

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setKeyboardTracking(False)
        self.step_spin.setDecimals(3)
        self.step_spin.setSingleStep(0.01)
        self.step_spin.setSuffix(f" {self.units}")
        self.step_spin.setRange(0.0, 1e6)
        self.step_spin.setValue(self._step_default)

        self.jog_minus_btn = QPushButton("−")
        self.jog_plus_btn = QPushButton("+")

        self.velocity_spin = QDoubleSpinBox()
        self.velocity_spin.setKeyboardTracking(False)
        self.velocity_spin.setDecimals(3)
        self.velocity_spin.setSingleStep(0.5)
        self.velocity_spin.setRange(0.001, 1000.0)
        self.velocity_spin.setSuffix(f" {self.units}/s")

        self.home_btn = QPushButton("Home")
        self.enable_btn = QPushButton("Enable")
        self.disable_btn = QPushButton("Disable")
        self.reset_btn = QPushButton("Reset")
        # Escape hatch out of JOGGING state. Sends JM0 (silence keypad)
        # then JD (leave JOGGING). Both transient — no flash writes.
        # Hidden until polling reports JOGGING; the layout row collapses
        # when it's not visible.
        self.leave_jog_btn = QPushButton("Leave jog")
        self.leave_jog_btn.setVisible(False)

        # Stop sits on the top-right of the panel (same line as status)
        # rather than as a full-width banner. Red styling keeps it visually
        # distinct as a panic button without taking a row to itself.
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-weight: bold; padding: 2px 10px; }"
            "QPushButton:pressed { background-color: #962d22; }"
        )

        self._build_layout()
        self._wire_signals()

        self._worker: _PollWorker | None = None
        self._worker_thread: QThread | None = None
        self._last_state_code: str | None = None
        # Cached position from the polling worker — read by the
        # click-vs-hold continuous-fire path so it doesn't have to do a
        # synchronous serial query on the GUI thread.
        self._last_position: float | None = None

        # Click-vs-hold state for the ± jog buttons. _jog_direction is
        # 0 when idle, ±1 while a button is held. _jog_continuous flips
        # to True once the timer fires (we've committed to continuous
        # mode). See _on_jog_pressed / _on_jog_released.
        self._jog_direction: int = 0
        self._jog_continuous: bool = False
        self._jog_timer = QTimer(self)
        self._jog_timer.setSingleShot(True)
        self._jog_timer.setInterval(self.JOG_HOLD_MS)
        self._jog_timer.timeout.connect(self._on_jog_continuous_fire)

        try:
            self.axis.open()
        except Exception as e:  # noqa: BLE001
            self.status_label.setText(f"open failed: {e}")
            self._set_all_motion_enabled(False)
            return

        try:
            self.id_label.setText(self.axis.identify())
        except SMC100Error:
            pass

        # Tighten the target spinner to the effective software clamp (the
        # intersection of position_limits and the controller's SL/SR). If
        # neither is available we leave the wide default range.
        eff = self.axis.effective_limits
        if eff is not None:
            self._apply_limits(eff)

        # Pre-populate velocity from the controller (best-effort).
        try:
            current_v = self.axis.velocity()
            self.velocity_spin.blockSignals(True)
            self.velocity_spin.setValue(current_v)
            self.velocity_spin.blockSignals(False)
        except SMC100Error:
            pass

        # Apply default_velocity if requested. Note: this is a transient
        # write (VA without PW), not persisted to flash.
        if self._default_velocity is not None:
            try:
                self.axis.set_velocity(float(self._default_velocity))
                self.velocity_spin.blockSignals(True)
                self.velocity_spin.setValue(float(self._default_velocity))
                self.velocity_spin.blockSignals(False)
            except SMC100Error as e:
                log.warning("set_velocity failed: %s", e)

        self.status_label.setText(f"connected on {self.axis.port}")

        # Pre-populate before the worker starts so the GUI shows real values
        # from t=0 instead of "—".
        self._apply_state(self._read_state())

        self._worker_thread = QThread()
        self._worker = _PollWorker(self._read_state, self._poll_interval_s)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.state_ready.connect(self._apply_state)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker_thread.start()

    # --- layout / wiring ----------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.stop_btn)
        outer.addLayout(status_row)
        outer.addWidget(self.error_label)
        outer.addWidget(self.id_label)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep1)

        outer.addWidget(self.position_label)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep2)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Go to:"))
        target_row.addWidget(self.target_spin, stretch=1)
        target_row.addWidget(self.go_btn)
        outer.addLayout(target_row)

        step_speed_row = QHBoxLayout()
        step_speed_row.addWidget(QLabel("Step:"))
        step_speed_row.addWidget(self.step_spin, stretch=1)
        step_speed_row.addWidget(QLabel("Speed:"))
        step_speed_row.addWidget(self.velocity_spin, stretch=1)
        outer.addLayout(step_speed_row)

        jog_row = QHBoxLayout()
        jog_row.addWidget(self.jog_minus_btn, stretch=1)
        jog_row.addWidget(self.jog_plus_btn, stretch=1)
        outer.addLayout(jog_row)

        action_row = QHBoxLayout()
        action_row.addWidget(self.home_btn)
        action_row.addWidget(self.enable_btn)
        action_row.addWidget(self.disable_btn)
        action_row.addWidget(self.reset_btn)
        outer.addLayout(action_row)

        outer.addWidget(self.leave_jog_btn)
        outer.addStretch(1)

    def _wire_signals(self) -> None:
        self.go_btn.clicked.connect(self._on_go)
        # Click=step, hold>JOG_HOLD_MS=continuous. On press we just start a
        # one-shot timer. If release fires first → step. If the timer fires
        # first → continuous travel toward the limit. Release in continuous
        # mode → stop. See _on_jog_pressed / _on_jog_released.
        self.jog_minus_btn.pressed.connect(lambda: self._on_jog_pressed(-1))
        self.jog_minus_btn.released.connect(self._on_jog_released)
        self.jog_plus_btn.pressed.connect(lambda: self._on_jog_pressed(+1))
        self.jog_plus_btn.released.connect(self._on_jog_released)

        self.velocity_spin.editingFinished.connect(self._on_set_velocity)

        self.home_btn.clicked.connect(lambda: self._safe(self.axis.home))
        self.enable_btn.clicked.connect(lambda: self._safe(self.axis.enable))
        self.disable_btn.clicked.connect(lambda: self._safe(self.axis.disable))
        self.reset_btn.clicked.connect(lambda: self._safe(self.axis.reset))
        self.leave_jog_btn.clicked.connect(self._on_leave_jog)

        self.stop_btn.clicked.connect(lambda: self._safe(self.axis.stop))

    def _apply_limits(self, limits: tuple[float, float]) -> None:
        """Tighten the target spinner range to the effective software clamp."""
        lo, hi = limits
        self.target_spin.setRange(lo, hi)

    # --- click handlers (GUI thread) ----------------------------------------

    def _on_go(self) -> None:
        self._safe(self.axis.move_absolute, self.target_spin.value())

    def _on_jog_pressed(self, direction: int) -> None:
        """Mouse-down on a jog button — arm the click-vs-hold timer."""
        log.info("smc100: jog pressed dir=%+d (timer armed)", direction)
        self._jog_direction = direction
        self._jog_continuous = False
        self._jog_timer.start()

    def _on_jog_continuous_fire(self) -> None:
        """Hold threshold elapsed — switch from step to continuous travel.

        Uses the cached position from the polling worker rather than a
        synchronous query — the polling worker holds the serial lock most
        of the time, so a query on the GUI thread can stall ~100 ms,
        making the press feel like it "cuts off after 250 ms". The cache
        is at most one poll interval stale (default 100 ms), accurate
        enough for travel-to-limit math.
        """
        if self._jog_direction == 0:
            return  # released before the timer got here
        direction = self._jog_direction
        log.info(
            "smc100: jog continuous-fire dir=%+d here=%s",
            direction,
            self._last_position,
        )
        if self.axis.effective_limits is not None and self._last_position is not None:
            here = self._last_position
            lo, hi = self.axis.effective_limits
            target = lo if direction < 0 else hi
            travel = target - here
            if (direction < 0 and travel < 0) or (direction > 0 and travel > 0):
                self._safe(self.axis.move_relative, travel)
        else:
            # No limits or no cached position — fall back to a step move.
            # Better than refusing to move at all.
            self._safe(self.axis.move_relative, direction * self.step_spin.value())
        self._jog_continuous = True

    def _on_jog_released(self) -> None:
        """Mouse-up on either jog button — finish a step or stop a continuous run."""
        if self._jog_direction == 0:
            return
        log.info(
            "smc100: jog released dir=%+d continuous=%s",
            self._jog_direction,
            self._jog_continuous,
        )
        if self._jog_continuous:
            self._safe(self.axis.stop)
        else:
            # Quick click: cancel the pending continuous switch and fire
            # a single step move using whatever's in the Step spinbox.
            self._jog_timer.stop()
            self._safe(
                self.axis.move_relative,
                self._jog_direction * self.step_spin.value(),
            )
        self._jog_direction = 0
        self._jog_continuous = False
        # Reconcile button-enabled state now that the jog buttons are no
        # longer held. _refresh_button_enables skipped them while pressed.
        if self._last_state_code is not None:
            self._refresh_button_enables(self._last_state_code)

    def _on_set_velocity(self) -> None:
        self._safe(self.axis.set_velocity, self.velocity_spin.value())

    def _on_leave_jog(self) -> None:
        # JM0 first to silence the keypad (transient — reverts to JM1 on
        # next boot), then JD to leave JOGGING. Done as a single try-block
        # so we surface whichever step failed and keep the order semantic.
        log.info("smc100: leave-jog — sending JM0 then JD")
        try:
            self.axis.disable_keypad()
            self.axis.leave_jog()
        except Exception as e:  # noqa: BLE001
            log.exception("smc100: leave-jog failed")
            self.status_label.setText(f"leave jog err: {e}")

    def _safe(self, fn, *args) -> None:
        name = getattr(fn, "__name__", repr(fn))
        log.info("smc100: %s(%s)", name, args if args else "")
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.exception("smc100: %s failed", name)
            self.status_label.setText(f"err: {e}")

    # --- worker / state plumbing --------------------------------------------

    def _read_state(self) -> dict:
        """Worker-thread: read axis state. Must not touch Qt widgets."""
        payload: dict = {}
        try:
            payload["pos"] = self.axis.position()
        except Exception as e:  # noqa: BLE001
            log.warning("smc100 position read failed: %s", e)
            payload["pos_err"] = str(e)
        try:
            sc, ec = self.axis.state()
            payload["state_code"] = sc
            payload["error_code"] = ec
        except Exception as e:  # noqa: BLE001
            log.warning("smc100 state read failed: %s", e)
            payload["state_err"] = str(e)
        if payload.get("state_code") in MOVING_STATES:
            try:
                payload["setpoint"] = self.axis.setpoint()
            except Exception:  # noqa: BLE001 — non-fatal, no log
                pass
        return payload

    @Slot(object)
    def _apply_state(self, payload: dict) -> None:
        """Main-thread: render a payload from the polling worker."""
        if "_worker_err" in payload:
            self.status_label.setText(f"poll err: {payload['_worker_err']}")
            return

        # Position readout. The cache feeds _on_jog_continuous_fire so it
        # doesn't have to do its own synchronous serial query.
        if "pos" in payload:
            pos = payload["pos"]
            self._last_position = pos
            text = f"{pos:.3f} {self.units}"
            if "setpoint" in payload:
                text += f"  →  {payload['setpoint']:.3f}"
            self.position_label.setText(text)
        elif "pos_err" in payload:
            self.position_label.setText(f"pos err: {payload['pos_err']}")

        # State + error.
        if "state_code" in payload:
            sc = payload["state_code"]
            ec = payload["error_code"]
            self._last_state_code = sc
            self.status_label.setText(state_label(sc))
            if ec != "0000":
                self.error_label.setText(f"error 0x{ec}: {error_label(ec)}")
                self.error_label.setVisible(True)
            else:
                self.error_label.setText("")
                self.error_label.setVisible(False)
            # Highlight NOT REFERENCED states in the same red as errors,
            # since they're operationally similar (axis won't move).
            if sc in _FAULT_STATES or sc == "10":
                self.status_label.setStyleSheet("color: #b04040; font-weight: bold;")
            else:
                self.status_label.setStyleSheet("")
            self._refresh_button_enables(sc)
        elif "state_err" in payload:
            self.status_label.setText(f"state err: {payload['state_err']}")
            self.status_label.setStyleSheet("color: #b04040;")

    def _refresh_button_enables(self, state_code: str) -> None:
        """Grey out commands the controller would reject in the current state.

        Stop and Reset stay live whenever the controller is responding.
        Leave-jog only when in JOGGING. Home only from NOT REFERENCED.
        Move commands and velocity edits only in READY. Enable from
        DISABLE, Disable from READY.
        """
        in_ready = state_code in READY_STATES
        in_disable = state_code in DISABLE_STATES
        in_not_ref = state_code in NOT_REFERENCED_STATES
        in_jogging = state_code in JOGGING_STATES

        self.go_btn.setEnabled(in_ready)
        # Jog buttons: skip toggling while one is held. Qt fires released()
        # when you setEnabled(False) on a pressed button, so disabling them
        # mid-press (e.g. when the controller transitions READY → MOVING
        # after our move_relative goes through) cancels the continuous-mode
        # jog right after it started. _on_jog_released reconciles state on
        # actual release.
        if self._jog_direction == 0:
            self.jog_minus_btn.setEnabled(in_ready)
            self.jog_plus_btn.setEnabled(in_ready)
        self.target_spin.setEnabled(in_ready)
        self.step_spin.setEnabled(in_ready)
        self.velocity_spin.setEnabled(in_ready)
        self.home_btn.setEnabled(in_not_ref)
        self.enable_btn.setEnabled(in_disable)
        self.disable_btn.setEnabled(in_ready)
        # Leave-jog only appears when actually in JOGGING — it's an
        # escape-hatch button, not part of the normal control surface.
        self.leave_jog_btn.setVisible(in_jogging)
        # Stop and Reset are always live as long as the controller is
        # responding — Stop is the panic button, Reset works from any
        # state except CONFIGURATION (and we don't expose CONFIG-mode here).
        self.stop_btn.setEnabled(True)
        self.reset_btn.setEnabled(True)

    def _set_all_motion_enabled(self, enabled: bool) -> None:
        """Used at startup if open() failed — kill every interactive control.

        With no live controller, even Reset/Stop are pointless — there's
        nothing to reset or stop. Polling will re-enable Stop / Reset (and
        the rest, per state) as soon as the controller starts answering.
        """
        for btn in (
            self.go_btn,
            self.jog_minus_btn,
            self.jog_plus_btn,
            self.home_btn,
            self.enable_btn,
            self.disable_btn,
            self.reset_btn,
            self.leave_jog_btn,
            self.stop_btn,
        ):
            btn.setEnabled(enabled)
        for spin in (self.target_spin, self.step_spin, self.velocity_spin):
            spin.setEnabled(enabled)

    # --- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        if self._worker is not None and self._worker_thread is not None:
            self._worker.stop()
            self._worker_thread.quit()
            if not self._worker_thread.wait(2000):
                log.warning("smc100 worker thread did not exit cleanly")
        self.axis.close()
