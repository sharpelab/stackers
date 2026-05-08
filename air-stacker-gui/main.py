"""Live frame viewer for the Air Stacker camera (Flea3 via harvesters/GenTL)."""

from __future__ import annotations

import argparse
import contextlib
import logging
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
from PySide6.QtCore import QObject, QRect, Qt, QThread, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QIcon, QImage, QPainter, QPainterPath, QPen
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
    QVBoxLayout,
    QWidget,
)
from superqt import QRangeSlider

from conex import ConexAxis, error_label, state_label
from heater import OmegaPlatinum

import tomllib

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "icons" / "air_stacker.ico"

log = logging.getLogger("airstacker")


def configure_logging(verbose: int) -> None:
    """verbose=0 → WARNING (default); 1 → INFO; 2+ → DEBUG."""
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose >= 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False


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
    """Convert a harvesters component buffer into an owned RGB888 ndarray.

    All paths return a fresh, owned, C-contiguous array — safe to use
    after the harvesters buffer is requeued. cv2.cvtColor already
    allocates fresh output; the RGB pass-through is the only path that
    would otherwise return a view, so we copy it explicitly there.
    """
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
        return data.reshape(height, width, 3).copy()
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

    cv2.calcHist handles channel selection internally (no split copy)
    and uses cv2's SIMD-tuned inner loop. Measured ~4x faster than
    split+bincount and consistent across platforms.
    """
    out = np.empty((3, 256), dtype=np.int64)
    for c in range(3):
        h = cv2.calcHist([rgb], [c], None, [256], [0, 256])
        out[c] = h.ravel().astype(np.int64)
    return out


class CameraDisplay(QLabel):
    """Camera frame view with a bottom-left FPS overlay.

    paintEvent does aspect-preserved drawImage with scale capped
    at 1.0 — when the window exceeds the source, we render at
    native size with letterboxing instead of CPU-upscaling.
    Per-frame paint cost is bounded by source size.
    """

    OVERLAY_MARGIN = 8

    def __init__(self) -> None:
        super().__init__()
        self._image: QImage | None = None
        # Hold the numpy buffer the QImage views into until the next
        # frame arrives — QImage(data, ...) does not own its bytes.
        self._frame_ref = None
        self.fps_label = QLabel("-- fps", self)
        self.fps_label.setStyleSheet(
            "color: white; background-color: rgba(0, 0, 0, 140);"
            " padding: 2px 6px; border-radius: 3px;"
        )
        self.fps_label.adjustSize()
        self._reposition_overlay()

    def set_frame(self, image: QImage, frame_ref) -> None:
        self._image = image
        self._frame_ref = frame_ref
        self.update()

    def clear_frame(self) -> None:
        self._image = None
        self._frame_ref = None
        self.update()

    def set_fps(self, fps: float | None) -> None:
        self.fps_label.setText("-- fps" if fps is None else f"{fps:.1f} fps")
        self.fps_label.adjustSize()
        self._reposition_overlay()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_overlay()

    def paintEvent(self, event) -> None:  # noqa: ARG002
        if self._image is None:
            super().paintEvent(event)  # QLabel text path
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))
        sw, sh = self._image.width(), self._image.height()
        tw, th = self.width(), self.height()
        if sw <= 0 or sh <= 0 or tw <= 0 or th <= 0:
            return
        # Cap at 1.0 — never CPU-upscale. Above source size we
        # letterbox at native size; below we downscale (cheap).
        scale = min(tw / sw, th / sh, 1.0)
        dw = int(sw * scale)
        dh = int(sh * scale)
        x = (tw - dw) // 2
        y = (th - dh) // 2
        painter.drawImage(QRect(x, y, dw, dh), self._image)

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


class FrameMailbox:
    """Single-slot, latest-wins mailbox between worker threads.

    publish() overwrites whatever's there and wakes a waiting taker.
    take(timeout) blocks until a frame is published or the timeout
    expires. Drops intermediate frames silently — by design, since
    the consumer always wants the freshest one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None
        self._wake = threading.Event()

    def publish(self, frame: np.ndarray) -> None:
        with self._lock:
            self._latest = frame
        self._wake.set()

    def take(self, timeout: float | None = None) -> np.ndarray | None:
        if not self._wake.wait(timeout):
            return None
        self._wake.clear()
        with self._lock:
            f = self._latest
            self._latest = None
            return f

    def wake(self) -> None:
        """Unblock any taker — used to break the take() wait at shutdown."""
        self._wake.set()


class CameraAcquireWorker(QObject):
    """Thread A: pull from harvesters, debayer, hand off to processing.

    Stays pinned to the camera frame rate. Pushes debayered RGB
    frames into a FrameMailbox; if processing is slower than the
    camera, intermediate frames drop at that mailbox (latest-wins),
    but the camera buffer queue stays drained.
    """

    error = Signal(str)
    finished = Signal()

    def __init__(self, acquirer, mailbox: FrameMailbox) -> None:
        super().__init__()
        self._acquirer = acquirer
        self._mailbox = mailbox
        self._running = False

    @Slot()
    def run(self) -> None:
        self._running = True
        log_count = 0
        log_start = time.monotonic()
        t_fetch = t_debayer = t_publish = 0.0
        while self._running:
            try:
                t0 = time.monotonic()
                with self._acquirer.fetch(timeout=0.5) as buffer:
                    comp = buffer.payload.components[0]
                    t1 = time.monotonic()
                    rgb = to_rgb(comp.data, comp.width, comp.height, comp.data_format)
                t2 = time.monotonic()
                self._mailbox.publish(rgb)
                t3 = time.monotonic()
                t_fetch += t1 - t0
                t_debayer += t2 - t1
                t_publish += t3 - t2
                log_count += 1
                if t3 - log_start > 2.0:
                    span = t3 - log_start
                    log.info(
                        "acq    %.1f fps  fetch=%.1f deb=%.1f pub=%.2f ms/frame",
                        log_count / span,
                        t_fetch / log_count * 1000,
                        t_debayer / log_count * 1000,
                        t_publish / log_count * 1000,
                    )
                    log_count = 0
                    log_start = t3
                    t_fetch = t_debayer = t_publish = 0.0
            except Exception as e:  # noqa: BLE001 — surface errors then keep trying
                self.error.emit(str(e))
                time.sleep(0.1)
        self.finished.emit()

    def stop(self) -> None:
        self._running = False


class CameraProcessWorker(QObject):
    """Thread B: histogram + adjustments + GUI handoff.

    Decoupled from the camera: takes the latest debayered frame
    from the acquire mailbox, runs the heavy per-pixel work, and
    publishes the result for the GUI. Slow adjustments don't back
    up the camera queue — they just reduce the *processed* fps.
    """

    frame_ready = Signal()
    histograms_ready = Signal(object)
    finished = Signal()

    def __init__(
        self,
        source: FrameMailbox,
        adjustments: ImageAdjustments | None = None,
    ) -> None:
        super().__init__()
        self._source = source
        self._adjustments = adjustments
        self._running = False
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None

    @Slot()
    def run(self) -> None:
        self._running = True
        log_count = 0
        log_start = time.monotonic()
        t_wait = t_hist = t_adjust = t_publish = 0.0
        while self._running:
            t0 = time.monotonic()
            rgb = self._source.take(timeout=0.1)
            if rgb is None:
                continue
            t1 = time.monotonic()
            try:
                self.histograms_ready.emit(compute_histograms(rgb))
            except Exception as e:  # noqa: BLE001
                log.warning("histogram err: %s", e)
            t2 = time.monotonic()
            if self._adjustments is not None:
                snap = self._adjustments.get()
                if not snap.is_identity:
                    try:
                        rgb = apply_adjustments(rgb, snap)
                    except Exception as e:  # noqa: BLE001
                        log.warning("adjustment err: %s", e)
            t3 = time.monotonic()
            with self._lock:
                notify = self._latest is None
                self._latest = rgb
            if notify:
                self.frame_ready.emit()
            t4 = time.monotonic()
            t_wait += t1 - t0
            t_hist += t2 - t1
            t_adjust += t3 - t2
            t_publish += t4 - t3
            log_count += 1
            if t4 - log_start > 2.0:
                span = t4 - log_start
                log.info(
                    "proc   %.1f fps  wait=%.1f hist=%.1f adj=%.1f pub=%.2f ms/frame",
                    log_count / span,
                    t_wait / log_count * 1000,
                    t_hist / log_count * 1000,
                    t_adjust / log_count * 1000,
                    t_publish / log_count * 1000,
                )
                log_count = 0
                log_start = t4
                t_wait = t_hist = t_adjust = t_publish = 0.0
        self.finished.emit()

    def take_latest(self) -> np.ndarray | None:
        with self._lock:
            f = self._latest
            self._latest = None
            return f

    def stop(self) -> None:
        self._running = False
        self._source.wake()  # unblock the take(timeout) wait


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
                log.warning("axis %s thread did not exit cleanly", self.title())
        self.axis.close()


class CameraOptionsPanel(QGroupBox):
    """Live camera controls (GenICam node map): gain / exposure (with
    auto) and white balance (auto + manual Red/Blue ratios).

    The Defaults button reverts everything to OUR_DEFAULTS — factory values
    plus the lab's preferred gain/exposure tweaks. config.toml's [camera]
    section can override gain/exposure for those keys it provides.

    Gamma and sharpness are intentionally not exposed: the on-board nodes
    are read-only on this Flea3 / Spinnaker 2.3 combo. Software gamma
    lives in the Image Adjustments panel.
    """

    # Hard cap on the exposure spinbox. The camera's actual ceiling is
    # ~1/AcquisitionFrameRate.min (≈900 ms on the Flea3); 1 s is the
    # operator-friendly round number we expose, and writes are clamped
    # to whatever the camera allows.
    MAX_EXPOSURE_MS: float = 1000.0

    OUR_DEFAULTS: dict = {
        "Gain": 1.61,
        "ExposureTime": 16798.0,  # µs (camera-native units)
        "GainAuto": "Off",
        "ExposureAuto": "Off",
        "BalanceWhiteAuto": "Off",
        "BalanceRatioRed": 0.6,
        "BalanceRatioBlue": 3.3,
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
        self.exp_spin.setDecimals(2)
        self.exp_spin.setSingleStep(1.0)
        self.exp_spin.setSuffix(" ms")
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

        self.defaults_btn = QPushButton("Defaults")

        outer = QVBoxLayout(self)
        for label, spin, auto in (
            ("Gain:", self.gain_spin, self.gain_auto),
            ("Exposure:", self.exp_spin, self.exp_auto),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(spin, stretch=1)
            row.addWidget(auto)
            outer.addLayout(row)
        wb_red_row = QHBoxLayout()
        wb_red_row.addWidget(QLabel("WB Red:"))
        wb_red_row.addWidget(self.wb_red_spin, stretch=1)
        wb_red_row.addWidget(self.wb_auto)
        outer.addLayout(wb_red_row)
        wb_blue_row = QHBoxLayout()
        wb_blue_row.addWidget(QLabel("WB Blue:"))
        wb_blue_row.addWidget(self.wb_blue_spin, stretch=1)
        outer.addLayout(wb_blue_row)
        outer.addWidget(self.defaults_btn)

        self._setup_range_float("Gain", self.gain_spin)
        # Exposure spinbox is in ms; the camera reports ExposureTime in µs.
        # Set a fixed range up to MAX_EXPOSURE_MS — writes are clamped to
        # whatever the camera actually allows (gated by AcquisitionFrameRate).
        self.exp_spin.setRange(0.05, self.MAX_EXPOSURE_MS)
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
            lambda: self._set_exposure_us(self.exp_spin.value() * 1000.0)
        )
        self.gain_auto.toggled.connect(self._on_gain_auto_toggled)
        self.exp_auto.toggled.connect(self._on_exp_auto_toggled)
        self.wb_auto.toggled.connect(self._on_wb_auto_toggled)
        self.wb_red_spin.editingFinished.connect(
            lambda: self._set_balance_ratio("Red", self.wb_red_spin.value())
        )
        self.wb_blue_spin.editingFinished.connect(
            lambda: self._set_balance_ratio("Blue", self.wb_blue_spin.value())
        )
        self.defaults_btn.clicked.connect(self._apply_defaults)

    def _setup_range_float(self, name: str, spin: QDoubleSpinBox) -> None:
        try:
            node = getattr(self.node_map, name)
            spin.setRange(float(node.min), float(node.max))
        except Exception as e:
            spin.setEnabled(False)
            log.debug("camera %s range: %s", name, e)

    def _set_float(self, name: str, value: float) -> None:
        try:
            node = getattr(self.node_map, name)
            node.value = max(node.min, min(node.max, float(value)))
        except Exception as e:
            log.debug("camera %s: %s", name, e)

    def _set_enum(self, name: str, value: str) -> None:
        try:
            getattr(self.node_map, name).value = value
        except Exception as e:
            log.debug("camera %s: %s", name, e)

    def _on_gain_auto_toggled(self, on: bool) -> None:
        self._set_enum("GainAuto", "Continuous" if on else "Off")
        self.gain_spin.setEnabled(not on)
        if not on:
            try:
                self.gain_spin.setValue(float(self.node_map.Gain.value))
            except Exception:
                pass

    def _on_exp_auto_toggled(self, on: bool) -> None:
        self._set_enum("ExposureAuto", "Continuous" if on else "Off")
        self.exp_spin.setEnabled(not on)
        if not on:
            # Camera reports ExposureTime in µs; spinbox is in ms.
            try:
                self.exp_spin.setValue(float(self.node_map.ExposureTime.value) / 1000.0)
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
                    log.debug("camera BalanceRatio %s: %s", color, e)

    def _set_balance_ratio(self, color: str, value: float) -> None:
        if self.wb_auto.isChecked():
            return
        try:
            self.node_map.BalanceRatioSelector.value = color
            node = self.node_map.BalanceRatio
            node.value = max(node.min, min(node.max, float(value)))
        except Exception as e:
            log.debug("camera BalanceRatio %s: %s", color, e)

    def _set_exposure_us(self, value_us: float) -> None:
        """Write ExposureTime (µs) and pin AcquisitionFrameRate to the
        camera-computed max for that exposure.

        ExposureTime.max is gated by the current AcquisitionFrameRate, so
        going from a short exposure to a long one requires lowering
        AcquisitionFrameRate first to unlock the range. Sequence:
          1. AcquisitionFrameRate → its min (unlocks long exposures)
          2. ExposureTime → desired
          3. AcquisitionFrameRate → its (now-recomputed) max
        """
        try:
            afr = self.node_map.AcquisitionFrameRate
            afr.value = float(afr.min)
        except Exception as e:
            log.debug("camera AcquisitionFrameRate (drop): %s", e)
        try:
            et = self.node_map.ExposureTime
            et.value = max(float(et.min), min(float(et.max), float(value_us)))
        except Exception as e:
            log.debug("camera ExposureTime: %s", e)
        try:
            afr = self.node_map.AcquisitionFrameRate
            afr.value = float(afr.max)
        except Exception as e:
            log.debug("camera AcquisitionFrameRate (raise): %s", e)

    def _apply_defaults(self) -> None:
        """Push OUR_DEFAULTS to the camera and sync the widgets. Used at
        startup and by the Defaults button."""
        d = self._defaults
        self._set_enum("GainAuto", d["GainAuto"])
        self._set_enum("ExposureAuto", d["ExposureAuto"])
        self._set_enum("BalanceWhiteAuto", d["BalanceWhiteAuto"])
        self._set_float("Gain", d["Gain"])
        self._set_exposure_us(float(d["ExposureTime"]))
        # WB ratios are only writable while BalanceWhiteAuto is Off.
        if d["BalanceWhiteAuto"] != "Continuous":
            for color, key in (("Red", "BalanceRatioRed"), ("Blue", "BalanceRatioBlue")):
                try:
                    self.node_map.BalanceRatioSelector.value = color
                    node = self.node_map.BalanceRatio
                    node.value = max(node.min, min(node.max, float(d[key])))
                except Exception as e:
                    log.debug("camera BalanceRatio %s: %s", color, e)
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
        # Spinbox is ms; OUR_DEFAULTS stores µs.
        self.exp_spin.setValue(float(d["ExposureTime"]) / 1000.0)
        self.wb_red_spin.setValue(float(d["BalanceRatioRed"]))
        self.wb_blue_spin.setValue(float(d["BalanceRatioBlue"]))


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

        self.r_range = self._make_range_slider()
        self.g_range = self._make_range_slider()
        self.b_range = self._make_range_slider()

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

        for css_color, header, slider, hist in (
            ("#c92a2a", self.r_header, self.r_range, self.r_hist),
            ("#2f9e44", self.g_header, self.g_range, self.g_hist),
            ("#1971c2", self.b_header, self.b_range, self.b_hist),
        ):
            header.setStyleSheet(f"color: {css_color}; font-weight: bold;")
            outer.addWidget(header)
            outer.addWidget(hist)
            outer.addWidget(slider)

        outer.addWidget(self.defaults_btn)

        self.brightness_slider.valueChanged.connect(self._on_brightness)
        self.contrast_slider.valueChanged.connect(self._on_contrast)
        self.saturation_slider.valueChanged.connect(self._on_saturation)
        for slider, name in (
            (self.r_range, "r_range"),
            (self.g_range, "g_range"),
            (self.b_range, "b_range"),
        ):
            slider.valueChanged.connect(lambda _v, n=name: self._on_range(n))

        self.defaults_btn.clicked.connect(self._apply_defaults)
        self._building = False

    @Slot(object)
    def set_histograms(self, hist: np.ndarray) -> None:
        """Slot for CameraProcessWorker.histograms_ready ((3, 256) int array)."""
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

    def _make_range_slider(self) -> QRangeSlider:
        slider = QRangeSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 255)
        slider.setValue((0, 255))
        slider.setSingleStep(1)
        slider.setPageStep(10)
        return slider

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
        slider, hist, header, prefix = {
            "r_range": (self.r_range, self.r_hist, self.r_header, "R"),
            "g_range": (self.g_range, self.g_hist, self.g_header, "G"),
            "b_range": (self.b_range, self.b_hist, self.b_header, "B"),
        }[name]
        lo, hi = (int(v) for v in slider.value())
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
            for slider, hist, header, prefix, rng in (
                (self.r_range, self.r_hist, self.r_header, "R", snap.r_range),
                (self.g_range, self.g_hist, self.g_header, "G", snap.g_range),
                (self.b_range, self.b_hist, self.b_header, "B", snap.b_range),
            ):
                slider.setValue(rng)
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
        outer.addLayout(btn_row)
        outer.addStretch(1)

        self.setpoint_spin.editingFinished.connect(self._on_set)
        self.max_output_spin.editingFinished.connect(self._on_set_max_output)
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)

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
                log.warning("heater thread did not exit cleanly")
        self.heater.close()


class CameraWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Air Stacker — live")
        self.label = CameraDisplay()
        self.label.setText("connecting…")
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

        # Two-stage pipeline: acq thread fetches+debayers and pushes
        # into the acq mailbox; proc thread takes the latest, runs
        # histogram + adjustments, and publishes for the GUI. Slow
        # adjustments slow the proc rate but never the acq rate.
        self.acq_mailbox = FrameMailbox()

        self.acq_thread = QThread()
        self.acq_worker = CameraAcquireWorker(self.acquirer, self.acq_mailbox)
        self.acq_worker.moveToThread(self.acq_thread)
        self.acq_thread.started.connect(self.acq_worker.run)
        self.acq_worker.error.connect(self._on_frame_error)
        self.acq_worker.finished.connect(self.acq_thread.quit)

        self.proc_thread = QThread()
        self.proc_worker = CameraProcessWorker(self.acq_mailbox, self.adjustments)
        self.proc_worker.moveToThread(self.proc_thread)
        self.proc_thread.started.connect(self.proc_worker.run)
        self.proc_worker.frame_ready.connect(self._on_frame)
        self.proc_worker.finished.connect(self.proc_thread.quit)
        if self.adjustments_panel is not None:
            self.proc_worker.histograms_ready.connect(
                self.adjustments_panel.set_histograms,
                Qt.ConnectionType.QueuedConnection,
            )

        self.acq_thread.start()
        self.proc_thread.start()

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
                log.warning("camera %s: %s", name, e)
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
            log.info(
                "camera AcquisitionFrameRate = %.2f Hz (range %.2f..%.2f)",
                rate.value,
                rate.min,
                rate.max,
            )
        except Exception as e:
            log.warning("camera AcquisitionFrameRate: %s", e)

    def _build_settings_panel(self, camera_cfg: dict) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.adjustments_panel = ImageAdjustmentsPanel(self.adjustments)

        self.camera_options_panel = CameraOptionsPanel(
            self.acquirer.remote_device.node_map, camera_cfg
        )

        layout.addWidget(self.camera_options_panel)
        layout.addWidget(self.adjustments_panel)
        layout.addStretch(1)
        return panel

    def _build_right_panel(self, axes_cfg: list[dict], heater_cfg: dict | None) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(300)
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
        layout.addWidget(recording)

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
        rgb = self.proc_worker.take_latest()
        if rgb is None:
            return  # drained by a previous slot run
        t0 = time.monotonic()
        h, w, _ = rgb.shape
        qimg = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
        # No CPU pre-scale, no QPixmap copy — paintEvent does an
        # aspect-preserving drawImage at the widget's current size.
        self.label.set_frame(qimg, rgb)
        self._update_fps()
        elapsed = time.monotonic() - t0
        self._proc_total += elapsed
        self._proc_max = max(self._proc_max, elapsed)
        self._proc_count += 1
        if self._proc_count >= 60:
            avg_ms = self._proc_total / self._proc_count * 1000
            max_ms = self._proc_max * 1000
            log.debug(
                "_on_frame avg=%.1fms max=%.1fms n=%d",
                avg_ms,
                max_ms,
                self._proc_count,
            )
            self._proc_total = 0.0
            self._proc_max = 0.0
            self._proc_count = 0

    def _on_frame_error(self, msg: str) -> None:
        self.label.clear_frame()
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
        # Stop acq first so no new frames land in the mailbox; then
        # proc, which will drain its take(timeout) wait via the wake.
        self.acq_worker.stop()
        self.proc_worker.stop()
        self.acq_thread.quit()
        self.proc_thread.quit()
        if not self.acq_thread.wait(2000):
            log.warning("acq thread did not exit cleanly")
        if not self.proc_thread.wait(2000):
            log.warning("proc thread did not exit cleanly")
        for ap in self.axis_panels:
            ap.shutdown()
        if self.heater_panel is not None:
            self.heater_panel.shutdown()
        self.acquirer.stop()
        self.acquirer.destroy()
        self.harvester.reset()
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(prog="air-stacker-gui")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v: include INFO messages; -vv: include DEBUG messages",
    )
    args, qt_args = parser.parse_known_args()
    configure_logging(args.verbose)

    # Set an explicit AppUserModelID on Windows so the taskbar uses our
    # icon instead of grouping under the pythonw.exe parent.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "sharpelab.airstacker.gui"
            )
        except Exception as e:  # noqa: BLE001
            log.debug("AppUserModelID set failed: %s", e)

    app = QApplication([sys.argv[0]] + qt_args)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = CameraWindow()
    win.resize(960, 720)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
