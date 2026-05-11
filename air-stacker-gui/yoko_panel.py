"""PySide6 panel for the Yokogawa 7651 driving the NPM140 fine-Z piezo.

Self-contained: not yet imported from ``main.py``. When the master session
wires it up, the integration call site will look like::

    from yoko_panel import YokoPanel
    yoko_cfg = config.get("yoko")
    if yoko_cfg:
        self.yoko_panel = YokoPanel(yoko_cfg)

Voltage-only by design — the rig drives the NPM140 piezo as a DC voltage
source, so this panel doesn't expose the 7651's current-source mode. Anyone
who needs current mode reaches for :class:`yoko.Yoko7651` directly.

Expected ``cfg`` keys (parallel to the ``[heater]`` and ``[smc100]`` blocks
in ``config.toml``):

  - ``resource`` (str, required) — VISA resource, e.g. ``"GPIB0::15::INSTR"``
  - ``voltage_limits`` (list of two floats, optional) — operational software
    cap; passed through to :class:`Yoko7651`. The piezo caller passes
    ``[-20.0, 30.0]`` (anything below −20 V damages the piezo).
  - ``current_limits`` (list of two floats, optional) — kept around for
    completeness even though the panel doesn't use them.
  - ``nm_per_volt`` (float, optional, default 933.33) — for the µm-equivalent
    readout shown beside the volts. NPM140 datasheet gives 140 µm over the
    full −20 → +130 V range = 150 V span = 933.33 nm/V.
  - ``poll_interval_ms`` (int, default 1000) — OD; polling cadence.
  - ``ramp_step_v`` (float, default 0.05) — ramp step size, in volts.
  - ``ramp_delay_ms`` (int, default 50) — ramp delay between steps.

Persistent / setup-mode commands and current-mode operation are not exposed.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot
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

from keithley617 import Keithley617, format_engineering
from yoko import VOLTAGE_RANGE_V, Yoko7651, YokoError

log = logging.getLogger("airstacker.yoko")

# NPM140 datasheet: 140 µm open-loop over -20 → +130 V (150 V span)
# = 933.33 nm/V. See docs/air-stacker-pc.md.
DEFAULT_NM_PER_VOLT = 140_000.0 / 150.0  # ≈ 933.33

# Default ramp parameters — 0.1 V steps every 20 ms = 5 V/s. The piezo's
# resonant frequency is 670 Hz (period 1.5 ms), so 20 ms per step is
# ~13× the period — plenty of settling time, no risk of mechanical ringing.
# yoko.py.ramp_voltage()'s own defaults are gentler (1 V/s); the panel
# uses the faster pace because that's what the operator actually wants
# at the GUI. Override per-deployment via [yoko] in config.toml.
#
# Caveat: the 7651 is also current-limited (Albert sets the output range
# / current cap deliberately to protect the piezo). The actual on-the-wire
# slew rate is min(software ramp rate, I_limit / C_piezo). If the unit is
# slewing slower than our software ramp, the ramp_progress label will say
# "done" before OD actually reaches the target — you'll see OD continue
# climbing for a while after. If that becomes annoying, either raise the
# current limit on the unit or add OD-settle-detection to _on_ramp_finished.
DEFAULT_RAMP_STEP_V = 0.1
DEFAULT_RAMP_DELAY_MS = 20


class _PollWorker(QObject):
    """Voltage-readout polling worker — gated mailbox via Qt's queued signal.

    Same shape as ``smc100_panel._PollWorker`` and ``main.PollWorker``; we
    inline a copy here to keep this module standalone (no import from
    ``main`` while the master session is editing it).
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


class _RampWorker(QObject):
    """One-shot ramp worker — runs Yoko7651.ramp_voltage off the GUI thread.

    Created fresh per ramp, runs to completion (or until cancel), emits
    ``finished`` with success/failure info, then its thread quits. The GUI
    can call :meth:`cancel` to bail mid-ramp; we patch the driver's step
    write through a cancel-aware shim so the ramp can be interrupted at a
    granular level rather than only after each step.
    """

    finished = Signal(object)  # dict payload

    def __init__(
        self,
        yoko: Yoko7651,
        target: float,
        step: float,
        delay_s: float,
    ) -> None:
        super().__init__()
        self._yoko = yoko
        self._target = target
        self._step = step
        self._delay_s = delay_s
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        # Loop calling set_voltage one step at a time, checking the cancel
        # flag between steps. Mirrors the inner loop of Yoko7651.ramp_voltage
        # but interrupts cleanly. Reads the current voltage from the cache
        # (or live OD if the cache is empty).
        try:
            cur = self._yoko.cached_voltage
            if cur is None:
                r = self._yoko.read_output()
                if r.function != "V":
                    raise YokoError(
                        "can't ramp voltage — instrument is in current mode"
                    )
                cur = r.value
                self._yoko.seed_voltage_cache(cur)

            target = self._target
            step = self._step
            if step <= 0:
                raise YokoError("step must be > 0")

            direction = 1 if target >= cur else -1
            step_signed = step * direction

            while (direction > 0 and cur < target) or (direction < 0 and cur > target):
                if self._cancel.is_set():
                    self.finished.emit({"cancelled": True, "stopped_at": cur})
                    return
                nxt = cur + step_signed
                if (direction > 0 and nxt > target) or (direction < 0 and nxt < target):
                    nxt = target
                self._yoko.set_voltage(nxt)
                cur = nxt
                if self._cancel.wait(self._delay_s):
                    self.finished.emit({"cancelled": True, "stopped_at": cur})
                    return
            self.finished.emit({"ok": True, "final": cur})
        except Exception as e:  # noqa: BLE001
            self.finished.emit({"err": str(e)})


class YokoPanel(QGroupBox):
    """Live readout + control surface for a Yokogawa 7651 driving a piezo."""

    DEFAULT_POLL_MS = 1000

    def __init__(self, cfg: dict) -> None:
        super().__init__("Fine Z (Yoko 7651 → NPM140)")
        self._poll_interval_s = (
            int(cfg.get("poll_interval_ms", self.DEFAULT_POLL_MS)) / 1000.0
        )
        self._nm_per_volt = float(cfg.get("nm_per_volt", DEFAULT_NM_PER_VOLT))
        self._ramp_step_v = float(cfg.get("ramp_step_v", DEFAULT_RAMP_STEP_V))
        self._ramp_delay_s = (
            int(cfg.get("ramp_delay_ms", DEFAULT_RAMP_DELAY_MS)) / 1000.0
        )

        # voltage_limits / current_limits: tomlkit hands us list-likes; coerce
        # to plain tuples of floats. Defaulting voltage_limits to the 7651's
        # full hardware envelope rather than the piezo-safe range — callers
        # (config.toml) are responsible for the −20 V floor, mirroring how
        # Yoko7651 itself defaults.
        v_raw = cfg.get("voltage_limits")
        if v_raw is None:
            voltage_limits = VOLTAGE_RANGE_V
        else:
            v_list = [float(x) for x in v_raw]
            if len(v_list) != 2:
                raise ValueError(f"voltage_limits must have 2 entries (got {v_raw!r})")
            voltage_limits = (v_list[0], v_list[1])
        self._voltage_limits = voltage_limits

        c_raw = cfg.get("current_limits")
        if c_raw is None:
            current_kwargs: dict = {}
        else:
            c_list = [float(x) for x in c_raw]
            if len(c_list) != 2:
                raise ValueError(f"current_limits must have 2 entries (got {c_raw!r})")
            current_kwargs = {"current_limits": (c_list[0], c_list[1])}

        self.yoko = Yoko7651(
            resource=cfg["resource"],
            voltage_limits=voltage_limits,
            fix_voltage_range=True,
            **current_kwargs,
        )

        # Optional Keithley 617 readback — wired in series with the
        # piezo to measure charging / leakage current. Configured via
        # a `keithley617` sub-block under [yoko] in config.toml.
        k617_cfg = cfg.get("keithley617")
        if k617_cfg:
            self.keithley = Keithley617(resource=k617_cfg["resource"])
            self._k617_poll_interval_s = (
                int(k617_cfg.get("poll_interval_ms", 1000)) / 1000.0
            )
        else:
            self.keithley = None
            self._k617_poll_interval_s = 1.0

        # --- widgets --------------------------------------------------------
        self.status_label = QLabel("disconnected")
        self.overload_label = QLabel("")
        self.overload_label.setStyleSheet(
            "color: white; background-color: #b04040; "
            "font-weight: bold; padding: 2px 6px;"
        )
        self.overload_label.setVisible(False)
        self.id_label = QLabel("")
        self.id_label.setStyleSheet("color: #888;")
        self.id_label.setVisible(False)

        self.voltage_label = QLabel("—")
        v_font = QFont(self.voltage_label.font())
        v_font.setPointSize(v_font.pointSize() + 6)
        v_font.setStyleHint(QFont.StyleHint.Monospace)
        v_font.setFamily("monospace")
        self.voltage_label.setFont(v_font)
        self.travel_label = QLabel("")
        self.travel_label.setStyleSheet("color: #888;")
        # Keithley 617 current readout — sibling of the voltage display.
        # Set visible only when a 617 is configured (see __init__ tail).
        self.current_label = QLabel("—")
        self.current_label.setFont(v_font)
        self.current_label.setVisible(False)
        self.current_status_label = QLabel("")
        self.current_status_label.setStyleSheet("color: #888;")
        self.current_status_label.setVisible(False)

        # Ramp is the only path for changing voltage — single-shot Set has
        # been removed because piezo callers shouldn't ever step the
        # output, and 1 V/s ramps from the same spinbox cover the
        # fine-adjustment case at one or two seconds of cost.
        self.ramp_spin = QDoubleSpinBox()
        self.ramp_spin.setKeyboardTracking(False)
        self.ramp_spin.setDecimals(3)
        self.ramp_spin.setSingleStep(0.1)
        self.ramp_spin.setSuffix(" V")
        self.ramp_spin.setRange(voltage_limits[0], voltage_limits[1])
        self.ramp_btn = QPushButton("Ramp")
        # STOP sits at top-right (status row) matching SMC100 and rotation
        # panels. Always enabled — the handler no-ops when no ramp is in
        # flight, so a wasted click is harmless and we don't lose clicks
        # to a near-completed-ramp race.
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-weight: bold; padding: 2px 10px; }"
            "QPushButton:pressed { background-color: #962d22; }"
        )
        # Output state — Enable / Disable buttons. Disable always ramps
        # to 0 first (via the existing ramp worker) before dropping the
        # relay, so the operator path uses the shutdown protocol by
        # default. The two-button design also acts as a state indicator:
        # the inactive direction is disabled, so the live button shows
        # what we last commanded. Cache is None at startup; we show both
        # live until the operator's first action sets the cache.
        self.enable_btn = QPushButton("Enable")
        self.disable_btn = QPushButton("Disable")
        self.disable_btn.setToolTip(
            "Ramp voltage to 0 V, then send O0. Always uses the shutdown "
            "protocol — never slams the relay open at non-zero V."
        )

        self._build_layout()
        self._wire_signals()

        self._poll_worker: _PollWorker | None = None
        self._poll_thread: QThread | None = None
        self._ramp_worker: _RampWorker | None = None
        self._ramp_thread: QThread | None = None
        # Set when Disable kicks off a ramp-to-0; consumed by
        # _on_ramp_finished to chain set_output(False) on success.
        self._disable_after_ramp = False
        self._cache_seeded = False

        try:
            self.yoko.open()
        except Exception as e:  # noqa: BLE001
            self.status_label.setText(f"open failed: {e}")
            self._set_all_enabled(False)
            return

        # Output state is unknown at open() — the 7651 protocol is
        # write-only, no way to query whether the relay is engaged.
        # _refresh_status_label will show "output state unknown" until
        # the operator clicks Enable or Disable.
        self._refresh_status_label()

        # Seed-from-OD: don't write anything; the first poll picks up the
        # live OD value and we use seed_voltage_cache so a subsequent ramp
        # has a known starting point. Output stays in whatever state the
        # unit was in.
        self._apply_state(self._read_state())

        # Pre-populate the ramp spinbox with the live voltage so a
        # fat-fingered Ramp click doesn't sweep the piezo from the
        # spinbox's default of 0 to wherever the user actually wanted.
        if self.yoko.cached_voltage is not None:
            self.ramp_spin.blockSignals(True)
            self.ramp_spin.setValue(self.yoko.cached_voltage)
            self.ramp_spin.blockSignals(False)

        self._poll_thread = QThread()
        self._poll_worker = _PollWorker(self._read_state, self._poll_interval_s)
        self._poll_worker.moveToThread(self._poll_thread)
        self._poll_thread.started.connect(self._poll_worker.run)
        self._poll_worker.state_ready.connect(self._apply_state)
        self._poll_worker.finished.connect(self._poll_thread.quit)
        self._poll_thread.start()

        # Keithley 617 — second poll thread, independent of the Yoko.
        # If open() fails we drop self.keithley to None and skip the
        # readout entirely; the panel still functions as a Yoko-only
        # surface.
        self._k617_poll_worker: _PollWorker | None = None
        self._k617_poll_thread: QThread | None = None
        if self.keithley is not None:
            try:
                self.keithley.open()
            except Exception as e:  # noqa: BLE001
                log.warning("keithley617 open failed: %s", e)
                self.current_status_label.setText(f"keithley open failed: {e}")
                self.current_status_label.setVisible(True)
                self.keithley = None
            else:
                self.current_label.setVisible(True)
                self.current_status_label.setVisible(True)
                self.current_status_label.setText(
                    f"617 @ {self.keithley.resource}"
                )
                self._k617_poll_thread = QThread()
                self._k617_poll_worker = _PollWorker(
                    self._read_keithley, self._k617_poll_interval_s
                )
                self._k617_poll_worker.moveToThread(self._k617_poll_thread)
                self._k617_poll_thread.started.connect(self._k617_poll_worker.run)
                self._k617_poll_worker.state_ready.connect(self._apply_keithley)
                self._k617_poll_worker.finished.connect(
                    self._k617_poll_thread.quit
                )
                self._k617_poll_thread.start()

    # --- layout / wiring ----------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        # Top status row — connection text on the left, STOP on the
        # right. Matches the SMC100 / rotation panel layouts.
        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.stop_btn)
        outer.addLayout(status_row)
        outer.addWidget(self.overload_label)
        outer.addWidget(self.id_label)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep1)

        # Output state — Enable / Disable buttons. The button-enabled
        # state mirrors the cache: when output is on, Enable is greyed
        # out; when off, Disable is greyed out; when unknown (first
        # connect, no command yet), both are live so the operator can
        # commit to either direction.
        output_row = QHBoxLayout()
        output_row.addWidget(self.enable_btn)
        output_row.addWidget(self.disable_btn)
        output_row.addStretch(1)
        outer.addLayout(output_row)
        self._refresh_output_buttons()

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep2)

        outer.addWidget(self.voltage_label)
        outer.addWidget(self.travel_label)
        outer.addWidget(self.current_label)
        outer.addWidget(self.current_status_label)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep3)

        ramp_row = QHBoxLayout()
        ramp_row.addWidget(QLabel("Ramp to:"))
        ramp_row.addWidget(self.ramp_spin, stretch=1)
        ramp_row.addWidget(self.ramp_btn)
        outer.addLayout(ramp_row)

        sep4 = QFrame()
        sep4.setFrameShape(QFrame.Shape.HLine)
        sep4.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep4)

        # Controls — Enable / Disable at the bottom, matching the SMC100
        # and rotation panels' action_row pattern.
        action_row = QHBoxLayout()
        action_row.addWidget(self.enable_btn)
        action_row.addWidget(self.disable_btn)
        action_row.addStretch(1)
        outer.addLayout(action_row)

        outer.addStretch(1)

    def _wire_signals(self) -> None:
        self.ramp_btn.clicked.connect(self._on_ramp)
        self.stop_btn.clicked.connect(self._on_ramp_stop)
        self.enable_btn.clicked.connect(self._on_enable)
        self.disable_btn.clicked.connect(self._on_disable)

    # --- click handlers (GUI thread) ----------------------------------------

    def _on_ramp(self) -> None:
        target = self.ramp_spin.value()
        lo, hi = self._voltage_limits
        if not lo <= target <= hi:
            self.status_label.setText(
                f"ramp target {target} V outside ({lo}, {hi})"
            )
            return
        self._start_ramp_to(target, label=f"ramping to {target:+.3f} V…")

    def _on_enable(self) -> None:
        self._safe(self.yoko.set_output, True)
        self._refresh_output_buttons()
        self._refresh_status_label()

    def _on_disable(self) -> None:
        if self._ramp_thread is not None:
            return  # already ramping; ignore
        v = self.yoko.cached_voltage
        if v is None or abs(v) < 1e-4:
            # Already at zero (or unknown) — drop the relay directly,
            # no ramp needed.
            self._safe(self.yoko.set_output, False)
            self._refresh_output_buttons()
            self._refresh_status_label()
            return
        # Non-zero — ramp to 0 in worker, then disable on finish.
        self._disable_after_ramp = True
        self._start_ramp_to(0.0, label="ramping to 0 V before disable…")

    def _start_ramp_to(self, target: float, *, label: str) -> None:
        """Kick off a _RampWorker and disable the action buttons."""
        if self._ramp_thread is not None:
            return  # one ramp at a time
        self._ramp_thread = QThread()
        self._ramp_worker = _RampWorker(
            self.yoko, target, self._ramp_step_v, self._ramp_delay_s
        )
        self._ramp_worker.moveToThread(self._ramp_thread)
        self._ramp_thread.started.connect(self._ramp_worker.run)
        self._ramp_worker.finished.connect(self._on_ramp_finished)
        self._ramp_thread.start()

        self.ramp_btn.setEnabled(False)
        self.enable_btn.setEnabled(False)
        self.disable_btn.setEnabled(False)
        self.status_label.setText(label)

    def _on_ramp_stop(self) -> None:
        if self._ramp_worker is not None:
            self._ramp_worker.cancel()

    @Slot(object)
    def _on_ramp_finished(self, payload: dict) -> None:
        if self._ramp_thread is not None:
            self._ramp_thread.quit()
            self._ramp_thread.wait(1000)
            self._ramp_thread = None
        self._ramp_worker = None
        self.ramp_btn.setEnabled(True)

        do_disable = self._disable_after_ramp
        self._disable_after_ramp = False

        if payload.get("ok"):
            if do_disable:
                self._safe(self.yoko.set_output, False)
            # In either case, refresh the static state below.
            transient = None
        elif payload.get("cancelled"):
            # User aborted via STOP. Respect that — don't auto-disable
            # even if we were heading toward a disable.
            transient = f"ramp cancelled at {payload['stopped_at']:+.3f} V"
        elif "err" in payload:
            transient = f"ramp err: {payload['err']}"
        else:
            transient = None

        self._refresh_output_buttons()
        if transient is not None:
            self.status_label.setText(transient)
        else:
            self._refresh_status_label()

    def _refresh_status_label(self) -> None:
        """Set the status text to the current static output state.

        Called on idle transitions (after enable, after a successful
        ramp, after a direct disable). During an in-flight ramp the
        handlers write transient text directly; this method is the
        canonical 'static' restoration.
        """
        state = self.yoko.cached_output_on
        if state is None:
            self.status_label.setText("output state unknown")
        elif state:
            self.status_label.setText("OUTPUT ON")
        else:
            self.status_label.setText("OUTPUT OFF")

    def _refresh_output_buttons(self) -> None:
        """Sync Enable / Disable enabled state with the cached output state.

        Cache is None at startup (we never queried, can't query) — both
        buttons are live. Once we command on or off, the cache reflects
        what we last sent and the opposite-direction button greys out.
        """
        state = self.yoko.cached_output_on
        if state is None:
            self.enable_btn.setEnabled(True)
            self.disable_btn.setEnabled(True)
        elif state:
            self.enable_btn.setEnabled(False)
            self.disable_btn.setEnabled(True)
        else:
            self.enable_btn.setEnabled(True)
            self.disable_btn.setEnabled(False)

    def _safe(self, fn, *args) -> None:
        name = getattr(fn, "__name__", repr(fn))
        log.info("yoko: %s(%s)", name, args if args else "")
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.exception("yoko: %s failed", name)
            self.status_label.setText(f"err: {e}")

    # --- worker / state plumbing --------------------------------------------

    def _read_keithley(self) -> dict:
        """Worker-thread: read the next 617 measurement. No Qt widgets."""
        if self.keithley is None:
            return {"k617_err": "no keithley"}
        try:
            r = self.keithley.read()
        except Exception as e:  # noqa: BLE001
            log.warning("keithley617 read failed: %s", e)
            return {"k617_err": str(e)}
        return {
            "k617_value": r.value,
            "k617_function": r.function,
            "k617_unit": r.unit,
            "k617_status": r.status,
        }

    @Slot(object)
    def _apply_keithley(self, payload: dict) -> None:
        if "_worker_err" in payload:
            self.current_status_label.setText(f"poll err: {payload['_worker_err']}")
            return
        if "k617_err" in payload:
            self.current_label.setText("—")
            self.current_status_label.setText(f"617 err: {payload['k617_err']}")
            return
        value = payload["k617_value"]
        unit = payload["k617_unit"]
        function = payload["k617_function"]
        status = payload["k617_status"]
        self.current_label.setText(format_engineering(value, unit))
        # Suffix function + status so an unexpected mode (e.g. operator
        # bumped front panel from AMPS to VOLTS) is visible.
        suffix = f"617 · {function}"
        if status != "N":
            suffix += " · OVERFLOW"
        self.current_status_label.setText(suffix)

    def _read_state(self) -> dict:
        """Worker-thread: read live output. Must not touch Qt widgets."""
        try:
            r = self.yoko.read_output()
        except Exception as e:  # noqa: BLE001
            # Surface to console so the offending raw OD reply is visible
            # — the on-panel label truncates. read_output() bakes the
            # raw response into the message via repr().
            log.warning("yoko OD read failed: %s", e)
            return {"od_err": str(e)}
        return {
            "value": r.value,
            "function": r.function,
            "status": r.status,
        }

    @Slot(object)
    def _apply_state(self, payload: dict) -> None:
        if "_worker_err" in payload:
            self.status_label.setText(f"poll err: {payload['_worker_err']}")
            return
        if "od_err" in payload:
            self.voltage_label.setText(f"od err: {payload['od_err']}")
            return

        value = payload["value"]
        function = payload["function"]
        status = payload["status"]

        # Seed the cache from the first OD reading so ramp_voltage has a
        # known starting point without first issuing OD; itself.
        if not self._cache_seeded and function == "V":
            self.yoko.seed_voltage_cache(value)
            self._cache_seeded = True

        if function == "V":
            self.voltage_label.setText(f"{value:+.3f} V")
            travel_um = value * self._nm_per_volt / 1000.0
            self.travel_label.setText(f"≈ {travel_um:+.2f} µm extension")
        else:
            # Current mode — surface it but don't try to map to µm.
            self.voltage_label.setText(f"{value:+.6f} A (current mode)")
            self.travel_label.setText("")

        if status == "E":
            self.overload_label.setText("OVERLOAD")
            self.overload_label.setVisible(True)
        else:
            self.overload_label.setText("")
            self.overload_label.setVisible(False)

    def _set_all_enabled(self, enabled: bool) -> None:
        for w in (
            self.ramp_spin,
            self.ramp_btn,
            self.enable_btn,
            self.disable_btn,
        ):
            w.setEnabled(enabled)
        # stop_btn stays live regardless — it's a no-op when no ramp is
        # in flight, and we want it usable as a panic abort.

    # --- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        if self._ramp_worker is not None:
            self._ramp_worker.cancel()
        if self._ramp_thread is not None:
            self._ramp_thread.quit()
            self._ramp_thread.wait(2000)
        if self._poll_worker is not None and self._poll_thread is not None:
            self._poll_worker.stop()
            self._poll_thread.quit()
            if not self._poll_thread.wait(2000):
                log.warning("yoko poll thread did not exit cleanly")
        if (
            self._k617_poll_worker is not None
            and self._k617_poll_thread is not None
        ):
            self._k617_poll_worker.stop()
            self._k617_poll_thread.quit()
            if not self._k617_poll_thread.wait(2000):
                log.warning("keithley617 poll thread did not exit cleanly")
        if self.keithley is not None:
            self.keithley.close()
        # Shutdown protocol: ramp to 0 V before dropping the output
        # relay. safe_disable is a no-op on the ramp if cache says we're
        # already near 0 V; it always sends the final O0;. Blocking is
        # fine here — the GUI is tearing down anyway.
        if self.yoko.is_open and self.yoko.cached_output_on:
            try:
                self.yoko.safe_disable(
                    step=self._ramp_step_v, delay=self._ramp_delay_s
                )
            except Exception as e:  # noqa: BLE001
                log.warning("yoko safe_disable failed during shutdown: %s", e)
        self.yoko.close()
