# Air Stacker GUI — USB Webcam View Proposal

Status: DRAFT — for review before implementation.
Companion to (eventually): `RECORDING_PROPOSAL.md` — out of scope for v0.

---

## TL;DR

- Add a second live video view sourced from the **Anker PowerConf C200** USB
  webcam already attached to the Air Stacker PC. Intended use: a contextual
  view of the stage + front-panel instruments while the FLIR Flea3 stays
  focused through the objective.
- **Capture lib**: **`PySide6.QtMultimedia`** (recommended). Already installed
  via PySide6; probe (`§2`) shows it enumerates the C200 with NV12 / MJPEG
  formats and **negotiates 30 fps even at 2560×1440** — cv2/DSHOW only
  achieved ~18 fps because it forced YUYV. Native async camera lifecycle, no
  manual QThread, future `QMediaRecorder` path available for free.
  cv2.VideoCapture stays as the documented fallback.
- **Default mode**: **1280×720 @ 30 fps** (NV12). Conservative on shared USB-3
  bandwidth with the FLIR. 1080p30 and 2K30 are config-overridable for
  experiments once contention is measured.
- **UI**: detached top-level `WebcamWindow` (recommended default), opened by a
  toggle button on the bottom **StatusBar**. Closed by default — no capture
  thread runs until the user clicks it on.
- **Threading**: parallel pipeline, completely separate from the FLIR's
  `acq → proc → hist` chain. `QCamera` + `QMediaCaptureSession` + `QVideoSink`
  render straight into a `QVideoWidget`. No new manual workers; no code
  changes in the FLIR critical path.
- **Display-only in v0**. No recording, no in-app control panel for
  focus/exposure/WB (set those once via the Anker utility). Hooks for both
  are flagged in `§13` and `§14`.
- **Doc updates** to `docs/air-stacker-pc.md` (new "Secondary camera" section)
  and `air-stacker-gui/CLAUDE.md` (one-line pointer) are bundled with the
  implementation. See `§15`.

---

## 1 — The webcam

Identified via `Get-PnpDevice -Class Camera`:

```
FriendlyName : Anker PowerConf C200
InstanceId   : USB\VID_291A&PID_3369&MI_00\7&17714C58&0&0000
Class        : Camera
```

Spec from Anker: 2K (2560×1440) UVC USB-C webcam, autofocus, AI-tracking
(speaker/face follow), dual mics. None of those features matter to us —
we want a static wide view of the stage + instrument panels. Tracking
should be disabled in the C200 utility if it's pointed at a fixed scene
(otherwise the lens hunts as the operator's hands move through the frame).

### Probe results

Two probes run on the Air Stacker PC 2026-05-10:

**Probe 1 — `cv2.VideoCapture` (DirectShow), forces YUY2:**

| Requested  | Actual     | FourCC | Achieved fps |
|------------|------------|--------|--------------|
| 1280×720   | 1280×720   | YUY2   | **29.0**     |
| 1920×1080  | 1920×1080  | YUY2   | 28.0         |
| 2560×1440  | 2560×1440  | YUY2   | 17.4         |
| 3840×2160  | 2560×1440 (capped) | YUY2 | 18.5  |
| 640×480    | 640×480    | YUY2   | 23.3         |
| 320×240    | 320×240    | YUY2   | 29.6         |

**Probe 2 — `QtMultimedia.QMediaDevices.videoInputs()[0].videoFormats()`:**

```
id          = \\?\usb#vid_291a&pid_3369&mi_00#...
description = 'Anker PowerConf C200'

2560×1440  NV12 (and MJPEG)  30 fps
1920×1080  NV12 (and MJPEG)  30 fps
1280×720   NV12 (and MJPEG)  30 fps
 640×480   NV12 (and MJPEG)  30 fps
 640×360   NV12 (and MJPEG)  30 fps
 320×240   NV12 (and MJPEG)  30 fps
 640×480   YUYV              30 fps
 640×360   YUYV              30 fps
 320×240   YUYV              30 fps
```

(`Format_Invalid` in the raw probe is Qt's symbolic-mapping miss for MJPEG —
the format is present, just not labeled. Same resolution table, same fps.)

Key observations:
- **Qt Multimedia (via Media Foundation) negotiates 30 fps at every supported
  resolution including 2560×1440**, because it picks NV12 or MJPEG. cv2/DSHOW
  forced YUYV, which the camera caps at ~18 fps above 1080p.
- All standard UVC resolutions present down to 320×240. Default = 1280×720.
- Index 1 under CAP_DSHOW was the Spinnaker DirectShow filter for the FLIR
  (Spinnaker installs one). Don't point capture there — the second open call
  also segfaulted cv2 in the probe. Pin device by Anker's stable UVC id (Qt
  Multimedia provides this as `QCameraDevice.id()`) rather than a numeric
  index that could shift.

---

## 2 — Current FLIR pipeline (orientation)

For reference, the existing chain (post-PySpin / status-bar refactors):

```
        ┌─────────────┐  acq_mailbox  ┌──────────────┐  hist_mailbox  ┌────────┐
PySpin  │ acq thread  │ ───────────►  │ proc thread  │ ─────────────► │  hist  │
Flea3 ──┤ debayer     │               │ adjustments  │                │ thread │
        └─────────────┘               └──────┬───────┘                └────┬───┘
                                             │ frame_ready (Qt signal)     │ images_ready
                                             ▼                             ▼
                                      _on_frame slot                ImageAdjustmentsPanel
                                      ─► _CameraGLWindow            (histogram strip)
                                      paintGL upload+blit
```

Key files:
- `main.py:923` `FrameMailbox` — single-slot, latest-wins, lock + threading.Event.
- `main.py:956` `CameraAcquireWorker` — `cam.GetNextImage` + `to_rgb` (debayer).
- `main.py:1036` `CameraProcessWorker` — takes from acq mailbox, applies
  `ImageAdjustments`, forks pre-adjust to hist sink.
- `main.py:700` `_CameraGLWindow` — `QOpenGLWindow` with direct texture
  upload + blitter (no QImage indirection, no FBO).
- `main.py:839` `CameraDisplay` — wraps the GL window + a centered status label.
- `main.py:2949` `CameraWindow._spawn_workers` — three QThreads constructed
  per-acquisition (rebuilt on binning swap).

**Important: the webcam path will not mutate any of this.** It runs in
parallel and shares only the Qt event loop.

---

## 3 — Library options

| Library                       | Already a dep? | UVC control | Notes |
|-------------------------------|----------------|-------------|-------|
| **`PySide6.QtMultimedia`**    | yes (PySide6 6.3 ships QtMultimedia + QtMultimediaWidgets) | typed enum API, focus / exposure / WB / zoom | **Recommended.** Probe-validated. Picks NV12 / MJPEG on MF → 30 fps at 2K. Async camera lifecycle (no manual QThread). `QVideoSink` / `QVideoWidget` integrate with the existing widget tree. `QMediaRecorder` available later. |
| `cv2.VideoCapture` + DSHOW    | yes (OpenCV 4.5) | partial (DSHOW UVC subset) | Probe-validated. Returns BGR ndarrays. Same shape as our FLIR `GetNextImage` loop, so consistent with the rest of the codebase if we want numpy frames. **Fallback if Qt Multimedia has Windows quirks; also the right choice if v1 wants per-frame numpy access.** |
| `cv2.VideoCapture` + MSMF     | yes            | better (MSMF UVC subset) | Probed: reports more accurate values than DSHOW (AUTO_EXPOSURE, TEMPERATURE, FPS) but warns on shutdown ("terminating async callback") in OpenCV 4.5. Last-resort fallback. |
| `pyuvc` (libuvc bindings)     | no             | full        | Linux/macOS-native; on Windows requires swapping the C200's driver to WinUSB, which would unbind it from DirectShow / MF for every other Windows app. **Skip.** |
| `PyAV` + DirectShow input     | no (proposed for recording) | full ffmpeg side | Overkill for live preview. Useful later if we record the webcam stream too. **Out of scope for display-only v0.** |
| Native DirectShow / MF via `comtypes` / `pythonnet` | no | full | Most code, most control. Not justified for v0. **Skip.** |

**Recommendation: Qt Multimedia.** Three reasons:

1. **Already installed.** Verified import on the box:
   `from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QVideoSink`.
   No new package, no wheel hunt.
2. **Better format negotiation.** Qt's Media Foundation backend picks NV12 /
   MJPEG and gets 30 fps at every supported resolution, including 2560×1440.
   cv2/DSHOW forced YUYV and capped at ~18 fps above 1080p.
3. **Less code.** The whole capture + display becomes ~30 lines:

   ```python
   self._camera = QCamera(self._pick_anker_device())
   fmt = self._pick_format(self._camera.cameraDevice(), 1280, 720, 30)
   self._camera.setCameraFormat(fmt)
   self._session = QMediaCaptureSession()
   self._session.setCamera(self._camera)
   self._video_widget = QVideoWidget()
   self._session.setVideoOutput(self._video_widget)
   self._camera.start()  # async — runs on Qt's event loop
   ```

   No `QThread`, no mailbox, no `paintGL`. Qt does the texture upload via
   `QVideoSink` under the hood, hardware-accelerated.

**Fallback path**: if Qt Multimedia surfaces a Windows-specific bug
(MF crashes, plugin missing, etc.), drop to `cv2.VideoCapture(0, cv2.CAP_DSHOW)`
+ a `WebcamAcquireWorker` + `_WebcamGLWindow` mirror of the FLIR pattern.
The fallback is fully spec'd in `§7b` so the implementation can pivot quickly
without redesigning.

---

## 4 — UI options

User input: "should have a toggle (can live in the bottom bar)". So
regardless of layout choice, the entry point is a single button in the
existing `StatusBar`. The choice below is about *where the view appears*
when the toggle fires.

### Option A — Detached `WebcamWindow` (RECOMMENDED)

```
┌──────────────────────────────────────────────┐    ┌──────────────────────┐
│ Air Stacker — live                            │    │ Webcam — Anker C200  │
│ ┌────────────────────────────────┐  ┌──────┐ │    │ ┌──────────────────┐ │
│ │ FLIR Flea3                     │  │ Yoko │ │    │ │                  │ │
│ │ 1600×1200 BayerBG → RGB        │  │ ───  │ │    │ │ 1280×720 stage + │ │
│ │                                │  │SMC100│ │    │ │ instrument panels│ │
│ │                                │  │ ───  │ │    │ │                  │ │
│ │                                │  │heater│ │    │ │                  │ │
│ └────────────────────────────────┘  └──────┘ │    │ │                  │ │
│ status bar │ FPS │ sharpness │ [▣ Webcam]    │    │ └──────────────────┘ │
└──────────────────────────────────────────────┘    └──────────────────────┘
                                                     (separate top-level Qt
                                                      window — draggable to
                                                      monitor 2, sizeable
                                                      independent of main)
```

- Toggle button (StatusBar) opens/closes a top-level `WebcamWindow`. While
  closed, the capture thread doesn't exist — zero idle cost.
- Persist last position + size in `config.toml` (writable via tomlkit at
  shutdown) so the window comes back where the operator left it.
- Bottom-status text in the webcam window mirrors the main one (FPS,
  resolution, "no signal" state if cv2 read fails).

**Pros**: zero impact on main GUI layout and perf budget when closed; matches
how operators with multiple monitors already work; tightly bounded blast
radius (a webcam regression cannot break the FLIR critical path).

**Cons**: another window to alt-tab to; toggle is "out of sight, out of mind"
unless the StatusBar button is well-styled; not great if the user only has
one monitor and ends up overlapping windows.

### Option B — Side pane in the main window

```
┌──────────────────────────────────────────────────────────────────┐
│ Air Stacker — live                                                │
│ ┌─────────────┐ ┌─────────────────────┐ ┌─────────┐  ┌─────────┐ │
│ │ adjustments │ │ FLIR Flea3 1600×1200│ │ Webcam  │  │ Yoko    │ │
│ │ camera opts │ │                     │ │ 1280×720│  │ SMC100  │ │
│ │             │ │                     │ │         │  │ heater  │ │
│ └─────────────┘ └─────────────────────┘ └─────────┘  └─────────┘ │
│ status bar │ FPS │ sharpness │ [▣ Webcam]                        │
└──────────────────────────────────────────────────────────────────┘
```

- Toggle hides/shows the column in-place. Closed = column collapsed to 0 width.
- Webcam pane sits between the FLIR viewer and the right-hand instrument
  column (or could be tucked at the far right; see open question).

**Pros**: single window, both feeds always visible side-by-side when on,
preserves at-a-glance awareness; smaller learning curve than "where did
that window go?".

**Cons**: eats horizontal real estate from the FLIR viewer (the FLIR is
1600×1200 = 4:3 and dominates layout already); awkward on small monitors;
the toggle changes the FLIR viewer's effective scale, which is jarring
mid-session.

### Option C — Picture-in-picture corner inset

```
┌──────────────────────────────────────────────────┐
│ Air Stacker — live                                │
│ ┌──────────────────────────────┐ ┌─────────────┐ │
│ │ FLIR Flea3        ┌────────┐ │ │ Yoko        │ │
│ │                   │webcam  │ │ │ SMC100      │ │
│ │                   │inset   │ │ │ heater      │ │
│ │                   └────────┘ │ │             │ │
│ │                              │ │             │ │
│ └──────────────────────────────┘ └─────────────┘ │
│ status bar │ FPS │ sharpness │ [▣ Webcam]        │
└──────────────────────────────────────────────────┘
```

Two implementation paths:
1. **Floating native child window** — a second `_WebcamGLWindow` embedded
   via `createWindowContainer`, positioned over the `CameraDisplay`'s
   widget with manual geometry tracking on the parent's `resizeEvent`. Z-order
   handled by `raise_()` — same pattern `CameraDisplay.text_label` already
   uses for the centered status overlay.
2. **Second textured quad in `paintGL`** — render the webcam as a second
   blit inside the existing `_CameraGLWindow.paintGL`. Requires moving
   the webcam frame buffer into the GL window's state and synchronizing
   updates from two source pipelines into one paint event.

Both have downsides: (1) is fiddly across resizes / monitor moves and the
native-window stacking on Windows is brittle (we already paper over it
with `WA_NativeWindow` for the status overlay — adding a second native
child compounds the z-order debugging). (2) is more invasive — touches
the FLIR's paint path, which has been tuned hard (see `paintGL` cycle
logging at `main.py:822`).

**Pros**: zero net real-estate cost; spatial co-location of both views;
visually compact.

**Cons**: occludes part of the FLIR view by definition; corner / size /
opacity all become UX knobs; either implementation path touches code we'd
otherwise leave alone for v0.

### Recommendation

**Ship Option A (detached window) in v0.** If users find themselves wanting
the webcam permanently visible, revisit with Option B in v1.1. Option C
is the most code complexity for the least real benefit on this rig
(operators have desk space for a second window; the FLIR view is what
they're actually staring at).

---

## 5 — Toggle UX (status-bar button)

Per the user note: toggle lives in the bottom **StatusBar**.

- Existing `StatusBar` (`status_bar.py`) currently displays FPS + sharpness.
  Add a small button on the right edge labeled `▣ Webcam` (off) /
  `▣ Webcam ●` (on, with a live red dot).
- Click while off: spawn capture worker, open the `WebcamWindow`, start
  streaming. First-frame latency: ~300–500 ms (DirectShow open + first
  YUY2→BGR cycle).
- Click while on: stop capture, close the window. ~100 ms shutdown.
- Closing the `WebcamWindow` directly (X button) also stops capture; the
  StatusBar button reflects the off state. Single source of truth lives in
  `CameraWindow._webcam_window is not None`.
- No "minimize without stopping" mode in v0. Off = no thread running.

A keyboard shortcut (`W`, `Ctrl+W`, …) is out of scope for v0; operators can
click. Worth a follow-up.

---

## 6 — Pipeline / threading (Qt Multimedia path)

```
                (parallel, fully isolated from the FLIR chain)

QCamera (Media Foundation backend, async, owned by Qt event loop)
   │
   │ frames produced on Qt-managed thread, delivered to:
   ▼
QMediaCaptureSession  ─────►  QVideoSink  ─────►  QVideoWidget
                              (HW-accelerated      (lives inside
                               texture upload)      WebcamWindow)
```

- No manual `QThread`, no `FrameMailbox`, no `paintGL`. `QCamera.start()` /
  `stop()` controls the capture lifecycle; everything else is wiring.
- `WebcamWindow.__init__` constructs the `QCamera` / `QMediaCaptureSession` /
  `QVideoWidget`, then calls `QCamera.start()`. `closeEvent` calls
  `QCamera.stop()` and lets Qt clean up the rest.
- Device pinning via `QCameraDevice.id()` (a stable per-device UVC string —
  see `§2` probe output) — not numeric index. This survives USB reordering
  and a Spinnaker DirectShow filter being installed.
- Format pinning: enumerate `device.videoFormats()`, pick the
  `(width, height, pixelFormat, fps)` tuple matching config. Prefer NV12 over
  MJPEG (no CPU decode); avoid YUYV at >480p (probe showed it caps fps).
- Error handling: connect `QCamera.errorOccurred` → StatusBar label +
  toggle-off. `QCamera.errorChanged` exposes a `QCamera.Error` enum
  (CameraError, NotSupportedFeatureError, etc.) for clear diagnostics.

### 6b — Fallback pipeline (cv2 / DSHOW)

If Qt Multimedia has issues on the box (MF plugin missing, driver bug,
unexplained stalls), drop to the cv2 path:

```
cv2.VideoCapture(0, CAP_DSHOW)
        │  read() blocks until next frame
        ▼
  ┌──────────────────┐  webcam_mailbox  ┌─────────────────┐
  │ WebcamAcquireWk  │ ───────────────► │ _WebcamGLWindow │
  │ (its own QThread)│                  │ paintGL upload  │
  └──────────────────┘                  └─────────────────┘
```

- Reuses the existing `FrameMailbox` primitive (`main.py:923`).
- `_WebcamGLWindow` is a copy of `_CameraGLWindow` with the same texture +
  blitter pattern.
- Mirrors the FLIR's `acq` thread shape; BGR→RGB conversion is one
  `cv2.cvtColor` (~2 ms at 720p, releases GIL).

This fallback is fully implementable from the existing FLIR code. We don't
build both — we build Qt Multimedia first, and only swap if it fails QA.

---

## 6c — What controls the C200 actually exposes

Probed live (`probe_webcam_controls.py`) against both APIs. The Anker
PowerConf C200 is a stock UVC device; controls are standard with one
vendor extension.

### Image controls (standard UVC, readable/writable on both APIs)

| Control     | Range observed   | Qt Multimedia path                              | cv2 path                          |
|-------------|------------------|-------------------------------------------------|-----------------------------------|
| Brightness  | 0..100 (mid=50)  | `QCamera` doesn't expose directly — use the OS  | `cv2.CAP_PROP_BRIGHTNESS`         |
| Contrast    | 0..100 (mid=50)  |   "                                              | `cv2.CAP_PROP_CONTRAST`           |
| Saturation  | 0..100 (mid=50)  |   "                                              | `cv2.CAP_PROP_SATURATION`         |
| Sharpness   | 0..100 (mid=50)  |   "                                              | `cv2.CAP_PROP_SHARPNESS`          |
| Hue         | int (current=0)  |   "                                              | `cv2.CAP_PROP_HUE`                |
| Gamma       | observed = 400   |   "                                              | `cv2.CAP_PROP_GAMMA`              |

Qt Multimedia's `QCamera` API doesn't surface every UVC control as a typed
method — brightness/contrast/saturation/sharpness/gamma must be set via
the OS camera dialog (`QCamera.captureSession().platformImageCapture()` is
not the right path). For these knobs, the right move on the C200 is the
**Anker utility** that's already installed on the box; setting them once
sticks across sessions.

### Camera-mechanism controls (typed in Qt Multimedia, partial in cv2)

| Capability      | Qt Multimedia                          | cv2 (probed)                       |
|-----------------|----------------------------------------|------------------------------------|
| Auto-focus      | `QCamera.setFocusMode(FocusModeAuto / FocusModeManual)` | `CAP_PROP_AUTOFOCUS` (0/1) |
| Manual focus    | `QCamera.setFocusDistance(0.0..1.0)`   | `CAP_PROP_FOCUS` (raw UVC int)     |
| Auto-exposure   | `QCamera.setExposureMode(...)`         | `CAP_PROP_AUTO_EXPOSURE` (MSMF only; DSHOW returns -1) |
| Manual exposure | `QCamera.setManualExposureTime(secs)`  | `CAP_PROP_EXPOSURE` (log2 µs)      |
| WB mode         | `QCamera.setWhiteBalanceMode(WhiteBalanceAuto / Tungsten / ...)` | `CAP_PROP_AUTO_WB` (-1 on DSHOW, not supported there) |
| WB temperature  | `QCamera.setColorTemperature(K)`       | `CAP_PROP_WB_TEMPERATURE` (MSMF reports 4500 K default) |
| Digital zoom    | `QCamera.setZoomFactor(1.0..max)`      | `CAP_PROP_ZOOM` (100 = baseline)   |
| Digital pan     | (UVC extension)                        | `CAP_PROP_PAN` (observed 8)        |
| Digital tilt    | (UVC extension)                        | `CAP_PROP_TILT` (observed -10)     |

`PAN` / `TILT` are nonzero out of the box because Anker's AI-tracking
feature has been steering the digital ROI. **Turn AI tracking off** in
the Anker utility once the camera is fixed-mounted — otherwise the lens
will appear to "hunt" through the GUI feed as the operator's hands move.

### Vendor extension (NOT exposed by standard APIs)

The C200's AI-tracking, HDR toggle, anti-flicker, and FoV preset live on
UVC Extension Units (XU). Neither Qt Multimedia nor cv2 exposes XUs —
they're configurable only via the Anker Work utility. **Already configured
on the box; the GUI doesn't need to touch any of these.** None of those
vendor features are useful for our static-framing monitoring use case.

### Not exposed on this camera

`CAP_PROP_GAIN` and `CAP_PROP_BACKLIGHT` return `-1` — the C200 doesn't
surface them. (Gain is coupled to exposure in the C200's auto-pipeline.)

### v0 scope decision

**No in-app webcam control panel in v0.** Setting focus/exposure/WB via
the Anker utility once is the right move for a fixed-mount monitoring
camera. If operators later want in-GUI sliders (e.g., bumping exposure
when panel LEDs are too bright), Qt Multimedia's `QCamera` typed API
makes those one-line additions later. Flagged as `§14` follow-up.

---

## 7 — Simultaneous-capture risks

The big technical unknown.

### USB3 bandwidth

Both cameras live on the same Air Stacker PC USB tree. Numbers (peak, not
typical):

- FLIR Flea3 @ 1600×1200 BayerBG8 × 60 fps = ~115 MB/s sustained over USB3.
- Anker C200 @ 1280×720 YUY2 × 30 fps = ~55 MB/s. (At 1080p30: ~125 MB/s.
  At 2K@18 fps: ~130 MB/s.)
- A single USB 3.0 host controller has ~400–500 MB/s of usable bandwidth.

If both cameras enumerate on the same xHCI controller, **720p30 should fit
with margin**. 1080p30 is borderline. 2K is asking for trouble.

The risk symptom would be the FLIR's acq thread seeing `image.IsIncomplete()`
returns or rising drop counts — code already logs these at
`main.py:992`. The webcam side would show `cap.read()` returning `False`.

### Mitigation

1. **Default 1280×720 @ 30 fps.** Hardcoded ceiling in the panel; expose
   higher resolutions only via config.toml override after live testing.
2. **Document a USB port move** in `docs/air-stacker-pc.md` if testing shows
   contention: plug the webcam into a USB-2 port (the C200 is USB-C UVC
   and works on USB-2 at reduced max resolution). The Flea3 must stay on
   USB-3.
3. **Test plan** (for the impl phase, not now):
   - Baseline: open the FLIR-only GUI for 60 s, log incomplete-frame count.
   - With webcam at 720p30: same test. Compare drop counts. Pass criteria:
     no statistically significant increase.
   - Worst case (1080p30, then 2K@18): repeat. Document the breaking point.
4. **Graceful degrade**: if `cap.read()` returns `False` 5 times consecutively,
   the acquire worker emits a "webcam disconnected" state, stops the thread,
   and the StatusBar toggle returns to off. No crashes, no retries that
   could starve the FLIR bus.

### Frame-format conversion cost

YUY2 → BGR happens inside `cap.read()` (libopencv hands us a converted BGR
ndarray; the YUY2 wire format is transparent). Our extra BGR → RGB is one
`cvtColor` at 720p ≈ 2 ms. Total per-frame cost on the webcam thread:
~3–5 ms for the read, ~2 ms for the convert, negligible publish. Off the
GUI / FLIR threads entirely.

---

## 8 — Performance budget

Today: FLIR acq+proc+hist+paint+GUI ≈ 14–16 ms / frame (16.7 ms vsync budget).

Add webcam:
- Webcam acq thread: ~3–5 ms / frame at 720p30, runs at 30 Hz (33 ms budget,
  trivially met).
- Webcam paint: ~2–3 ms / frame, runs at 30 Hz on a *different* `paintGL` event.
  Doesn't share a thread with the FLIR's paintGL.
- GUI thread: an extra `_on_webcam_frame` slot at 30 Hz ≈ +0.5 ms of event-loop
  work / sec. Untouchable in the noise.
- CPU envelope: ~2–4 % additional, dominated by the YUY2 path.

This is well within budget. The FLIR critical path stays untouched.

---

## 9 — Config additions

```toml
# USB webcam — display-only secondary viewer. Live toggle in the bottom
# StatusBar opens/closes the window. Currently configured for the Anker
# PowerConf C200 (see docs/air-stacker-pc.md "Secondary camera").
[webcam]
# Skip the webcam entirely (no UI button shown). Default: false.
enabled = true

# Device matching. Qt Multimedia identifies cameras by a stable UVC id
# string (`QCameraDevice.id()`). Prefer matching by description substring
# so the config survives USB port reshuffles; fall back to the explicit id
# if multiple cameras have the same description (not the case today).
device_description = "Anker PowerConf C200"
# device_id = "\\\\?\\usb#vid_291a&pid_3369&mi_00#..."   # exact match override

# Capture format. 1280×720 NV12 @ 30 fps is the validated default on the
# Anker C200 (probed 2026-05-10). NV12 is preferred over MJPEG (no decode)
# and over YUYV (caps fps above 1080p). 2560×1440 NV12 @ 30 fps also works
# if USB-3 contention with the Flea3 turns out to be a non-issue.
width = 1280
height = 720
target_fps = 30
pixel_format = "NV12"        # "NV12" | "MJPEG" | "YUYV"

# Whether the window opens on app launch. Default: false (operator clicks
# the StatusBar toggle when they want it).
default_visible = false

# Capture backend. "qt" (PySide6.QtMultimedia, recommended) | "cv2"
# (cv2.VideoCapture fallback). The Qt path uses Media Foundation under
# the hood on Windows; cv2 path uses DirectShow.
backend = "qt"

# Last window geometry — written on close, restored on open. Operators can
# leave it alone; tomlkit preserves the section across edits.
# window_geometry = { x = 1920, y = 100, width = 800, height = 600 }
```

`WebcamConfig` dataclass next to the existing `OUR_DEFAULTS` pattern in
`main.py`. Validation at load time: if `device_description` doesn't match
any present camera, log a warning and disable the StatusBar toggle (don't
crash the app).

---

## 10 — Out of scope for v0

- **Recording.** Display-only. The hook for adding the webcam stream to a
  recording session is noted in `§11`.
- **Audio.** The C200 has dual mics. We don't want them.
- **UVC property control** (focus / WB / exposure / AI-tracking off). Set
  these once via the Anker utility on the box; revisit if operators want
  in-app sliders.
- **Multi-cam** (more than one USB webcam). Hardcode `device_index` for now.
- **Hot-plug / disconnect recovery.** If the camera vanishes mid-session the
  worker stops and reports an error. Operator re-toggles to retry.
- **Re-arming the FLIR pipeline** on webcam state changes. They're independent.
- **Factoring** `_CameraGLWindow` into a shared base. Copy in v0, refactor
  in a follow-up when both viewers exist.

---

## 11 — Open questions / unknowns for the impl phase

Confirmed decisions (no longer open):
- UI: detached `WebcamWindow`. — confirmed.
- Capture lib: Qt Multimedia. — confirmed.
- Default resolution: 1280×720 NV12 @ 30 fps. — confirmed.
- Default state on launch: closed; operator clicks the StatusBar toggle.
  — confirmed.
- Anker utility / vendor features: already configured on the box; the GUI
  doesn't touch them. — confirmed.

Remaining unknowns (resolved empirically during implementation, not
upstream of it):

1. **USB-3 contention.** The bandwidth math says we're fine at 720p30, but
   a 10-minute live test with both feeds running is the only way to know.
   Acceptance: FLIR `IsIncomplete()` rate stays at the FLIR-only baseline.
2. **Qt Multimedia capture session on Windows.** Probed: enumeration works,
   format negotiation works. Unknown: whether `QCamera.start()` actually
   produces frames on the C200 through the Qt 6.3 MF plugin without
   surprises (the plugin has had bugs across point releases). If it fails,
   pivot to `§6b` (cv2 / DSHOW).
3. **Aspect ratio.** The C200 is 16:9 at 1280×720. `QVideoWidget` handles
   aspect-ratio fit natively; just confirming no UI tweak is needed at the
   detached-window resize path.
4. **Geometry persistence.** Save last (x, y, width, height) to
   `config.toml` on close, restore on open. If the saved position lands
   offscreen (monitor unplugged), let Qt clamp — no v0 per-monitor logic.

---

## 12 — What I'd build first (one-week scope)

If green-lit:

1. `WebcamConfig` + `[webcam]` section in `config.toml` (loader + validation).
2. `WebcamWindow` (new top-level `QWidget`) — owns a `QCamera` +
   `QMediaCaptureSession` + `QVideoWidget`. Constructor pins the device by
   description, picks the matching `QCameraFormat`, calls `QCamera.start()`.
   `closeEvent` stops and tears down.
3. `StatusBar` button — right-edge toggle. Off → spawn `WebcamWindow`,
   call `show()`. On → `WebcamWindow.close()`.
4. Wire `QCamera.errorOccurred` → StatusBar status text → toggle state. Five
   consecutive errors → auto-toggle off.
5. **Stop. Live test on the box.**
   - Verify FLIR `IsIncomplete()` rate is unchanged at 720p30 webcam +
     full-rate FLIR.
   - Verify 1080p30 and 2K30 modes via config override (don't ship them as
     default until contention is measured).
6. **Doc updates** (see `§15` for the exact content): new "Secondary camera"
   section in `docs/air-stacker-pc.md` covering the C200's identity,
   supported formats, controls, and the DirectShow-filter conflict note;
   stale-info fix in repo-root `stackers/CLAUDE.md` (harvesters/GenTL →
   PySpin, plus a phrase for the new secondary view).

Everything in §10 stays explicitly out. The cv2 fallback (`§6b`) gets
built only if step 5 fails Qt Multimedia QA.

---

## 13 — Recording hook (out of scope for v0, flagged for v1)

When `RECORDING_PROPOSAL.md`-style recording lands, the webcam recording
slot is essentially free:

- `QMediaRecorder` attached to the same `QMediaCaptureSession` writes
  `webcam.mp4` directly. No PyAV plumbing, no separate encoder pipeline.
- Per-run dir: `run_NNN/webcam.mp4` alongside the existing `video.mp4`.
- Per-frame timestamping: `QVideoSink.videoFrameChanged(QVideoFrame)` gives
  us `frame.startTime()` (camera-side, microseconds). Fork to a
  `webcam_frames.csv` mirroring `frames.csv`.
- The Intel QuickSync iGPU on the Air Stacker PC handles two simultaneous
  H.264 encodes (FLIR 1600×1200@60 + webcam 1280×720@30) trivially.

If we pick the cv2 fallback path, recording goes through a separate
`RecordingWorker` instance per `RECORDING_PROPOSAL §3`. More code, same
output.

---

## 14 — In-app webcam control panel (out of scope for v0)

Qt Multimedia's `QCamera` makes per-property control sliders one-line
additions later. Likely useful knobs if the static Anker-utility config
isn't enough:

- Focus mode + manual distance (helpful if depth of field on panel readouts
  needs tuning)
- Exposure compensation (when the room's ceiling lights wash out the panels)
- White-balance mode + manual K (matching the FLIR's color)
- Zoom factor (digital crop into the panel cluster)

These would live in a `WebcamSettingsPanel` group inside the `WebcamWindow`,
collapsed by default. Not v0.

---

## 15 — Doc updates that ship with the implementation

### `docs/air-stacker-pc.md`

Add a new section **`## Secondary camera`** placed right after the existing
`## Camera` section (which documents the FLIR Flea3). Content:

```markdown
## Secondary camera

- **Hardware**: **Anker PowerConf C200** USB-C UVC webcam. Used as a
  contextual view of the stage + front-panel instruments alongside the
  through-objective FLIR feed. Display-only — not part of the science
  data path.
- **USB enumeration**: `VID_291A`, `PID_3369`, single video interface
  (`MI_00`). PnP `FriendlyName = "Anker PowerConf C200"`. Enumerates under
  `Get-PnpDevice -Class Camera`.
- **Native resolutions & framerates** (probed 2026-05-10 via
  `QMediaDevices.videoInputs()`):

  | Resolution | NV12 / MJPEG | YUYV    |
  |------------|--------------|---------|
  | 2560×1440  | 30 fps       | —       |
  | 1920×1080  | 30 fps       | —       |
  | 1280×720   | 30 fps       | —       |
  |  640×480   | 30 fps       | 30 fps  |
  |  640×360   | 30 fps       | 30 fps  |
  |  320×240   | 30 fps       | 30 fps  |

  Notes: cv2/DSHOW forces YUYV and caps at ~18 fps above 1080p. Qt
  Multimedia / Media Foundation negotiates NV12 or MJPEG and sustains
  30 fps at every supported resolution. Recommended default: **1280×720
  NV12 @ 30 fps**.

- **GUI integration**: `air-stacker-gui` opens it via `PySide6.QtMultimedia`
  (`QCamera` + `QMediaCaptureSession` + `QVideoWidget`). Toggle lives on
  the bottom `StatusBar`. Closed by default. See
  [`WEBCAM_PIP_PROPOSAL.md`](../air-stacker-gui/WEBCAM_PIP_PROPOSAL.md).

- **Standard UVC controls accessible** (typed in Qt Multimedia where noted):
  brightness, contrast, saturation, sharpness, gamma, hue, focus + autofocus
  (`QCamera.setFocusMode` / `setFocusDistance`), exposure + auto (`setExposureMode`
  / `setManualExposureTime`), white balance + auto (`setWhiteBalanceMode` /
  `setColorTemperature`), digital zoom (`setZoomFactor`), digital pan/tilt.
  Gain, backlight, iris, roll are not exposed on this model. AI tracking,
  HDR, anti-flicker, FoV preset are vendor UVC extension units (XU) —
  configurable only via the Anker utility, not via standard APIs.

- **Vendor features (AI tracking, HDR, anti-flicker, FoV preset)**: live on
  UVC extension units — configurable only via the Anker Work utility, not
  through Qt Multimedia or cv2. None are useful for our static-frame
  monitoring use case; already configured on the box and the GUI doesn't
  touch them.

- **DirectShow filter conflict**: index 1 under `cv2.CAP_DSHOW` enumerates
  the Spinnaker DirectShow filter for the FLIR Flea3 (Spinnaker installs
  one). Don't open by numeric index — pin by `QCameraDevice.id()` or
  description match.

- **Probe scripts**: [`probe_webcam.py`](../air-stacker-gui/probe_webcam.py)
  (cv2 / DSHOW resolution × fps grid),
  [`probe_webcam_controls.py`](../air-stacker-gui/probe_webcam_controls.py)
  (cv2 CAP_PROP_* + Qt Multimedia format enumeration).
```

Probe scripts now live at
[`probe_webcam.py`](probe_webcam.py) and
[`probe_webcam_controls.py`](probe_webcam_controls.py), mirroring
`probe_smc100*.py` / `probe_yoko.py` / `probe_binning.py`. Both are
read-only and safe to re-run on the box.

### `air-stacker-gui/CLAUDE.md` (worktree root note)

No change needed beyond a passing reference if a "Cameras" subsection ever
appears. Today's CLAUDE.md is one-line-per-subdirectory; the proposal +
doc updates above cover the detail.

### `stackers/CLAUDE.md` (repo-root)

Current line for `air-stacker-gui/` reads:

> `air-stacker-gui/` — custom microscope viewer + recorder for the Air
> Stacker (FLIR Flea3 via harvesters/GenTL).

Two fixes once the webcam ships:

1. `harvesters/GenTL` is stale — the camera path is **PySpin (FLIR
   Spinnaker SDK)** post-2026-05 refactor. Update independently of this
   proposal.
2. Append a phrase about the secondary view:
   `... (FLIR Flea3 via PySpin, plus an Anker PowerConf C200 USB webcam
   secondary view).`

### `air-stacker-gui/config.toml`

Inline comments per `§9`. Document the `[webcam]` section conservatively
so operators understand `backend = "qt"` vs `"cv2"` and the format
tradeoffs without needing to read the proposal.
