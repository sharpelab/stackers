"""Live frame viewer for the Air Stacker camera (Flea3 via harvesters/GenTL)."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np
from harvesters.core import Harvester
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
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
    QSizePolicy,
    QSlider,
    QSpinBox,
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


@dataclass(frozen=True)
class AdjustmentSnapshot:
    """Immutable snapshot of image-adjustment parameters.

    Brightness / contrast / saturation are integer percent (100 = identity).
    R/G/B ranges are inclusive [lo, hi] in [0, 255]; (0, 255) is identity.
    """

    brightness: int = 100
    contrast: int = 100
    saturation: int = 100
    r_range: tuple[int, int] = (0, 255)
    g_range: tuple[int, int] = (0, 255)
    b_range: tuple[int, int] = (0, 255)

    @property
    def is_identity(self) -> bool:
        return (
            self.brightness == 100
            and self.contrast == 100
            and self.saturation == 100
            and self.r_range == (0, 255)
            and self.g_range == (0, 255)
            and self.b_range == (0, 255)
        )


class ImageAdjustments:
    """Thread-safe live container for an AdjustmentSnapshot.

    The GUI thread mutates via update(); the camera worker reads
    via get(). Snapshots are immutable so the worker can hold one
    across a frame's adjustment pass without any locking.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = AdjustmentSnapshot()

    def get(self) -> AdjustmentSnapshot:
        with self._lock:
            return self._snap

    def update(self, **kwargs) -> AdjustmentSnapshot:
        with self._lock:
            self._snap = replace(self._snap, **kwargs)
            return self._snap

    def reset(self) -> AdjustmentSnapshot:
        with self._lock:
            self._snap = AdjustmentSnapshot()
            return self._snap


def _build_brightness_lut(brightness_pct: int) -> np.ndarray:
    """LUT for `out = clip(in * brightness/100)`."""
    x = np.arange(256, dtype=np.float32)
    return np.clip(x * (brightness_pct / 100.0), 0.0, 255.0).astype(np.uint8)


def _build_channel_lut(contrast_pct: int, lo: int, hi: int) -> np.ndarray:
    """LUT folding contrast (around 128) then per-channel range remap."""
    x = np.arange(256, dtype=np.float32)
    y = (x - 128.0) * (contrast_pct / 100.0) + 128.0
    if (lo, hi) != (0, 255):
        span = max(hi - lo, 1)
        y = (y - lo) * (255.0 / span)
    return np.clip(y, 0.0, 255.0).astype(np.uint8)


def apply_adjustments(rgb: np.ndarray, adj: AdjustmentSnapshot) -> np.ndarray:
    """Apply brightness/saturation/contrast/RGB-range to an RGB uint8 frame.

    Order matches the flakes-website CSS filter chain:
    brightness → saturate → contrast → per-channel range remap.

    Returns a new array; input is not mutated. If `adj` is identity,
    returns the input unchanged (fast path).
    """
    if adj.is_identity:
        return rgb

    out = rgb
    if adj.brightness != 100:
        out = cv2.LUT(out, _build_brightness_lut(adj.brightness))

    if adj.saturation != 100:
        hsv = cv2.cvtColor(out, cv2.COLOR_RGB2HSV)
        s = hsv[..., 1].astype(np.float32) * (adj.saturation / 100.0)
        hsv[..., 1] = np.clip(s, 0.0, 255.0).astype(np.uint8)
        out = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)

    needs_per_channel = (
        adj.contrast != 100
        or adj.r_range != (0, 255)
        or adj.g_range != (0, 255)
        or adj.b_range != (0, 255)
    )
    if needs_per_channel:
        r_lut = _build_channel_lut(adj.contrast, *adj.r_range)
        g_lut = _build_channel_lut(adj.contrast, *adj.g_range)
        b_lut = _build_channel_lut(adj.contrast, *adj.b_range)
        # Numpy fancy-indexing is comparable to cv2.LUT here and
        # natively supports a different LUT per channel.
        result = np.empty_like(out)
        result[..., 0] = r_lut[out[..., 0]]
        result[..., 1] = g_lut[out[..., 1]]
        result[..., 2] = b_lut[out[..., 2]]
        out = result

    return out


def compute_histograms(rgb: np.ndarray) -> np.ndarray:
    """256-bin per-channel histogram of an RGB uint8 frame.

    Returns a (3, 256) int64 array. Cheap (~1-2 ms at 1280x1024).
    """
    out = np.empty((3, 256), dtype=np.int64)
    flat = rgb.reshape(-1, 3)
    out[0] = np.bincount(flat[:, 0], minlength=256)
    out[1] = np.bincount(flat[:, 1], minlength=256)
    out[2] = np.bincount(flat[:, 2], minlength=256)
    return out


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


class PollWorker(QObject):
    """Generic device-polling worker.

    Calls `read_fn()` in its own thread, emits the result via
    `state_ready`, sleeps `poll_interval_s` between iterations. The
    main thread connects a slot to `state_ready` and updates Qt
    widgets there (queued connection across threads).

    `read_fn` runs on the worker thread, so it must not touch Qt
    widgets — only do the device I/O and return a payload (dict
    works well for partial-failure handling).
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


class CameraWorker(QObject):
    """Pulls frames off the harvesters acquirer in a worker thread.

    Latest-frame mailbox: each new RGB array overwrites the slot. The
    `frame_ready` signal is emitted only on the empty→full transition,
    so at most one notification is in-flight; if the GUI thread is
    behind, it picks up the newest frame on next slot run rather than
    backlogging older ones.
    """

    frame_ready = Signal()  # mailbox notify only; main thread pulls via take_latest()
    histograms_ready = Signal(object)  # carries (3, 256) int64 ndarray
    error = Signal(str)
    finished = Signal()

    HIST_INTERVAL_S = 0.2  # ~5 Hz (camera runs at ~60 Hz)

    def __init__(self, acquirer, adjustments: ImageAdjustments | None = None) -> None:
        super().__init__()
        self._acquirer = acquirer
        self._adjustments = adjustments
        self._running = False
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None

    @Slot()
    def run(self) -> None:
        self._running = True
        log_count = 0
        log_start = time.monotonic()
        hist_last = 0.0
        while self._running:
            try:
                with self._acquirer.fetch(timeout=0.5) as buffer:
                    comp = buffer.payload.components[0]
                    rgb = np.ascontiguousarray(
                        to_rgb(comp.data, comp.width, comp.height, comp.data_format)
                    ).copy()
                # Histograms are computed on the raw frame (pre-adjustment)
                # so they read like a stable baseline of the sensor output.
                now = time.monotonic()
                if now - hist_last > self.HIST_INTERVAL_S:
                    hist_last = now
                    try:
                        self.histograms_ready.emit(compute_histograms(rgb))
                    except Exception as e:  # noqa: BLE001
                        print(f"[camera] histogram err: {e}", file=sys.stderr, flush=True)
                if self._adjustments is not None:
                    snap = self._adjustments.get()
                    if not snap.is_identity:
                        try:
                            rgb = np.ascontiguousarray(apply_adjustments(rgb, snap))
                        except Exception as e:  # noqa: BLE001
                            print(f"[camera] adjustment err: {e}", file=sys.stderr, flush=True)
                with self._lock:
                    notify = self._latest is None
                    self._latest = rgb
                if notify:
                    self.frame_ready.emit()
                log_count += 1
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

        self._worker: PollWorker | None = None
        self._worker_thread: QThread | None = None

        try:
            self.axis.open()
        except Exception as e:
            self.status_label.setText(f"open failed: {e}")
            self._set_motion_enabled(False)
            return

        self.id_label.setText(self.axis.identify())
        self.status_label.setText(f"connected on {self.axis.port}")

        self._worker_thread = QThread()
        self._worker = PollWorker(self._read_state, self.POLL_MS / 1000.0)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.state_ready.connect(self._apply_state)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker_thread.start()

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
        return payload

    @Slot(object)
    def _apply_state(self, payload: dict) -> None:
        """Main-thread: render a payload from the polling worker."""
        if "pos" in payload:
            self.position_label.setText(f"{payload['pos']:.6f} {self.units}")
        elif "pos_err" in payload:
            self.position_label.setText(f"pos err: {payload['pos_err']}")
        if "state_code" in payload:
            label = state_label(payload["state_code"])
            err = payload["error_code"]
            err_suffix = (
                "" if err == "0000"
                else f"  [err {err}: {error_label(err)}]"
            )
            self.status_label.setText(f"{label}{err_suffix}")
        elif "state_err" in payload:
            self.status_label.setText(f"state err: {payload['state_err']}")

    def shutdown(self) -> None:
        if self._worker is not None and self._worker_thread is not None:
            self._worker.stop()
            self._worker_thread.quit()
            if not self._worker_thread.wait(2000):
                print(
                    f"axis {self.title()} thread did not exit cleanly",
                    file=sys.stderr,
                    flush=True,
                )
        self.axis.close()


class CameraOptionsPanel(QGroupBox):
    """Live camera controls (GenICam node map): gain / exposure (with
    auto), white balance (auto + manual Red/Blue ratios), gamma, sharpness.

    The Defaults button reverts everything to OUR_DEFAULTS — factory values
    plus the lab's preferred gain/exposure tweaks. config.toml's [camera]
    section can override gain/exposure for those keys it provides.
    """

    OUR_DEFAULTS: dict = {
        "Gain": 10.0,
        "ExposureTime": 5000.0,
        "GainAuto": "Off",
        "ExposureAuto": "Off",
        "BalanceWhiteAuto": "Continuous",
        "Gamma": 1.0,
        "Sharpness": 1024,
    }

    def __init__(self, node_map, defaults: dict) -> None:
        super().__init__("Camera Options")
        self.node_map = node_map
        self._defaults = dict(self.OUR_DEFAULTS)
        if defaults.get("gain") is not None:
            self._defaults["Gain"] = float(defaults["gain"])
        if defaults.get("exposure_us") is not None:
            self._defaults["ExposureTime"] = float(defaults["exposure_us"])

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

        self.wb_red_spin = QDoubleSpinBox()
        self.wb_red_spin.setKeyboardTracking(False)
        self.wb_red_spin.setDecimals(3)
        self.wb_red_spin.setSingleStep(0.05)
        self.wb_blue_spin = QDoubleSpinBox()
        self.wb_blue_spin.setKeyboardTracking(False)
        self.wb_blue_spin.setDecimals(3)
        self.wb_blue_spin.setSingleStep(0.05)
        self.wb_auto = QCheckBox("auto")

        self.gamma_spin = QDoubleSpinBox()
        self.gamma_spin.setKeyboardTracking(False)
        self.gamma_spin.setDecimals(2)
        self.gamma_spin.setSingleStep(0.05)

        self.sharpness_spin = QSpinBox()
        self.sharpness_spin.setKeyboardTracking(False)
        self.sharpness_spin.setSingleStep(64)

        self.defaults_btn = QPushButton("Defaults")

        outer = QVBoxLayout(self)
        for label, spin, auto in (
            ("Gain:", self.gain_spin, self.gain_auto),
            ("Exposure:", self.exp_spin, self.exp_auto),
            ("WB Red:", self.wb_red_spin, self.wb_auto),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(spin, stretch=1)
            row.addWidget(auto)
            outer.addLayout(row)
        for label, spin in (
            ("WB Blue:", self.wb_blue_spin),
            ("Gamma:", self.gamma_spin),
            ("Sharpness:", self.sharpness_spin),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(spin, stretch=1)
            outer.addLayout(row)
        outer.addWidget(self.defaults_btn)

        self._setup_range_float("Gain", self.gain_spin)
        self._setup_range_float("ExposureTime", self.exp_spin)
        self._setup_range_float("Gamma", self.gamma_spin)
        self._setup_range_int("Sharpness", self.sharpness_spin)
        # BalanceRatio is locked while WB auto is on. Try to read the
        # node's bounds anyway; fall back to Flea3-observed [0.25, 4.0].
        lo, hi = 0.25, 4.0
        try:
            br = self.node_map.BalanceRatio
            lo, hi = float(br.min), float(br.max)
        except Exception:
            pass
        self.wb_red_spin.setRange(lo, hi)
        self.wb_blue_spin.setRange(lo, hi)

        self._apply_defaults()

        self.gain_spin.editingFinished.connect(
            lambda: self._set_float("Gain", self.gain_spin.value())
        )
        self.exp_spin.editingFinished.connect(
            lambda: self._set_float("ExposureTime", self.exp_spin.value())
        )
        self.gain_auto.toggled.connect(
            lambda on: self._on_auto_toggled("GainAuto", on, self.gain_spin, "Gain")
        )
        self.exp_auto.toggled.connect(
            lambda on: self._on_auto_toggled("ExposureAuto", on, self.exp_spin, "ExposureTime")
        )
        self.wb_auto.toggled.connect(self._on_wb_auto_toggled)
        self.wb_red_spin.editingFinished.connect(
            lambda: self._set_balance_ratio("Red", self.wb_red_spin.value())
        )
        self.wb_blue_spin.editingFinished.connect(
            lambda: self._set_balance_ratio("Blue", self.wb_blue_spin.value())
        )
        self.gamma_spin.editingFinished.connect(
            lambda: self._set_float("Gamma", self.gamma_spin.value())
        )
        self.sharpness_spin.editingFinished.connect(
            lambda: self._set_int("Sharpness", self.sharpness_spin.value())
        )
        self.defaults_btn.clicked.connect(self._apply_defaults)

    def _setup_range_float(self, name: str, spin: QDoubleSpinBox) -> None:
        try:
            node = getattr(self.node_map, name)
            spin.setRange(float(node.min), float(node.max))
        except Exception as e:
            spin.setEnabled(False)
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _setup_range_int(self, name: str, spin: QSpinBox) -> None:
        try:
            node = getattr(self.node_map, name)
            spin.setRange(int(node.min), int(node.max))
        except Exception as e:
            spin.setEnabled(False)
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _set_float(self, name: str, value: float) -> None:
        try:
            node = getattr(self.node_map, name)
            node.value = max(node.min, min(node.max, float(value)))
        except Exception as e:
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _set_int(self, name: str, value: int) -> None:
        try:
            node = getattr(self.node_map, name)
            node.value = max(node.min, min(node.max, int(value)))
        except Exception as e:
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _set_enum(self, name: str, value: str) -> None:
        try:
            getattr(self.node_map, name).value = value
        except Exception as e:
            print(f"camera {name}: {e}", file=sys.stderr, flush=True)

    def _on_auto_toggled(
        self, auto_name: str, on: bool, manual_spin: QDoubleSpinBox, value_name: str
    ) -> None:
        self._set_enum(auto_name, "Continuous" if on else "Off")
        manual_spin.setEnabled(not on)
        if not on:
            # Surface whatever auto-mode converged to.
            try:
                manual_spin.setValue(float(getattr(self.node_map, value_name).value))
            except Exception:
                pass

    def _on_wb_auto_toggled(self, on: bool) -> None:
        self._set_enum("BalanceWhiteAuto", "Continuous" if on else "Off")
        self.wb_red_spin.setEnabled(not on)
        self.wb_blue_spin.setEnabled(not on)
        if not on:
            for color, spin in (("Red", self.wb_red_spin), ("Blue", self.wb_blue_spin)):
                try:
                    self.node_map.BalanceRatioSelector.value = color
                    spin.setValue(float(self.node_map.BalanceRatio.value))
                except Exception as e:
                    print(f"camera BalanceRatio {color}: {e}", file=sys.stderr, flush=True)

    def _set_balance_ratio(self, color: str, value: float) -> None:
        if self.wb_auto.isChecked():
            return
        try:
            self.node_map.BalanceRatioSelector.value = color
            node = self.node_map.BalanceRatio
            node.value = max(node.min, min(node.max, float(value)))
        except Exception as e:
            print(f"camera BalanceRatio {color}: {e}", file=sys.stderr, flush=True)

    def _apply_defaults(self) -> None:
        """Push OUR_DEFAULTS to the camera and sync the widgets. Used at
        startup and by the Defaults button."""
        d = self._defaults
        self._set_enum("GainAuto", d["GainAuto"])
        self._set_enum("ExposureAuto", d["ExposureAuto"])
        self._set_enum("BalanceWhiteAuto", d["BalanceWhiteAuto"])
        self._set_float("Gain", d["Gain"])
        self._set_float("ExposureTime", d["ExposureTime"])
        self._set_float("Gamma", d["Gamma"])
        self._set_int("Sharpness", d["Sharpness"])
        gain_auto_on = d["GainAuto"] == "Continuous"
        exp_auto_on = d["ExposureAuto"] == "Continuous"
        wb_auto_on = d["BalanceWhiteAuto"] == "Continuous"
        # blockSignals so toggling these from code doesn't re-trigger the
        # auto-toggled handlers (which would do redundant writes).
        for cb, on in (
            (self.gain_auto, gain_auto_on),
            (self.exp_auto, exp_auto_on),
            (self.wb_auto, wb_auto_on),
        ):
            cb.blockSignals(True)
            cb.setChecked(on)
            cb.blockSignals(False)
        self.gain_spin.setEnabled(not gain_auto_on)
        self.exp_spin.setEnabled(not exp_auto_on)
        self.wb_red_spin.setEnabled(not wb_auto_on)
        self.wb_blue_spin.setEnabled(not wb_auto_on)
        self.gain_spin.setValue(float(d["Gain"]))
        self.exp_spin.setValue(float(d["ExposureTime"]))
        self.gamma_spin.setValue(float(d["Gamma"]))
        self.sharpness_spin.setValue(int(d["Sharpness"]))


class ChannelHistogram(QWidget):
    """Single-channel histogram with range markers / shaded out-of-range zones.

    Each ImageAdjustmentsPanel embeds three of these — one beneath
    each RGB row — so the histogram is visually attached to the
    spinboxes that drive its remap range.
    """

    SMOOTH_RADIUS = 2

    def __init__(self, color: tuple[int, int, int]) -> None:
        super().__init__()
        self._color = QColor(*color)
        self._data: np.ndarray | None = None
        self._lo = 0
        self._hi = 255
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, data: np.ndarray) -> None:
        if data.shape != (256,):
            return
        self._data = data
        self.update()

    def set_range(self, lo: int, hi: int) -> None:
        if (lo, hi) == (self._lo, self._hi):
            return
        self._lo = lo
        self._hi = hi
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        if self._data is not None:
            curve = self._smoothed(self._data)
            peak = float(curve.max())
            if peak > 0:
                ys = np.sqrt(curve / peak) * h
                path = QPainterPath()
                path.moveTo(0, h)
                for i in range(256):
                    x = i * (w / 255.0)
                    y = h - ys[i]
                    path.lineTo(x, y)
                path.lineTo(w, h)
                path.closeSubpath()
                fill = QColor(self._color)
                fill.setAlpha(80)
                stroke = QColor(self._color)
                stroke.setAlpha(200)
                painter.setBrush(QBrush(fill))
                pen = QPen(stroke)
                pen.setWidthF(1.0)
                painter.setPen(pen)
                painter.drawPath(path)

        # Shade [0, lo] and [hi, 255] to show what gets clipped to 0 / 255.
        if self._lo > 0 or self._hi < 255:
            shade = QColor(0, 0, 0, 110)
            x_lo = self._lo / 255.0 * w
            x_hi = self._hi / 255.0 * w
            if self._lo > 0:
                painter.fillRect(0, 0, int(x_lo), h, shade)
            if self._hi < 255:
                painter.fillRect(int(x_hi), 0, w - int(x_hi), h, shade)

        # Range marker lines.
        marker = QPen(QColor(255, 255, 255, 180))
        marker.setWidthF(1.0)
        painter.setPen(marker)
        if self._lo > 0:
            x = self._lo / 255.0 * w
            painter.drawLine(int(x), 0, int(x), h)
        if self._hi < 255:
            x = self._hi / 255.0 * w
            painter.drawLine(int(x), 0, int(x), h)

    def _smoothed(self, arr: np.ndarray) -> np.ndarray:
        r = self.SMOOTH_RADIUS
        if r <= 0:
            return arr.astype(np.float64)
        x = arr.astype(np.float64)
        n = x.size
        cs = np.concatenate(([0.0], np.cumsum(x)))
        idx = np.arange(n)
        lo = np.maximum(0, idx - r)
        hi = np.minimum(n - 1, idx + r)
        return (cs[hi + 1] - cs[lo]) / (hi - lo + 1)


class ImageAdjustmentsPanel(QGroupBox):
    """Software-side image adjustments applied per-frame on the camera worker.

    Mutates a shared ImageAdjustments under a lock; the worker reads
    immutable snapshots. UI controls publish on every change so feedback
    is live.
    """

    BRIGHTNESS_RANGE = (50, 300)
    CONTRAST_RANGE = (50, 300)
    SATURATION_RANGE = (0, 200)

    CHANNEL_COLORS = (
        (224, 49, 49),   # R
        (47, 158, 68),   # G
        (25, 113, 194),  # B
    )

    def __init__(self, adjustments: ImageAdjustments) -> None:
        super().__init__("Image Adjustments")
        self._adj = adjustments
        self._building = True

        self.brightness_slider, self.brightness_label = self._make_slider_row(
            self.BRIGHTNESS_RANGE, 100
        )
        self.contrast_slider, self.contrast_label = self._make_slider_row(
            self.CONTRAST_RANGE, 100
        )
        self.saturation_slider, self.saturation_label = self._make_slider_row(
            self.SATURATION_RANGE, 100
        )

        self.r_lo, self.r_hi = self._make_range_sliders()
        self.g_lo, self.g_hi = self._make_range_sliders()
        self.b_lo, self.b_hi = self._make_range_sliders()

        self.r_hist = ChannelHistogram(self.CHANNEL_COLORS[0])
        self.g_hist = ChannelHistogram(self.CHANNEL_COLORS[1])
        self.b_hist = ChannelHistogram(self.CHANNEL_COLORS[2])

        self.r_header = QLabel("R: 0 – 255")
        self.g_header = QLabel("G: 0 – 255")
        self.b_header = QLabel("B: 0 – 255")

        self.defaults_btn = QPushButton("Defaults")

        outer = QVBoxLayout(self)
        for name, slider, value_label in (
            ("Brightness", self.brightness_slider, self.brightness_label),
            ("Contrast", self.contrast_slider, self.contrast_label),
            ("Saturation", self.saturation_slider, self.saturation_label),
        ):
            head = QHBoxLayout()
            head.addWidget(QLabel(f"{name}:"))
            head.addStretch(1)
            head.addWidget(value_label)
            outer.addLayout(head)
            outer.addWidget(slider)

        for css_color, header, lo, hi, hist in (
            ("#c92a2a", self.r_header, self.r_lo, self.r_hi, self.r_hist),
            ("#2f9e44", self.g_header, self.g_lo, self.g_hi, self.g_hist),
            ("#1971c2", self.b_header, self.b_lo, self.b_hi, self.b_hist),
        ):
            header.setStyleSheet(f"color: {css_color}; font-weight: bold;")
            outer.addWidget(header)
            outer.addWidget(hist)
            outer.addWidget(lo)
            outer.addWidget(hi)

        outer.addWidget(self.defaults_btn)

        self.brightness_slider.valueChanged.connect(self._on_brightness)
        self.contrast_slider.valueChanged.connect(self._on_contrast)
        self.saturation_slider.valueChanged.connect(self._on_saturation)
        for lo, hi, name in (
            (self.r_lo, self.r_hi, "r_range"),
            (self.g_lo, self.g_hi, "g_range"),
            (self.b_lo, self.b_hi, "b_range"),
        ):
            lo.valueChanged.connect(lambda _v, n=name: self._on_range(n))
            hi.valueChanged.connect(lambda _v, n=name: self._on_range(n))

        self.defaults_btn.clicked.connect(self._apply_defaults)
        self._building = False

    @Slot(object)
    def set_histograms(self, hist: np.ndarray) -> None:
        """Slot for CameraWorker.histograms_ready ((3, 256) int array)."""
        if hist.shape != (3, 256):
            return
        self.r_hist.set_data(hist[0])
        self.g_hist.set_data(hist[1])
        self.b_hist.set_data(hist[2])

    def _make_slider_row(
        self, span: tuple[int, int], default: int
    ) -> tuple[QSlider, QLabel]:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(*span)
        slider.setValue(default)
        slider.setSingleStep(1)
        slider.setPageStep(10)
        value_label = QLabel(f"{default}%")
        value_label.setMinimumWidth(48)
        value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        return slider, value_label

    def _make_range_sliders(self) -> tuple[QSlider, QSlider]:
        lo = QSlider(Qt.Orientation.Horizontal)
        lo.setRange(0, 254)
        lo.setValue(0)
        lo.setSingleStep(1)
        lo.setPageStep(10)
        hi = QSlider(Qt.Orientation.Horizontal)
        hi.setRange(1, 255)
        hi.setValue(255)
        hi.setSingleStep(1)
        hi.setPageStep(10)
        return lo, hi

    def _on_brightness(self, v: int) -> None:
        self.brightness_label.setText(f"{v}%")
        if self._building:
            return
        self._adj.update(brightness=v)

    def _on_contrast(self, v: int) -> None:
        self.contrast_label.setText(f"{v}%")
        if self._building:
            return
        self._adj.update(contrast=v)

    def _on_saturation(self, v: int) -> None:
        self.saturation_label.setText(f"{v}%")
        if self._building:
            return
        self._adj.update(saturation=v)

    def _on_range(self, name: str) -> None:
        if self._building:
            return
        sliders, hist, header, prefix = {
            "r_range": ((self.r_lo, self.r_hi), self.r_hist, self.r_header, "R"),
            "g_range": ((self.g_lo, self.g_hi), self.g_hist, self.g_header, "G"),
            "b_range": ((self.b_lo, self.b_hi), self.b_hist, self.b_header, "B"),
        }[name]
        lo, hi = sliders[0].value(), sliders[1].value()
        if lo >= hi:
            if self.sender() is sliders[0]:
                hi = min(255, lo + 1)
                sliders[1].blockSignals(True)
                sliders[1].setValue(hi)
                sliders[1].blockSignals(False)
            else:
                lo = max(0, hi - 1)
                sliders[0].blockSignals(True)
                sliders[0].setValue(lo)
                sliders[0].blockSignals(False)
        hist.set_range(lo, hi)
        header.setText(f"{prefix}: {lo} – {hi}")
        self._adj.update(**{name: (lo, hi)})

    def _apply_defaults(self) -> None:
        snap = self._adj.reset()
        self._building = True
        try:
            self.brightness_slider.setValue(snap.brightness)
            self.contrast_slider.setValue(snap.contrast)
            self.saturation_slider.setValue(snap.saturation)
            for (lo_w, hi_w), hist, header, prefix, rng in (
                ((self.r_lo, self.r_hi), self.r_hist, self.r_header, "R", snap.r_range),
                ((self.g_lo, self.g_hi), self.g_hist, self.g_header, "G", snap.g_range),
                ((self.b_lo, self.b_hi), self.b_hist, self.b_header, "B", snap.b_range),
            ):
                lo_w.setValue(rng[0])
                hi_w.setValue(rng[1])
                hist.set_range(*rng)
                header.setText(f"{prefix}: {rng[0]} – {rng[1]}")
            self.brightness_label.setText(f"{snap.brightness}%")
            self.contrast_label.setText(f"{snap.contrast}%")
            self.saturation_label.setText(f"{snap.saturation}%")
        finally:
            self._building = False


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

        self.max_output_spin = QDoubleSpinBox()
        self.max_output_spin.setRange(0.0, 100.0)
        self.max_output_spin.setDecimals(1)
        self.max_output_spin.setSingleStep(1.0)
        self.max_output_spin.setSuffix(" %")
        self.max_output_spin.setKeyboardTracking(False)
        self.max_output_spin.setValue(float(cfg.get("max_output_default", 40.0)))

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
        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("Max output:"))
        max_row.addWidget(self.max_output_spin, stretch=1)
        outer.addLayout(max_row)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.diag_btn)
        outer.addLayout(btn_row)
        outer.addStretch(1)

        self.setpoint_spin.editingFinished.connect(self._on_set)
        self.max_output_spin.editingFinished.connect(self._on_set_max_output)
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.diag_btn.clicked.connect(self._on_diag)

        self._poll_interval_s = int(cfg.get("poll_interval_ms", 1000)) / 1000.0
        self._worker: PollWorker | None = None
        self._worker_thread: QThread | None = None

        try:
            self.heater.open()
        except Exception as e:
            self.status_label.setText(f"open failed: {e}")
            self.setpoint_spin.setEnabled(False)
            self.max_output_spin.setEnabled(False)
            self.run_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.diag_btn.setEnabled(False)
            return

        self.status_label.setText(f"connected on {self.heater.port}")
        # Pre-populate before the worker starts so the spinner and labels
        # show real values from t=0 instead of QDoubleSpinBox's default 0.
        self._apply_state(self._read_state())

        self._worker_thread = QThread()
        self._worker = PollWorker(self._read_state, self._poll_interval_s)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.state_ready.connect(self._apply_state)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker_thread.start()

    def _on_set(self) -> None:
        try:
            self.heater.set_setpoint(self.setpoint_spin.value())
        except Exception as e:
            self.status_label.setText(f"set err: {e}")

    def _on_set_max_output(self) -> None:
        try:
            self.heater.set_output_limit_high(self.max_output_spin.value())
        except Exception as e:
            self.status_label.setText(f"max-output err: {e}")

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

    def _read_state(self) -> dict:
        """Worker-thread: read heater state. Must not touch Qt widgets."""
        payload: dict = {}
        try:
            payload["pv"] = self.heater.process_value()
        except Exception as e:  # noqa: BLE001
            payload["pv_err"] = str(e)
        try:
            payload["sp"] = self.heater.setpoint()
        except Exception:  # noqa: BLE001 — keep going
            pass
        try:
            payload["state"] = self.heater.system_state()
        except Exception:  # noqa: BLE001
            pass
        try:
            payload["out"] = self.heater.output_percent()
        except Exception as e:  # noqa: BLE001
            payload["out_err"] = str(e)
        try:
            payload["out_hi"] = self.heater.output_limit_high()
        except Exception:  # noqa: BLE001 — keep going
            pass
        return payload

    @Slot(object)
    def _apply_state(self, payload: dict) -> None:
        """Main-thread: render a payload from the polling worker."""
        if "pv" in payload:
            self.pv_label.setText(f"{payload['pv']:.2f} {self.units}")
        elif "pv_err" in payload:
            self.pv_label.setText(f"pv err: {payload['pv_err']}")
        if "sp" in payload and not self.setpoint_spin.hasFocus():
            self.setpoint_spin.setValue(payload["sp"])
        if "state" in payload:
            self.run_label.setText(f"state: {payload['state'].name}")
        else:
            self.run_label.setText("")
        if "out" in payload:
            self.output_label.setText(f"output: {payload['out']:.1f} %")
        elif "out_err" in payload:
            self.output_label.setText(f"output err: {payload['out_err']}")
        if "out_hi" in payload and not self.max_output_spin.hasFocus():
            self.max_output_spin.setValue(payload["out_hi"])

    def shutdown(self) -> None:
        if self._worker is not None and self._worker_thread is not None:
            self._worker.stop()
            self._worker_thread.quit()
            if not self._worker_thread.wait(2000):
                print(
                    "heater thread did not exit cleanly",
                    file=sys.stderr,
                    flush=True,
                )
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
        self.adjustments_panel: ImageAdjustmentsPanel | None = None
        self.adjustments = ImageAdjustments()

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
        self.camera_worker = CameraWorker(self.acquirer, self.adjustments)
        self.camera_worker.moveToThread(self.camera_thread)
        self.camera_thread.started.connect(self.camera_worker.run)
        self.camera_worker.frame_ready.connect(self._on_frame)
        self.camera_worker.error.connect(self._on_frame_error)
        self.camera_worker.finished.connect(self.camera_thread.quit)
        if self.adjustments_panel is not None:
            self.camera_worker.histograms_ready.connect(
                self.adjustments_panel.set_histograms,
                Qt.ConnectionType.QueuedConnection,
            )
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

        self.adjustments_panel = ImageAdjustmentsPanel(self.adjustments)

        self.camera_options_panel = CameraOptionsPanel(
            self.acquirer.remote_device.node_map, camera_cfg
        )

        layout.addWidget(recording)
        layout.addWidget(presets)
        layout.addWidget(self.adjustments_panel)
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
