"""Bottom status bar for the camera viewer.

Lives directly under the GL display in the middle pane. Holds a row of
left-anchored slots (FPS, sharpness, …) separated by sunken VLine
dividers, plus right-anchored slots for toggles (webcam, …). Each side
grows independently via `add_slot` / `add_right_slot`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from overlay import DrawTool


class StatusBar(QFrame):
    """Thin horizontal strip: left-anchored slots, trailing stretch, right-anchored slots."""

    webcam_toggled = Signal(bool)
    layers_toggled = Signal(bool)
    pencil_toggled = Signal(bool)
    tool_changed = Signal(object)  # emits a DrawTool
    move_toggled = Signal(bool)
    clear_drawing_clicked = Signal()

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

        # Toggle the docked Layers panel's visibility. Default unchecked
        # (panel hidden); the owner syncs the panel's initial visibility from
        # this button's state.
        self.layers_button = QPushButton("▦ Layers")
        self.layers_button.setCheckable(True)
        self.layers_button.setFlat(True)
        self.layers_button.toggled.connect(self.layers_toggled.emit)
        self.add_right_slot(self.layers_button)

        self.pencil_button = QPushButton("✎ Draw")
        self.pencil_button.setCheckable(True)
        self.pencil_button.setFlat(True)
        self.pencil_button.toggled.connect(self._on_pencil_toggled)
        self.add_right_slot(self.pencil_button)

        # Tool selector: two mutually-exclusive checkable buttons in one
        # container, so they read as a group (no divider between them) and
        # add as a single right-anchored slot. Gated on draw mode — moot
        # until the operator turns drawing on.
        self.tool_group_widget = QWidget()
        tool_layout = QHBoxLayout(self.tool_group_widget)
        tool_layout.setContentsMargins(0, 0, 0, 0)
        tool_layout.setSpacing(4)
        self.freehand_button = QPushButton("✎ Freehand")
        self.freehand_button.setCheckable(True)
        self.freehand_button.setFlat(True)
        self.freehand_button.setChecked(True)
        self.line_button = QPushButton("／ Line")
        self.line_button.setCheckable(True)
        self.line_button.setFlat(True)
        tool_layout.addWidget(self.freehand_button)
        tool_layout.addWidget(self.line_button)
        self.tool_button_group = QButtonGroup(self)
        self.tool_button_group.setExclusive(True)
        self.tool_button_group.addButton(self.freehand_button)
        self.tool_button_group.addButton(self.line_button)
        self._tool_for_button = {
            self.freehand_button: DrawTool.FREEHAND,
            self.line_button: DrawTool.LINE,
        }
        self.tool_button_group.buttonToggled.connect(self._on_tool_button_toggled)
        self.tool_group_widget.setEnabled(False)
        self.pencil_button.toggled.connect(self.tool_group_widget.setEnabled)
        self.add_right_slot(self.tool_group_widget)

        # Move tool: a distinct mode (drag the active layer), mutually
        # exclusive with Draw — see _on_pencil_toggled / _on_move_toggled.
        self.move_button = QPushButton("✥ Move")
        self.move_button.setCheckable(True)
        self.move_button.setFlat(True)
        self.move_button.toggled.connect(self._on_move_toggled)
        self.add_right_slot(self.move_button)

        self.clear_drawing_button = QPushButton("🗑 Clear")
        self.clear_drawing_button.setFlat(True)
        self.clear_drawing_button.clicked.connect(self.clear_drawing_clicked.emit)
        self.add_right_slot(self.clear_drawing_button)

    def _on_pencil_toggled(self, checked: bool) -> None:
        # Draw and Move are mutually exclusive modes.
        if checked and self.move_button.isChecked():
            self.move_button.setChecked(False)
        self.pencil_toggled.emit(checked)

    def _on_move_toggled(self, checked: bool) -> None:
        if checked and self.pencil_button.isChecked():
            self.pencil_button.setChecked(False)
        self.move_toggled.emit(checked)

    def _on_tool_button_toggled(self, button, checked: bool) -> None:
        # Exclusive group fires twice per switch (one off, one on); only
        # act on the newly-checked button.
        if checked:
            self.tool_changed.emit(self._tool_for_button[button])

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
