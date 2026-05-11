"""Bottom status bar for the camera viewer.

Lives directly under the GL display in the middle pane. Holds a row of
left-anchored slots (FPS, sharpness, …) separated by sunken VLine
dividers, plus right-anchored slots for toggles (webcam, …). Each side
grows independently via `add_slot` / `add_right_slot`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class StatusBar(QFrame):
    """Thin horizontal strip: left-anchored slots, trailing stretch, right-anchored slots."""

    webcam_toggled = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(8)
        # Sentinel stretch separates left-anchored slots from right-anchored ones.
        # `_stretch_index` tracks its current position; `add_slot` inserts before
        # it, `add_right_slot` inserts after.
        self._layout.addStretch(1)
        self._stretch_index = 0

        self.fps_label = QLabel("-- fps")
        self.sharpness_label = QLabel("-- sharp")
        self.add_slot(self.fps_label)
        self.add_slot(self.sharpness_label)

        self.webcam_button = QPushButton("▣ Webcam")
        self.webcam_button.setCheckable(True)
        self.webcam_button.setFlat(True)
        self.webcam_button.toggled.connect(self.webcam_toggled.emit)
        self.add_right_slot(self.webcam_button)

    def add_slot(self, widget: QLabel) -> None:
        """Insert a widget as a new left-anchored slot.

        Each slot is followed by a sunken VLine divider, so successive
        slots line up with the same visual cadence regardless of which
        order they're added.
        """
        idx = self._stretch_index
        if idx > 0:
            divider = QFrame()
            divider.setFrameShape(QFrame.Shape.VLine)
            divider.setFrameShadow(QFrame.Shadow.Sunken)
            self._layout.insertWidget(idx, divider)
            self._stretch_index += 1
            idx += 1
        widget.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._layout.insertWidget(idx, widget)
        self._stretch_index += 1

    def add_right_slot(self, widget) -> None:
        """Insert a widget as a new right-anchored slot (after the stretch).

        Right-anchored slots get a leading VLine divider so the column of
        toggles reads as visually distinct from the left-anchored status
        labels.
        """
        # Always insert at the end of the layout — order of right-anchored
        # slots reflects insertion order, growing leftward toward the stretch.
        if self._layout.count() > self._stretch_index + 1:
            divider = QFrame()
            divider.setFrameShape(QFrame.Shape.VLine)
            divider.setFrameShadow(QFrame.Shadow.Sunken)
            self._layout.addWidget(divider)
        self._layout.addWidget(widget)

    def set_fps(self, fps: float | None) -> None:
        self.fps_label.setText("-- fps" if fps is None else f"{fps:.1f} fps")

    def set_sharpness(self, value: float | None) -> None:
        self.sharpness_label.setText(
            "-- sharp" if value is None else f"{value:.0f} sharp"
        )

    def set_webcam_checked(self, checked: bool) -> None:
        """Update the toggle's checked state without re-firing webcam_toggled.

        Used when the WebcamWindow is closed via its own X button — we want
        the toggle to reflect the closed state without bouncing the signal
        back to CameraWindow.
        """
        self.webcam_button.blockSignals(True)
        self.webcam_button.setChecked(checked)
        self.webcam_button.blockSignals(False)
