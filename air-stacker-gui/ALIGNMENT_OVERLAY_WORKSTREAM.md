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

## Phase 1a — Aspect-correct coordinate adjustment (foundation for rotation)

**What:** change the stored coordinate space from `[0..1]²` to *aspect-correct
normalized* — **y ∈ [0, 1], x ∈ [0, IMAGE_ASPECT]** with `IMAGE_ASPECT = 4/3`
hardcoded (the Flea3 is 1440×1080). Image center is `(2/3, 1/2)`.

**Why:** Phase 2 rotation/scale about the image center must be *geometrically
rigid*, not sheared. In `[0..1]²` the map to screen px is anisotropic (x and y
have different px-per-unit), so a rotation there shears. Making one x-unit equal
one y-unit *in screen pixels* — which is exactly what `x ∈ [0, 4/3]` does
against the 4:3 letterboxed target rect — lets rotation/scale compose as an
ordinary isotropic transform. This is the load-bearing prerequisite; do it as
its own increment so Phase 2 isn't debugging coords and layers at once.

**Why hardcoded:** a different camera/aspect is an explicit code change anyway
(swap the constant) — not worth deriving per-frame. The constant must equal the
source pixel aspect for units to stay square.

**Where / how:**
- Fold `IMAGE_ASPECT` into `overlay.py`'s `normalize_pos` / `to_widget` (x maps
  through the `4/3` extent instead of `1.0`). Primitives and the mouse/paint
  code are otherwise untouched.
- Update `test_overlay.py` for the new extent (round-trip, resize-invariance).

**Visible effect: none.** With no rotation yet, freehand/line land exactly
where they do today; only the internal x-number changes. 1a is verified by
"tools still land correctly + tests pass + harness looks identical."

## Phase 2 — Layers: visibility, opacity, translate, rotate, scale

**What:** group primitives into layers; each layer has visibility, opacity, and
a translate + **rotate + scale** transform about the fixed image center, applied
to the layer as a unit.

**Why:** lets the operator build up reference geometry, dim/hide parts, and
translate/rotate/scale a whole set into place without redrawing.

**Where / how — this is the real refactor (built on 1a's square-unit space):**
- Replace the flat `_strokes` with `_layers: list[OverlayLayer]`:

  ```python
  @dataclass
  class OverlayLayer:
      name: str
      primitives: list[OverlayPrimitive]   # aspect-correct coords (Phase 1a)
      visible: bool = True
      opacity: float = 1.0
      offset: QPointF        # translation, aspect-correct units
      rotation_deg: float = 0.0   # about image center (2/3, 1/2)
      scale: float = 1.0          # uniform, about image center
  ```
- **Fixed pivot:** every layer rotates/scales about the image center — "a layer
  is fixed at image size." No centroid recompute, no per-layer pivot.
- New drawing goes onto the **active layer**; mouse handlers target it.
- Render loop: for each visible layer, `painter.save()`, `setOpacity`, apply the
  layer's `translate→rotate→scale` about center as one `QTransform`, draw its
  primitives, `painter.restore()`. Because 1a made the space square-unit, the
  transform is built directly — no px-space juggling. Existing `setClipRect`
  trims rotated/scaled content that overflows the frame.
- **Move tool:** translates the active layer's `offset`. Hit-testing a cursor
  back into layer space means inverting the layer transform (`QTransform
  .inverted()`) — the spot to be careful.
- **UI:** a layer panel — per-layer visibility checkbox, opacity slider,
  rotation dial, scale spinbox, active-layer select. Rotate/scale are
  panel-driven (no on-canvas handles this phase). Status bar's pencil/trash
  grows into the overlay toolbar.

**Open questions:** layer management scope (add/delete/reorder/rename) — lean
minimal first; on-canvas rotate/scale handles deferred.

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
- **Placement:** decide how the image maps into the aspect-correct space
  (fit / native + offset). The Phase 2 layer transform already provides
  translate + rotate + scale about the image center, so an image layer just
  reuses it — no new transform work expected.

**Open question:** image layers honor the same `offset/rotation_deg/scale`,
but a GL-textured image needs that transform applied on the **texture blit**
path (not QPainter) — fold the layer `QTransform` into the blit matrix.

## Cross-cutting

- **Coordinate system:** everything lives in *aspect-correct normalized* space
  (Phase 1a) — y ∈ [0,1], x ∈ [0, `IMAGE_ASPECT`=4/3], so overlays track
  features across resize/binning AND rotation/scale about center stay rigid.
  Phase 1 shipped on plain `[0..1]²`; 1a is the migration.
- **Persistence (likely needed):** save/restore a layer set across sessions —
  primitives as JSON, image layers by file reference. Not in any single phase;
  decide once layers exist (Phase 2). Ties into the existing settings.toml
  `gui_state` mechanism.
- **Interaction model:** the tool set grows (freehand, line, move, maybe
  select). Plan the overlay toolbar UI early so Phase 1 doesn't paint us into
  a single-button corner.
- **Undo/redo — TODO (future phase, not yet scheduled):** wanted. Discrete
  typed primitives + layers make it cheap — either snapshot the layer set or a
  command stack (add-primitive / move / transform / clear). Slot it in after
  layers (Phase 2) land; revisit whether it spans layer ops or just drawing.

## Build order summary

1. Line tool (typed primitives; rubber-band; keep freehand). ✅ shipped
1a. Aspect-correct coords (`IMAGE_ASPECT=4/3`; foundation for rigid rotation).
2. Layer model + panel (visibility, opacity, translate, rotate, scale about
   the fixed image center; active-layer drawing + move tool).
3. Image-import layer (GL-textured; reuses the Phase 2 transform on the blit
   path; trace then hide).
