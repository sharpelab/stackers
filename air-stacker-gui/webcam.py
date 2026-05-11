"""USB webcam configuration + device / format selection helpers.

Separated from the window class so the config-parsing logic and the
QCameraDevice / QCameraFormat picking is unit-testable without a Qt
event loop spinning up. The window itself lives in
[`webcam_window.py`](webcam_window.py).

See [`WEBCAM_PIP_PROPOSAL.md`](WEBCAM_PIP_PROPOSAL.md) for design rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtMultimedia import (
    QCameraDevice,
    QCameraFormat,
    QMediaDevices,
    QVideoFrameFormat,
)

log = logging.getLogger("airstacker.webcam")


# Map config-friendly pixel-format names to Qt's QVideoFrameFormat enum.
# NV12 is the right default on Windows MF for this camera (probed 2026-05-10).
PIXEL_FORMAT_MAP: dict[str, QVideoFrameFormat.PixelFormat] = {
    "NV12":  QVideoFrameFormat.PixelFormat.Format_NV12,
    "MJPEG": QVideoFrameFormat.PixelFormat.Format_Jpeg,
    "YUYV":  QVideoFrameFormat.PixelFormat.Format_YUYV,
    "YUV420P": QVideoFrameFormat.PixelFormat.Format_YUV420P,
    "RGBA":  QVideoFrameFormat.PixelFormat.Format_RGBA8888,
}


@dataclass(frozen=True)
class WebcamConfig:
    """Parsed `[webcam]` section of config.toml.

    Use `from_toml(section)` to build one with defaults filled in.
    """

    enabled: bool
    device_description: str
    device_id: str | None  # exact-match override; takes precedence if set
    width: int
    height: int
    target_fps: float
    pixel_format: str
    default_visible: bool
    backend: str  # "qt" | "cv2"; only "qt" is wired in v0
    window_geometry: tuple[int, int, int, int] | None  # (x, y, w, h)

    @classmethod
    def from_toml(cls, section) -> WebcamConfig:
        """Build a WebcamConfig from a tomlkit section (or any mapping).

        Missing keys fall back to sensible defaults for the Anker C200.
        """
        if section is None:
            section = {}
        geom = section.get("window_geometry")
        geom_tuple: tuple[int, int, int, int] | None = None
        if geom is not None:
            try:
                geom_tuple = (
                    int(geom["x"]), int(geom["y"]),
                    int(geom["width"]), int(geom["height"]),
                )
            except (KeyError, TypeError, ValueError) as e:
                log.warning("webcam.window_geometry malformed: %s", e)
        return cls(
            enabled=bool(section.get("enabled", True)),
            device_description=str(section.get("device_description", "Anker PowerConf C200")),
            device_id=str(section["device_id"]) if "device_id" in section else None,
            width=int(section.get("width", 1280)),
            height=int(section.get("height", 720)),
            target_fps=float(section.get("target_fps", 30.0)),
            pixel_format=str(section.get("pixel_format", "NV12")).upper(),
            default_visible=bool(section.get("default_visible", False)),
            backend=str(section.get("backend", "qt")),
            window_geometry=geom_tuple,
        )


def pick_camera_device(cfg: WebcamConfig) -> QCameraDevice | None:
    """Return the QCameraDevice matching `cfg`, or None.

    Match priority: exact `device_id` if set, else first device whose
    description contains `device_description` (case-insensitive substring).
    """
    devices = QMediaDevices.videoInputs()
    if cfg.device_id:
        wanted = cfg.device_id.encode("utf-8", errors="replace")
        for d in devices:
            if d.id().data() == wanted:
                return d
        log.warning("webcam: no device matching id=%r (have %d device(s))",
                    cfg.device_id, len(devices))
        return None
    needle = cfg.device_description.lower()
    for d in devices:
        if needle in d.description().lower():
            return d
    log.warning(
        "webcam: no device matching description=%r (have %d device(s): %s)",
        cfg.device_description, len(devices),
        ", ".join(d.description() for d in devices),
    )
    return None


def pick_camera_format(
    device: QCameraDevice,
    width: int,
    height: int,
    pixel_format: str,
    target_fps: float,
) -> QCameraFormat | None:
    """Return the best-matching QCameraFormat from the device's supported list.

    Priority:
      1. Exact resolution + pixel format + fps available → take it.
      2. Exact resolution + pixel format, closest fps.
      3. Exact resolution, any pixel format (prefer NV12 > MJPEG > YUYV).
      4. None — caller logs and disables the toggle.
    """
    pf_enum = PIXEL_FORMAT_MAP.get(pixel_format)
    if pf_enum is None:
        log.warning("webcam: unknown pixel_format=%r — falling back to NV12", pixel_format)
        pf_enum = QVideoFrameFormat.PixelFormat.Format_NV12

    formats = device.videoFormats()

    def resolution_matches(f: QCameraFormat) -> bool:
        r = f.resolution()
        return r.width() == width and r.height() == height

    def fps_match_score(f: QCameraFormat) -> float:
        # Smaller is better. 0 = target lies within [min, max].
        if f.minFrameRate() <= target_fps <= f.maxFrameRate():
            return 0.0
        return min(abs(target_fps - f.minFrameRate()), abs(target_fps - f.maxFrameRate()))

    # Tier 1+2: exact resolution + exact pixel format.
    exact_pf = [f for f in formats if resolution_matches(f) and f.pixelFormat() == pf_enum]
    if exact_pf:
        return min(exact_pf, key=fps_match_score)

    # Tier 3: exact resolution, any pixel format. Prefer NV12 > MJPEG > YUYV.
    pf_preference = [
        QVideoFrameFormat.PixelFormat.Format_NV12,
        QVideoFrameFormat.PixelFormat.Format_Jpeg,
        QVideoFrameFormat.PixelFormat.Format_YUYV,
        QVideoFrameFormat.PixelFormat.Format_YUV420P,
    ]
    for fallback_pf in pf_preference:
        candidates = [f for f in formats if resolution_matches(f) and f.pixelFormat() == fallback_pf]
        if candidates:
            chosen = min(candidates, key=fps_match_score)
            log.info(
                "webcam: format %s not at %dx%d; falling back to %s",
                pixel_format, width, height, str(chosen.pixelFormat()).split(".")[-1],
            )
            return chosen

    log.warning(
        "webcam: no format matching %dx%d (have %d formats); webcam disabled",
        width, height, len(formats),
    )
    return None
