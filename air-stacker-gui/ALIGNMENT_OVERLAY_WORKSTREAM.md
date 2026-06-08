# Alignment overlay — workstream

Status: workstream record. **Phase 1 (line tool) shipped** — see below.
Phases 2–3 are planning. Captures the intended build order; each phase is a
shippable increment.

## Goal

Help the operator **align things during stacking** by building drawing/overlay
UI on the live camera view — reference geometry they can line features up
against, and eventually a reference image to trace over.

This is distinct from `STACKING_PROPOSAL.md` (frame stacking for noise
reduction). Same "stacker", different feature.

## Current state — what exists today

There is already a **freehand drawing overlay** on the live FLIR view:

- Lives in `_CameraGLWindow` ([main.py:765](main.py#L765)), the direct GL
  surface that renders the camera frame.
- Strokes are stored as **lists of points in normalized camera-image coords
  (0..1, 0..1)** so they survive window resize and binning swaps
  ([main.py:804](main.py#L804)): `_strokes: list[list[QPointF]]`,
  `_active_stroke`, `_drawing_enabled`.
- Mouse handlers map widget → normalized via `_pos_to_normalized` using
  `_target_rect` (the letterboxed camera-content rect, refreshed every
  `paintGL`): press starts a stroke, move appends points, release finalizes
  ([main.py:841](main.py#L841)–[main.py:888](main.py#L888)).
- Rendered in `_paint_overlay(target)` at the end of `paintGL`
  ([main.py:970](main.py#L970)) — QPainter over the GL surface, after the
  camera texture is blitted via `QOpenGLTextureBlitter`.
- Wired to the status bar: pencil toggles draw mode, trash clears
  (`set_drawing_enabled` / `clear_strokes`, status-bar signals around
  [main.py:3308](main.py#L3308)).

The **normalized-image-space coordinate model is the load-bearing decision**
to preserve — everything below stays in image space so overlays track the
feature, not the pixels of the widget.

## Phase 1 — Straight lines instead of (only) freehand  ✅ shipped

**What:** add a line tool. Click-drag = rubber-band a straight segment;
release commits its two endpoints. Keep freehand as a mode.

**Why first:** alignment wants straight edges and reference axes, not
scribbles. Smallest useful increment, no new architecture.

**As built:**
- New module `overlay.py` (PySide6-only, no PySpin/cv2/numpy) holds the
  typed primitives `FreehandStroke` / `LineSegment` (subclasses of
  `OverlayPrimitive`, each self-rendering via `draw(painter, rect)`) plus
  the pure `normalize_pos` / `to_widget` mapping. Dependency-light so the
  geometry is unit-testable headless (`test_overlay.py`) and the model can
  grow into layers (Phase 2) without dragging in the camera pipeline.
- `_CameraGLWindow._strokes` is now `list[OverlayPrimitive]`; `_active` is
  the in-progress primitive; `_tool: DrawTool` selects what a press starts.
- Mouse: freehand appends every move (unchanged); line sets `start` on
  press, rubber-bands `end` on move, commits on release (zero-length click
  discarded). A tool switch or draw-disable mid-drag drops `_active`.
- Render: `_paint_overlay` is a flat per-primitive `prim.draw(...)` loop;
  each primitive maps its normalized coords through `_target_rect` itself.
- Tool selection: `DrawTool` enum driven from the status bar — an exclusive
  Freehand/Line button group (gated on the `✎ Draw` master toggle),
  `tool_changed(DrawTool)` → `CameraDisplay.set_tool`.

**Nice-to-haves (defer):** shift-to-constrain horizontal/vertical, endpoint
snap, a fixed crosshair/center reticle as a built-in primitive.

## Phase 2 — Basic layer support: visibility, opacity, drag-to-move

**What:** group primitives into layers; each layer has visibility, opacity,
and can be dragged to reposition as a unit.

**Why:** lets the operator build up reference geometry, dim/hide parts, and
nudge a whole set into place without redrawing.

**Where / how — this is the real refactor:**
- Replace the flat `_strokes` with `_layers: list[OverlayLayer]`, where a
  layer holds `primitives`, `visible: bool`, `opacity: float`, and an
  `offset: QPointF` (normalized) for drag-to-move. (Generalize to a full
  2D transform if rotate/scale show up later.)
- New drawing goes onto the **active layer**; mouse handlers target it.
- Render loop: for each visible layer, `painter.setOpacity(layer.opacity)`,
  translate by `layer.offset`, draw its primitives.
- **Drag-to-move:** a "move" tool that translates the active layer's offset.
  Whole-layer move is enough for alignment (no per-primitive selection
  needed in v1).
- **UI:** a small layer panel — list with per-layer visibility checkbox,
  opacity slider, and active-layer select. Status bar's pencil/trash grows
  into a small overlay toolbar or a side panel.

**Open questions:** hit-testing to pick which layer a drag grabs (or just
"drag moves the active layer"); add/delete/reorder layers UI scope.

## Phase 3 — Import an image layer to trace, then hide

**What:** a layer whose content is an **imported image** (PNG/JPG) — load it,
position/scale it, trace over it on a drawing layer, then toggle it hidden.

**Why:** trace a reference (target pattern, prior flake, an alignment
template) directly on the live view.

**Where / how:**
- Extend the layer model so a layer's content can be an image instead of
  primitives. Honor the same visibility / opacity / offset from Phase 2.
- **Render perf matters:** the overlay repaints every `paintGL` (~60 Hz).
  `QPainter.drawImage` of a large bitmap every frame is expensive — prefer
  uploading the image **once as a GL texture and blitting it** (same path as
  the camera frame, with alpha for opacity). QPainter is fine for the line
  primitives; reserve the texture path for image layers.
- **Placement:** decide how the image maps into normalized image-space
  (fit / native + offset). Drag-to-move (Phase 2) handles translation;
  alignment will likely also want **scale and rotation** — flag as a
  Phase 3.5 if tracing reveals it's needed.

**Open question:** import is the point where **scale/rotate** become real;
Phase 2's translate-only transform may need to grow.

## Cross-cutting

- **Coordinate system:** keep everything in normalized image-space (0..1) so
  overlays track features across resize/binning — preserve the existing
  invariant.
- **Persistence (likely needed):** save/restore a layer set across sessions —
  primitives as JSON, image layers by file reference. Not in any single phase;
  decide once layers exist (Phase 2). Ties into the existing settings.toml
  `gui_state` mechanism.
- **Interaction model:** the tool set grows (freehand, line, move, maybe
  select). Plan the overlay toolbar UI early so Phase 1 doesn't paint us into
  a single-button corner.
- **Undo:** becomes cheap once primitives are discrete (Phase 1+). Nice-to-have.

## Build order summary

1. Line tool (typed primitives; rubber-band; keep freehand).
2. Layer model + panel (visibility, opacity, drag-to-move active layer).
3. Image-import layer (GL-textured, opacity/visibility/move; trace then hide)
   — promote transform to scale/rotate if tracing needs it.
