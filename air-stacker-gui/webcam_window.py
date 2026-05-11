"""Detached webcam viewer window (PySide6.QtMultimedia path).

Top-level QWidget owning a QCamera + QMediaCaptureSession + QVideoWidget.
Spawns capture on construction, stops on close. Geometry is persisted to
config.toml so the window comes back where the operator left it.

The toggle that opens/closes this window lives on the main GUI's
StatusBar — see [`status_bar.py`](status_bar.py) and `CameraWindow` in
[`main.py`](main.py).

See [`WEBCAM_PIP_PROPOSAL.md`](WEBCAM_PIP_PROPOSAL.md) for design rationale.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtMultimedia import (
    QCamera,
    QMediaCaptureSession,
)
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from webcam import WebcamConfig, pick_camera_device, pick_camera_format

log = logging.getLogger("airstacker.webcam")


class WebcamWindow(QWidget):
    """Top-level window that displays a single USB webcam feed.

    Lifecycle:
      __init__   → pick device + format, build QCamera/Session/Widget,
                   start camera, restore geometry.
      closeEvent → save geometry, stop camera, emit `closed` signal so
                   the StatusBar toggle updates its checked state.

    The `closed` signal lets the main window keep its toggle button in
    sync when the operator dismisses the window via its X button rather
    than via the StatusBar toggle.
    """

    closed = Signal()

    def __init__(self, cfg: WebcamConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Top-level window even with a parent — Qt.WindowType.Window promotes
        # this widget to its own native window. Parent is for lifetime
        # ownership only.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowTitle("Air Stacker — webcam")
        self._cfg = cfg

        self._status = QLabel("starting…")
        self._status.setStyleSheet("color: rgb(180, 180, 180);")

        self._video = QVideoWidget()
        self._video.setMinimumSize(640, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._video, stretch=1)
        layout.addWidget(self._status)

        self._camera: QCamera | None = None
        self._session: QMediaCaptureSession | None = None
        self._error_count = 0

        self._start_camera()
        self._restore_geometry()

    def _start_camera(self) -> None:
        device = pick_camera_device(self._cfg)
        if device is None:
            self._status.setText(
                f"no camera matching {self._cfg.device_description!r}"
            )
            return
        fmt = pick_camera_format(
            device,
            self._cfg.width,
            self._cfg.height,
            self._cfg.pixel_format,
            self._cfg.target_fps,
        )
        if fmt is None:
            self._status.setText(
                f"no format matching {self._cfg.width}x{self._cfg.height}"
            )
            return

        self._camera = QCamera(device)
        self._camera.setCameraFormat(fmt)
        self._camera.errorOccurred.connect(self._on_camera_error)

        self._session = QMediaCaptureSession()
        self._session.setCamera(self._camera)
        self._session.setVideoOutput(self._video)

        res = fmt.resolution()
        pf_name = str(fmt.pixelFormat()).split(".")[-1]
        self._status.setText(
            f"{device.description()} — {res.width()}×{res.height()} "
            f"{pf_name} @ {fmt.maxFrameRate():.0f} fps"
        )
        log.info(
            "webcam start: %s %dx%d %s @ %.1ffps",
            device.description(), res.width(), res.height(),
            pf_name, fmt.maxFrameRate(),
        )
        self._camera.start()

    def _on_camera_error(self, error: QCamera.Error, message: str) -> None:
        if error == QCamera.Error.NoError:
            return
        self._error_count += 1
        log.warning("webcam error #%d: %s (%s)", self._error_count, message, error)
        self._status.setText(f"error: {message}")
        # Five strikes and we self-close. Per proposal §6 — no retry loop.
        if self._error_count >= 5:
            log.warning("webcam: 5 consecutive errors, auto-closing")
            self.close()

    def _restore_geometry(self) -> None:
        geom = self._cfg.window_geometry
        if geom is None:
            self.resize(960, 540)
            return
        x, y, w, h = geom
        self.setGeometry(x, y, w, h)

    def saved_geometry(self) -> tuple[int, int, int, int]:
        """Return current (x, y, width, height) for persistence."""
        g = self.geometry()
        return (g.x(), g.y(), g.width(), g.height())

    def closeEvent(self, event) -> None:
        if self._camera is not None:
            self._camera.stop()
        # Tear down the session so subsequent opens get a fresh pipeline.
        # Holding a stale QMediaCaptureSession across reopens has produced
        # MF-side stalls in past debugging sessions on other projects.
        self._session = None
        self._camera = None
        self.closed.emit()
        super().closeEvent(event)
