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

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from smc100 import (
    DISABLE_STATES,
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


class TravelBar(QWidget):
    """Vertical travel bar with min/max ticks and a current-position marker.

    Convention: low values at the top, high values at the bottom (+Y down,
    matching the stage frame and our project-wide convention). Limits are
    drawn as horizontal ticks; the position marker is a wider line at the
    interpolated y.
    """

    BAR_WIDTH_PX = 6
    TICK_LEN_PX = 8
    MARKER_LEN_PX = 16

    def __init__(self, lo: float, hi: float, units: str = "mm") -> None:
        super().__init__()
        if lo >= hi:
            raise ValueError(f"lo must be < hi (got {lo}, {hi})")
        self._lo = lo
        self._hi = hi
        self._units = units
        self._position: float | None = None
        self.setMinimumHeight(160)
        self.setMinimumWidth(96)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.MinimumExpanding
        )

    def set_position(self, pos: float | None) -> None:
        self._position = pos
        self.update()

    def set_range(self, lo: float, hi: float) -> None:
        if lo >= hi or (lo, hi) == (self._lo, self._hi):
            return
        self._lo = lo
        self._hi = hi
        self.update()

    def _y_for(self, value: float, top: int, bottom: int) -> int:
        # +Y down: lo at top, hi at bottom.
        frac = (value - self._lo) / (self._hi - self._lo)
        frac = max(0.0, min(1.0, frac))
        return int(top + frac * (bottom - top))

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        margin = 16
        top = margin
        bottom = h - margin
        bar_x = w // 3
        bar_w = self.BAR_WIDTH_PX

        # Travel bar background.
        painter.fillRect(bar_x, top, bar_w, bottom - top, QColor(60, 60, 60))

        # Min/max ticks + labels.
        tick_pen = QPen(QColor(180, 180, 180))
        tick_pen.setWidthF(1.0)
        painter.setPen(tick_pen)
        painter.drawLine(
            bar_x - self.TICK_LEN_PX,
            top,
            bar_x + bar_w + self.TICK_LEN_PX,
            top,
        )
        painter.drawLine(
            bar_x - self.TICK_LEN_PX,
            bottom,
            bar_x + bar_w + self.TICK_LEN_PX,
            bottom,
        )
        label_x = bar_x + bar_w + self.TICK_LEN_PX + 4
        painter.drawText(label_x, top + 4, f"{self._lo:.1f} {self._units}")
        painter.drawText(label_x, bottom + 4, f"{self._hi:.1f} {self._units}")

        # Position marker.
        if self._position is not None:
            pos_y = self._y_for(self._position, top, bottom)
            in_range = self._lo <= self._position <= self._hi
            color = QColor(80, 200, 120) if in_range else QColor(220, 100, 100)
            marker_pen = QPen(color)
            marker_pen.setWidthF(2.5)
            painter.setPen(marker_pen)
            painter.drawLine(
                bar_x - self.MARKER_LEN_PX,
                pos_y,
                bar_x + bar_w + self.MARKER_LEN_PX,
                pos_y,
            )


class SMC100Panel(QGroupBox):
    """Live status + control surface for a single SMC100CC axis."""

    DEFAULT_POLL_MS = 100

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

        self.position_label = QLabel("—")
        pos_font = QFont(self.position_label.font())
        pos_font.setPointSize(pos_font.pointSize() + 6)
        pos_font.setStyleHint(QFont.StyleHint.Monospace)
        pos_font.setFamily("monospace")
        self.position_label.setFont(pos_font)

        # Travel bar — placeholder range until open() succeeds.
        bar_lo, bar_hi = position_limits if position_limits else (0.0, 1.0)
        self.travel_bar = TravelBar(bar_lo, bar_hi, units=self.units)

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

        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-weight: bold; padding: 8px; }"
            "QPushButton:pressed { background-color: #962d22; }"
        )
        stop_font = QFont(self.stop_btn.font())
        stop_font.setPointSize(stop_font.pointSize() + 2)
        self.stop_btn.setFont(stop_font)

        self._build_layout()
        self._wire_signals()

        self._worker: _PollWorker | None = None
        self._worker_thread: QThread | None = None
        self._last_state_code: str | None = None

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

        outer.addWidget(self.status_label)
        outer.addWidget(self.error_label)
        outer.addWidget(self.id_label)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep1)

        # Position + travel bar side-by-side.
        pos_row = QHBoxLayout()
        pos_col = QVBoxLayout()
        pos_col.addWidget(self.position_label)
        pos_col.addStretch(1)
        pos_row.addLayout(pos_col, stretch=1)
        pos_row.addWidget(self.travel_bar)
        outer.addLayout(pos_row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep2)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Go to:"))
        target_row.addWidget(self.target_spin, stretch=1)
        target_row.addWidget(self.go_btn)
        outer.addLayout(target_row)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step:"))
        step_row.addWidget(self.step_spin, stretch=1)
        step_row.addWidget(self.jog_minus_btn)
        step_row.addWidget(self.jog_plus_btn)
        outer.addLayout(step_row)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed:"))
        speed_row.addWidget(self.velocity_spin, stretch=1)
        outer.addLayout(speed_row)

        action_row = QHBoxLayout()
        action_row.addWidget(self.home_btn)
        action_row.addWidget(self.enable_btn)
        action_row.addWidget(self.disable_btn)
        action_row.addWidget(self.reset_btn)
        outer.addLayout(action_row)

        outer.addWidget(self.stop_btn)
        outer.addStretch(1)

    def _wire_signals(self) -> None:
        self.go_btn.clicked.connect(self._on_go)
        # Hold-for-continuous: on press, send move_relative toward the
        # appropriate limit; on release, stop. Single clicks still work
        # because release fires before the controller has moved far.
        self.jog_minus_btn.pressed.connect(self._on_jog_minus_pressed)
        self.jog_minus_btn.released.connect(lambda: self._safe(self.axis.stop))
        self.jog_plus_btn.pressed.connect(self._on_jog_plus_pressed)
        self.jog_plus_btn.released.connect(lambda: self._safe(self.axis.stop))

        self.velocity_spin.editingFinished.connect(self._on_set_velocity)

        self.home_btn.clicked.connect(lambda: self._safe(self.axis.home))
        self.enable_btn.clicked.connect(lambda: self._safe(self.axis.enable))
        self.disable_btn.clicked.connect(lambda: self._safe(self.axis.disable))
        self.reset_btn.clicked.connect(lambda: self._safe(self.axis.reset))

        self.stop_btn.clicked.connect(lambda: self._safe(self.axis.stop))

    def _apply_limits(self, limits: tuple[float, float]) -> None:
        """Tighten the spin ranges and travel bar to the effective limits."""
        lo, hi = limits
        self.target_spin.setRange(lo, hi)
        self.travel_bar.set_range(lo, hi)

    # --- click handlers (GUI thread) ----------------------------------------

    def _on_go(self) -> None:
        self._safe(self.axis.move_absolute, self.target_spin.value())

    def _on_jog_minus_pressed(self) -> None:
        # If we have effective limits, drive toward the lower bound; the
        # controller will decelerate at SL or wherever we stop it. Without
        # limits, fall back to a single-step jog.
        if (
            self.axis.effective_limits is not None
            and self._last_state_code in READY_STATES
        ):
            try:
                here = self.axis.position()
            except SMC100Error:
                self._safe(self.axis.move_relative, -self.step_spin.value())
                return
            lo, _ = self.axis.effective_limits
            travel = lo - here  # negative
            if travel < 0:
                self._safe(self.axis.move_relative, travel)
            return
        self._safe(self.axis.move_relative, -self.step_spin.value())

    def _on_jog_plus_pressed(self) -> None:
        if (
            self.axis.effective_limits is not None
            and self._last_state_code in READY_STATES
        ):
            try:
                here = self.axis.position()
            except SMC100Error:
                self._safe(self.axis.move_relative, self.step_spin.value())
                return
            _, hi = self.axis.effective_limits
            travel = hi - here  # positive
            if travel > 0:
                self._safe(self.axis.move_relative, travel)
            return
        self._safe(self.axis.move_relative, self.step_spin.value())

    def _on_set_velocity(self) -> None:
        self._safe(self.axis.set_velocity, self.velocity_spin.value())

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            self.status_label.setText(f"err: {e}")

    # --- worker / state plumbing --------------------------------------------

    def _read_state(self) -> dict:
        """Worker-thread: read axis state. Must not touch Qt widgets."""
        payload: dict = {}
        try:
            payload["pos"] = self.axis.position()
        except Exception as e:  # noqa: BLE001
            payload["pos_err"] = str(e)
        try:
            sc, ec = self.axis.state()
            payload["state_code"] = sc
            payload["error_code"] = ec
        except Exception as e:  # noqa: BLE001
            payload["state_err"] = str(e)
        if payload.get("state_code") in MOVING_STATES:
            try:
                payload["setpoint"] = self.axis.setpoint()
            except Exception:  # noqa: BLE001 — non-fatal
                pass
        return payload

    @Slot(object)
    def _apply_state(self, payload: dict) -> None:
        """Main-thread: render a payload from the polling worker."""
        if "_worker_err" in payload:
            self.status_label.setText(f"poll err: {payload['_worker_err']}")
            return

        # Position readout + travel bar.
        if "pos" in payload:
            pos = payload["pos"]
            text = f"{pos:.3f} {self.units}"
            if "setpoint" in payload:
                text += f"  →  {payload['setpoint']:.3f}"
            self.position_label.setText(text)
            self.travel_bar.set_position(pos)
        elif "pos_err" in payload:
            self.position_label.setText(f"pos err: {payload['pos_err']}")
            self.travel_bar.set_position(None)

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

        Stop is always live. Reset works from anywhere except CONFIG; we
        leave it always-enabled. Home only works from NOT REFERENCED. Move
        commands and velocity edits only in READY. Enable from DISABLE,
        Disable from READY.
        """
        in_ready = state_code in READY_STATES
        in_disable = state_code in DISABLE_STATES
        in_not_ref = state_code in NOT_REFERENCED_STATES

        self.go_btn.setEnabled(in_ready)
        self.jog_minus_btn.setEnabled(in_ready)
        self.jog_plus_btn.setEnabled(in_ready)
        self.target_spin.setEnabled(in_ready)
        self.step_spin.setEnabled(in_ready)
        self.velocity_spin.setEnabled(in_ready)
        self.home_btn.setEnabled(in_not_ref)
        self.enable_btn.setEnabled(in_disable)
        self.disable_btn.setEnabled(in_ready)
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
