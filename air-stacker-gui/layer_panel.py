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
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from overlay import OverlayLayer


class LayerPanel(QWidget):
    add_layer_requested = Signal()
    import_image_requested = Signal()
    remove_layer_requested = Signal(int)
    select_layer_requested = Signal(int)
    visibility_toggled = Signal(int, bool)
    opacity_changed = Signal(int, float)
    color_changed = Signal(int, QColor)  # (index, stroke color)
    rotation_changed = Signal(int, float)  # (index, degrees)
    scale_changed = Signal(int, float)  # (index, scale factor)
    offset_changed = Signal(int, float, float)  # (index, x, y)
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

        self._import_button = QPushButton("⬇ Import Image…")
        self._import_button.clicked.connect(self.import_image_requested.emit)
        root.addWidget(self._import_button)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(divider)

        settings_title = QLabel("Layer Settings")
        settings_title.setStyleSheet("font-weight: bold;")
        root.addWidget(settings_title)

        # Opacity: slider only (a 0–100% drag reads fine without a number).
        self._opacity_label = QLabel("Opacity")
        root.addWidget(self._opacity_label)
        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setRange(0, 100)
        self._opacity.valueChanged.connect(self._on_opacity)
        root.addWidget(self._opacity)

        # Stroke color: a swatch button showing the layer's color; click
        # opens a color dialog. Disabled for image layers (no strokes).
        root.addWidget(QLabel("Color"))
        self._color = QColor(255, 0, 0)
        self._color_button = QPushButton()
        self._color_button.setFixedHeight(22)
        self._color_button.clicked.connect(self._on_color_clicked)
        root.addWidget(self._color_button)

        # Rotation + scale: each a slider (drag) paired with a spinbox
        # (type), kept in sync. Slider values are integers, so scale's
        # slider works in hundredths (5..500 ↔ 0.05..5.00).
        root.addWidget(QLabel("Rotation"))
        self._rotation_slider = QSlider(Qt.Orientation.Horizontal)
        self._rotation_slider.setRange(0, 360)
        self._rotation_spin = QSpinBox()
        self._rotation_spin.setRange(0, 360)
        self._rotation_spin.setSuffix("°")
        self._rotation_slider.valueChanged.connect(self._on_rotation_slider)
        self._rotation_spin.valueChanged.connect(self._on_rotation_spin)
        root.addLayout(self._pair_row(self._rotation_slider, self._rotation_spin))

        root.addWidget(QLabel("Scale"))
        self._scale_slider = QSlider(Qt.Orientation.Horizontal)
        self._scale_slider.setRange(5, 500)
        self._scale_spin = QDoubleSpinBox()
        self._scale_spin.setRange(0.05, 5.0)
        self._scale_spin.setSingleStep(0.05)
        self._scale_spin.setValue(1.0)
        self._scale_slider.valueChanged.connect(self._on_scale_slider)
        self._scale_spin.valueChanged.connect(self._on_scale_spin)
        root.addLayout(self._pair_row(self._scale_slider, self._scale_spin))

        # Offset X / Y: aspect-correct world units; the canvas Move tool
        # writes the same offset and live-syncs these via sync_active_values.
        # Sliders work in hundredths (-200..200 ↔ -2.00..2.00).
        root.addWidget(QLabel("Offset X"))
        self._offx_slider = QSlider(Qt.Orientation.Horizontal)
        self._offx_slider.setRange(-200, 200)
        self._offx_spin = QDoubleSpinBox()
        self._offx_spin.setRange(-2.0, 2.0)
        self._offx_spin.setSingleStep(0.01)
        self._offx_slider.valueChanged.connect(self._on_offx_slider)
        self._offx_spin.valueChanged.connect(self._on_offx_spin)
        root.addLayout(self._pair_row(self._offx_slider, self._offx_spin))

        root.addWidget(QLabel("Offset Y"))
        self._offy_slider = QSlider(Qt.Orientation.Horizontal)
        self._offy_slider.setRange(-200, 200)
        self._offy_spin = QDoubleSpinBox()
        self._offy_spin.setRange(-2.0, 2.0)
        self._offy_spin.setSingleStep(0.01)
        self._offy_slider.valueChanged.connect(self._on_offy_slider)
        self._offy_spin.valueChanged.connect(self._on_offy_spin)
        root.addLayout(self._pair_row(self._offy_slider, self._offy_spin))

        root.addStretch(1)

    @staticmethod
    def _pair_row(slider: QSlider, spin: QWidget) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(slider, stretch=1)
        h.addWidget(spin)
        return h

    def _on_opacity(self, value: int) -> None:
        self.opacity_changed.emit(self._active, value / 100.0)

    def _on_color_clicked(self) -> None:
        color = QColorDialog.getColor(self._color, self, "Layer stroke color")
        if color.isValid():
            # The swatch syncs back via overlay_mutated → sync_active_values.
            self.color_changed.emit(self._active, color)

    def _on_rotation_slider(self, value: int) -> None:
        self._rotation_spin.blockSignals(True)
        self._rotation_spin.setValue(value)
        self._rotation_spin.blockSignals(False)
        self.rotation_changed.emit(self._active, float(value))

    def _on_rotation_spin(self, value: int) -> None:
        self._rotation_slider.blockSignals(True)
        self._rotation_slider.setValue(value)
        self._rotation_slider.blockSignals(False)
        self.rotation_changed.emit(self._active, float(value))

    def _on_scale_slider(self, value: int) -> None:
        scale = value / 100.0
        self._scale_spin.blockSignals(True)
        self._scale_spin.setValue(scale)
        self._scale_spin.blockSignals(False)
        self.scale_changed.emit(self._active, scale)

    def _on_scale_spin(self, value: float) -> None:
        self._scale_slider.blockSignals(True)
        self._scale_slider.setValue(round(value * 100))
        self._scale_slider.blockSignals(False)
        self.scale_changed.emit(self._active, value)

    def _emit_offset(self) -> None:
        self.offset_changed.emit(
            self._active, self._offx_spin.value(), self._offy_spin.value()
        )

    def _on_offx_slider(self, value: int) -> None:
        self._offx_spin.blockSignals(True)
        self._offx_spin.setValue(value / 100.0)
        self._offx_spin.blockSignals(False)
        self._emit_offset()

    def _on_offx_spin(self, value: float) -> None:
        self._offx_slider.blockSignals(True)
        self._offx_slider.setValue(round(value * 100))
        self._offx_slider.blockSignals(False)
        self._emit_offset()

    def _on_offy_slider(self, value: int) -> None:
        self._offy_spin.blockSignals(True)
        self._offy_spin.setValue(value / 100.0)
        self._offy_spin.blockSignals(False)
        self._emit_offset()

    def _on_offy_spin(self, value: float) -> None:
        self._offy_slider.blockSignals(True)
        self._offy_slider.setValue(round(value * 100))
        self._offy_slider.blockSignals(False)
        self._emit_offset()

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

        self._sync_settings(layers[active])

    def sync_active_values(self, layers: list[OverlayLayer], active: int) -> None:
        """Refresh only the Layer Settings controls for the active layer —
        no row rebuild. Cheap enough to call on every overlay mutation (e.g.
        live-updating the offset fields while the layer is dragged)."""
        self._active = active
        self._sync_settings(layers[active])

    def _sync_settings(self, active_layer: OverlayLayer) -> None:
        """Load the active layer's values into the settings controls without
        re-firing their change signals."""
        self._opacity_label.setText(f"Opacity — {active_layer.name}")
        self._opacity.blockSignals(True)
        self._opacity.setValue(round(active_layer.opacity * 100))
        self._opacity.blockSignals(False)

        self._color = QColor(active_layer.color)
        is_image = active_layer.image is not None
        self._color_button.setEnabled(not is_image)
        self._color_button.setStyleSheet(
            f"background-color: {self._color.name()};"
        )
        self._color_button.setToolTip(
            "Image layers have no stroke color"
            if is_image
            else "Stroke color for this layer's drawing"
        )

        deg = round(active_layer.rotation_deg) % 360
        for w in (self._rotation_slider, self._rotation_spin):
            w.blockSignals(True)
            w.setValue(deg)
            w.blockSignals(False)

        self._scale_slider.blockSignals(True)
        self._scale_slider.setValue(round(active_layer.scale * 100))
        self._scale_slider.blockSignals(False)
        self._scale_spin.blockSignals(True)
        self._scale_spin.setValue(active_layer.scale)
        self._scale_spin.blockSignals(False)

        for slider, spin, val in (
            (self._offx_slider, self._offx_spin, active_layer.offset.x()),
            (self._offy_slider, self._offy_spin, active_layer.offset.y()),
        ):
            slider.blockSignals(True)
            slider.setValue(round(val * 100))
            slider.blockSignals(False)
            spin.blockSignals(True)
            spin.setValue(val)
            spin.blockSignals(False)

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
