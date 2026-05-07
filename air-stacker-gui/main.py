"""Live frame viewer for the Air Stacker camera (Flea3 via harvesters/GenTL)."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from harvesters.core import Harvester
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from conex import ConexAxis, error_label, state_label
from heater import OmegaPlatinum, diagnose

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"missing config: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


@contextlib.contextmanager
def silenced_stderr():
    """Mute OS-level stderr (fd 2) so C-side prints from genicam don't leak."""
    sys.stderr.flush()
    old_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(old_fd, 2)
        os.close(devnull)
        os.close(old_fd)


def resolve_cti(producer: str) -> str:
    p = Path(producer)
    if p.is_file():
        return str(p)
    if p.is_dir():
        for entry in sorted(p.iterdir()):
            if entry.suffix.lower() == ".cti":
                return str(entry)
        raise FileNotFoundError(f"no .cti file in {p}")
    raise FileNotFoundError(f"GenTL producer path not found: {p}")


def to_rgb(data: np.ndarray, width: int, height: int, fmt: str) -> np.ndarray:
    """Convert a harvesters component buffer into an RGB888 ndarray."""
    if "Bayer" in fmt:
        bayer = data.reshape(height, width)
        bayer_code = {
            "BayerRG": cv2.COLOR_BayerRG2RGB,
            "BayerGR": cv2.COLOR_BayerGR2RGB,
            "BayerGB": cv2.COLOR_BayerGB2RGB,
            "BayerBG": cv2.COLOR_BayerBG2RGB,
        }
        for prefix, code in bayer_code.items():
            if fmt.startswith(prefix):
                return cv2.cvtColor(bayer, code)
        return cv2.cvtColor(bayer, cv2.COLOR_BayerRG2RGB)
    if "Mono" in fmt:
        return cv2.cvtColor(data.reshape(height, width), cv2.COLOR_GRAY2RGB)
    if fmt.startswith("RGB"):
        return data.reshape(height, width, 3)
    if fmt.startswith("BGR"):
        return cv2.cvtColor(data.reshape(height, width, 3), cv2.COLOR_BGR2RGB)
    raise ValueError(f"unsupported pixel format: {fmt}")


class CameraDisplay(QLabel):
    """Camera frame view with a bottom-left FPS overlay."""

    OVERLAY_MARGIN = 8

    def __init__(self) -> None:
        super().__init__()
        self.fps_label = QLabel("-- fps", self)
        self.fps_label.setStyleSheet(
            "color: white; background-color: rgba(0, 0, 0, 140);"
            " padding: 2px 6px; border-radius: 3px;"
        )
        self.fps_label.adjustSize()
        self._reposition_overlay()

    def set_fps(self, fps: float | None) -> None:
        self.fps_label.setText("-- fps" if fps is None else f"{fps:.1f} fps")
        self.fps_label.adjustSize()
        self._reposition_overlay()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_overlay()

    def _reposition_overlay(self) -> None:
        m = self.OVERLAY_MARGIN
        self.fps_label.move(m, self.height() - self.fps_label.height() - m)


class CameraWorker(QObject):
    """Pulls frames off the harvesters acquirer in a worker thread.

    Latest-frame mailbox: each new RGB array overwrites the slot. The
    `frame_ready` signal is emitted only on the empty→full transition,
    so at most one notification is in-flight; if the GUI thread is
    behind, it picks up the newest frame on next slot run rather than
    backlogging older ones.
    """

    frame_ready = Signal()  # mailbox notify only; main thread pulls via take_latest()
    error = Signal(str)
    finished = Signal()

    def __init__(self, acquirer) -> None:
        super().__init__()
        self._acquirer = acquirer
        self._running = False
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None

    @Slot()
    def run(self) -> None:
        self._running = True
        log_count = 0
        log_start = time.monotonic()
        while self._running:
            try:
                with self._acquirer.fetch(timeout=0.5) as buffer:
                    comp = buffer.payload.components[0]
                    rgb = np.ascontiguousarray(
                        to_rgb(comp.data, comp.width, comp.height, comp.data_format)
                    ).copy()
                with self._lock:
                    notify = self._latest is None
                    self._latest = rgb
                if notify:
                    self.frame_ready.emit()
                log_count += 1
                now = time.monotonic()
                if now - log_start > 2.0:
                    print(
                        f"[camera] produced {log_count / (now - log_start):.1f} fps",
                        file=sys.stderr,
                        flush=True,
                    )
                    log_count = 0
                    log_start = now
            except Exception as e:  # noqa: BLE001 — surface errors then keep trying
                self.error.emit(str(e))
                time.sleep(0.1)
        self.finished.emit()

    def take_latest(self) -> np.ndarray | None:
        """Pop the most-recent frame, or None if already drained."""
        with self._lock:
            f = self._latest
            self._latest = None
            return f

    def stop(self) -> None:
        """Request loop exit. Safe to call from any thread."""
        self._running = False


class ConexAxisPanel(QGroupBox):
    """Newport-style control surface for a single CONEX-CC axis."""

    POLL_MS = 100

    def __init__(self, axis_config: dict) -> None:
        super().__init__(axis_config.get("name", "Axis"))
        self.units = axis_config.get("units", "")
        self.axis = ConexAxis(
            port=axis_config["port"],
            baud=int(axis_config.get("baud", 921600)),
        )

        self.status_label = QLabel("disconnected")
        self.position_label = QLabel("—")
        font = self.position_label.font()
        font.setPointSize(font.pointSize() + 4)
        self.position_label.setFont(font)
        self.id_label = QLabel("")
        self.id_label.setStyleSheet("color: #888;")

        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(-1e6, 1e6)
        self.target_spin.setDecimals(6)
        self.target_spin.setSingleStep(0.01)
        self.go_btn = QPushButton("Go")

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(0.0, 1e6)
        self.step_spin.setDecimals(6)
        self.step_spin.setValue(float(axis_config.get("step", 0.01)))

        self.jog_minus_btn = QPushButton("−")
        self.jog_plus_btn = QPushButton("+")
        self.stop_btn = QPushButton("Stop")
        self.home_btn = QPushButton("Home")
        self.enable_btn = QPushButton("Enable")
        self.disable_btn = QPushButton("Disable")

        self._build_layout()
        self._wire_signals()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)

        try:
            self.axis.open()
        except Exception as e:
            self.status_label.setText(f"open failed: {e}")
            self._set_motion_enabled(False)
            return

        self.id_label.setText(self.axis.identify())
        self.status_label.setText(f"connected on {self.axis.port}")
        self.timer.start(self.POLL_MS)

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(self.status_label)
        outer.addWidget(self.position_label)
        outer.addWidget(self.id_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep)

        abs_row = QHBoxLayout()
        abs_row.addWidget(QLabel("Target:"))
        abs_row.addWidget(self.target_spin, stretch=1)
        abs_row.addWidget(self.go_btn)
        outer.addLayout(abs_row)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step:"))
        step_row.addWidget(self.step_spin, stretch=1)
        outer.addLayout(step_row)

        jog_row = QHBoxLayout()
        jog_row.addWidget(self.jog_minus_btn)
        jog_row.addWidget(self.jog_plus_btn)
        outer.addLayout(jog_row)

        action_row = QHBoxLayout()
        action_row.addWidget(self.stop_btn)
        action_row.addWidget(self.home_btn)
        outer.addLayout(action_row)

        enable_row = QHBoxLayout()
        enable_row.addWidget(self.enable_btn)
        enable_row.addWidget(self.disable_btn)
        outer.addLayout(enable_row)

        outer.addStretch(1)

    def _wire_signals(self) -> None:
        self.go_btn.clicked.connect(self._on_go)
        self.jog_minus_btn.clicked.connect(lambda: self._safe(self.axis.move_relative, -self.step_spin.value()))
        self.jog_plus_btn.clicked.connect(lambda: self._safe(self.axis.move_relative, self.step_spin.value()))
        self.stop_btn.clicked.connect(lambda: self._safe(self.axis.stop))
        self.home_btn.clicked.connect(lambda: self._safe(self.axis.home))
        self.enable_btn.clicked.connect(lambda: self._safe(self.axis.enable))
        self.disable_btn.clicked.connect(lambda: self._safe(self.axis.disable))

    def _set_motion_enabled(self, enabled: bool) -> None:
        for btn in (
            self.go_btn,
            self.jog_minus_btn,
            self.jog_plus_btn,
            self.stop_btn,
            self.home_btn,
            self.enable_btn,
            self.disable_btn,
        ):
            btn.setEnabled(enabled)

    def _on_go(self) -> None:
        self._safe(self.axis.move_absolute, self.target_spin.value())

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            self.status_label.setText(f"err: {e}")

    def poll(self) -> None:
        try:
            pos = self.axis.position()
            self.position_label.setText(f"{pos:.6f} {self.units}")
        except Exception as e:
            self.position_label.setText(f"pos err: {e}")
        try:
            state_code, error_code = self.axis.state()
            label = state_label(state_code)
            err_suffix = (
                "" if error_code == "0000"
                else f"  [err {error_code}: {error_label(error_code)}]"
            )
            self.status_label.setText(f"{label}{err_suffix}")
        except Exception as e:
            self.status_label.setText(f"state err: {e}")

    def shutdown(self) -> None:
        self.timer.stop()
        self.axis.close()


class CameraOptionsPanel(QGroupBox):
    """Live gain / exposure controls (GenICam node map). Auto checkboxes
    map to GainAuto / ExposureAuto Off ↔ Continuous."""

    def __init__(self, node_map, defaults: dict) -> None:
        super().__init__("Camera Options")
        self.node_map = node_map

        self.gain_spin = QDoubleSpinBox()
        self.gain_spin.setKeyboardTracking(False)
        self.gain_spin.setDecimals(2)
        self.gain_spin.setSingleStep(0.5)
        self.gain_auto = QCheckBox("auto")

        self.exp_spin = QDoubleSpinBox()
        self.exp_spin.setKeyboardTracking(False)
        self.exp_spin.setDecimals(0)
        self.exp_spin.setSingleStep(100)
        self.exp_spin.setSuffix(" μs")
        self.exp_auto = QCheckBox("auto")

        outer = QVBoxLayout(self)
        gain_row = QHBoxLayout()
        gain_row.addWidget(QLabel("Gain:"))
        gain_row.addWidget(self.gain_spin, stretch=1)
        gain_row.addWidget(self.gain_auto)
        outer.addLayout(gain_row)
        exp_row = QHBoxLayout()
        exp_row.addWidget(QLabel("Exposure:"))
        exp_row.addWidget(self.exp_spin, stretch=1)
        exp_row.addWidget(self.exp_auto)
        outer.addLayout(exp_row)

        self._init_float("Gain", self.gain_spin, defaults.get("gain"))
        self._init_float("ExposureTime", self.exp_spin, defaults.get("exposure_us"))
        self._init_auto("GainAuto", self.gain_auto, self.gain_spin, default_on=False)
        self._init_auto("ExposureAuto", self.exp_auto, self.exp_spin, default_on=False)

        self.gain_spin.editingFinished.connect(
            lambda: self._set_float("Gain", self.gain_spin.value())
        )
        self.exp_spin.editingFinished.connect(
            lambda: self._set_float("ExposureTime", self.exp_spin.value())
        )
        self.gain_auto.toggled.connect(
            lambda on: self._set_auto("GainAuto", on, self.gain_spin)
        )
        self.exp_auto.toggled.connect(
            lambda on: self._set_auto("ExposureAuto", on, self.exp_spin)
        )

    def _init_float(self, name: str, spin: QDoubleSpinBox, default) -> None:
        try:
            node = getattr(self.node_map, name)
            spin.setRange(float(node.min), float(node.max))
            current = float(default) if default is not None else float(node.value)
            current = max(node.min, min(node.max, current))
            node.value = current
            spin.setValue(current)
        except Exception as e:
            spin.setEnabled(False)
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _init_auto(self, name: str, cb: QCheckBox, manual_spin: QDoubleSpinBox, default_on: bool) -> None:
        try:
            node = getattr(self.node_map, name)
            node.value = "Continuous" if default_on else "Off"
            cb.setChecked(default_on)
            manual_spin.setEnabled(not default_on)
        except Exception as e:
            cb.setEnabled(False)
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _set_float(self, name: str, value: float) -> None:
        try:
            node = getattr(self.node_map, name)
            node.value = max(node.min, min(node.max, float(value)))
        except Exception as e:
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _set_auto(self, name: str, on: bool, manual_spin: QDoubleSpinBox) -> None:
        try:
            node = getattr(self.node_map, name)
            node.value = "Continuous" if on else "Off"
            manual_spin.setEnabled(not on)
        except Exception as e:
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)


class HeaterPanel(QGroupBox):
    """Live temperature + setpoint control for an Omega Platinum controller."""

    def __init__(self, cfg: dict) -> None:
        super().__init__("Heater")
        self.units = cfg.get("units", "°C")
        self.heater = OmegaPlatinum(
            port=cfg["port"],
            baud=int(cfg.get("baud", 19200)),
            slave_id=int(cfg.get("slave_id", 1)),
        )

        self.status_label = QLabel("disconnected")
        self.pv_label = QLabel("—")
        font = self.pv_label.font()
        font.setPointSize(font.pointSize() + 4)
        self.pv_label.setFont(font)
        self.run_label = QLabel("")
        self.run_label.setStyleSheet("color: #888;")
        self.output_label = QLabel("output: —")

        self.setpoint_spin = QDoubleSpinBox()
        self.setpoint_spin.setRange(-1000.0, 1000.0)
        self.setpoint_spin.setDecimals(2)
        self.setpoint_spin.setSingleStep(1.0)
        self.setpoint_spin.setKeyboardTracking(False)
        self.run_btn = QPushButton("Run")
        self.stop_btn = QPushButton("Stop")
        self.diag_btn = QPushButton("Diag")

        outer = QVBoxLayout(self)
        outer.addWidget(self.status_label)
        outer.addWidget(QLabel("Process:"))
        outer.addWidget(self.pv_label)
        outer.addWidget(self.run_label)
        outer.addWidget(self.output_label)
        sp_row = QHBoxLayout()
        sp_row.addWidget(QLabel("Setpoint:"))
        sp_row.addWidget(self.setpoint_spin, stretch=1)
        outer.addLayout(sp_row)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.diag_btn)
        outer.addLayout(btn_row)
        outer.addStretch(1)

        self.setpoint_spin.editingFinished.connect(self._on_set)
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.diag_btn.clicked.connect(self._on_diag)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)

        try:
            self.heater.open()
        except Exception as e:
            self.status_label.setText(f"open failed: {e}")
            self.setpoint_spin.setEnabled(False)
            self.run_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.diag_btn.setEnabled(False)
            return

        self.status_label.setText(f"connected on {self.heater.port}")
        # Pre-populate the panel from the controller before the periodic
        # timer kicks in, so the setpoint spinner and labels reflect the
        # heater's real state from t=0 instead of the spinner's default 0.
        self.poll()
        self.timer.start(int(cfg.get("poll_interval_ms", 1000)))

    def _on_set(self) -> None:
        try:
            self.heater.set_setpoint(self.setpoint_spin.value())
        except Exception as e:
            self.status_label.setText(f"set err: {e}")

    def _on_run(self) -> None:
        try:
            self.heater.run()
        except Exception as e:
            self.status_label.setText(f"run err: {e}")

    def _on_stop(self) -> None:
        try:
            self.heater.stop()
        except Exception as e:
            self.status_label.setText(f"stop err: {e}")

    def _on_diag(self) -> None:
        try:
            print(diagnose(self.heater).summary(), flush=True)
        except Exception as e:
            self.status_label.setText(f"diag err: {e}")

    def poll(self) -> None:
        try:
            pv = self.heater.process_value()
            self.pv_label.setText(f"{pv:.2f} {self.units}")
        except Exception as e:
            self.pv_label.setText(f"pv err: {e}")
        try:
            sp = self.heater.setpoint()
            if not self.setpoint_spin.hasFocus():
                self.setpoint_spin.setValue(sp)
        except Exception:
            pass
        try:
            state = self.heater.system_state()
            self.run_label.setText(f"state: {state.name}")
        except Exception:
            self.run_label.setText("")
        try:
            out = self.heater.output_percent()
            self.output_label.setText(f"output: {out:.1f} %")
        except Exception as e:
            self.output_label.setText(f"output err: {e}")

    def shutdown(self) -> None:
        self.timer.stop()
        self.heater.close()


class CameraWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Air Stacker — live")
        self.label = CameraDisplay()
        self.label.setText("connecting…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMinimumSize(640, 480)
        self._frame_times: deque[float] = deque(maxlen=60)
        self._fps_last_update = 0.0
        self._proc_total = 0.0
        self._proc_max = 0.0
        self._proc_count = 0

        self.axis_panels: list[ConexAxisPanel] = []
        self.heater_panel: HeaterPanel | None = None
        self.camera_options_panel: CameraOptionsPanel | None = None

        config = load_config()
        camera_cfg = config.get("camera", {})

        cti = resolve_cti(config["gentl"]["producer"])
        device_index = int(camera_cfg.get("device_index", 0))

        with silenced_stderr():
            self.harvester = Harvester()
            self.harvester.add_file(cti)
            self.harvester.update()
            if not self.harvester.device_info_list:
                raise RuntimeError("no cameras enumerated by GenTL producer")
            self.acquirer = self.harvester.create(device_index)

        # Camera config + start: outside silenced_stderr so any errors and
        # our diagnostic prints are visible. silenced_stderr was originally
        # only there to mute the GenTL producer's enumeration noise.
        self._apply_camera_startup()
        self.acquirer.start()

        settings_panel = self._build_settings_panel(camera_cfg)
        right_panel = self._build_right_panel(config.get("axis", []), config.get("heater"))

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(settings_panel)
        layout.addWidget(self.label, stretch=1)
        layout.addWidget(right_panel)
        self.setCentralWidget(central)

        self.camera_thread = QThread()
        self.camera_worker = CameraWorker(self.acquirer)
        self.camera_worker.moveToThread(self.camera_thread)
        self.camera_thread.started.connect(self.camera_worker.run)
        self.camera_worker.frame_ready.connect(self._on_frame)
        self.camera_worker.error.connect(self._on_frame_error)
        self.camera_worker.finished.connect(self.camera_thread.quit)
        self.camera_thread.start()

    def _apply_camera_startup(self) -> None:
        """Set acquisition mode, balance-white-auto, and unlock the frame rate.

        Frame rate matters: SpinView leaves AcquisitionFrameRate set on the
        camera (it's persistent), so without resetting it we inherit
        whatever the last user picked. We turn off auto, enable explicit
        control, and pin to the camera's max for live preview.

        Gain / exposure / their auto modes are applied later by
        CameraOptionsPanel from the camera section of config.toml.
        """
        nm = self.acquirer.remote_device.node_map
        for name, value in (
            ("AcquisitionMode", "Continuous"),
            ("BalanceWhiteAuto", "Continuous"),
            ("AcquisitionFrameRateAuto", "Off"),
        ):
            try:
                getattr(nm, name).value = value
            except Exception as e:
                print(f"camera {name}: {e}", file=sys.stderr, flush=True)
        # The "enabled" node is named differently across FLIR generations.
        for name in ("AcquisitionFrameRateEnabled", "AcquisitionFrameRateEnable"):
            try:
                getattr(nm, name).value = True
                break
            except Exception:
                continue
        try:
            rate = nm.AcquisitionFrameRate
            rate.value = float(rate.max)
            print(
                f"camera AcquisitionFrameRate = {rate.value:.2f} Hz "
                f"(range {rate.min:.2f}..{rate.max:.2f})",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            print(f"camera AcquisitionFrameRate: {e}", file=sys.stderr, flush=True)

    def _build_settings_panel(self, camera_cfg: dict) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        recording = QGroupBox("Recording")
        recording_layout = QHBoxLayout(recording)
        record_btn = QPushButton("Record")
        record_btn.setEnabled(False)
        stop_btn = QPushButton("Stop")
        stop_btn.setEnabled(False)
        recording_layout.addWidget(record_btn)
        recording_layout.addWidget(stop_btn)

        presets = QGroupBox("Presets")
        presets_layout = QVBoxLayout(presets)
        presets_layout.addWidget(QLabel("TODO"))

        self.camera_options_panel = CameraOptionsPanel(
            self.acquirer.remote_device.node_map, camera_cfg
        )

        layout.addWidget(recording)
        layout.addWidget(presets)
        layout.addWidget(self.camera_options_panel)
        layout.addStretch(1)
        return panel

    def _build_right_panel(self, axes_cfg: list[dict], heater_cfg: dict | None) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        for cfg in axes_cfg:
            ap = ConexAxisPanel(cfg)
            self.axis_panels.append(ap)
            layout.addWidget(ap)
        if heater_cfg:
            self.heater_panel = HeaterPanel(heater_cfg)
            layout.addWidget(self.heater_panel)
        layout.addStretch(1)
        return panel

    def _on_frame(self) -> None:
        rgb = self.camera_worker.take_latest()
        if rgb is None:
            return  # drained by a previous slot run
        t0 = time.monotonic()
        h, w, _ = rgb.shape
        qimg = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
        # Scale on the QImage (smaller buffer) before converting to QPixmap
        # — QPixmap.fromImage on a full 1280x1024 frame is the dominant
        # blit cost on Windows.
        scaled = qimg.scaled(
            self.label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.label.setPixmap(QPixmap.fromImage(scaled))
        self._update_fps()
        elapsed = time.monotonic() - t0
        self._proc_total += elapsed
        self._proc_max = max(self._proc_max, elapsed)
        self._proc_count += 1
        if self._proc_count >= 60:
            avg_ms = self._proc_total / self._proc_count * 1000
            max_ms = self._proc_max * 1000
            print(
                f"[gui] _on_frame avg={avg_ms:.1f}ms max={max_ms:.1f}ms n={self._proc_count}",
                file=sys.stderr,
                flush=True,
            )
            self._proc_total = 0.0
            self._proc_max = 0.0
            self._proc_count = 0

    def _on_frame_error(self, msg: str) -> None:
        self.label.setText(f"frame error: {msg}")
        self._frame_times.clear()
        self.label.set_fps(None)

    def _update_fps(self) -> None:
        now = time.monotonic()
        self._frame_times.append(now)
        if now - self._fps_last_update < 0.25:
            return
        self._fps_last_update = now
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            fps = (len(self._frame_times) - 1) / span if span > 0 else None
            self.label.set_fps(fps)

    def closeEvent(self, event) -> None:
        self.camera_worker.stop()
        self.camera_thread.quit()
        if not self.camera_thread.wait(2000):
            print("camera thread did not exit cleanly", file=sys.stderr, flush=True)
        for ap in self.axis_panels:
            ap.shutdown()
        if self.heater_panel is not None:
            self.heater_panel.shutdown()
        self.acquirer.stop()
        self.acquirer.destroy()
        self.harvester.reset()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = CameraWindow()
    win.resize(960, 720)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
