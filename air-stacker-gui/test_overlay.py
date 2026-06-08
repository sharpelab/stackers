"""Headless checks for overlay coordinate mapping + primitives.

Run: uv run python test_overlay.py   (plain asserts, no pytest dep)

QPointF / QRectF / QPainter are QtCore/QtGui value types — constructing
them needs no QApplication or display, so this runs without the GUI or the
camera. We never build a QPainter here, only exercise the pure geometry
and the primitive data model.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF

from overlay import FreehandStroke, LineSegment, normalize_pos, to_widget


def _approx(a: float, b: float, eps: float = 1e-9) -> bool:
    return abs(a - b) <= eps


def _pt_approx(p: QPointF, x: float, y: float) -> bool:
    return _approx(p.x(), x) and _approx(p.y(), y)


def test_roundtrip_widget_to_normalized_to_widget() -> None:
    rect = QRectF(10, 20, 200, 100)  # offset, non-square
    for px, py in [(10, 20), (110, 70), (210, 120), (60, 45)]:
        n = normalize_pos(QPointF(px, py), rect)
        assert n is not None
        w = to_widget(n, rect)
        assert _pt_approx(w, px, py)


def test_corners_map_to_unit_square() -> None:
    rect = QRectF(0, 0, 100, 50)
    tl = normalize_pos(QPointF(0, 0), rect)
    br = normalize_pos(QPointF(100, 50), rect)
    assert tl is not None and br is not None
    assert _pt_approx(tl, 0.0, 0.0)
    assert _pt_approx(br, 1.0, 1.0)


def test_outside_rect_is_none() -> None:
    rect = QRectF(10, 10, 100, 100)
    assert normalize_pos(QPointF(5, 50), rect) is None  # left of rect
    assert normalize_pos(QPointF(200, 50), rect) is None  # right
    assert normalize_pos(QPointF(50, 5), rect) is None  # above
    assert normalize_pos(QPointF(50, 200), rect) is None  # below


def test_degenerate_rect_is_none() -> None:
    assert normalize_pos(QPointF(0, 0), QRectF(0, 0, 0, 0)) is None


def test_resize_invariance() -> None:
    # The same normalized point lands at the geometrically-correct widget
    # pixel under two different rects — i.e. the overlay tracks the imaged
    # feature across a window resize / binning swap, not the widget pixels.
    n = QPointF(0.25, 0.5)
    small = QRectF(0, 0, 200, 100)
    large = QRectF(40, 30, 800, 400)
    ws = to_widget(n, small)
    wl = to_widget(n, large)
    assert _pt_approx(ws, 50, 50)
    assert _pt_approx(wl, 40 + 0.25 * 800, 30 + 0.5 * 400)
    # ...and mapping each widget pixel back recovers the normalized point.
    back_s = normalize_pos(ws, small)
    back_l = normalize_pos(wl, large)
    assert back_s is not None and back_l is not None
    assert _pt_approx(back_s, 0.25, 0.5)
    assert _pt_approx(back_l, 0.25, 0.5)


def test_line_preview_then_paint_under_different_rect() -> None:
    # Rubber-band: start is fixed at the press, end tracks the cursor.
    start = QPointF(0.1, 0.1)
    seg = LineSegment(start, QPointF(start))
    seg.end = QPointF(0.8, 0.4)  # cursor moved
    assert _pt_approx(seg.start, 0.1, 0.1)  # start untouched by preview

    # Because endpoints are normalized, painting under a rect different
    # from any drag-time rect still places them correctly.
    r1 = QRectF(0, 0, 100, 100)
    r2 = QRectF(0, 0, 1000, 1000)
    assert _pt_approx(to_widget(seg.start, r1), 10, 10)
    assert _pt_approx(to_widget(seg.end, r1), 80, 40)
    assert _pt_approx(to_widget(seg.start, r2), 100, 100)
    assert _pt_approx(to_widget(seg.end, r2), 800, 400)


def test_zero_length_line_detectable() -> None:
    # A click with no drag leaves start == end; the release handler uses
    # this to discard the degenerate segment.
    start = QPointF(0.3, 0.3)
    seg = LineSegment(start, QPointF(start))
    assert seg.start == seg.end


def test_freehand_append() -> None:
    s = FreehandStroke([QPointF(0.0, 0.0)])
    s.points.append(QPointF(0.5, 0.5))
    assert len(s.points) == 2
    assert _pt_approx(s.points[-1], 0.5, 0.5)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")
