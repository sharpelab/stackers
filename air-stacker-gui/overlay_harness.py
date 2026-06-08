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

from PySide6.QtWidgets import (  # noqa: E402  (after the PySpin stub)
    QApplication,
    QVBoxLayout,
    QWidget,
)

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
    lay = QVBoxLayout(win)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    disp = CameraDisplay()
    bar = StatusBar()
    lay.addWidget(disp, 1)
    lay.addWidget(bar)

    # Same wiring as CameraWindow.
    bar.pencil_toggled.connect(disp.set_drawing_enabled)
    bar.tool_changed.connect(disp.set_tool)
    bar.clear_drawing_clicked.connect(disp.clear_strokes)

    win.resize(1200, 800)
    win.show()

    # The GL window keeps its own reference to the frame buffer, so a
    # single push is enough — no need to retain `frame` ourselves.
    disp.set_frame(make_frame())

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
