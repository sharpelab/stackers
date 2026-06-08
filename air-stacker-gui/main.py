"""Live frame viewer for the Air Stacker camera (Flea3 via FLIR Spinnaker / PySpin)."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

import cv2
import numpy as np
import PySpin
from PySide6.QtCore import QLockFile, QObject, QPointF, QRect, QRectF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QPainter,
    QPen,
    QSurfaceFormat,
)
from PySide6.QtOpenGL import QOpenGLTexture, QOpenGLTextureBlitter, QOpenGLWindow
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from superqt import QRangeSlider

from conex import ConexAxis, ConexError, error_label, state_label
from focus_metric import sharpness as compute_sharpness
from heater import OmegaPlatinum, SystemState
from overlay import (
    DrawTool,
    FreehandStroke,
    LineSegment,
    OverlayLayer,
    OverlayPrimitive,
    normalize_pos,
)
from smc100_panel import SMC100Panel
from status_bar import StatusBar
from webcam import WebcamConfig
from webcam_window import WebcamWindow
from widgets import action_button
from yoko_panel import YokoPanel

import tomlkit

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
# Per-machine GUI state (user presets, window geometries). Untracked — see
# .gitignore. Lives next to config.toml so the GUI doesn't need a separate
# config-dir convention; contents diverge per checkout (lab PC vs laptops).
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.toml"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "icons" / "air_stacker.ico"

# On-disk log directory. Top-level of the operator's home so it's easy to
# find after a crash (vs. buried under AppData on Windows), and outside the
# git checkout so rotated files don't pollute the working tree.
LOG_DIR = Path.home() / "air-stacker-gui-logs"
# Single-instance lock — sits next to the log file. QLockFile auto-removes
# on normal exit and detects dead-PID staleness on its own; if the GUI was
# SIGKILL'd or the host rebooted with the file still present, the operator
# can delete it manually (it's listed in the "already running" dialog).
LOCK_PATH = LOG_DIR / ".air-stacker-gui.lock"

log = logging.getLogger("airstacker")


def configure_logging(verbose: int) -> None:
    """verbose=0 → WARNING (default); 1 → INFO; 2+ → DEBUG.

    Stderr verbosity honors ``verbose``. A separate ``RotatingFileHandler``
    captures INFO+ to ``~/air-stacker-gui-logs/`` so forensics survive
    terminal closure. Size-based rotation (10 MiB × 10) caps disk use at
    ~110 MB regardless of session length; time-based rotation would either
    truncate busy days or blow the budget when the GUI is left running for
    many days. INFO threshold (not DEBUG) keeps the file from flooding with
    periodic perf tickers (camera fps, paintGL ms) — those live at DEBUG
    and are only reachable via ``-vv`` on stderr.
    """
    if verbose >= 2:
        stderr_level = logging.DEBUG
    elif verbose >= 1:
        stderr_level = logging.INFO
    else:
        stderr_level = logging.WARNING

    # Logger sits at DEBUG so both handlers see every record; each handler
    # gates its own level. Without this, the verbose=0 path would mask
    # DEBUG before the file handler could ever see it.
    log.setLevel(logging.DEBUG)
    log.propagate = False

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(stderr_level)
    stderr.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    log.addHandler(stderr)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = LOG_DIR / "air-stacker-gui.log"
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        log.addHandler(file_handler)
        log.info("logging to %s", file_path)
    except OSError as e:
        # Bad permissions or read-only home shouldn't keep the GUI from
        # starting — fall through with stderr-only logging.
        log.warning("could not configure file logging at %s: %s", LOG_DIR, e)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"missing config: {CONFIG_PATH}")
    return tomlkit.parse(CONFIG_PATH.read_text(encoding="utf-8"))


def load_settings() -> dict:
    """Read settings.toml. Returns an empty dict if the file doesn't exist
    yet — fresh checkouts have no local state until the GUI writes some.
    """
    if not SETTINGS_PATH.exists():
        return {}
    return tomlkit.parse(SETTINGS_PATH.read_text(encoding="utf-8"))


def _node_float_get(nm, name: str) -> float | None:
    try:
        node = PySpin.CFloatPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
            return float(node.GetValue())
    except Exception as e:
        log.debug("camera %s read: %s", name, e)
    return None


def _node_float_range(nm, name: str) -> tuple[float, float] | None:
    try:
        node = PySpin.CFloatPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
            return float(node.GetMin()), float(node.GetMax())
    except Exception as e:
        log.debug("camera %s range: %s", name, e)
    return None


def _node_float_set(nm, name: str, value: float) -> bool:
    try:
        node = PySpin.CFloatPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
            v = max(node.GetMin(), min(node.GetMax(), float(value)))
            node.SetValue(v)
            return True
    except Exception as e:
        log.debug("camera %s set: %s", name, e)
    return False


def _node_enum_get(nm, name: str) -> str | None:
    try:
        node = PySpin.CEnumerationPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
            return node.GetCurrentEntry().GetSymbolic()
    except Exception as e:
        log.debug("camera %s read: %s", name, e)
    return None


def _node_enum_set(nm, name: str, value: str) -> bool:
    try:
        node = PySpin.CEnumerationPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
            entry = node.GetEntryByName(value)
            if entry and PySpin.IsAvailable(entry) and PySpin.IsReadable(entry):
                node.SetIntValue(entry.GetValue())
                return True
    except Exception as e:
        log.debug("camera %s set: %s", name, e)
    return False


def _node_bool_set(nm, name: str, value: bool) -> bool:
    try:
        node = PySpin.CBooleanPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
            node.SetValue(bool(value))
            return True
    except Exception as e:
        log.debug("camera %s set: %s", name, e)
    return False


def _node_int_get(nm, name: str) -> int | None:
    try:
        node = PySpin.CIntegerPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
            return int(node.GetValue())
    except Exception as e:
        log.debug("camera %s read: %s", name, e)
    return None


def _node_int_range(nm, name: str) -> tuple[int, int] | None:
    try:
        node = PySpin.CIntegerPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
            return int(node.GetMin()), int(node.GetMax())
    except Exception as e:
        log.debug("camera %s range: %s", name, e)
    return None


def _node_int_set(nm, name: str, value: int) -> bool:
    try:
        node = PySpin.CIntegerPtr(nm.GetNode(name))
        if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
            v = max(node.GetMin(), min(node.GetMax(), int(value)))
            node.SetValue(v)
            return True
    except Exception as e:
        log.debug("camera %s set: %s", name, e)
    return False


def to_rgb(data: np.ndarray, width: int, height: int, fmt: str) -> np.ndarray:
    """Convert a Spinnaker image buffer into an owned RGB888 ndarray.

    All paths return a fresh, owned, C-contiguous array — safe to use
    after the source image is released. cv2.cvtColor already allocates
    fresh output; the RGB pass-through is the only path that would
    otherwise return a view, so we copy it explicitly there.

    Accepts either flat (1D) or already-shaped input — PySpin's
    GetNDArray() returns shaped arrays for most formats.
    """
    if "Bayer" in fmt:
        bayer = data if data.ndim == 2 else data.reshape(height, width)
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
        mono = data if data.ndim == 2 else data.reshape(height, width)
        return cv2.cvtColor(mono, cv2.COLOR_GRAY2RGB)
    if fmt.startswith("RGB"):
        rgb = data if data.ndim == 3 else data.reshape(height, width, 3)
        return rgb.copy()
    if fmt.startswith("BGR"):
        bgr = data if data.ndim == 3 else data.reshape(height, width, 3)
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
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


DEFAULT_ADJUSTMENTS = AdjustmentSnapshot()


@dataclass(frozen=True)
class CameraSettingsSnapshot:
    """Immutable snapshot of CameraOptionsPanel state for a preset.

    Manual fields (gain, exposure_us, balance_ratio_red, balance_ratio_blue)
    are None when the corresponding auto enum is "Continuous" at capture
    time. Applying a snapshot writes the auto enum and leaves the
    camera's auto-controlled value alone — the live poll keeps the
    spinbox in sync with whatever auto picks.

    auto enums use the camera's symbolic strings ("Off" | "Continuous").
    binning_vertical is always populated.
    """

    gain_auto: str
    exposure_auto: str
    balance_white_auto: str
    binning_vertical: int
    gain: float | None = None
    exposure_us: float | None = None
    balance_ratio_red: float | None = None
    balance_ratio_blue: float | None = None


def default_camera_snapshot(merged: dict) -> CameraSettingsSnapshot:
    """Snapshot derived from CameraOptionsPanel.OUR_DEFAULTS (merged with
    any [camera] gain / exposure_us overrides from config.toml).

    Manual fields are populated only when their auto enum is not
    "Continuous", matching the Continuous→None invariant other snapshots
    obey.
    """
    return CameraSettingsSnapshot(
        gain_auto=str(merged["GainAuto"]),
        exposure_auto=str(merged["ExposureAuto"]),
        balance_white_auto=str(merged["BalanceWhiteAuto"]),
        binning_vertical=int(merged["BinningVertical"]),
        gain=(
            float(merged["Gain"])
            if merged["GainAuto"] != "Continuous"
            else None
        ),
        exposure_us=(
            float(merged["ExposureTime"])
            if merged["ExposureAuto"] != "Continuous"
            else None
        ),
        balance_ratio_red=(
            float(merged["BalanceRatioRed"])
            if merged["BalanceWhiteAuto"] != "Continuous"
            else None
        ),
        balance_ratio_blue=(
            float(merged["BalanceRatioBlue"])
            if merged["BalanceWhiteAuto"] != "Continuous"
            else None
        ),
    )


def _coerce_pair(v) -> tuple[int, int]:
    lo, hi = v
    return (int(lo), int(hi))


class ImagePresetStore:
    """Round-trips user-created [[image_preset]] entries to/from settings.toml.

    "Default" is hardcoded in the panel and never stored — only user
    presets live in the file. Saves rewrite the image_preset array-of-tables
    wholesale via tomlkit, leaving every other section (and its comments)
    untouched.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, AdjustmentSnapshot]:
        if not self._path.exists():
            return {}
        doc = tomlkit.parse(self._path.read_text(encoding="utf-8"))
        out: dict[str, AdjustmentSnapshot] = {}
        for entry in doc.get("image_preset", []):
            try:
                name = str(entry["name"])
                if not name.strip() or name == "Default":
                    log.warning("skipping invalid preset name: %r", name)
                    continue
                if name in out:
                    log.warning("skipping duplicate preset name: %r", name)
                    continue
                out[name] = AdjustmentSnapshot(
                    brightness=int(entry["brightness"]),
                    contrast=int(entry["contrast"]),
                    saturation=int(entry["saturation"]),
                    r_range=_coerce_pair(entry["r_range"]),
                    g_range=_coerce_pair(entry["g_range"]),
                    b_range=_coerce_pair(entry["b_range"]),
                )
            except (KeyError, TypeError, ValueError) as e:
                log.warning("skipping malformed image_preset entry: %s", e)
        return out

    def save_all(self, presets: dict[str, AdjustmentSnapshot]) -> None:
        if self._path.exists():
            doc = tomlkit.parse(self._path.read_text(encoding="utf-8"))
        else:
            doc = tomlkit.document()

        if "image_preset" in doc:
            del doc["image_preset"]

        if presets:
            new_aot = tomlkit.aot()
            for name, snap in presets.items():
                t = tomlkit.table()
                t["name"] = name
                t["brightness"] = snap.brightness
                t["contrast"] = snap.contrast
                t["saturation"] = snap.saturation
                t["r_range"] = list(snap.r_range)
                t["g_range"] = list(snap.g_range)
                t["b_range"] = list(snap.b_range)
                new_aot.append(t)
            doc["image_preset"] = new_aot

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        tmp.replace(self._path)


class CameraPresetStore:
    """Round-trips user-created [[camera_preset]] entries to/from settings.toml.

    "Default" is hardcoded (derived from OUR_DEFAULTS + [camera] overrides)
    and never stored — only user presets live in the file. Saves rewrite
    the camera_preset array-of-tables wholesale via tomlkit, leaving every
    other section (and its comments) untouched.

    Manual-value fields (gain, exposure_us, balance_ratio_*) are written
    as TOML keys when present and omitted when None — i.e. when the
    corresponding auto enum is "Continuous" at save-time.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, CameraSettingsSnapshot]:
        if not self._path.exists():
            return {}
        doc = tomlkit.parse(self._path.read_text(encoding="utf-8"))
        out: dict[str, CameraSettingsSnapshot] = {}
        for entry in doc.get("camera_preset", []):
            try:
                name = str(entry["name"])
                if not name.strip() or name == "Default":
                    log.warning("skipping invalid camera preset name: %r", name)
                    continue
                if name in out:
                    log.warning("skipping duplicate camera preset name: %r", name)
                    continue
                out[name] = CameraSettingsSnapshot(
                    gain_auto=str(entry["gain_auto"]),
                    exposure_auto=str(entry["exposure_auto"]),
                    balance_white_auto=str(entry["balance_white_auto"]),
                    binning_vertical=int(entry["binning_vertical"]),
                    gain=(
                        float(entry["gain"]) if "gain" in entry else None
                    ),
                    exposure_us=(
                        float(entry["exposure_us"])
                        if "exposure_us" in entry
                        else None
                    ),
                    balance_ratio_red=(
                        float(entry["balance_ratio_red"])
                        if "balance_ratio_red" in entry
                        else None
                    ),
                    balance_ratio_blue=(
                        float(entry["balance_ratio_blue"])
                        if "balance_ratio_blue" in entry
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError) as e:
                log.warning("skipping malformed camera_preset entry: %s", e)
        return out

    def save_all(self, presets: dict[str, CameraSettingsSnapshot]) -> None:
        if self._path.exists():
            doc = tomlkit.parse(self._path.read_text(encoding="utf-8"))
        else:
            doc = tomlkit.document()

        if "camera_preset" in doc:
            del doc["camera_preset"]

        if presets:
            new_aot = tomlkit.aot()
            for name, snap in presets.items():
                t = tomlkit.table()
                t["name"] = name
                t["gain_auto"] = snap.gain_auto
                t["exposure_auto"] = snap.exposure_auto
                t["balance_white_auto"] = snap.balance_white_auto
                t["binning_vertical"] = snap.binning_vertical
                if snap.gain is not None:
                    t["gain"] = snap.gain
                if snap.exposure_us is not None:
                    t["exposure_us"] = snap.exposure_us
                if snap.balance_ratio_red is not None:
                    t["balance_ratio_red"] = snap.balance_ratio_red
                if snap.balance_ratio_blue is not None:
                    t["balance_ratio_blue"] = snap.balance_ratio_blue
                new_aot.append(t)
            doc["camera_preset"] = new_aot

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        tmp.replace(self._path)


# LUT/matrix builders are cached: callers feed them slider values
# (small ints) that change only when the user moves a slider, but
# apply_adjustments runs every frame. lru_cache turns these into
# O(1) lookups in the steady state.
@lru_cache(maxsize=16)
def _build_brightness_lut(brightness_pct: int) -> np.ndarray:
    """LUT for `out = clip(in * brightness/100)`."""
    x = np.arange(256, dtype=np.float32)
    return np.clip(x * (brightness_pct / 100.0), 0.0, 255.0).astype(np.uint8)


@lru_cache(maxsize=64)
def _build_channel_lut(contrast_pct: int, lo: int, hi: int) -> np.ndarray:
    """LUT folding contrast (around 128) then per-channel range remap."""
    x = np.arange(256, dtype=np.float32)
    y = (x - 128.0) * (contrast_pct / 100.0) + 128.0
    if (lo, hi) != (0, 255):
        span = max(hi - lo, 1)
        y = (y - lo) * (255.0 / span)
    return np.clip(y, 0.0, 255.0).astype(np.uint8)


@lru_cache(maxsize=16)
def _build_saturate_matrix(saturation_pct: int) -> np.ndarray:
    """3×3 RGB color matrix per CSS Filter Effects `saturate()`.

    Uses BT.709 luma (0.213, 0.715, 0.072). Identity at s=1.0,
    grayscale at s=0.0. Replaces the HSV roundtrip (two cvtColor
    passes) with a single cv2.transform call.
    """
    s = saturation_pct / 100.0
    return np.array([
        [0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
        [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
        [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s],
    ], dtype=np.float32)


def apply_adjustments(
    rgb: np.ndarray,
    adj: AdjustmentSnapshot,
    scratch: np.ndarray | None = None,
) -> np.ndarray:
    """Apply brightness/saturation/contrast/RGB-range to an RGB uint8 frame.

    Order matches the flakes-website CSS filter chain:
    brightness → saturate → contrast → per-channel range remap.

    Pipeline collapses into at most two cv2 passes:
      • saturate matrix (with brightness folded in) — when saturation ≠ 100
      • per-channel LUT (with brightness folded in) — when contrast/range or
        brightness needs to apply

    `scratch`, if provided (same shape/dtype as `rgb`), is used as the
    intermediate buffer between passes; the final pass writes into a
    fresh allocation so the returned buffer is safe to publish.

    Note on brightness > 100 + saturation: the matrix fold loses CSS's
    intermediate clip-to-255 between brightness and saturate. Output
    differences are confined to near-saturated pixels and absorbed
    almost entirely by the final output clip.

    Returns a new array; input is not mutated. If `adj` is identity,
    returns the input unchanged (fast path).
    """
    if adj.is_identity:
        return rgb

    needs_brightness = adj.brightness != 100
    needs_saturation = adj.saturation != 100
    needs_per_channel = (
        adj.contrast != 100
        or adj.r_range != (0, 255)
        or adj.g_range != (0, 255)
        or adj.b_range != (0, 255)
    )

    # Build the pass list — at most two entries.
    passes: list[tuple[str, np.ndarray]] = []
    if needs_saturation:
        m = _build_saturate_matrix(adj.saturation)
        if needs_brightness:
            m = m * (adj.brightness / 100.0)  # fold brightness scalar
        passes.append(("transform", m.astype(np.float32, copy=False)))

    fold_brightness = needs_brightness and not needs_saturation
    if needs_per_channel or fold_brightness:
        r_lut = _build_channel_lut(adj.contrast, *adj.r_range)
        g_lut = _build_channel_lut(adj.contrast, *adj.g_range)
        b_lut = _build_channel_lut(adj.contrast, *adj.b_range)
        if fold_brightness:
            b_pre = _build_brightness_lut(adj.brightness)
            r_lut = r_lut[b_pre]
            g_lut = g_lut[b_pre]
            b_lut = b_lut[b_pre]
        lut3 = np.stack([r_lut, g_lut, b_lut], axis=-1).reshape(1, 256, 3)
        passes.append(("lut", lut3))

    if not passes:
        return rgb  # all flags false (shouldn't happen post is_identity)

    # Intermediate passes write into scratch; final pass writes into a
    # fresh buffer so the caller can hold onto it without seeing the
    # next iteration overwrite it.
    src = rgb
    last = len(passes) - 1
    for i, (op, arg) in enumerate(passes):
        dst = np.empty_like(rgb) if i == last else scratch
        if op == "transform":
            src = cv2.transform(src, arg, dst=dst)
        else:
            src = cv2.LUT(src, arg, dst=dst)
    return src


_HIST_DOWNSAMPLE = (320, 256)  # 4x4 from 1280x1024; ~16x fewer pixels


def compute_histograms(rgb: np.ndarray) -> np.ndarray:
    """256-bin per-channel histogram of an RGB uint8 frame.

    Downsamples to ~80K pixels via cv2.resize before binning. The
    histogram widget is 200x48 — full pixel coverage is wasted work.
    On hardware where cv2.calcHist is slow per-pixel (some Windows
    opencv-python wheels are ~50× slower than Linux), keeping the
    work small here is what makes every-frame histogramming viable.
    cv2.resize itself is heavily SIMD-optimized everywhere.
    """
    sub = cv2.resize(rgb, _HIST_DOWNSAMPLE, interpolation=cv2.INTER_NEAREST)
    out = np.empty((3, 256), dtype=np.int64)
    for c in range(3):
        h = cv2.calcHist([sub], [c], None, [256], [0, 256])
        out[c] = h.ravel().astype(np.int64)
    return out


_HIST_RENDER_W = 256
_HIST_RENDER_H = 48
_HIST_SMOOTH_RADIUS = 2


def render_hist_image(
    counts: np.ndarray,
    color: tuple[int, int, int],
    width: int = _HIST_RENDER_W,
    height: int = _HIST_RENDER_H,
) -> QImage:
    """Render a single-channel histogram curve into an ARGB32 QImage.

    Pure-numpy path: writes directly into the QImage's pixel buffer
    via memoryview. Avoids QPainter + a 256-iteration lineTo Python
    loop, which on the HistWorker thread held the GIL for ~25 ms
    per channel and starved the GUI thread of paint dispatches.
    Trade: the curve edge is stair-stepped at column granularity
    (no antialiasing). At 256-px-wide render scaled to ~200-px
    widget the difference is barely visible.
    """
    qimg = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    ptr = qimg.bits()
    bgra = np.frombuffer(ptr, dtype=np.uint8).reshape(height, width, 4)
    # Format_ARGB32 little-endian: B, G, R, A bytes. Background fill.
    bgra[..., 0] = 20
    bgra[..., 1] = 20
    bgra[..., 2] = 20
    bgra[..., 3] = 255

    r = _HIST_SMOOTH_RADIUS
    x = counts.astype(np.float64)
    cs = np.concatenate(([0.0], np.cumsum(x)))
    idx = np.arange(256)
    lo_i = np.maximum(0, idx - r)
    hi_i = np.minimum(255, idx + r)
    smoothed = (cs[hi_i + 1] - cs[lo_i]) / (hi_i - lo_i + 1)
    peak = float(smoothed.max())
    if peak <= 0:
        return qimg

    ys = np.sqrt(smoothed / peak) * height
    if width == 256:
        ys_w = ys
    else:
        ys_w = np.interp(np.linspace(0, 255, width), np.arange(256), ys)

    threshold = height - ys_w  # row index above which to fill (per column)
    y_grid = np.arange(height).reshape(height, 1)
    fill_mask = y_grid >= threshold[None, :]

    # Composite source (channel color, alpha=80) over background (20,20,20).
    # Premultiplied ARGB requires the RGB bytes to already be * (A/255).
    a_src = 80
    bg_factor = (255 - a_src) / 255.0
    cb = int(color[2] * a_src / 255 + 20 * bg_factor)
    cg = int(color[1] * a_src / 255 + 20 * bg_factor)
    cr = int(color[0] * a_src / 255 + 20 * bg_factor)
    bgra[fill_mask, 0] = cb
    bgra[fill_mask, 1] = cg
    bgra[fill_mask, 2] = cr
    return qimg


class _CameraGLWindow(QOpenGLWindow):
    """Direct-rendered GL surface for one camera frame at a time.

    QOpenGLWindow renders straight to its native window surface — no
    FBO indirection, no Qt compositor blit. paintGL ends with a real
    swapBuffers, controlled by swapInterval(0) on the surface format,
    so we're not capped at the compositor's present rate. (The
    QOpenGLWidget version we replaced sat at ~25 Hz cycle even with
    paint=6 ms because of its FBO→window blit step.)

    Embedded into a regular widget tree via QWidget.createWindowContainer.
    """

    def __init__(self) -> None:
        super().__init__()
        fmt = self.format()
        fmt.setSwapInterval(0)
        self.setFormat(fmt)

        self._frame_ref: np.ndarray | None = None
        self._frame_dirty = False
        self._tex: QOpenGLTexture | None = None
        self._tex_w = 0
        self._tex_h = 0
        self._blitter: QOpenGLTextureBlitter | None = None
        self._paint_total = 0.0
        self._paint_max = 0.0
        self._paint_count = 0
        self._paint_last_t = 0.0
        self._paint_cycle_total = 0.0
        self._paint_cycle_max = 0.0

        # Drawing overlay. Primitives are typed (FreehandStroke /
        # LineSegment, see overlay.py) and store their points in
        # aspect-correct image coords (x∈[0,IMAGE_ASPECT], y∈[0,1]) so they
        # re-render correctly across window resizes and binning swaps within
        # the same image-aspect family. `_target_rect` caches the current
        # camera-content rect (refreshed every paintGL) so mouse handlers
        # can map widget coords → normalized without re-running the
        # letterbox math. `_active` is the in-progress primitive during a
        # drag; `_tool` selects what a press starts. Primitives are grouped
        # into layers; new drawing lands on the active layer. (Layer
        # transforms / UI arrive in later Phase 2 steps; 2a seeds one layer
        # so behavior matches the pre-layer single list.)
        self._layers: list[OverlayLayer] = [OverlayLayer(name="Layer 1")]
        self._active_layer = 0
        self._layer_seq = 1  # monotonic, for auto-naming new layers
        self._active: OverlayPrimitive | None = None
        self._drawing_enabled = False
        self._tool: DrawTool = DrawTool.FREEHAND
        self._target_rect: QRectF | None = None
        # The overlay is rasterized to an offscreen ARGB image (CPU raster
        # engine) and blitted as a finished image, rather than stroked
        # straight onto the GL surface: the GL paint engine misrenders
        # semi-transparent antialiased wide strokes (per-layer opacity came
        # out as a flat bounding-box blob). Cached + dirty-flagged so the
        # re-raster happens on overlay edits, not every camera frame.
        self._overlay_img: QImage | None = None
        self._overlay_dirty = True
        self._overlay_target: QRectF | None = None

    def set_frame(self, frame_ref: np.ndarray) -> None:
        self._frame_ref = frame_ref
        self._frame_dirty = True
        self.update()

    def clear_frame(self) -> None:
        self._frame_ref = None
        self._frame_dirty = False
        self.update()

    def _touch_overlay(self) -> None:
        """Mark the overlay image stale and request a repaint. Used by every
        overlay mutation so the offscreen image re-rasters; plain camera
        frames call `update()` only, reusing the cached overlay image."""
        self._overlay_dirty = True
        self.update()

    def set_drawing_enabled(self, enabled: bool) -> None:
        """Toggle draw mode. Switches the cursor to a crosshair when on,
        default when off. An in-progress primitive is dropped when drawing
        is turned off mid-drag (the operator can't see the cursor anymore,
        so finishing the stroke would be surprising)."""
        self._drawing_enabled = enabled
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
            if self._active is not None:
                self._active = None
                self._touch_overlay()

    def set_tool(self, tool: DrawTool) -> None:
        """Select the active tool (freehand vs straight line). A switch
        mid-drag drops the in-progress primitive — same rationale as
        disabling draw mode mid-drag."""
        self._tool = tool
        if self._active is not None:
            self._active = None
            self._touch_overlay()

    def _active_layer_obj(self) -> OverlayLayer:
        return self._layers[self._active_layer]

    # --- Layer management (driven by the layer panel) -------------------

    def layers(self) -> list[OverlayLayer]:
        return self._layers

    def active_layer_index(self) -> int:
        return self._active_layer

    def add_layer(self, name: str | None = None) -> None:
        """Append a new layer and make it active."""
        self._layer_seq += 1
        self._layers.append(OverlayLayer(name=name or f"Layer {self._layer_seq}"))
        self._active_layer = len(self._layers) - 1
        self._touch_overlay()

    def remove_layer(self, index: int) -> None:
        """Drop a layer. Never removes the last one (always ≥1 layer)."""
        if len(self._layers) <= 1 or not (0 <= index < len(self._layers)):
            return
        self._layers.pop(index)
        # Keep the active index valid and pointing at the same-or-nearest layer.
        if self._active_layer >= len(self._layers):
            self._active_layer = len(self._layers) - 1
        elif self._active_layer > index:
            self._active_layer -= 1
        self._touch_overlay()

    def set_active_layer(self, index: int) -> None:
        if 0 <= index < len(self._layers):
            self._active_layer = index

    def set_layer_visible(self, index: int, visible: bool) -> None:
        if 0 <= index < len(self._layers):
            self._layers[index].visible = visible
            self._touch_overlay()

    def set_layer_opacity(self, index: int, opacity: float) -> None:
        if 0 <= index < len(self._layers):
            self._layers[index].opacity = max(0.0, min(1.0, opacity))
            self._touch_overlay()

    def move_layer(self, index: int, delta: int) -> None:
        """Reorder a layer by `delta` in the draw stack (+1 = toward front)."""
        j = index + delta
        if not (0 <= index < len(self._layers) and 0 <= j < len(self._layers)):
            return
        self._layers[index], self._layers[j] = self._layers[j], self._layers[index]
        # Keep `active` pointing at the same layer object after the swap.
        if self._active_layer == index:
            self._active_layer = j
        elif self._active_layer == j:
            self._active_layer = index
        self._touch_overlay()

    def clear_strokes(self) -> None:
        """Drop the active layer's primitives + any in-progress one.

        (Single-layer today, so this clears everything; per-layer vs
        clear-all semantics is a layer-panel decision in a later step.)"""
        layer = self._active_layer_obj()
        had_anything = bool(layer.primitives) or self._active is not None
        layer.primitives = []
        self._active = None
        if had_anything:
            self._touch_overlay()

    def _pos_to_normalized(self, pos: QPointF) -> QPointF | None:
        """Map widget coords → (nx, ny) in aspect-correct image space
        (x∈[0,IMAGE_ASPECT], y∈[0,1]).

        Returns None when the position falls in the letterbox bars or
        before the first paintGL has computed `_target_rect`.
        """
        rect = self._target_rect
        if rect is None:
            return None
        return normalize_pos(pos, rect)

    def mousePressEvent(self, event) -> None:
        if (
            self._drawing_enabled
            and event.button() == Qt.MouseButton.LeftButton
        ):
            n = self._pos_to_normalized(event.position())
            if n is not None:
                if self._tool is DrawTool.LINE:
                    # Start fixed at the press; end rubber-bands on move.
                    self._active = LineSegment(n, QPointF(n))
                else:
                    self._active = FreehandStroke([n])
                self._touch_overlay()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._active is not None:
            n = self._pos_to_normalized(event.position())
            if n is not None:
                if isinstance(self._active, LineSegment):
                    # Rubber-band: only the free endpoint tracks the cursor.
                    self._active.end = n
                elif isinstance(self._active, FreehandStroke):
                    self._active.points.append(n)
                self._touch_overlay()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (
            self._active is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            # Drop a zero-length line (a click with no drag in line mode) —
            # it's invisible and would just clutter storage. A freehand
            # single-point stroke is kept: it renders as a deliberate dot.
            if (
                isinstance(self._active, LineSegment)
                and self._active.start == self._active.end
            ):
                self._active = None
            else:
                self._active_layer_obj().primitives.append(self._active)
                self._active = None
            self._touch_overlay()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def initializeGL(self) -> None:
        log.info(
            "_CameraGLWindow GL ctx swapInterval=%d",
            self.format().swapInterval(),
        )
        self._blitter = QOpenGLTextureBlitter()
        self._blitter.create()

    def _ensure_texture(self, w: int, h: int) -> None:
        if self._tex is not None and self._tex_w == w and self._tex_h == h:
            return
        if self._tex is not None:
            self._tex.destroy()
        self._tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        self._tex.setSize(w, h)
        self._tex.setFormat(QOpenGLTexture.TextureFormat.RGB8_UNorm)
        self._tex.setMipLevels(1)
        self._tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
        self._tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        self._tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        self._tex.allocateStorage(
            QOpenGLTexture.PixelFormat.RGB,
            QOpenGLTexture.PixelType.UInt8,
        )
        self._tex_w = w
        self._tex_h = h
        log.info("_CameraGLWindow texture allocated %dx%d", w, h)

    def paintGL(self) -> None:
        if self._blitter is None:
            return
        t0 = time.perf_counter()
        if self._paint_last_t > 0.0:
            cycle = t0 - self._paint_last_t
            self._paint_cycle_total += cycle
            self._paint_cycle_max = max(self._paint_cycle_max, cycle)
        self._paint_last_t = t0
        gl = self.context().functions()
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(0x4000)  # GL_COLOR_BUFFER_BIT
        if self._frame_ref is not None:
            sh, sw, _ = self._frame_ref.shape
            self._ensure_texture(sw, sh)
            assert self._tex is not None  # set by _ensure_texture
            if self._frame_dirty:
                # Pass the numpy buffer directly via the buffer protocol —
                # avoids the ~5 MB tobytes() copy that occasionally spikes
                # paint past the 16.7 ms vsync boundary. The PySide6 stub
                # only types `data` as `int` (the void* address overload),
                # not the buffer-protocol form Qt also accepts at runtime.
                self._tex.setData(
                    QOpenGLTexture.PixelFormat.RGB,
                    QOpenGLTexture.PixelType.UInt8,
                    self._frame_ref.data,  # ty: ignore[invalid-argument-type]
                )
                self._frame_dirty = False
            tw, th = self.width(), self.height()
            # No 1.0 cap — binned frames (e.g. 800×600 at BinningVertical=2)
            # should upscale to fill the display, not sit pixel-mapped in a
            # tiny rectangle. Linear filtering on _tex handles the upscale.
            scale = min(tw / sw, th / sh)
            dw = int(sw * scale)
            dh = int(sh * scale)
            x = (tw - dw) // 2
            y = (th - dh) // 2
            target = QRectF(x, y, dw, dh)
            self._target_rect = target
            viewport = QRect(0, 0, tw, th)
            transform = QOpenGLTextureBlitter.targetTransform(target, viewport)
            self._blitter.bind()
            self._blitter.blit(
                self._tex.textureId(),
                transform,
                QOpenGLTextureBlitter.Origin.OriginTopLeft,
            )
            self._blitter.release()
            self._paint_overlay(target)
        elapsed = time.perf_counter() - t0
        self._paint_total += elapsed
        self._paint_max = max(self._paint_max, elapsed)
        self._paint_count += 1
        if self._paint_count >= 60:
            cycles = max(self._paint_count - 1, 1)
            log.debug(
                "paintGL avg=%.1fms max=%.1fms cycle=%.1fms cycle_max=%.1fms surf=%dx%d n=%d",
                self._paint_total / self._paint_count * 1000,
                self._paint_max * 1000,
                self._paint_cycle_total / cycles * 1000,
                self._paint_cycle_max * 1000,
                self.width(), self.height(),
                self._paint_count,
            )
            self._paint_total = 0.0
            self._paint_max = 0.0
            self._paint_count = 0
            self._paint_cycle_total = 0.0
            self._paint_cycle_max = 0.0

    def _paint_overlay(self, target: QRectF) -> None:
        """Composite the overlay layers over the camera blit.

        The overlay is rasterized to an offscreen ARGB image with the CPU
        raster engine — where semi-transparent antialiased strokes (per-layer
        opacity) render correctly — then drawn onto the GL surface as one
        finished image. Drawing straight onto the GL paint engine instead
        made per-layer opacity come out as a flat bounding-box blob. The
        image is cached and only re-rasterized when the overlay changed
        (`_overlay_dirty`) or the widget/target geometry changed; ordinary
        camera frames just re-blit the cached image.
        """
        any_prims = any(layer.primitives for layer in self._layers)
        if not any_prims and self._active is None:
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        if (
            self._overlay_img is None
            or self._overlay_img.width() != w
            or self._overlay_img.height() != h
            or self._overlay_dirty
            or self._overlay_target != target
        ):
            self._overlay_img = self._render_overlay_image(w, h, target)
            self._overlay_target = QRectF(target)
            self._overlay_dirty = False
        painter = QPainter(self)
        try:
            painter.drawImage(0, 0, self._overlay_img)
        finally:
            painter.end()

    def _render_overlay_image(self, w: int, h: int, target: QRectF) -> QImage:
        """Rasterize visible layers + the in-progress primitive to a
        transparent ARGB image. Layers draw back-to-front, hidden ones
        skipped; per-layer opacity via `setOpacity` is correct on the raster
        engine. The active primitive draws last at full opacity."""
        # Pen width in display pixels: 0.2% of the displayed camera-rect
        # width, so visual weight stays consistent across binning + resize.
        pen_w = max(1.0, 0.002 * target.width())
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setClipRect(target)
            pen = QPen(QColor(255, 0, 0))
            pen.setWidthF(pen_w)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            for layer in self._layers:
                if not layer.visible:
                    continue
                painter.setOpacity(layer.opacity)
                for prim in layer.primitives:
                    prim.draw(painter, target)
            painter.setOpacity(1.0)
            if self._active is not None:
                self._active.draw(painter, target)
        finally:
            painter.end()
        return img


class CameraDisplay(QWidget):
    """Wrapper around _CameraGLWindow + a centered status QLabel.

    The GL window is embedded via createWindowContainer (a native
    child window), with the status label stacked on top via
    WA_NativeWindow + raise_() so it gets its own native window
    handle and proper Win32 z-order above the GL container.
    """

    def __init__(self) -> None:
        super().__init__()
        self._gl_window = _CameraGLWindow()
        self._gl_container = QWidget.createWindowContainer(self._gl_window, self)
        self._gl_container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._gl_container.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.text_label = QLabel("", self)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label.setStyleSheet(
            "color: rgb(220, 220, 220); background: transparent;"
        )
        self.text_label.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.text_label.hide()

    def set_frame(self, frame_ref: np.ndarray) -> None:
        self._gl_window.set_frame(frame_ref)
        if self.text_label.isVisible():
            self.text_label.hide()

    def clear_frame(self) -> None:
        self._gl_window.clear_frame()

    def set_drawing_enabled(self, enabled: bool) -> None:
        self._gl_window.set_drawing_enabled(enabled)

    def set_tool(self, tool: DrawTool) -> None:
        self._gl_window.set_tool(tool)

    def clear_strokes(self) -> None:
        self._gl_window.clear_strokes()

    # Layer management — delegated to the GL window, driven by LayerPanel.
    def layers(self) -> list[OverlayLayer]:
        return self._gl_window.layers()

    def active_layer_index(self) -> int:
        return self._gl_window.active_layer_index()

    def add_layer(self, name: str | None = None) -> None:
        self._gl_window.add_layer(name)

    def remove_layer(self, index: int) -> None:
        self._gl_window.remove_layer(index)

    def set_active_layer(self, index: int) -> None:
        self._gl_window.set_active_layer(index)

    def set_layer_visible(self, index: int, visible: bool) -> None:
        self._gl_window.set_layer_visible(index, visible)

    def set_layer_opacity(self, index: int, opacity: float) -> None:
        self._gl_window.set_layer_opacity(index, opacity)

    def move_layer(self, index: int, delta: int) -> None:
        self._gl_window.move_layer(index, delta)

    def setText(self, text: str) -> None:
        self.text_label.setText(text)
        self.text_label.setGeometry(self.rect())
        self.text_label.show()
        self.text_label.raise_()
        self._gl_window.clear_frame()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._gl_container.setGeometry(self.rect())
        self.text_label.setGeometry(self.rect())
        # Keep the text overlay above the GL container after every resize.
        self.text_label.raise_()


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
    """Thread A: pull from PySpin, debayer, hand off to processing.

    Stays pinned to the camera frame rate. Pushes debayered RGB
    frames into a FrameMailbox; if processing is slower than the
    camera, intermediate frames drop at that mailbox (latest-wins),
    but the camera buffer queue stays drained.
    """

    error = Signal(str)
    finished = Signal()

    # GetNextImage timeout in ms — long enough to cover the slowest
    # exposure we expose (1 s) plus headroom.
    FETCH_TIMEOUT_MS = 1500

    def __init__(self, cam, mailbox: FrameMailbox) -> None:
        super().__init__()
        self._cam = cam
        self._mailbox = mailbox
        self._running = False

    @Slot()
    def run(self) -> None:
        self._running = True
        log_count = 0
        log_start = time.perf_counter()
        t_fetch = t_debayer = t_publish = 0.0
        # Cache stream-invariant image properties on first frame.
        props: tuple[int, int, str] | None = None
        while self._running:
            try:
                t0 = time.perf_counter()
                image = self._cam.GetNextImage(self.FETCH_TIMEOUT_MS)
                try:
                    if image.IsIncomplete():
                        log.debug(
                            "incomplete image: status=%s",
                            image.GetImageStatus(),
                        )
                        continue
                    if props is None:
                        props = (
                            image.GetWidth(),
                            image.GetHeight(),
                            image.GetPixelFormatName(),
                        )
                    width, height, fmt = props
                    t1 = time.perf_counter()
                    rgb = to_rgb(image.GetNDArray(), width, height, fmt)
                finally:
                    image.Release()
                t2 = time.perf_counter()
                self._mailbox.publish(rgb)
                t3 = time.perf_counter()
                t_fetch += t1 - t0
                t_debayer += t2 - t1
                t_publish += t3 - t2
                log_count += 1
                if t3 - log_start > 2.0:
                    span = t3 - log_start
                    log.debug(
                        "acq    %.1f fps  fetch=%.1f deb=%.1f pub=%.2f ms/frame",
                        log_count / span,
                        t_fetch / log_count * 1000,
                        t_debayer / log_count * 1000,
                        t_publish / log_count * 1000,
                    )
                    log_count = 0
                    log_start = t3
                    t_fetch = t_debayer = t_publish = 0.0
            except PySpin.SpinnakerException as e:
                self.error.emit(str(e))
                time.sleep(0.1)
        self.finished.emit()

    def stop(self) -> None:
        self._running = False


class CameraProcessWorker(QObject):
    """Thread B: adjustments + GUI handoff.

    Takes the latest debayered frame from the acquire mailbox,
    applies image adjustments, and publishes the result for the
    GUI. Forks the *pre-adjustment* (raw post-debayer) frame to a
    side mailbox consumed by HistWorker — histograms are a sensor-
    data diagnostic, not a preview of display-only LUTs. Standard
    convention in scientific imaging tools (ImageJ, μManager).
    Hist compute also lives off this thread so proc isn't gated by
    Windows opencv variance.

    Publishes via overwrite-always: every iteration overwrites
    `_latest` and emits frame_ready. take_latest returns the
    freshest frame (or None if a prior call already drained it).
    """

    frame_ready = Signal()
    finished = Signal()

    def __init__(
        self,
        source: FrameMailbox,
        adjustments: ImageAdjustments | None = None,
        hist_sink: FrameMailbox | None = None,
    ) -> None:
        super().__init__()
        self._source = source
        self._adjustments = adjustments
        self._hist_sink = hist_sink
        self._running = False
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None
        # Reused intermediate buffer for apply_adjustments (avoids a
        # 5.76 MB alloc per frame in multi-pass cases).
        self._adjust_scratch: np.ndarray | None = None

    @Slot()
    def run(self) -> None:
        self._running = True
        log_count = 0
        log_emits = 0
        log_start = time.perf_counter()
        t_wait = t_adjust = t_publish = 0.0
        while self._running:
            t0 = time.perf_counter()
            rgb = self._source.take(timeout=0.1)
            if rgb is None:
                continue
            # Fork the raw post-debayer frame to hist before adjustments.
            if self._hist_sink is not None:
                self._hist_sink.publish(rgb)
            t1 = time.perf_counter()
            if self._adjustments is not None:
                snap = self._adjustments.get()
                if not snap.is_identity:
                    try:
                        if (
                            self._adjust_scratch is None
                            or self._adjust_scratch.shape != rgb.shape
                            or self._adjust_scratch.dtype != rgb.dtype
                        ):
                            self._adjust_scratch = np.empty_like(rgb)
                        rgb = apply_adjustments(rgb, snap, scratch=self._adjust_scratch)
                    except Exception as e:  # noqa: BLE001
                        log.warning("adjustment err: %s", e)
            t3 = time.perf_counter()
            with self._lock:
                self._latest = rgb
            self.frame_ready.emit()
            log_emits += 1
            t4 = time.perf_counter()
            t_wait += t1 - t0
            t_adjust += t3 - t1
            t_publish += t4 - t3
            log_count += 1
            if t4 - log_start > 2.0:
                span = t4 - log_start
                log.debug(
                    "proc   %.1f fps emit=%.1f/s  wait=%.1f adj=%.1f pub=%.2f ms/frame",
                    log_count / span,
                    log_emits / span,
                    t_wait / log_count * 1000,
                    t_adjust / log_count * 1000,
                    t_publish / log_count * 1000,
                )
                log_count = 0
                log_emits = 0
                log_start = t4
                t_wait = t_adjust = t_publish = 0.0
        self.finished.emit()

    def take_latest(self) -> np.ndarray | None:
        with self._lock:
            f = self._latest
            self._latest = None
            return f

    def stop(self) -> None:
        self._running = False
        self._source.wake()  # unblock the take(timeout) wait


class HistWorker(QObject):
    """Thread C: histogram compute + curve render.

    Decoupled from proc; takes from a side mailbox proc publishes
    to. Each iteration: compute_histograms (~18 ms on Windows
    opencv), render three small QImages (~2-3 ms total), emit. The
    GUI's hist widgets just drawImage the result, so per-paint cost
    on the GUI thread is sub-ms.
    """

    images_ready = Signal(QImage, QImage, QImage)
    metrics_ready = Signal(float)
    finished = Signal()

    CHANNEL_COLORS = (
        (224, 49, 49),   # R
        (47, 158, 68),   # G
        (25, 113, 194),  # B
    )

    def __init__(self, source: FrameMailbox) -> None:
        super().__init__()
        self._source = source
        self._running = False

    @Slot()
    def run(self) -> None:
        self._running = True
        log_start = time.perf_counter()
        # Per-iteration ms samples — distribution shape distinguishes
        # GIL-contention starvation (heavy-tailed) from genuine slow work
        # (tight cluster near the mean).
        comp_ms: list[float] = []
        rend_ms: list[float] = []
        while self._running:
            rgb = self._source.take(timeout=0.1)
            if rgb is None:
                continue
            t1 = time.perf_counter()
            try:
                hist = compute_histograms(rgb)
            except Exception as e:  # noqa: BLE001
                log.warning("hist compute err: %s", e)
                continue
            t2 = time.perf_counter()
            try:
                images = (
                    render_hist_image(hist[0], self.CHANNEL_COLORS[0]),
                    render_hist_image(hist[1], self.CHANNEL_COLORS[1]),
                    render_hist_image(hist[2], self.CHANNEL_COLORS[2]),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("hist render err: %s", e)
                continue
            t3 = time.perf_counter()
            self.images_ready.emit(*images)
            try:
                self.metrics_ready.emit(compute_sharpness(rgb))
            except Exception as e:  # noqa: BLE001
                log.warning("sharpness compute err: %s", e)
            comp_ms.append((t2 - t1) * 1000)
            rend_ms.append((t3 - t2) * 1000)
            if t3 - log_start > 2.0:
                span = t3 - log_start
                n = len(comp_ms)
                ca = np.asarray(comp_ms)
                ra = np.asarray(rend_ms)
                log.debug(
                    "hist   %.1f fps  comp min/med/p90/max=%.1f/%.1f/%.1f/%.1f  "
                    "rend min/med/p90/max=%.1f/%.1f/%.1f/%.1f  ms/iter (n=%d)",
                    n / span,
                    ca.min(), np.median(ca), np.percentile(ca, 90), ca.max(),
                    ra.min(), np.median(ra), np.percentile(ra, 90), ra.max(),
                    n,
                )
                comp_ms.clear()
                rend_ms.clear()
                log_start = t3
        self.finished.emit()

    def stop(self) -> None:
        self._running = False
        self._source.wake()


class ConexAxisPanel(QGroupBox):
    """Newport-style control surface for a single CONEX-CC axis."""

    POLL_MS = 100
    # Mouse-down duration past which a jog button switches from step to
    # continuous mode. Matches the SMC100 panel's threshold.
    JOG_HOLD_MS = 250

    def __init__(self, axis_config: dict) -> None:
        super().__init__(axis_config.get("name", "Axis"))
        self.units = axis_config.get("units", "")
        self._default_velocity = axis_config.get("default_velocity")
        self.axis = ConexAxis(
            port=axis_config["port"],
            baud=int(axis_config.get("baud", 921600)),
        )

        self.status_label = QLabel("disconnected")
        self.position_label = QLabel("—")
        pos_font = QFont(self.position_label.font())
        pos_font.setPointSize(pos_font.pointSize() + 6)
        pos_font.setStyleHint(QFont.StyleHint.Monospace)
        pos_font.setFamily("monospace")
        self.position_label.setFont(pos_font)
        self.id_label = QLabel("")
        self.id_label.setStyleSheet("color: #888;")
        self.id_label.setVisible(False)

        self.target_spin = QDoubleSpinBox()
        self.target_spin.setKeyboardTracking(False)
        self.target_spin.setRange(-1e6, 1e6)
        self.target_spin.setDecimals(6)
        self.target_spin.setSingleStep(0.01)
        if self.units:
            self.target_spin.setSuffix(f" {self.units}")
        self.go_btn = action_button("Go")

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setKeyboardTracking(False)
        self.step_spin.setRange(0.0, 1e6)
        self.step_spin.setDecimals(6)
        self.step_spin.setSingleStep(0.01)
        if self.units:
            self.step_spin.setSuffix(f" {self.units}")
        self.step_spin.setValue(float(axis_config.get("step", 0.01)))

        self.velocity_spin = QDoubleSpinBox()
        self.velocity_spin.setKeyboardTracking(False)
        self.velocity_spin.setDecimals(3)
        self.velocity_spin.setSingleStep(0.5)
        self.velocity_spin.setRange(0.001, 1000.0)
        self.velocity_spin.setSuffix(f" {self.units}/s" if self.units else "/s")

        self.jog_minus_btn = QPushButton("−")
        self.jog_plus_btn = QPushButton("+")
        self.home_btn = action_button("Home")
        self.enable_btn = action_button("Enable")
        self.disable_btn = action_button("Disable")

        # Stop sits on the top-right of the status row (matches SMC100).
        self.stop_btn = action_button("STOP")
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; "
            "font-weight: bold; padding: 2px 10px; }"
            "QPushButton:pressed { background-color: #962d22; }"
        )

        self._build_layout()
        self._wire_signals()

        self._worker: PollWorker | None = None
        self._worker_thread: QThread | None = None

        # Cached state used by the click-vs-hold continuous jog path.
        self._effective_limits: tuple[float, float] | None = None
        self._jog_direction: int = 0
        self._jog_continuous: bool = False
        self._jog_timer = QTimer(self)
        self._jog_timer.setSingleShot(True)
        self._jog_timer.setInterval(self.JOG_HOLD_MS)
        self._jog_timer.timeout.connect(self._on_jog_continuous_fire)

        try:
            self.axis.open()
        except Exception as e:
            self.status_label.setText(f"open failed: {e}")
            self._set_motion_enabled(False)
            return

        self.id_label.setText(self.axis.identify())
        self.status_label.setText(f"connected on {self.axis.port}")

        # Cache software limits — the continuous-hold path moves toward
        # whichever limit matches the jog direction. CONEX-CC has no
        # JOGGING command, so Newport's documented pattern is PA(SL/SR)
        # then ST on release.
        try:
            lo = self.axis.negative_limit()
            hi = self.axis.positive_limit()
            self._effective_limits = (lo, hi)
        except ConexError:
            pass

        # Apply default velocity if requested. Transient (VA only) — no
        # flash write, controller reverts on power-cycle.
        if self._default_velocity is not None:
            try:
                self.axis.set_velocity(float(self._default_velocity))
            except ConexError as e:
                log.warning("conex set_velocity failed: %s", e)

        # Pre-populate velocity spinner from the controller (best-effort,
        # picks up whatever the previous step just wrote).
        try:
            v = self.axis.velocity()
            self.velocity_spin.blockSignals(True)
            self.velocity_spin.setValue(v)
            self.velocity_spin.blockSignals(False)
        except ConexError:
            pass

        self._worker_thread = QThread()
        self._worker = PollWorker(self._read_state, self.POLL_MS / 1000.0)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.state_ready.connect(self._apply_state)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker_thread.start()

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.stop_btn)
        outer.addLayout(status_row)
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
        outer.addLayout(action_row)

        outer.addStretch(1)

    def _wire_signals(self) -> None:
        self.go_btn.clicked.connect(self._on_go)
        # Click=step, hold>JOG_HOLD_MS=continuous toward limit. Mirrors the
        # SMC100 panel pattern; CONEX-CC has no JOGGING command, so the
        # continuous path uses PA(SL/SR) then ST on release.
        self.jog_minus_btn.pressed.connect(lambda: self._on_jog_pressed(-1))
        self.jog_minus_btn.released.connect(self._on_jog_released)
        self.jog_plus_btn.pressed.connect(lambda: self._on_jog_pressed(+1))
        self.jog_plus_btn.released.connect(self._on_jog_released)
        self.stop_btn.clicked.connect(lambda: self._safe(self.axis.stop))
        self.home_btn.clicked.connect(lambda: self._safe(self.axis.home))
        self.enable_btn.clicked.connect(lambda: self._safe(self.axis.enable))
        self.disable_btn.clicked.connect(lambda: self._safe(self.axis.disable))
        self.velocity_spin.editingFinished.connect(self._on_set_velocity)

    def _on_set_velocity(self) -> None:
        self._safe(self.axis.set_velocity, self.velocity_spin.value())

    def _on_jog_pressed(self, direction: int) -> None:
        """Mouse-down on a jog button — arm the click-vs-hold timer."""
        log.info("conex: jog pressed dir=%+d (timer armed)", direction)
        self._jog_direction = direction
        self._jog_continuous = False
        self._jog_timer.start()

    def _on_jog_continuous_fire(self) -> None:
        """Hold threshold elapsed — switch from step to continuous travel.

        CONEX-CC has no jog command. We send PA(limit) toward the SL/SR
        software cap; on release the matching _on_jog_released sends ST
        which decelerates per AC and returns the controller to READY.
        Falls back to a step move if we don't have limits cached.
        """
        if self._jog_direction == 0:
            return  # released before timer fired
        direction = self._jog_direction
        log.info(
            "conex: jog continuous-fire dir=%+d limits=%s",
            direction,
            self._effective_limits,
        )
        if self._effective_limits is not None:
            lo, hi = self._effective_limits
            target = lo if direction < 0 else hi
            self._safe(self.axis.move_absolute, target)
        else:
            # No limits cached — fall back to a step move so we still do
            # something rather than ignoring the hold.
            self._safe(
                self.axis.move_relative,
                direction * self.step_spin.value(),
            )
        self._jog_continuous = True

    def _on_jog_released(self) -> None:
        """Mouse-up — finish a step or stop a continuous run."""
        if self._jog_direction == 0:
            return
        log.info(
            "conex: jog released dir=%+d continuous=%s",
            self._jog_direction,
            self._jog_continuous,
        )
        if self._jog_continuous:
            self._safe(self.axis.stop)
        else:
            # Quick click — cancel the pending continuous switch and fire
            # a single step move using whatever's in the Step spinbox.
            self._jog_timer.stop()
            self._safe(
                self.axis.move_relative,
                self._jog_direction * self.step_spin.value(),
            )
        self._jog_direction = 0
        self._jog_continuous = False

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
            log.warning("%s position read failed: %s", self.title(), e)
            payload["pos_err"] = str(e)
        try:
            sc, ec = self.axis.state()
            payload["state_code"] = sc
            payload["error_code"] = ec
        except Exception as e:  # noqa: BLE001
            log.warning("%s state read failed: %s", self.title(), e)
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
    Binning resets to OUR_DEFAULTS["BinningVertical"] too, but it's
    routed through binning_change_requested so CameraWindow can do the
    EndAcquisition / BeginAcquisition restart dance.

    Gamma and sharpness are intentionally not exposed: the on-board nodes
    are read-only on this Flea3 / Spinnaker 2.3 combo. Software gamma
    lives in the Image Adjustments panel.
    """

    # Emitted when the user picks a new binning value. CameraWindow owns
    # the restart dance (stop workers → EndAcquisition → write node →
    # BeginAcquisition → respawn workers) and calls binning_change_complete()
    # to re-enable the dropdown when the swap finishes.
    binning_change_requested = Signal(int)

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
        "BinningVertical": 1,  # 1 = no binning, 2 = 2×2 (800×600)
    }

    # Binning options shown in the dropdown. Filtered against the actual
    # camera-reported BinningVertical range at construction time. On the
    # Flea3 only [1, 2] is supported; entries outside that range get
    # dropped silently.
    BINNING_OPTIONS: tuple[tuple[int, str], ...] = (
        (1, "1× (1600×1200)"),
        (2, "2× (800×600)"),
    )

    def __init__(
        self, cam, defaults: dict, store: CameraPresetStore
    ) -> None:
        super().__init__("Camera Options")
        self._cam = cam
        self._nm = cam.GetNodeMap()
        # Serializes BalanceRatioSelector swap-and-access sequences. The
        # poll worker reads Red then Blue (each = swap selector + read
        # ratio); GUI-thread WB writes also swap selector + write ratio.
        # Without the lock those two pairs could interleave and one side
        # would see/write the wrong color.
        self._wb_lock = threading.Lock()
        self._poll_thread: QThread | None = None
        self._poll_worker: PollWorker | None = None
        self._defaults = dict(self.OUR_DEFAULTS)
        if defaults.get("gain") is not None:
            self._defaults["Gain"] = float(defaults["gain"])
        if defaults.get("exposure_us") is not None:
            self._defaults["ExposureTime"] = float(defaults["exposure_us"])

        self._store = store
        self._presets: dict[str, CameraSettingsSnapshot] = store.load()
        self._loaded_preset_name: str = "Default"
        self._loaded_snapshot: CameraSettingsSnapshot = default_camera_snapshot(
            self._defaults
        )

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

        self.binning_combo = QComboBox()
        self._populate_binning_combo()

        self.preset_combo = _PresetCombo(self._refresh_preset_ui)
        self.preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.preset_combo.setMinimumContentsLength(14)
        self.preset_combo.setMaximumWidth(160)
        self.preset_combo.addItem("Default", "Default")
        for name in self._presets:
            self.preset_combo.addItem(name, name)

        self.preset_new_btn = QPushButton("+")
        self.preset_save_btn = QPushButton("💾")
        self.preset_delete_btn = QPushButton("🗑")
        for b in (self.preset_new_btn, self.preset_save_btn, self.preset_delete_btn):
            b.setFixedSize(28, 28)

        outer = QVBoxLayout(self)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset_combo, stretch=1)
        preset_row.addWidget(self.preset_new_btn)
        preset_row.addWidget(self.preset_save_btn)
        preset_row.addWidget(self.preset_delete_btn)
        outer.addLayout(preset_row)

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

        binning_row = QHBoxLayout()
        binning_row.addWidget(QLabel("Binning:"))
        binning_row.addWidget(self.binning_combo, stretch=1)
        outer.addLayout(binning_row)

        self._setup_range_float("Gain", self.gain_spin)
        # Exposure spinbox is in ms; the camera reports ExposureTime in µs.
        # Set a fixed range up to MAX_EXPOSURE_MS — writes are clamped to
        # whatever the camera actually allows (gated by AcquisitionFrameRate).
        self.exp_spin.setRange(0.05, self.MAX_EXPOSURE_MS)
        # BalanceRatio is locked while WB auto is on. Try to read the
        # node's bounds anyway; fall back to Flea3-observed [0.25, 4.0].
        lo, hi = 0.25, 4.0
        rng = _node_float_range(self._nm, "BalanceRatio")
        if rng is not None:
            lo, hi = rng
        self.wb_red_spin.setRange(lo, hi)
        self.wb_blue_spin.setRange(lo, hi)

        self._apply_snapshot(self._loaded_snapshot)

        self.gain_spin.editingFinished.connect(self._on_gain_edited)
        self.exp_spin.editingFinished.connect(self._on_exp_edited)
        self.gain_auto.toggled.connect(self._on_gain_auto_toggled)
        self.exp_auto.toggled.connect(self._on_exp_auto_toggled)
        self.wb_auto.toggled.connect(self._on_wb_auto_toggled)
        self.wb_red_spin.editingFinished.connect(self._on_wb_red_edited)
        self.wb_blue_spin.editingFinished.connect(self._on_wb_blue_edited)
        self.binning_combo.activated.connect(self._on_binning_activated)
        self.preset_combo.activated.connect(self._on_preset_activated)
        self.preset_new_btn.clicked.connect(self._on_preset_new)
        self.preset_save_btn.clicked.connect(self._on_preset_save)
        self.preset_delete_btn.clicked.connect(self._on_preset_delete)
        self._refresh_preset_ui()

        # Worker-thread poll so the spinboxes track camera-driven values
        # (auto gain / auto exposure / auto WB) live. The slot ignores
        # readings for any channel whose auto checkbox is off — manual
        # values stay pinned to the user's last edit.
        self._poll_thread = QThread()
        self._poll_worker = PollWorker(self._poll_camera_state, 0.2)
        self._poll_worker.moveToThread(self._poll_thread)
        self._poll_thread.started.connect(self._poll_worker.run)
        self._poll_worker.state_ready.connect(self._apply_polled_state)
        self._poll_worker.finished.connect(self._poll_thread.quit)
        self._poll_thread.start()

    def _populate_binning_combo(self) -> None:
        """Build entries from BINNING_OPTIONS, filtered to the camera's
        actual BinningVertical range. Selects the current value. If only
        one option remains, the dropdown is disabled (purely informational)."""
        rng = _node_int_range(self._nm, "BinningVertical")
        current = _node_int_get(self._nm, "BinningVertical")
        if rng is None or current is None:
            self.binning_combo.addItem("unavailable", 1)
            self.binning_combo.setEnabled(False)
            return
        lo, hi = rng
        # `activated` (not currentIndexChanged) is used so this initial
        # populate doesn't fire a binning-change signal.
        for v, label in self.BINNING_OPTIONS:
            if lo <= v <= hi:
                self.binning_combo.addItem(label, v)
        idx = self.binning_combo.findData(current)
        if idx >= 0:
            self.binning_combo.setCurrentIndex(idx)
        self.binning_combo.setEnabled(self.binning_combo.count() > 1)

    def _on_binning_activated(self, idx: int) -> None:
        value = self.binning_combo.itemData(idx)
        if value is None:
            return
        # Disable while the restart is in flight; CameraWindow re-enables
        # via binning_change_complete() once workers are back up.
        self.binning_combo.setEnabled(False)
        self.binning_change_requested.emit(int(value))
        self._refresh_preset_ui()

    def binning_change_complete(self, applied_value: int) -> None:
        """Re-enable the combo and sync its index to whatever actually got
        applied (defends against the camera ignoring or clamping the
        write — e.g. if EndAcquisition/Begin failed mid-cycle)."""
        idx = self.binning_combo.findData(applied_value)
        if idx >= 0:
            # blockSignals so the resync doesn't re-emit the request.
            self.binning_combo.blockSignals(True)
            self.binning_combo.setCurrentIndex(idx)
            self.binning_combo.blockSignals(False)
        self.binning_combo.setEnabled(self.binning_combo.count() > 1)
        self._refresh_preset_ui()

    def _setup_range_float(self, name: str, spin: QDoubleSpinBox) -> None:
        rng = _node_float_range(self._nm, name)
        if rng is None:
            spin.setEnabled(False)
            return
        spin.setRange(*rng)

    def _set_float(self, name: str, value: float) -> None:
        _node_float_set(self._nm, name, value)

    def _set_enum(self, name: str, value: str) -> None:
        _node_enum_set(self._nm, name, value)

    def _on_gain_auto_toggled(self, on: bool) -> None:
        self._set_enum("GainAuto", "Continuous" if on else "Off")
        self.gain_spin.setEnabled(not on)
        if not on:
            v = _node_float_get(self._nm, "Gain")
            if v is not None:
                self.gain_spin.setValue(v)
        self._refresh_preset_ui()

    def _on_exp_auto_toggled(self, on: bool) -> None:
        self._set_enum("ExposureAuto", "Continuous" if on else "Off")
        self.exp_spin.setEnabled(not on)
        if not on:
            # Camera reports ExposureTime in µs; spinbox is in ms.
            v = _node_float_get(self._nm, "ExposureTime")
            if v is not None:
                self.exp_spin.setValue(v / 1000.0)
        self._refresh_preset_ui()

    def _on_wb_auto_toggled(self, on: bool) -> None:
        self._set_enum("BalanceWhiteAuto", "Continuous" if on else "Off")
        self.wb_red_spin.setEnabled(not on)
        self.wb_blue_spin.setEnabled(not on)
        if not on:
            for color, spin in (("Red", self.wb_red_spin), ("Blue", self.wb_blue_spin)):
                with self._wb_lock:
                    if not _node_enum_set(self._nm, "BalanceRatioSelector", color):
                        continue
                    v = _node_float_get(self._nm, "BalanceRatio")
                if v is not None:
                    spin.setValue(v)
        self._refresh_preset_ui()

    def _on_gain_edited(self) -> None:
        self._set_float("Gain", self.gain_spin.value())
        self._refresh_preset_ui()

    def _on_exp_edited(self) -> None:
        self._set_exposure_us(self.exp_spin.value() * 1000.0)
        self._refresh_preset_ui()

    def _on_wb_red_edited(self) -> None:
        self._set_balance_ratio("Red", self.wb_red_spin.value())
        self._refresh_preset_ui()

    def _on_wb_blue_edited(self) -> None:
        self._set_balance_ratio("Blue", self.wb_blue_spin.value())
        self._refresh_preset_ui()

    def _set_balance_ratio(self, color: str, value: float) -> None:
        if self.wb_auto.isChecked():
            return
        with self._wb_lock:
            if _node_enum_set(self._nm, "BalanceRatioSelector", color):
                _node_float_set(self._nm, "BalanceRatio", value)

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
        rng = _node_float_range(self._nm, "AcquisitionFrameRate")
        if rng is not None:
            _node_float_set(self._nm, "AcquisitionFrameRate", rng[0])
        _node_float_set(self._nm, "ExposureTime", value_us)
        rng = _node_float_range(self._nm, "AcquisitionFrameRate")
        if rng is not None:
            _node_float_set(self._nm, "AcquisitionFrameRate", rng[1])

    def _apply_snapshot(self, snap: CameraSettingsSnapshot) -> None:
        """Push a snapshot to the camera and sync the widgets.

        Manual fields (gain, exposure_us, balance_ratio_*) that are None
        are skipped — only the matching auto enum is written, and the
        spinbox is left where it is. The live poll catches the spinbox
        up to whatever auto picks within ~200 ms.

        Order: auto enums first, then manual values (inline), then widget
        sync, then the binning swap (which routes through
        binning_change_requested → CameraWindow's worker-chain restart).
        Inline-then-bin matches what _apply_defaults did before.
        """
        # Auto enums first so a "manual → continuous" transition writes
        # the new mode before any inline manual write becomes inactive.
        self._set_enum("GainAuto", snap.gain_auto)
        self._set_enum("ExposureAuto", snap.exposure_auto)
        self._set_enum("BalanceWhiteAuto", snap.balance_white_auto)

        if snap.gain is not None:
            self._set_float("Gain", float(snap.gain))
        if snap.exposure_us is not None:
            self._set_exposure_us(float(snap.exposure_us))
        # WB ratios are only writable while BalanceWhiteAuto is Off.
        if snap.balance_white_auto != "Continuous":
            for color, value in (
                ("Red", snap.balance_ratio_red),
                ("Blue", snap.balance_ratio_blue),
            ):
                if value is None:
                    continue
                with self._wb_lock:
                    if _node_enum_set(self._nm, "BalanceRatioSelector", color):
                        _node_float_set(self._nm, "BalanceRatio", float(value))

        gain_auto_on = snap.gain_auto == "Continuous"
        exp_auto_on = snap.exposure_auto == "Continuous"
        wb_auto_on = snap.balance_white_auto == "Continuous"
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
        if snap.gain is not None:
            self.gain_spin.setValue(float(snap.gain))
        if snap.exposure_us is not None:
            # Spinbox is ms; snapshot stores µs.
            self.exp_spin.setValue(float(snap.exposure_us) / 1000.0)
        if snap.balance_ratio_red is not None:
            self.wb_red_spin.setValue(float(snap.balance_ratio_red))
        if snap.balance_ratio_blue is not None:
            self.wb_blue_spin.setValue(float(snap.balance_ratio_blue))

        # Binning is a separate flow because the swap requires
        # EndAcquisition / BeginAcquisition. Only kick the restart if
        # we'd actually change the value — and only if the dropdown is
        # enabled (i.e. the camera supports more than one binning level).
        target_bin = snap.binning_vertical
        current_bin = _node_int_get(self._nm, "BinningVertical")
        if (
            self.binning_combo.isEnabled()
            and current_bin is not None
            and current_bin != target_bin
        ):
            self.binning_combo.setEnabled(False)
            self.binning_change_requested.emit(target_bin)

        # Re-capture from the widgets so the loaded snapshot reflects
        # what the spinboxes actually hold post-rounding. Without this,
        # values like ExposureTime=16798 µs (= 16.798 ms) get rounded by
        # exp_spin's 2-decimal display to 16.80 ms and round-trip back as
        # 16800 µs — leaving the panel permanently "dirty" relative to
        # the snapshot we just applied.
        self._loaded_snapshot = self._current_snapshot()

    def _current_snapshot(self) -> CameraSettingsSnapshot:
        """Snapshot the current widget state. Manual fields go to None when
        the corresponding auto checkbox is on — matching what gets written
        to TOML on save and what _apply_snapshot leaves alone on load."""
        gain_auto = "Continuous" if self.gain_auto.isChecked() else "Off"
        exposure_auto = "Continuous" if self.exp_auto.isChecked() else "Off"
        wb_auto = "Continuous" if self.wb_auto.isChecked() else "Off"
        return CameraSettingsSnapshot(
            gain_auto=gain_auto,
            exposure_auto=exposure_auto,
            balance_white_auto=wb_auto,
            binning_vertical=int(self.binning_combo.currentData() or 1),
            gain=(
                float(self.gain_spin.value()) if gain_auto != "Continuous" else None
            ),
            exposure_us=(
                float(self.exp_spin.value()) * 1000.0
                if exposure_auto != "Continuous"
                else None
            ),
            balance_ratio_red=(
                float(self.wb_red_spin.value()) if wb_auto != "Continuous" else None
            ),
            balance_ratio_blue=(
                float(self.wb_blue_spin.value()) if wb_auto != "Continuous" else None
            ),
        )

    def _refresh_preset_ui(self) -> None:
        """Re-decorate the preset combo's display name (dirty marker) and
        gate the save / delete buttons."""
        dirty = self._current_snapshot() != self._loaded_snapshot

        if self._loaded_preset_name == "Default":
            display = "Unsaved preset" if dirty else "Default"
        else:
            display = (
                f"{self._loaded_preset_name} (dirty)"
                if dirty
                else self._loaded_preset_name
            )

        idx = self.preset_combo.findData(self._loaded_preset_name)
        if idx >= 0:
            self.preset_combo.setItemText(idx, display)
            self.preset_combo.setCurrentIndex(idx)

        is_default = self._loaded_preset_name == "Default"
        self.preset_save_btn.setEnabled(not is_default and dirty)
        self.preset_delete_btn.setEnabled(not is_default)

    def _on_preset_activated(self, idx: int) -> None:
        raw = self.preset_combo.itemData(idx)
        if raw is None:
            return
        name = str(raw)
        if name == "Default":
            snap = default_camera_snapshot(self._defaults)
        else:
            snap_or_none = self._presets.get(name)
            if snap_or_none is None:
                return
            snap = snap_or_none
        self._loaded_preset_name = name
        # _apply_snapshot re-captures _loaded_snapshot from the widgets
        # post-application so any spinbox-rounding gets absorbed.
        self._apply_snapshot(snap)
        self._refresh_preset_ui()

    def _on_preset_new(self) -> None:
        name, ok = QInputDialog.getText(self, "New camera preset", "Name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Invalid name", "Preset name cannot be empty.")
            return
        if name == "Default":
            QMessageBox.warning(self, "Invalid name", "'Default' is reserved.")
            return
        if name in self._presets:
            QMessageBox.warning(
                self,
                "Invalid name",
                f"A preset named {name!r} already exists.",
            )
            return

        snap = self._current_snapshot()
        self._presets[name] = snap
        try:
            self._store.save_all(self._presets)
        except OSError as e:
            del self._presets[name]
            QMessageBox.critical(
                self,
                "Save failed",
                f"Could not write preset to presets.toml:\n\n{e}",
            )
            return

        self.preset_combo.addItem(name, name)
        self._loaded_preset_name = name
        self._loaded_snapshot = snap
        self._refresh_preset_ui()

    def _on_preset_save(self) -> None:
        name = self._loaded_preset_name
        if name == "Default":
            return
        snap = self._current_snapshot()
        prev = self._presets.get(name)
        self._presets[name] = snap
        try:
            self._store.save_all(self._presets)
        except OSError as e:
            if prev is not None:
                self._presets[name] = prev
            QMessageBox.critical(
                self,
                "Save failed",
                f"Could not write preset to presets.toml:\n\n{e}",
            )
            return
        self._loaded_snapshot = snap
        self._refresh_preset_ui()

    def _on_preset_delete(self) -> None:
        name = self._loaded_preset_name
        if name == "Default":
            return
        reply = QMessageBox.question(
            self,
            "Delete preset",
            f"Delete preset {name!r}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        prev = self._presets.pop(name, None)
        try:
            self._store.save_all(self._presets)
        except OSError as e:
            if prev is not None:
                self._presets[name] = prev
            QMessageBox.critical(
                self,
                "Delete failed",
                f"Could not write presets.toml:\n\n{e}",
            )
            return

        idx = self.preset_combo.findData(name)
        if idx >= 0:
            self.preset_combo.removeItem(idx)

        # Drop back to Default. Note: this does NOT re-apply Default to the
        # camera — only the preset-name pointer changes. The user can
        # explicitly select Default in the combo to re-apply.
        self._loaded_preset_name = "Default"
        self._loaded_snapshot = default_camera_snapshot(self._defaults)
        self._refresh_preset_ui()

    def _poll_camera_state(self) -> dict:
        """Worker-thread: read live camera values. Must not touch Qt widgets.

        Always reads everything; the GUI-thread slot decides which spinboxes
        to update based on each channel's auto checkbox. Returns None for
        any field whose read fails so the slot can skip it.
        """
        payload: dict = {
            "gain": _node_float_get(self._nm, "Gain"),
            "exposure_us": _node_float_get(self._nm, "ExposureTime"),
        }
        # BalanceRatio is multiplexed via BalanceRatioSelector; the lock
        # protects the swap+read pair against concurrent GUI-thread writes
        # (which also swap+write under the same lock).
        for color, key in (("Red", "wb_red"), ("Blue", "wb_blue")):
            with self._wb_lock:
                if _node_enum_set(self._nm, "BalanceRatioSelector", color):
                    payload[key] = _node_float_get(self._nm, "BalanceRatio")
                else:
                    payload[key] = None
        return payload

    @Slot(object)
    def _apply_polled_state(self, payload: dict) -> None:
        """Main-thread: push live camera values to spinboxes for any channel
        whose auto is on and whose spinbox isn't currently being edited.

        Skipping focused spinboxes preserves in-progress user typing —
        without that, a poll landing mid-edit would replace the user's
        digits before editingFinished fires.
        """
        if (
            self.gain_auto.isChecked()
            and not self.gain_spin.hasFocus()
            and (v := payload.get("gain")) is not None
        ):
            self.gain_spin.setValue(float(v))
        if (
            self.exp_auto.isChecked()
            and not self.exp_spin.hasFocus()
            and (v := payload.get("exposure_us")) is not None
        ):
            # Camera reports µs; spinbox is ms.
            self.exp_spin.setValue(float(v) / 1000.0)
        if self.wb_auto.isChecked():
            if (
                not self.wb_red_spin.hasFocus()
                and (v := payload.get("wb_red")) is not None
            ):
                self.wb_red_spin.setValue(float(v))
            if (
                not self.wb_blue_spin.hasFocus()
                and (v := payload.get("wb_blue")) is not None
            ):
                self.wb_blue_spin.setValue(float(v))

    def shutdown(self) -> None:
        if self._poll_worker is not None and self._poll_thread is not None:
            self._poll_worker.stop()
            self._poll_thread.quit()
            if not self._poll_thread.wait(2000):
                log.warning("camera options poll thread did not exit cleanly")


class ChannelHistogram(QWidget):
    """Single-channel histogram with range markers / shaded out-of-range zones.

    Each ImageAdjustmentsPanel embeds three of these — one beneath
    each RGB row — so the histogram is visually attached to the
    spinboxes that drive its remap range.
    """

    def __init__(self, color: tuple[int, int, int]) -> None:
        super().__init__()
        self._color = QColor(*color)
        self._image: QImage | None = None
        self._lo = 0
        self._hi = 255
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_image(self, image: QImage) -> None:
        """Pre-rendered curve image from HistWorker (256×48 ARGB32)."""
        self._image = image
        self.update()

    def set_range(self, lo: int, hi: int) -> None:
        if (lo, hi) == (self._lo, self._hi):
            return
        self._lo = lo
        self._hi = hi
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        if self._image is not None:
            painter.drawImage(self.rect(), self._image)
        else:
            painter.fillRect(self.rect(), QColor(20, 20, 20))

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


class _PresetCombo(QComboBox):
    """QComboBox whose dropdown menu always shows canonical names.

    The closed display can carry decoration (e.g. "preset1 (dirty)") via
    setItemText on the active row. Before the popup opens we
    re-canonicalize every item; after it closes we ask the panel to
    redecorate, so a click-outside dismissal doesn't strand the items in
    canonical form.
    """

    def __init__(self, on_popup_hidden, parent=None) -> None:
        super().__init__(parent)
        self._on_popup_hidden = on_popup_hidden

    def showPopup(self) -> None:
        for i in range(self.count()):
            name = self.itemData(i)
            if name is not None:
                self.setItemText(i, str(name))
        super().showPopup()

    def hidePopup(self) -> None:
        super().hidePopup()
        self._on_popup_hidden()


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

    def __init__(
        self, adjustments: ImageAdjustments, store: ImagePresetStore
    ) -> None:
        super().__init__("Image Adjustments")
        self._adj = adjustments
        self._store = store
        self._presets: dict[str, AdjustmentSnapshot] = store.load()
        self._loaded_preset_name: str = "Default"
        self._loaded_snapshot: AdjustmentSnapshot = DEFAULT_ADJUSTMENTS
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

        self.preset_combo = _PresetCombo(self._refresh_preset_ui)
        self.preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.preset_combo.setMinimumContentsLength(14)
        self.preset_combo.setMaximumWidth(160)
        self.preset_combo.addItem("Default", "Default")
        for name in self._presets:
            self.preset_combo.addItem(name, name)

        self.preset_new_btn = QPushButton("+")
        self.preset_save_btn = QPushButton("💾")
        self.preset_delete_btn = QPushButton("🗑")
        for b in (self.preset_new_btn, self.preset_save_btn, self.preset_delete_btn):
            b.setFixedSize(28, 28)

        outer = QVBoxLayout(self)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset_combo, stretch=1)
        preset_row.addWidget(self.preset_new_btn)
        preset_row.addWidget(self.preset_save_btn)
        preset_row.addWidget(self.preset_delete_btn)
        outer.addLayout(preset_row)

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

        self.brightness_slider.valueChanged.connect(self._on_brightness)
        self.contrast_slider.valueChanged.connect(self._on_contrast)
        self.saturation_slider.valueChanged.connect(self._on_saturation)
        for slider, name in (
            (self.r_range, "r_range"),
            (self.g_range, "g_range"),
            (self.b_range, "b_range"),
        ):
            slider.valueChanged.connect(lambda _v, n=name: self._on_range(n))

        self.preset_combo.activated.connect(self._on_preset_activated)
        self.preset_new_btn.clicked.connect(self._on_preset_new)
        self.preset_save_btn.clicked.connect(self._on_preset_save)
        self.preset_delete_btn.clicked.connect(self._on_preset_delete)

        self._building = False
        self._refresh_preset_ui()

    @Slot(QImage, QImage, QImage)
    def set_hist_images(self, r: QImage, g: QImage, b: QImage) -> None:
        """Slot for HistWorker.images_ready — pre-rendered curves."""
        self.r_hist.set_image(r)
        self.g_hist.set_image(g)
        self.b_hist.set_image(b)

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
        self._refresh_preset_ui()

    def _on_contrast(self, v: int) -> None:
        self.contrast_label.setText(f"{v}%")
        if self._building:
            return
        self._adj.update(contrast=v)
        self._refresh_preset_ui()

    def _on_saturation(self, v: int) -> None:
        self.saturation_label.setText(f"{v}%")
        if self._building:
            return
        self._adj.update(saturation=v)
        self._refresh_preset_ui()

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
        self._refresh_preset_ui()

    def _apply_snapshot(self, snap: AdjustmentSnapshot) -> None:
        """Push snap to sliders + adjustments backend without firing handlers."""
        self._adj.update(
            brightness=snap.brightness,
            contrast=snap.contrast,
            saturation=snap.saturation,
            r_range=snap.r_range,
            g_range=snap.g_range,
            b_range=snap.b_range,
        )
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

    def _current_snapshot(self) -> AdjustmentSnapshot:
        return AdjustmentSnapshot(
            brightness=int(self.brightness_slider.value()),
            contrast=int(self.contrast_slider.value()),
            saturation=int(self.saturation_slider.value()),
            r_range=_coerce_pair(self.r_range.value()),
            g_range=_coerce_pair(self.g_range.value()),
            b_range=_coerce_pair(self.b_range.value()),
        )

    def _refresh_preset_ui(self) -> None:
        if self._building:
            return
        dirty = self._current_snapshot() != self._loaded_snapshot

        if self._loaded_preset_name == "Default":
            display = "Unsaved preset" if dirty else "Default"
        else:
            display = (
                f"{self._loaded_preset_name} (dirty)"
                if dirty
                else self._loaded_preset_name
            )

        idx = self.preset_combo.findData(self._loaded_preset_name)
        if idx >= 0:
            self.preset_combo.setItemText(idx, display)
            self.preset_combo.setCurrentIndex(idx)

        is_default = self._loaded_preset_name == "Default"
        self.preset_save_btn.setEnabled(not is_default and dirty)
        self.preset_delete_btn.setEnabled(not is_default)

    def _on_preset_activated(self, idx: int) -> None:
        raw = self.preset_combo.itemData(idx)
        if raw is None:
            return
        name = str(raw)
        if name == "Default":
            snap = DEFAULT_ADJUSTMENTS
        else:
            snap_or_none = self._presets.get(name)
            if snap_or_none is None:
                return
            snap = snap_or_none
        self._loaded_preset_name = name
        self._loaded_snapshot = snap
        self._apply_snapshot(snap)
        self._refresh_preset_ui()

    def _on_preset_new(self) -> None:
        name, ok = QInputDialog.getText(self, "New preset", "Name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Invalid name", "Preset name cannot be empty.")
            return
        if name == "Default":
            QMessageBox.warning(self, "Invalid name", "'Default' is reserved.")
            return
        if name in self._presets:
            QMessageBox.warning(
                self,
                "Invalid name",
                f"A preset named {name!r} already exists.",
            )
            return

        snap = self._current_snapshot()
        self._presets[name] = snap
        try:
            self._store.save_all(self._presets)
        except OSError as e:
            del self._presets[name]
            QMessageBox.critical(
                self,
                "Save failed",
                f"Could not write preset to presets.toml:\n\n{e}",
            )
            return

        self.preset_combo.addItem(name, name)
        self._loaded_preset_name = name
        self._loaded_snapshot = snap
        self._refresh_preset_ui()

    def _on_preset_save(self) -> None:
        name = self._loaded_preset_name
        if name == "Default":
            return
        snap = self._current_snapshot()
        prev = self._presets.get(name)
        self._presets[name] = snap
        try:
            self._store.save_all(self._presets)
        except OSError as e:
            if prev is not None:
                self._presets[name] = prev
            QMessageBox.critical(
                self,
                "Save failed",
                f"Could not write preset to presets.toml:\n\n{e}",
            )
            return
        self._loaded_snapshot = snap
        self._refresh_preset_ui()

    def _on_preset_delete(self) -> None:
        name = self._loaded_preset_name
        if name == "Default":
            return
        reply = QMessageBox.question(
            self,
            "Delete preset",
            f"Delete preset {name!r}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        prev = self._presets.pop(name, None)
        try:
            self._store.save_all(self._presets)
        except OSError as e:
            if prev is not None:
                self._presets[name] = prev
            QMessageBox.critical(
                self,
                "Delete failed",
                f"Could not write presets.toml:\n\n{e}",
            )
            return

        idx = self.preset_combo.findData(name)
        if idx >= 0:
            self.preset_combo.removeItem(idx)

        self._loaded_preset_name = "Default"
        self._loaded_snapshot = DEFAULT_ADJUSTMENTS
        self._apply_snapshot(DEFAULT_ADJUSTMENTS)
        self._refresh_preset_ui()


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

        # Hidden — kept so existing _on_set / _on_run / etc. error setters
        # don't need to change while we revamp the visible layout.
        self.status_label = QLabel("disconnected")
        self.status_label.setVisible(False)

        # --- hero readouts: PV + setpoint side by side ----------------------
        # Build the hero font from a real label's font so we inherit the
        # application's point size — bare QFont() can give pointSize() == -1.
        self.pv_label = QLabel("—")
        hero_font = QFont(self.pv_label.font())
        hero_font.setPointSize(hero_font.pointSize() + 8)
        hero_font.setStyleHint(QFont.StyleHint.Monospace)
        hero_font.setFamily("monospace")
        hero_font.setBold(True)
        # Pin a minimum height so QGridLayout can't squeeze the row to 0
        # when the right column is vertically constrained (e.g. on maximize).
        hero_min_h = QFontMetrics(hero_font).height() + 2
        # Align to bottom so the digits sit close to the caption below.
        hero_align = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
        self.pv_label.setFont(hero_font)
        self.pv_label.setAlignment(hero_align)
        self.pv_label.setMinimumHeight(hero_min_h)

        self.setpoint_value_label = QLabel("—")
        self.setpoint_value_label.setFont(hero_font)
        self.setpoint_value_label.setAlignment(hero_align)
        self.setpoint_value_label.setStyleSheet("color: #888;")
        self.setpoint_value_label.setMinimumHeight(hero_min_h)

        caption_style = "color: #888;"
        pv_caption = QLabel("process")
        pv_caption.setStyleSheet(caption_style)
        pv_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sp_caption = QLabel("setpoint")
        sp_caption.setStyleSheet(caption_style)
        sp_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # --- status line: status (left) + output (right) --------------------
        self.status_text_label = QLabel("status: —")
        self.output_pct_label = QLabel("output  —")
        self.output_cap_label = QLabel("")
        self.output_cap_label.setStyleSheet(caption_style)

        # --- editable controls ----------------------------------------------
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

        self.run_btn = action_button("Run")
        self.stop_btn = action_button("Stop")

        outer = QVBoxLayout(self)
        outer.addWidget(self.status_label)

        hero_grid = QGridLayout()
        hero_grid.setHorizontalSpacing(16)
        hero_grid.setVerticalSpacing(0)
        hero_grid.setRowStretch(0, 0)
        hero_grid.setRowStretch(1, 0)
        hero_grid.addWidget(self.pv_label, 0, 0)
        hero_grid.addWidget(self.setpoint_value_label, 0, 1)
        hero_grid.addWidget(pv_caption, 1, 0)
        hero_grid.addWidget(sp_caption, 1, 1)
        outer.addLayout(hero_grid)

        status_row = QHBoxLayout()
        status_row.addWidget(self.status_text_label)
        status_row.addStretch(1)
        status_row.addWidget(self.output_pct_label)
        status_row.addWidget(self.output_cap_label)
        outer.addLayout(status_row)

        outer.addSpacing(8)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep)

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
            log.warning("heater PV read failed: %s", e)
            payload["pv_err"] = str(e)
        try:
            payload["sp"] = self.heater.setpoint()
        except Exception as e:  # noqa: BLE001 — keep going
            log.warning("heater setpoint read failed: %s", e)
        try:
            payload["state"] = self.heater.system_state()
        except Exception as e:  # noqa: BLE001
            log.warning("heater state read failed: %s", e)
        try:
            payload["out"] = self.heater.output_percent()
        except Exception as e:  # noqa: BLE001
            log.warning("heater output read failed: %s", e)
            payload["out_err"] = str(e)
        try:
            payload["out_hi"] = self.heater.output_limit_high()
        except Exception as e:  # noqa: BLE001 — keep going
            log.warning("heater output_limit_high read failed: %s", e)
        return payload

    _STATUS_COLORS = {
        "run": "#2e7d32",   # green
        "fault": "#c0392b", # red
        "tune": "#1976d2",  # blue
        "idle": "#888888",
    }

    @Slot(object)
    def _apply_state(self, payload: dict) -> None:
        """Main-thread: render a payload from the polling worker."""
        if "pv" in payload:
            self.pv_label.setText(f"{payload['pv']:.2f} {self.units}")
        elif "pv_err" in payload:
            self.pv_label.setText("pv err")

        if "sp" in payload:
            sp = payload["sp"]
            self.setpoint_value_label.setText(f"{sp:.2f} {self.units}")
            if not self.setpoint_spin.hasFocus():
                self.setpoint_spin.setValue(sp)

        if "state" in payload:
            state = payload["state"]
            if state == SystemState.RUN:
                kind = "run"
            elif state in (SystemState.FAULT, SystemState.SHUTDOWN):
                kind = "fault"
            elif state == SystemState.AUTOTUNE:
                kind = "tune"
            else:
                kind = "idle"
            color = self._STATUS_COLORS[kind]
            self.status_text_label.setText(
                f"status: <span style='color: {color}; font-weight: bold;'>"
                f"{state.name}</span>"
            )
            # Run is a no-op while already running (and worse, can re-arm
            # the controller mid-cycle in some firmwares). Disable to
            # signal that. Stop stays always-on so it's the panic button.
            self.run_btn.setEnabled(state != SystemState.RUN)

        if "out" in payload:
            self.output_pct_label.setText(f"output  {payload['out']:.1f} %")
        elif "out_err" in payload:
            self.output_pct_label.setText(f"output err: {payload['out_err']}")

        if "out_hi" in payload:
            cap = payload["out_hi"]
            self.output_cap_label.setText(f"(cap {cap:.0f} %)")
            if not self.max_output_spin.hasFocus():
                self.max_output_spin.setValue(cap)

    def shutdown(self) -> None:
        if self._worker is not None and self._worker_thread is not None:
            self._worker.stop()
            self._worker_thread.quit()
            if not self._worker_thread.wait(2000):
                log.warning("heater thread did not exit cleanly")
        self.heater.close()


class StartupAborted(Exception):
    """Raised when the operator clicks X on LaunchingDialog mid-startup.

    Propagates up out of CameraWindow construction so main() can exit
    cleanly. Partial init state (PySpin handles, opened panels) is torn
    down in CameraWindow's except path before the exception re-raises.
    """


class LaunchingDialog(QDialog):
    """Small 'Launching…' indicator shown during CameraWindow construction.

    Closes the operator-feedback gap that lets the GUI process run for
    several seconds (PySpin / serial / VISA setup) with no visible
    window. The X (window close button) sets a cancel flag; CameraWindow
    checks it at construction checkpoints and raises StartupAborted.

    Honest caveat: X is responsive *between* construction steps, not
    *during* a single long step (e.g. Yoko's ~8.5 s VISA timeout when
    the 7651 is offline). Full responsiveness requires deferring device
    connects to worker threads — separate refactor.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Air Stacker — launching")
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint
        )
        self._label = QLabel("Launching Air Stacker GUI…")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addWidget(self._label)
        self.setMinimumWidth(360)
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def update_status(self, text: str) -> None:
        """Update the visible message, repaint, and let queued events
        (including an X click) through. Call this between long steps."""
        self._label.setText(text)
        QApplication.processEvents()

    def closeEvent(self, event) -> None:
        self._cancelled = True
        log.info("launch dialog X clicked — abort at next checkpoint")
        event.accept()


class CameraWindow(QMainWindow):
    def __init__(self, launch_dialog: LaunchingDialog | None = None) -> None:
        super().__init__()
        self._launch_dialog = launch_dialog
        # Sub-panel refs pre-init'd to None *here* (not in _initialize) so
        # _abort_cleanup can rely on their existence regardless of how
        # early the abort fires. Their nullability is already part of
        # the post-init runtime contract (each panel is conditional on
        # config presence), so this doesn't pollute the type at use sites.
        self.axis_panels: list[ConexAxisPanel] = []
        self.heater_panel: HeaterPanel | None = None
        self.smc100_panel: SMC100Panel | None = None
        self.yoko_panel: YokoPanel | None = None
        self.camera_options_panel: CameraOptionsPanel | None = None
        self.adjustments_panel: ImageAdjustmentsPanel | None = None
        # PySpin handles get assigned in _initialize. They are *not*
        # pre-init'd to None — once _initialize succeeds they're
        # non-nullable from every runtime method's perspective.
        # _abort_cleanup gates its PySpin teardown on this flag rather
        # than touching self.cam directly when it may not exist.
        self._pyspin_acquired = False
        try:
            self._initialize()
        except StartupAborted:
            self._abort_cleanup()
            raise

    def _checkpoint(self, status: str) -> None:
        """Update the launch dialog (if any) and abort if X was clicked.

        Call between long-running init steps so the dialog stays
        responsive and the operator's X click takes effect promptly.
        """
        if self._launch_dialog is None:
            return
        self._launch_dialog.update_status(status)
        if self._launch_dialog.cancelled:
            raise StartupAborted()

    def _abort_cleanup(self) -> None:
        """Best-effort tear-down of partially-constructed state.

        Sub-panel refs are pre-init'd to None in __init__, so the
        `is not None` checks here just mean "construction made it that
        far". PySpin teardown gates on _pyspin_acquired — the runtime
        contract is that self.cam / self._cam_list / self._spin_system
        are only valid when that flag is True.
        """
        log.info("startup aborted — tearing down partial CameraWindow")
        for p in (
            self.camera_options_panel,
            self.heater_panel,
            self.smc100_panel,
            self.yoko_panel,
        ):
            if p is not None:
                try:
                    p.shutdown()
                except Exception as e:  # noqa: BLE001
                    log.debug("abort cleanup (panel): %s", e)
        for ap in self.axis_panels:
            try:
                ap.shutdown()
            except Exception as e:  # noqa: BLE001
                log.debug("abort cleanup (axis): %s", e)
        if self._pyspin_acquired:
            try:
                self.cam.EndAcquisition()
            except Exception as e:  # noqa: BLE001
                log.debug("abort cleanup EndAcquisition: %s", e)
            try:
                self.cam.DeInit()
            except Exception as e:  # noqa: BLE001
                log.debug("abort cleanup DeInit: %s", e)
            try:
                self._cam_list.Clear()
            except Exception as e:  # noqa: BLE001
                log.debug("abort cleanup Clear: %s", e)
            try:
                self._spin_system.ReleaseInstance()
            except Exception as e:  # noqa: BLE001
                log.debug("abort cleanup ReleaseInstance: %s", e)
            self._pyspin_acquired = False

    def _initialize(self) -> None:
        self._checkpoint("Loading config…")
        self.setWindowTitle("Air Stacker — live")
        self.label = CameraDisplay()
        self.label.setText("connecting…")
        self.label.setMinimumSize(640, 480)
        self.status_bar = StatusBar()
        self._frame_times: deque[float] = deque(maxlen=60)
        self._fps_last_update = 0.0
        self._proc_total = 0.0
        self._proc_max = 0.0
        self._proc_count = 0
        self._on_frame_calls = 0
        self._on_frame_noops = 0
        self._on_frame_log_start = time.perf_counter()
        self.adjustments = ImageAdjustments()

        config = load_config()
        settings = load_settings()
        camera_cfg = config.get("camera", {})
        device_index = int(camera_cfg.get("device_index", 0))
        # WebcamConfig draws project-level keys (device, format, defaults)
        # from config.toml's [webcam] section and per-machine window geometry
        # from settings.toml's [gui_state.webcam] section.
        self._webcam_cfg = WebcamConfig.from_toml(
            config.get("webcam"),
            settings.get("gui_state", {}).get("webcam"),
        )
        self._webcam_window: WebcamWindow | None = None

        self._checkpoint("Initializing camera (Spinnaker)…")
        spin_system = PySpin.System.GetInstance()
        cam_list = spin_system.GetCameras()
        if cam_list.GetSize() == 0:
            # Roll back the local refs immediately — they never make
            # it onto self, so _abort_cleanup has nothing to do.
            cam_list.Clear()
            spin_system.ReleaseInstance()
            raise RuntimeError("no cameras enumerated by Spinnaker")
        self._spin_system = spin_system
        self._cam_list = cam_list
        self.cam = cam_list.GetByIndex(device_index)
        self.cam.Init()
        self._apply_camera_startup()
        self.cam.BeginAcquisition()
        # PySpin handles are now in their runtime-valid state. Any
        # later abort (or normal closeEvent) routes teardown through
        # _abort_cleanup / closeEvent, both of which see the flag set.
        self._pyspin_acquired = True

        self._checkpoint("Building UI…")
        settings_panel = self._build_settings_panel(camera_cfg)
        right_panel = self._build_right_panel(
            axes_cfg=config.get("axis", []),
            heater_cfg=config.get("heater"),
            smc100_cfg=config.get("smc100"),
            yoko_cfg=config.get("yoko"),
        )

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(settings_panel)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        middle_layout.addWidget(self.label, stretch=1)
        middle_layout.addWidget(self.status_bar)
        layout.addWidget(middle, stretch=1)

        layout.addWidget(right_panel)
        self.setCentralWidget(central)

        if self.camera_options_panel is not None:
            self.camera_options_panel.binning_change_requested.connect(
                self._on_binning_change_requested
            )

        # Webcam toggle wiring. The button is disabled if the [webcam] config
        # is `enabled = false` — operator can't open something we've been told
        # to ignore.
        if self._webcam_cfg.enabled:
            self.status_bar.webcam_toggled.connect(self._on_webcam_toggled)
            if self._webcam_cfg.default_visible:
                self.status_bar.webcam_button.setChecked(True)
        else:
            self.status_bar.webcam_button.setEnabled(False)
            self.status_bar.webcam_button.setToolTip("disabled in config.toml [webcam]")

        # Drawing overlay: pencil toggles draw mode on the FLIR GL surface,
        # the Freehand/Line group selects the active tool, trash clears.
        # Primitives live in the GL window (normalized camera-image coords);
        # see _CameraGLWindow / overlay.py.
        self.status_bar.pencil_toggled.connect(self.label.set_drawing_enabled)
        self.status_bar.tool_changed.connect(self.label.set_tool)
        self.status_bar.clear_drawing_clicked.connect(self.label.clear_strokes)

        # Pipeline state — workers and mailboxes are recreated every time
        # acquisition (re)starts, e.g. on a binning swap.
        self.acq_mailbox: FrameMailbox | None = None
        self.hist_mailbox: FrameMailbox | None = None
        self.acq_thread: QThread | None = None
        self.proc_thread: QThread | None = None
        self.hist_thread: QThread | None = None
        self.acq_worker: CameraAcquireWorker | None = None
        self.proc_worker: CameraProcessWorker | None = None
        self.hist_worker: HistWorker | None = None
        self._spawn_workers()

    def _spawn_workers(self) -> None:
        """Build the three-stage pipeline and start its threads.

        Three-stage pipeline:
          acq thread → acq_mailbox → proc thread ─┬─→ GUI mailbox → display
                                                  └─→ hist_mailbox → hist thread
        acq fetches + debayers; proc applies adjustments and forks the
        post-adjust frame to both the GUI (via take_latest / frame_ready)
        and the hist thread; hist computes + renders curve QImages off
        the critical path.

        Workers + mailboxes are freshly constructed each call so a
        binning swap (which goes through EndAcquisition / BeginAcquisition)
        starts with no stale frames in flight.
        """
        self.acq_mailbox = FrameMailbox()
        self.hist_mailbox = FrameMailbox()

        self.acq_thread = QThread()
        self.acq_worker = CameraAcquireWorker(self.cam, self.acq_mailbox)
        self.acq_worker.moveToThread(self.acq_thread)
        self.acq_thread.started.connect(self.acq_worker.run)
        self.acq_worker.error.connect(self._on_frame_error)
        self.acq_worker.finished.connect(self.acq_thread.quit)

        self.proc_thread = QThread()
        self.proc_worker = CameraProcessWorker(
            self.acq_mailbox, self.adjustments, self.hist_mailbox
        )
        self.proc_worker.moveToThread(self.proc_thread)
        self.proc_thread.started.connect(self.proc_worker.run)
        self.proc_worker.frame_ready.connect(self._on_frame)
        self.proc_worker.finished.connect(self.proc_thread.quit)

        self.hist_thread = QThread()
        self.hist_worker = HistWorker(self.hist_mailbox)
        self.hist_worker.moveToThread(self.hist_thread)
        self.hist_thread.started.connect(self.hist_worker.run)
        self.hist_worker.finished.connect(self.hist_thread.quit)
        if self.adjustments_panel is not None:
            self.hist_worker.images_ready.connect(
                self.adjustments_panel.set_hist_images,
                Qt.ConnectionType.QueuedConnection,
            )
        self.hist_worker.metrics_ready.connect(
            self.status_bar.set_sharpness,
            Qt.ConnectionType.QueuedConnection,
        )

        self.acq_thread.start()
        self.proc_thread.start()
        self.hist_thread.start()

    def _stop_workers(self) -> None:
        """Tear down the pipeline: stop workers, quit threads, wait.

        Stop order matters: acq first so no new frames land in the
        mailbox, then proc and hist (both will drain their take(timeout)
        waits via the wake)."""
        if self.acq_worker is not None:
            self.acq_worker.stop()
        if self.proc_worker is not None:
            self.proc_worker.stop()
        if self.hist_worker is not None:
            self.hist_worker.stop()
        for label, thread in (
            ("acq", self.acq_thread),
            ("proc", self.proc_thread),
            ("hist", self.hist_thread),
        ):
            if thread is None:
                continue
            thread.quit()
            if not thread.wait(2000):
                log.warning("%s thread did not exit cleanly", label)

    def _apply_camera_startup(self) -> None:
        """Set acquisition mode, balance-white-auto, and unlock the frame rate.

        Frame rate matters: SpinView leaves AcquisitionFrameRate set on the
        camera (it's persistent), so without resetting it we inherit
        whatever the last user picked. We turn off auto, enable explicit
        control, and pin to the camera's max for live preview.

        Binning likewise persists across SpinView/our-app sessions; we
        force BinningVertical = 1 so a fresh launch always starts at full
        resolution. Operators flip to 2× per session via the dropdown.

        Gain / exposure / their auto modes are applied later by
        CameraOptionsPanel from the camera section of config.toml.
        """
        nm = self.cam.GetNodeMap()
        for name, value in (
            ("AcquisitionMode", "Continuous"),
            ("BalanceWhiteAuto", "Continuous"),
            ("AcquisitionFrameRateAuto", "Off"),
        ):
            if not _node_enum_set(nm, name, value):
                log.warning("camera %s: not writable or missing", name)
        # The "enabled" node is named differently across FLIR generations.
        for name in ("AcquisitionFrameRateEnabled", "AcquisitionFrameRateEnable"):
            if _node_bool_set(nm, name, True):
                break
        # Default to no binning. Setting BinningVertical alone is the
        # right move on this Flea3: BinningHorizontal is read-only and
        # slaves to vertical, so writing 2 here gives full 2×2.
        _node_int_set(nm, "BinningVertical", 1)
        self._pin_frame_rate_max(nm)

    def _pin_frame_rate_max(self, nm) -> None:
        """Pin AcquisitionFrameRate to its current max. The achievable
        max shifts after binning / exposure changes, so re-pin after any
        of those."""
        rng = _node_float_range(nm, "AcquisitionFrameRate")
        if rng is None:
            log.warning("camera AcquisitionFrameRate: not available")
            return
        _node_float_set(nm, "AcquisitionFrameRate", rng[1])
        v = _node_float_get(nm, "AcquisitionFrameRate")
        log.info(
            "camera AcquisitionFrameRate = %.2f Hz (range %.2f..%.2f)",
            v if v is not None else float("nan"),
            rng[0], rng[1],
        )

    @Slot(int)
    def _on_binning_change_requested(self, value: int) -> None:
        """Stop workers, change BinningVertical, restart workers.

        Called from CameraOptionsPanel.binning_change_requested. Blocks
        the GUI thread for ~1 s while the worker chain restarts; the
        dropdown is disabled by the panel during that window."""
        log.info("binning change requested: %d×", value)
        self._stop_workers()
        try:
            self.cam.EndAcquisition()
        except Exception as e:  # noqa: BLE001
            log.warning("EndAcquisition during binning change: %s", e)
        nm = self.cam.GetNodeMap()
        if not _node_int_set(nm, "BinningVertical", value):
            log.warning("BinningVertical=%d write failed", value)
        # Frame-rate ceiling shifts after a binning change; re-pin to max.
        self._pin_frame_rate_max(nm)
        try:
            self.cam.BeginAcquisition()
        except PySpin.SpinnakerException as e:
            log.error("BeginAcquisition after binning change failed: %s", e)
            self.label.clear_frame()
            self.label.setText(f"binning swap failed: {e}")
            applied = _node_int_get(nm, "BinningVertical") or value
            if self.camera_options_panel is not None:
                # Re-enable the combo so the user can attempt to recover.
                self.camera_options_panel.binning_change_complete(applied)
            return
        # Reset status — old FPS timestamps are pre-restart and would
        # show a fake "frozen" rate during the recovery; sharpness from
        # the previous binning is meaningless once the sensor changes.
        self._frame_times.clear()
        self.status_bar.set_fps(None)
        self.status_bar.set_sharpness(None)
        # Drop strokes on binning swap. Stored coords are normalized to
        # the camera-image rect, so they'd technically re-render fine,
        # but the operator's reference points (a circled flake at full
        # res) usually mean nothing under the new field of view. Drawing
        # mode itself stays as-is — the toggle button isn't touched.
        self.label.clear_strokes()
        self._spawn_workers()
        applied = _node_int_get(nm, "BinningVertical") or value
        if self.camera_options_panel is not None:
            self.camera_options_panel.binning_change_complete(applied)
        log.info("binning change complete: %d×", applied)

    @Slot(bool)
    def _on_webcam_toggled(self, checked: bool) -> None:
        """Open or close the detached webcam window.

        Connected to `StatusBar.webcam_toggled`. The window is created
        lazily on first open; on close (either via the toggle or the
        window's X button) we save its geometry to config.toml so the
        next open lands in the same place.
        """
        if checked:
            if self._webcam_window is None:
                self._webcam_window = WebcamWindow(self._webcam_cfg, parent=self)
                self._webcam_window.closed.connect(self._on_webcam_closed)
            self._webcam_window.show()
            self._webcam_window.raise_()
            self._webcam_window.activateWindow()
        else:
            if self._webcam_window is not None:
                self._webcam_window.close()
                # _on_webcam_closed handles cleanup + geometry save.

    @Slot()
    def _on_webcam_closed(self) -> None:
        """Webcam window dismissed — save geometry, drop the ref, sync the toggle."""
        if self._webcam_window is not None:
            self._persist_webcam_geometry(self._webcam_window.saved_geometry())
            self._webcam_window.deleteLater()
            self._webcam_window = None
        self.status_bar.set_webcam_checked(False)

    def _persist_webcam_geometry(self, geometry: tuple[int, int, int, int]) -> None:
        """Write the webcam window's last (x, y, w, h) to settings.toml under
        `[gui_state.webcam] window_geometry`.

        settings.toml is per-machine state (untracked); the GUI creates it on
        first write. Best-effort — failures log but don't propagate.
        """
        try:
            if SETTINGS_PATH.exists():
                doc = tomlkit.parse(SETTINGS_PATH.read_text(encoding="utf-8"))
            else:
                doc = tomlkit.document()
            gui_state = doc.setdefault("gui_state", tomlkit.table())
            webcam = gui_state.setdefault("webcam", tomlkit.table())
            geom_table = tomlkit.inline_table()
            geom_table.update({
                "x": geometry[0], "y": geometry[1],
                "width": geometry[2], "height": geometry[3],
            })
            webcam["window_geometry"] = geom_table
            SETTINGS_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — settings write is best-effort
            log.warning("could not persist webcam geometry: %s", e)

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
        layout.addWidget(recording)

        self.adjustments_panel = ImageAdjustmentsPanel(
            self.adjustments, ImagePresetStore(SETTINGS_PATH)
        )

        self.camera_options_panel = CameraOptionsPanel(
            self.cam, camera_cfg, CameraPresetStore(SETTINGS_PATH)
        )

        layout.addWidget(self.camera_options_panel)
        layout.addWidget(self.adjustments_panel)
        layout.addStretch(1)
        return panel

    def _build_right_panel(
        self,
        axes_cfg: list[dict],
        heater_cfg: dict | None,
        smc100_cfg: dict | None = None,
        yoko_cfg: dict | None = None,
    ) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Ordering principle: top-to-bottom by order-of-use during a
        # stacking run. Rotation stage(s) come first (orient the sample),
        # then heater (bring up to temp), then coarse Z (SMC100, mm scale,
        # rough approach), then fine Z (Yoko/NPM140 piezo, µm scale, final
        # touch-down) at the bottom.
        for cfg in axes_cfg:
            self._checkpoint(f"Connecting to axis: {cfg.get('name', '?')}…")
            ap = ConexAxisPanel(cfg)
            self.axis_panels.append(ap)
            layout.addWidget(ap)
        if heater_cfg:
            self._checkpoint("Connecting to heater…")
            self.heater_panel = HeaterPanel(heater_cfg)
            layout.addWidget(self.heater_panel)
        if smc100_cfg:
            self._checkpoint("Connecting to SMC100 (Z stage)…")
            self.smc100_panel = SMC100Panel(smc100_cfg)
            layout.addWidget(self.smc100_panel)
        if yoko_cfg:
            # NB: when the 7651 is offline this call blocks ~8.5 s on
            # VISA timeouts. The launch dialog will sit on "Connecting
            # to Yoko…" for that whole window and not be responsive to
            # an X click until it returns. Deferred-connect refactor
            # would fix that.
            self._checkpoint("Connecting to Yoko (NPM140 piezo)…")
            self.yoko_panel = YokoPanel(yoko_cfg)
            layout.addWidget(self.yoko_panel)
        self._checkpoint("Finalizing UI…")
        layout.addStretch(1)
        return panel

    def _on_frame(self) -> None:
        self._on_frame_calls += 1
        if self.proc_worker is None:
            return
        rgb = self.proc_worker.take_latest()
        if rgb is None:
            self._on_frame_noops += 1
            self._maybe_log_on_frame_rates()
            return  # drained by a previous slot run
        t0 = time.perf_counter()
        # GL display: hand the numpy array straight to paintGL — no
        # QImage build, no CPU pre-scale. paintGL uploads to texture.
        self.label.set_frame(rgb)
        self._update_fps()
        elapsed = time.perf_counter() - t0
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
        self._maybe_log_on_frame_rates()

    def _maybe_log_on_frame_rates(self) -> None:
        now = time.perf_counter()
        span = now - self._on_frame_log_start
        if span < 2.0:
            return
        delivered = self._on_frame_calls - self._on_frame_noops
        log.debug(
            "_on_frame calls=%.1f/s delivered=%.1f/s noops=%.1f/s",
            self._on_frame_calls / span,
            delivered / span,
            self._on_frame_noops / span,
        )
        self._on_frame_calls = 0
        self._on_frame_noops = 0
        self._on_frame_log_start = now

    def _on_frame_error(self, msg: str) -> None:
        self.label.clear_frame()
        self.label.setText(f"frame error: {msg}")
        self._frame_times.clear()
        self.status_bar.set_fps(None)
        self.status_bar.set_sharpness(None)

    def _update_fps(self) -> None:
        now = time.perf_counter()
        self._frame_times.append(now)
        if now - self._fps_last_update < 0.25:
            return
        self._fps_last_update = now
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            fps = (len(self._frame_times) - 1) / span if span > 0 else None
            self.status_bar.set_fps(fps)

    def closeEvent(self, event) -> None:
        # Close the webcam first so its geometry gets persisted via the
        # `closed` signal before we tear down the main window.
        if self._webcam_window is not None:
            self._webcam_window.close()
        self._stop_workers()
        if self.camera_options_panel is not None:
            self.camera_options_panel.shutdown()
        for ap in self.axis_panels:
            ap.shutdown()
        if self.smc100_panel is not None:
            self.smc100_panel.shutdown()
        if self.yoko_panel is not None:
            self.yoko_panel.shutdown()
        if self.heater_panel is not None:
            self.heater_panel.shutdown()
        # PySpin shutdown: end stream, deinit, drop the Camera reference
        # before clearing the list and releasing the system instance.
        # ReleaseInstance asserts that all Camera handles are gone.
        try:
            self.cam.EndAcquisition()
        except Exception as e:
            log.debug("EndAcquisition: %s", e)
        try:
            self.cam.DeInit()
        except Exception as e:
            log.debug("DeInit: %s", e)
        del self.cam
        self._cam_list.Clear()
        self._spin_system.ReleaseInstance()
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

    # Disable vsync on the default GL surface — paintGL is fast (~5 ms)
    # and we want it to run at the camera rate (59 Hz), not be paced by
    # the display refresh. With vsync on, swapBuffers blocks ~16 ms per
    # paint, which jitters against the 16.8 ms proc-emit interval and
    # causes back-to-back emits to land in one event-loop turn (the
    # second drains an empty mailbox = noop).
    fmt = QSurfaceFormat()
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication([sys.argv[0]] + qt_args)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    # Single-instance lock. Acquired before CameraWindow construction
    # so a second launch never even starts spinning up PySpin / serial
    # / VISA — it sees the lock, tells the operator the GUI is already
    # running, and exits. QLockFile is held for the lifetime of the
    # process; assigning to a local var (not _, not transient) keeps
    # it from being GC'd before app.exec() returns.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(LOCK_PATH))
    instance_lock.setStaleLockTime(0)  # rely on Qt's PID liveness check
    if not instance_lock.tryLock(0):
        log.warning("another GUI instance is already running; lockfile=%s", LOCK_PATH)
        QMessageBox.warning(
            None,
            "Air Stacker GUI already running",
            (
                "An instance of Air Stacker GUI is already running.\n\n"
                f"Lock file: {LOCK_PATH}\n\n"
                "If you're certain no instance is running (e.g. the "
                "GUI crashed), delete that file and relaunch."
            ),
        )
        return 1
    log.info("acquired single-instance lock: %s", LOCK_PATH)

    # Show the launching dialog before CameraWindow construction so the
    # operator gets visible feedback during the slow Spinnaker / serial /
    # VISA setup. processEvents() forces an initial paint; subsequent
    # paints happen at each _checkpoint() inside CameraWindow.__init__.
    launch = LaunchingDialog()
    launch.show()
    app.processEvents()

    try:
        win = CameraWindow(launch_dialog=launch)
    except StartupAborted:
        log.info("startup aborted by operator")
        launch.close()
        return 0
    launch.close()

    win.resize(960, 720)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
