"""Local dev harness for the alignment-overlay UI — no camera, no rig.

The full GUI needs the FLIR camera (and serial/VISA/modbus devices), so it
can't come up on a dev workstation. But the overlay path (_CameraGLWindow /
CameraDisplay) only ever *receives* numpy frames — it never calls the
camera SDK. main.py imports PySpin at module top purely for the
acquisition code, so we stub PySpin in sys.modules and then drive the REAL
overlay classes against a synthetic frame.

Lets you iterate locally on the alignment-overlay workstream — freehand +
line tools, the tool selector, clear — without the rig.

    uv run python overlay_harness.py      # from the air-stacker-gui dir
"""

from __future__ import annotations

import sys
import types

import numpy as np

# Stub the camera SDK — never invoked on the overlay path.
sys.modules.setdefault("PySpin", types.ModuleType("PySpin"))

from PySide6.QtGui import QImage  # noqa: E402  (after the PySpin stub)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from layer_panel import LayerPanel  # noqa: E402
from main import CameraDisplay  # noqa: E402
from status_bar import StatusBar  # noqa: E402


def make_frame(h: int = 900, w: int = 1200) -> np.ndarray:
    """A gradient + 100px grid so there's structure to align against."""
    yy, xx = np.mgrid[0:h, 0:w]
    r = (xx * 255 // w).astype(np.uint8)
    g = (yy * 255 // h).astype(np.uint8)
    b = np.full((h, w), 40, np.uint8)
    frame = np.dstack([r, g, b])
    grid = ((xx % 100 < 2) | (yy % 100 < 2))
    frame[grid] = 220
    return np.ascontiguousarray(frame, dtype=np.uint8)


def main() -> None:
    app = QApplication(sys.argv)

    win = QWidget()
    win.setWindowTitle("overlay harness — alignment_overlay_phase1 (no camera)")
    outer = QVBoxLayout(win)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    disp = CameraDisplay()
    bar = StatusBar()
    panel = LayerPanel()
    panel.setFixedWidth(240)

    # Camera view | layers panel on top; full-width status bar beneath, so
    # the bar's buttons don't move when the layers panel is toggled.
    top = QWidget()
    top_layout = QHBoxLayout(top)
    top_layout.setContentsMargins(0, 0, 0, 0)
    top_layout.setSpacing(0)
    top_layout.addWidget(disp, stretch=1)
    top_layout.addWidget(panel)
    outer.addWidget(top, stretch=1)
    outer.addWidget(bar)

    # Same wiring as CameraWindow.
    bar.pencil_toggled.connect(disp.set_drawing_enabled)
    bar.move_toggled.connect(disp.set_move_enabled)
    bar.tool_changed.connect(disp.set_tool)
    bar.clear_drawing_clicked.connect(disp.clear_strokes)
    bar.layers_toggled.connect(panel.setVisible)

    # Layer panel ⇄ display. Structural changes (add/remove/move/select)
    # rebuild the panel rows; visibility/opacity only mutate the layer.
    def refresh() -> None:
        panel.set_layers(disp.layers(), disp.active_layer_index())

    def on_add() -> None:
        disp.add_layer()
        refresh()

    def on_remove(i: int) -> None:
        disp.remove_layer(i)
        refresh()

    def on_select(i: int) -> None:
        disp.set_active_layer(i)
        refresh()

    def on_move(i: int, delta: int) -> None:
        disp.move_layer(i, delta)
        refresh()

    def on_import() -> None:
        path, _ = QFileDialog.getOpenFileName(
            win,
            "Import reference image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)",
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            return
        disp.add_image_layer(img, path.rsplit("/", 1)[-1])
        refresh()

    def on_overlay_mutated() -> None:
        # Live-sync the settings controls (e.g. offset fields during a
        # canvas Move drag) without rebuilding the row list.
        panel.sync_active_values(disp.layers(), disp.active_layer_index())

    panel.add_layer_requested.connect(on_add)
    panel.import_image_requested.connect(on_import)
    panel.remove_layer_requested.connect(on_remove)
    panel.select_layer_requested.connect(on_select)
    panel.move_layer_requested.connect(on_move)
    panel.visibility_toggled.connect(disp.set_layer_visible)
    panel.opacity_changed.connect(disp.set_layer_opacity)
    panel.rotation_changed.connect(disp.set_layer_rotation)
    panel.scale_changed.connect(disp.set_layer_scale)
    panel.offset_changed.connect(disp.set_layer_offset)
    disp.overlay_mutated.connect(on_overlay_mutated)
    refresh()

    win.resize(1300, 800)
    win.show()

    # The GL window keeps its own reference to the frame buffer, so a
    # single push is enough — no need to retain `frame` ourselves.
    disp.set_frame(make_frame())

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
