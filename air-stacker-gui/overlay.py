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
from PySide6.QtGui import QPainter

# Source pixel aspect (width / height). Flea3 = 1440×1080 = 4:3.
IMAGE_ASPECT = 4 / 3


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

    Subclasses store their geometry in normalized image-space and render
    themselves against the current camera-content rect. Keeping the data
    as a discriminated set of typed primitives (rather than a flat
    point-list) is what lets Phase 2 group them into layers and add
    hit-testing without rewriting storage.
    """

    def draw(self, painter: QPainter, rect: QRectF) -> None:
        raise NotImplementedError


@dataclass
class FreehandStroke(OverlayPrimitive):
    """A polyline of sampled points — the original pencil tool."""

    points: list[QPointF] = field(default_factory=list)

    def draw(self, painter: QPainter, rect: QRectF) -> None:
        if not self.points:
            return
        if len(self.points) == 1:
            # A click with no drag renders as a dot — a deliberate mark.
            painter.drawPoint(to_widget(self.points[0], rect))
            return
        painter.drawPolyline([to_widget(p, rect) for p in self.points])


@dataclass
class LineSegment(OverlayPrimitive):
    """A straight segment defined by two endpoints (rubber-banded)."""

    start: QPointF
    end: QPointF

    def draw(self, painter: QPainter, rect: QRectF) -> None:
        painter.drawLine(to_widget(self.start, rect), to_widget(self.end, rect))


@dataclass
class OverlayLayer:
    """A named group of primitives drawn as a unit.

    `visible` / `opacity` apply per-layer. `offset` / `rotation_deg` /
    `scale` define the layer's transform about the fixed image center
    (Phase 2b applies it; in 2a they're carried but identity). New drawing
    lands on the active layer.
    """

    name: str
    primitives: list[OverlayPrimitive] = field(default_factory=list)
    visible: bool = True
    opacity: float = 1.0
    offset: QPointF = field(default_factory=lambda: QPointF(0.0, 0.0))
    rotation_deg: float = 0.0
    scale: float = 1.0
