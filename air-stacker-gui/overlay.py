"""Overlay primitives + coordinate mapping for the live camera view.

Kept dependency-light (PySide6 only — no PySpin/cv2/numpy) so the pure
geometry can be unit-tested headless and so the data model can grow into
layers (Phase 2 of the alignment-overlay workstream) without dragging the
camera pipeline along. See ALIGNMENT_OVERLAY_WORKSTREAM.md.

Coordinate model: every primitive stores points in ASPECT-CORRECT NORMALIZED
image-space — y ∈ [0, 1] and x ∈ [0, IMAGE_ASPECT] (4/3 for the 1440×1080
Flea3). `_CameraGLWindow` caches the letterboxed camera-content rect each
paintGL; mapping to/from widget pixels goes through `normalize_pos` /
`to_widget` against that rect, so overlays track the imaged feature across
window resize and binning swaps rather than the widget's pixels.

The x-extent is the image aspect (not 1.0) on purpose: against the 4:3
target rect it makes one x-unit equal one y-unit *in screen pixels*, so a
layer rotation/scale about the image center (Phase 2) stays geometrically
rigid instead of shearing. A different camera aspect is an explicit code
change — edit IMAGE_ASPECT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

# Source pixel aspect (width / height). Flea3 = 1440×1080 = 4:3.
IMAGE_ASPECT = 4 / 3
# Fixed pivot for every layer's rotate/scale — the image center, in
# aspect-correct coords (x ∈ [0, IMAGE_ASPECT], y ∈ [0, 1]).
IMAGE_CENTER = QPointF(IMAGE_ASPECT / 2.0, 0.5)


class DrawTool(Enum):
    """Active overlay tool. The tool set grows in later phases (move,
    select); keep it an enum so the status-bar selector stays typed."""

    FREEHAND = auto()
    LINE = auto()


def normalize_pos(pos: QPointF, rect: QRectF) -> QPointF | None:
    """Map widget coords → (nx, ny) in aspect-correct image space
    (x ∈ [0, IMAGE_ASPECT], y ∈ [0, 1]).

    Returns None when `pos` falls in the letterbox bars (outside `rect`)
    or when `rect` is degenerate.
    """
    if rect.width() <= 0 or rect.height() <= 0:
        return None
    if not rect.contains(pos):
        return None
    nx = (pos.x() - rect.x()) / rect.width() * IMAGE_ASPECT
    ny = (pos.y() - rect.y()) / rect.height()
    return QPointF(nx, ny)


def to_widget(n: QPointF, rect: QRectF) -> QPointF:
    """Map (nx, ny) in aspect-correct image space → widget coords within `rect`."""
    return QPointF(
        rect.x() + n.x() / IMAGE_ASPECT * rect.width(),
        rect.y() + n.y() * rect.height(),
    )


class OverlayPrimitive:
    """Base for normalized-coord overlay primitives.

    Subclasses store their geometry in aspect-correct image coords and draw
    in those *raw* coords — the painter carries a transform mapping
    (aspect-correct → widget px), optionally composed with the owning
    layer's transform, so primitives stay transform-agnostic. Keeping the
    data as a discriminated set of typed primitives (rather than a flat
    point-list) is what lets layers group them and add hit-testing.
    """

    def draw(self, painter: QPainter) -> None:
        raise NotImplementedError


@dataclass
class FreehandStroke(OverlayPrimitive):
    """A polyline of sampled points — the original pencil tool."""

    points: list[QPointF] = field(default_factory=list)

    def draw(self, painter: QPainter) -> None:
        if not self.points:
            return
        if len(self.points) == 1:
            # A click with no drag renders as a dot — a deliberate mark.
            painter.drawPoint(self.points[0])
            return
        painter.drawPolyline(self.points)


@dataclass
class LineSegment(OverlayPrimitive):
    """A straight segment defined by two endpoints (rubber-banded)."""

    start: QPointF
    end: QPointF

    def draw(self, painter: QPainter) -> None:
        painter.drawLine(self.start, self.end)


def layer_transform(layer: OverlayLayer) -> QTransform:
    """Map a layer's local coords → world (aspect-correct) coords:
    translate by the layer offset, then rotate + scale about the fixed
    image center. Composed with the (aspect-correct → px) map at paint."""
    cx, cy = IMAGE_CENTER.x(), IMAGE_CENTER.y()
    t = QTransform()
    t.translate(layer.offset.x(), layer.offset.y())
    t.translate(cx, cy)
    t.rotate(layer.rotation_deg)
    t.scale(layer.scale, layer.scale)
    t.translate(-cx, -cy)
    return t


def aspect_to_px(rect: QRectF) -> QTransform:
    """Map aspect-correct coords (x ∈ [0, IMAGE_ASPECT], y ∈ [0, 1]) → widget
    px within `rect` — the affine form of `to_widget`."""
    t = QTransform()
    t.translate(rect.x(), rect.y())
    t.scale(rect.width() / IMAGE_ASPECT, rect.height())
    return t


@dataclass
class OverlayLayer:
    """A named layer drawn as a unit under one transform.

    `visible` / `opacity` apply per-layer. `color` is the stroke color for
    the layer's primitives (image layers ignore it). `offset` /
    `rotation_deg` / `scale` define the layer's transform about the fixed
    image center.

    Invariant — a layer is EITHER a drawing layer OR an image layer:
      - drawing layer: `image is None`, holds `primitives`;
      - image layer:   `image` set, `primitives` stays empty (drawing on it
        is blocked) — an imported reference to trace over.
    `image is None` is the discriminator. (A formal tagged union is deferred
    until persistence needs explicit kinds; see the workstream doc.) New
    drawing lands on the active layer.
    """

    name: str
    primitives: list[OverlayPrimitive] = field(default_factory=list)
    visible: bool = True
    opacity: float = 1.0
    color: QColor = field(default_factory=lambda: QColor(255, 0, 0))
    offset: QPointF = field(default_factory=lambda: QPointF(0.0, 0.0))
    rotation_deg: float = 0.0
    scale: float = 1.0
    image: QImage | None = None


def image_fit_rect(img_w: int, img_h: int) -> QRectF:
    """Aspect-correct rect that fit-contains an `img_w`×`img_h` image within
    the full frame [0, IMAGE_ASPECT] × [0, 1], centered, preserving the
    image's aspect ratio. The layer transform adjusts it from there."""
    if img_w <= 0 or img_h <= 0:
        return QRectF(0.0, 0.0, IMAGE_ASPECT, 1.0)
    ai = img_w / img_h
    if ai >= IMAGE_ASPECT:  # wider than frame → limited by width
        w, h = IMAGE_ASPECT, IMAGE_ASPECT / ai
    else:  # taller than frame → limited by height
        w, h = ai, 1.0
    return QRectF((IMAGE_ASPECT - w) / 2.0, (1.0 - h) / 2.0, w, h)
