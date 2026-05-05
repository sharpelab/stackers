"""Live frame viewer for the Air Stacker camera (Flea3 via harvesters/GenTL)."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
from harvesters.core import Harvester
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

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


class CameraWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Air Stacker — live")
        self.label = QLabel("connecting…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMinimumSize(640, 480)

        settings_panel = self._build_settings_panel()

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(settings_panel)
        layout.addWidget(self.label, stretch=1)
        self.setCentralWidget(central)

        config = load_config()
        cti = resolve_cti(config["gentl"]["producer"])
        device_index = int(config.get("camera", {}).get("device_index", 0))

        self.harvester = Harvester()
        self.harvester.add_file(cti)
        self.harvester.update()
        if not self.harvester.device_info_list:
            raise RuntimeError("no cameras enumerated by GenTL producer")
        self.acquirer = self.harvester.create(device_index)
        self.acquirer.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        presets = QGroupBox("Presets")
        presets_layout = QVBoxLayout(presets)
        presets_layout.addWidget(QLabel("TODO"))

        options = QGroupBox("Options")
        options_layout = QVBoxLayout(options)
        options_layout.addWidget(QLabel("TODO"))

        layout.addWidget(presets)
        layout.addWidget(options, stretch=1)
        return panel

    def tick(self) -> None:
        try:
            with self.acquirer.fetch(timeout=1.0) as buffer:
                comp = buffer.payload.components[0]
                rgb = to_rgb(comp.data, comp.width, comp.height, comp.data_format)
                rgb = np.ascontiguousarray(rgb)
                h, w, _ = rgb.shape
                qimg = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
                pix = QPixmap.fromImage(qimg).scaled(
                    self.label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.label.setPixmap(pix)
        except Exception as e:
            self.label.setText(f"frame error: {e}")

    def closeEvent(self, event) -> None:
        try:
            self.acquirer.stop()
            self.acquirer.destroy()
        finally:
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
