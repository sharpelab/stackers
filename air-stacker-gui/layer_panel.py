"""Layer management panel for the overlay.

A view + signals over the GL window's layer list (the StatusBar pattern:
signals out, `set_layers` in). It owns no state — the owner wires the
signals to `CameraDisplay` layer ops and calls `set_layers` to refresh.

Rows are listed front-most at the top (reverse of the draw order, so the
visual stack matches what's painted on top). Each row carries a visibility
checkbox, a name button (click = make active), reorder ▲/▼, and a remove ✕.
Below the list: add-layer, and a single opacity slider for the active layer.

Signals carry *layer indices* (into the draw-order list), not row indices,
so the owner's handlers stay simple.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from overlay import OverlayLayer


class LayerPanel(QWidget):
    add_layer_requested = Signal()
    remove_layer_requested = Signal(int)
    select_layer_requested = Signal(int)
    visibility_toggled = Signal(int, bool)
    opacity_changed = Signal(int, float)
    move_layer_requested = Signal(int, int)  # (index, delta: +1 toward front)

    def __init__(self) -> None:
        super().__init__()
        self._active = 0
        self._layer_count = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        title = QLabel("Layers")
        title.setStyleSheet("font-weight: bold;")
        root.addWidget(title)

        # Container the rows are rebuilt into on every set_layers().
        self._rows = QWidget()
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        root.addWidget(self._rows)

        self._add_button = QPushButton("+ Add Layer")
        self._add_button.clicked.connect(self.add_layer_requested.emit)
        root.addWidget(self._add_button)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(divider)

        self._opacity_label = QLabel("Opacity")
        root.addWidget(self._opacity_label)
        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setRange(0, 100)
        self._opacity.valueChanged.connect(self._on_opacity)
        root.addWidget(self._opacity)

        root.addStretch(1)

    def _on_opacity(self, value: int) -> None:
        self.opacity_changed.emit(self._active, value / 100.0)

    def _clear_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def set_layers(self, layers: list[OverlayLayer], active: int) -> None:
        """Rebuild the row list from the current layer state."""
        self._active = active
        self._layer_count = len(layers)
        self._clear_rows()

        n = len(layers)
        name_group = QButtonGroup(self._rows)
        name_group.setExclusive(True)
        # Front-most (last drawn) at the top.
        for idx in range(n - 1, -1, -1):
            layer = layers[idx]
            self._rows_layout.addWidget(
                self._build_row(idx, layer, active, n, name_group)
            )

        # Opacity slider reflects the active layer (without re-firing).
        self._opacity_label.setText(f"Opacity — {layers[active].name}")
        self._opacity.blockSignals(True)
        self._opacity.setValue(round(layers[active].opacity * 100))
        self._opacity.blockSignals(False)

    def _build_row(
        self,
        idx: int,
        layer: OverlayLayer,
        active: int,
        n: int,
        name_group: QButtonGroup,
    ) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)

        vis = QCheckBox()
        vis.setChecked(layer.visible)
        vis.setToolTip("Visible")
        vis.toggled.connect(lambda checked, i=idx: self.visibility_toggled.emit(i, checked))
        h.addWidget(vis)

        name = QPushButton(layer.name)
        name.setCheckable(True)
        name.setChecked(idx == active)
        name.setFlat(True)
        name.setStyleSheet("text-align: left; font-weight: %s;" % ("bold" if idx == active else "normal"))
        name.clicked.connect(lambda _=False, i=idx: self.select_layer_requested.emit(i))
        name_group.addButton(name)
        h.addWidget(name, stretch=1)

        up = QPushButton("▲")
        up.setMaximumWidth(24)
        up.setEnabled(idx < n - 1)  # already front-most → can't go further
        up.setToolTip("Bring forward")
        up.clicked.connect(lambda _=False, i=idx: self.move_layer_requested.emit(i, 1))
        h.addWidget(up)

        down = QPushButton("▼")
        down.setMaximumWidth(24)
        down.setEnabled(idx > 0)  # already back-most
        down.setToolTip("Send backward")
        down.clicked.connect(lambda _=False, i=idx: self.move_layer_requested.emit(i, -1))
        h.addWidget(down)

        remove = QPushButton("✕")
        remove.setMaximumWidth(24)
        remove.setEnabled(n > 1)  # keep at least one layer
        remove.setToolTip("Remove layer")
        remove.clicked.connect(lambda _=False, i=idx: self.remove_layer_requested.emit(i))
        h.addWidget(remove)

        return row
