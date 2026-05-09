"""Bottom status bar for the camera viewer.

Lives directly under the GL display in the middle pane. Holds a row of
left-anchored slots (FPS, sharpness, …) separated by sunken VLine
dividers. Designed to grow: each slot is added through `add_slot`,
which inserts the label and a divider before the trailing stretch.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel


class StatusBar(QFrame):
    """Thin horizontal strip with extensible left-anchored slots."""

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)

        self.fps_label = QLabel("-- fps")
        self.sharpness_label = QLabel("-- sharp")
        self.add_slot(self.fps_label)
        self.add_slot(self.sharpness_label)

    def add_slot(self, widget: QLabel) -> None:
        """Insert a widget as a new slot before the trailing stretch.

        Each slot is followed by a sunken VLine divider, so successive
        slots line up with the same visual cadence regardless of which
        order they're added.
        """
        # Insert at the position of the trailing stretch (last item).
        idx = self._layout.count() - 1
        if idx > 0:
            divider = QFrame()
            divider.setFrameShape(QFrame.Shape.VLine)
            divider.setFrameShadow(QFrame.Shadow.Sunken)
            self._layout.insertWidget(idx, divider)
            idx += 1
        widget.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._layout.insertWidget(idx, widget)

    def set_fps(self, fps: float | None) -> None:
        self.fps_label.setText("-- fps" if fps is None else f"{fps:.1f} fps")

    def set_sharpness(self, value: float | None) -> None:
        self.sharpness_label.setText(
            "-- sharp" if value is None else f"{value:.0f} sharp"
        )
