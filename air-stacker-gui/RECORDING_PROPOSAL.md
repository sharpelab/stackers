# Air Stacker GUI — Recording Feature Proposal

Status: DRAFT — for review before implementation.
Author: Claude (drafted from a Linux dev box, no camera)
Scope: v0 of recording. Single record button, per-press run, post-adjustment H.264 encode.
Companion: `SESSION_LOG_PROPOSAL.md` — defines the snapshot + event streams written alongside the video.

---

## TL;DR

- **Source**: post-adjustment frames (what the user sees), tapped from `CameraProcessWorker`.
- **Format**: H.264 in MP4 — `h264_qsv` (Intel QuickSync) by default with `libx264 -preset veryfast` as software fallback. Visually lossless. Fragmented MP4 so a crash leaves a playable file.
- **Topology**: a fourth worker thread (`RecordingWorker`) consuming a bounded `RecordQueue` that proc fills non-blocking. Proc never waits on the writer.
- **Per-run sidecar JSON** with camera settings, adjustments at start (and any changes), frame counts, drops, host + camera timestamps. Session-level state lives in `session.jsonl` streams (see companion proposal).
- **Layout**: `<base>/session_<TS>/run_NNN_<TS>/{video.mp4, run.json, frames.csv}`; `session.json` + `events.jsonl` + per-stream native-rate CSVs (`axes.csv`, `heater.csv`) at the session level.
- **UX**: existing two-button placeholder (`Record` / `Stop`) collapses to a single toggle.
- **Default base**: `~/AirStackerRecordings`, configurable in `config.toml`.

---

## 1 — Frame source

Selected: **post-adjustment** (user-visible) frames.

Rationale (as confirmed):
- Adjustment-baked frames are what users want to share + review.
- Reduced effective entropy after LUTs → friendlier for H.264.
- `AdjustmentSnapshot` is recorded in the sidecar so the chain is documented.

Trade vs. raw post-debayer / raw bayer:
- Raw stream is the science convention (ImageJ, μManager). We lose dynamic range that adjustments clipped — if a user records with `r_range=(40, 200)`, the upper/lower 50 codes are gone forever. The sidecar records the snapshot, but you can't *invert* a clip.
- Mitigation: log a warning in the run sidecar (`adjustments_active: true`, list of non-identity fields) so reviewers know the stream isn't sensor-faithful.

Tap point in the pipeline:
```
acq → acq_mailbox ──► proc ─┬─► hist_mailbox  ─► hist
                            ├─► GUI (take_latest + frame_ready)
                            └─► record_queue ─► RecordingWorker  ◄── NEW
```
Forking happens inside `CameraProcessWorker.run()` immediately after the post-adjustment `rgb` is produced (after the `apply_adjustments` block, before `self._latest = rgb`). One ndarray reference, no copy.

Notes on correctness:
- The post-adjustment fork uses the *same* ndarray instance the GUI receives. `apply_adjustments` returns a fresh allocation on every non-identity call (its final pass writes into `np.empty_like(rgb)`); on identity it returns the input unchanged. Both cases are safe to publish to the writer queue — neither side mutates the buffer.
- Hist continues to fork the *pre*-adjustment frame. No change there; sensor-diagnostic convention preserved.

---

## 2 — File format / codec

Selected: **H.264 in MP4**, visually lossless.

Concrete params (proposed):
- Library: **PyAV** (ffmpeg bindings). Explicit container/codec control; PTS on every frame; cleaner shutdown than `cv2.VideoWriter`.
- Codec: **`h264_qsv`** (Intel QuickSync) by default — the air-stacker PC has an Intel iGPU. Software fallback: `libx264 -preset veryfast`.
- Quality: `global_quality = 22` for QSV (≈ visually lossless; QSV's scale differs from x264's CRF — empirical tune pass needed). `crf = 17` for the libx264 fallback.
- Input pixel format: `nv12` for QSV (hardware-native), `yuv420p` for libx264. PyAV converts from RGB.
- Container: MP4 with `-movflags +frag_keyframe+empty_moov+default_base_moof` so a crash truncates safely instead of producing an unplayable file.
- Color tagging: set BT.709 / limited range explicitly so VLC and ffprobe report colors correctly.

Bitrate / size budget (estimates, ground-truth in QA):
- ~30–80 Mbps at visually-lossless settings for moderate-motion microscopy at 1600×1200@60.
- 10-min run ≈ 2–6 GB. 1-hour run ≈ 13–36 GB.
- Fits comfortably even on a spinning disk in steady state.

Rejected alternatives:
- **PNG/TIFF sequence**: 60 fps × 5.76 MB raw → encoder must do >150 MB/s. PNG @ 1600×1200 is ~30–50 ms per frame to encode; can't keep up.
- **Uncompressed TIFF stack**: 345 MB/s sustained kills the SATA disk on the air-stacker PC. Disqualifying given user's "long runs on whatever's there".
- **FFV1/MKV (lossless)**: ~2–3× compression → still ~100–150 MB/s. Borderline. Visually-lossless H.264 is ~5× smaller and trivially playable.
- **Raw `.bin` stream**: ~345 MB/s. Same disk problem. Also opaque to users without tooling.

QSV availability:
- Detection at startup: try opening a 1×1 `h264_qsv` encoder; on failure, fall back to libx264.
- Requires the Intel media driver. On Windows this ships with the iGPU graphics driver; on Linux it's `intel-media-driver` + `libmfx`. PyAV must be built with QSV support (most prebuilt wheels are; verify with `av.codecs_available`).
- 1600×1200 @ 60 fps is trivial for any QSV-capable iGPU (Skylake+ does 4K60 H.264). Encode CPU drops to ~zero.
- Risks/quirks: monotonic PTS required (we already supply this); some QSV builds drop frames silently on PTS gaps; color-matrix tagging defaults aren't always 709 — set explicitly.

If QSV is unavailable and `libx264 -preset veryfast` can't sustain 60 fps × 1600×1200 on the host, mitigations in order:
  1. Drop to `superfast` preset.
  2. Halve the framerate to 30 fps in the encoder while still tagging acquisition timestamps.
  3. Last resort: drop to 800×600 record while keeping live preview at full res (not in v0; flagged as future work).

---

## 3 — I/O thread topology

A **new** worker thread (`RecordingWorker`), not a pool. Encoders are stateful in time — H.264 needs frames in monotone PTS order. Pooling buys nothing without a reorder buffer.

Components:

```python
class RecordQueue:
    """Bounded queue, non-blocking put with drop accounting.
    Distinct from FrameMailbox (latest-wins): we want every frame."""
    def __init__(self, maxsize: int): ...
    def put_nowait(self, frame: np.ndarray) -> bool: ...  # False on overflow
    def take(self, timeout: float) -> np.ndarray | None: ...
    @property
    def dropped_count(self) -> int: ...
```

- `maxsize ≈ 120` → 2 s of buffer at 60 fps → ~700 MB peak RAM. Generous; encode is the bottleneck, not RAM.
- Proc thread: `if self._recorder is not None: self._recorder.queue.put_nowait(rgb)`. Single attribute check + bounded put. Cost when recording is *off*: one `is None` check. When recording is *on* and queue has room: one ref-counted append.
- **Critically: this is non-blocking.** If the writer falls behind, frames drop *at the queue*, never blocking proc. Proc stays pinned to camera rate; GUI never stutters because of encoding pressure.

`RecordingWorker.run()` (sketch):

```
open container + stream + sidecar(status="running")
loop until stop_event:
    rgb = queue.take(timeout=0.5)
    if rgb is None: continue
    encode(rgb, pts=frame_index)
    write_timestamp_row(frame_index, host_ns, cam_ns)
    frame_index += 1
finalize container + sidecar(status="completed")
```

Lifecycle:
- Owned by `CameraWindow`. Created on first record press, garbage-collected when stopped.
- Lives on its own `QThread`. Same pattern as the other three workers (`moveToThread`, `started → run`).

Why a thread, not a `QTimer` or per-frame Qt signal:
- Same reason as the existing pipeline (cf. `feedback_premature_pessimization.md`): ffmpeg/libx264 holds the GIL through chunks of work; we want it on its own thread so proc and GUI keep cycling.
- Qt-signal-per-frame would marshal every frame across the event loop. Not free; not needed.

---

## 4 — Metadata

Two tiers: session and run.

### `session.json` (written once at session start)

```json
{
  "session_id": "session_2026-05-08_14-23-05",
  "start_iso": "2026-05-08T14:23:05-07:00",
  "hostname": "AIR-STACKER-PC",
  "username": "GGG-User",
  "app": {
    "git_sha": "e53a0cc",
    "git_dirty": false,
    "version": "0.0.0",
    "python": "3.10.13",
    "platform": "Windows-10-..."
  },
  "camera": {
    "vendor": "FLIR",
    "model": "Flea3 FL3-U3-32S2C-CS",
    "serial": "12345678",
    "firmware": "1.16.0",
    "pixel_format": "BayerRG8",
    "width": 1600,
    "height": 1200
  }
}
```

### `run.json` (written at run start with `status="running"`, finalized on stop)

```json
{
  "run_id": "run_001_2026-05-08_14-25-12",
  "session_id": "session_2026-05-08_14-23-05",
  "start_iso": "2026-05-08T14:25:12.418-07:00",
  "end_iso": null,
  "status": "running",
  "video": {
    "path": "video.mp4",
    "codec": "libx264",
    "preset": "veryfast",
    "crf": 17,
    "pix_fmt": "yuv420p",
    "container_flags": ["frag_keyframe", "empty_moov"]
  },
  "fps_target": 60,
  "fps_achieved_acq": null,
  "fps_achieved_write": null,
  "frame_count_written": 0,
  "frame_count_dropped_queue": 0,
  "frame_count_dropped_encoder": 0,
  "camera_settings": {
    "gain_db": 0.0,
    "exposure_us": 8000,
    "frame_rate_hz": 59.98,
    "balance_ratio_red": 1.0,
    "balance_ratio_blue": 1.0,
    "gamma": 1.0,
    "auto_gain": false,
    "auto_exposure": false,
    "balance_white_auto": "Continuous"
  },
  "adjustments_at_start": {
    "brightness": 100, "contrast": 100, "saturation": 100,
    "r_range": [0, 255], "g_range": [0, 255], "b_range": [0, 255]
  },
  "adjustment_changes": [
    {"frame_index": 1234, "host_perf_ns": 18234567890, "snapshot": {...}}
  ],
  "disk_at_start_gb_free": 412.7,
  "disk_at_end_gb_free": null
}
```

Status values: `running`, `completed`, `aborted_disk_full`, `aborted_user`, `aborted_crash` (set on next launch by scanning for stale `running` runs).

### `timestamps.csv` (one row per encoded frame)

```
frame_index,host_perf_ns,camera_ts_ns
0,18230000000,143258000000000
1,18246690000,143274690000000
...
```

`camera_ts_ns` from `image.GetTimeStamp()` — preserved end-to-end. Critical for any downstream synchronization with stage / heater / external triggers.

Optional v0+: append a row to a session-level `events.csv` whenever the user toggles record, hits a key, or moves a slider.

---

## 5 — Filesystem layout

Default base: `~/AirStackerRecordings`. Override in `config.toml`:

```toml
[recording]
base_dir = "D:\\AirStackerRecordings"   # optional; ~/AirStackerRecordings if omitted
max_run_gb = 50                         # hard stop per run
free_space_floor_gb = 5                 # abort if disk would drop below this
```

3 sessions × 2 runs each:

```
~/AirStackerRecordings/
├── session_2026-05-08_09-12-44/
│   ├── session.json
│   ├── run_001_2026-05-08_09-13-02/
│   │   ├── run.json
│   │   ├── video.mp4
│   │   └── timestamps.csv
│   └── run_002_2026-05-08_09-21-18/
│       ├── run.json
│       ├── video.mp4
│       └── timestamps.csv
├── session_2026-05-08_14-23-05/
│   ├── session.json
│   ├── run_001_2026-05-08_14-25-12/
│   │   ├── run.json
│   │   ├── video.mp4
│   │   └── timestamps.csv
│   └── run_002_2026-05-08_14-32-40/
│       ├── run.json
│       ├── video.mp4
│       └── timestamps.csv
└── session_2026-05-09_10-04-19/
    ├── session.json
    ├── run_001_2026-05-09_10-05-07/
    │   └── ...
    └── run_002_2026-05-09_10-44-22/
        └── ...
```

Notes:
- Run numbering is per-session, zero-padded to 3 digits (`run_001` … `run_999`).
- Folder timestamps use a Windows-friendly format (`YYYY-MM-DD_HH-MM-SS`, no colons).
- Empty session dirs (the user opens the GUI but never records) are *not* created until the first record press — avoids littering the data drive on every launch.

---

## 6 — Failure modes

| Failure | Behavior |
|---|---|
| **Disk full at start** | `RecordingWorker` checks free space ≥ `free_space_floor_gb`. If not, aborts before opening the container; UI shows "disk full"; no run dir is created. |
| **Disk full mid-record** | Encoder write fails with `OSError(ENOSPC)`. Worker catches, finalizes container best-effort, sets sidecar `status=aborted_disk_full`, surfaces to UI via signal. UI auto-stops; user sees a clear toast. |
| **Frames dropped (queue overflow)** | `put_nowait` returns False; `frame_count_dropped_queue` increments. Recording continues. Sidecar reflects drops. UI shows a small drop counter while recording. |
| **Frames dropped (encoder underrun)** | Tracked separately as `frame_count_dropped_encoder` if the encoder ever signals it (rare with non-blocking put + bounded queue, but bookkeeping is cheap). |
| **Double-press the record button** | Button is disabled while transitioning into/out of "running" state. State machine: `idle → starting → running → stopping → idle`. Clicks during `starting`/`stopping` are ignored. |
| **App crash mid-record** | Fragmented MP4 + `empty_moov` means the partial file plays in VLC/ffmpeg without recovery. Sidecar is left at `status="running"`. On next launch, the GUI scans `<base>/*/*/run.json` for stale `running` and rewrites them to `aborted_crash` (best-effort `end_iso = mtime(video.mp4)`). |
| **Camera disconnect mid-record** | Acq emits `error`; proc stops getting frames; queue drains to empty; `RecordingWorker.take` returns `None` repeatedly. After ~5 s of starvation, worker self-stops, finalizes sidecar with `status=aborted_camera`, container closed cleanly. |
| **PySpin yields `IsIncomplete()` frames** | Already discarded in acq (existing behavior). Not enqueued. Not counted as dropped. |
| **Adjustments change during record** | Captured as a row in `adjustment_changes` with frame_index + perf_counter ns. Stream itself reflects the post-change LUTs from that frame on. |
| **Stop pressed while writer is still flushing** | UI button shows "stopping…" and is disabled until writer signals `finished`. Hard timeout 5 s; on timeout we mark sidecar `aborted_user` with a flag and force-quit the worker (last resort; should never happen with fragmented MP4). |
| **Two GUI instances racing the same base dir** | Each session dir is unique (host + PID + start TS), so collision is essentially impossible. We won't lock the base dir; if two GUIs are open, both record fine to their own session subtrees. |

---

## 7 — Edge cases / caps

- **Per-run hard cap**: `max_run_gb` (default 50). On reach, gracefully finalize and mark `status=aborted_size_cap`. UI offers a "start new run" prompt.
- **Free-space floor**: `free_space_floor_gb` (default 5). Periodic check (every ~5 s on the writer thread, off the hot path). On approach, emit a UI warning at 1.5× floor, then hard-stop at floor.
- **Long-run inode pressure**: not an issue — per run we have ≤4 files. No sequence directories.
- **Per-session dir at v0+**: no rotation. One run = one MP4. If users record for 10 hours they get one ~150 GB MP4. Acceptable, MP4 + fragmented headers handles arbitrary length.
- **Recording with adjustments active**: warn (non-blocking) in the UI on first record press of a session, just to make sure it's intentional. Persist the dismissal in `config.toml` if annoying.
- **Camera settings change mid-record**: out of scope for v0. Camera options panel could be disabled while recording, or changes logged to `adjustment_changes`-equivalent. Recommend disabled to avoid confusing the resulting video (frame rate or exposure changes mid-stream produce nasty artifacts).

---

## 8 — UI changes

In `_build_right_panel`:

- Replace the existing two-button `[Record] [Stop]` placeholder with a single toggle button. Label: `● Record` (idle) / `■ Stop (00:12)` (running, with a 1 Hz live duration tick).
- Below it: a tiny status line — `12.3 GB free · 0 dropped` while idle, `↓ 240 MB · 720 frames · 0 dropped` while running.
- Disable the button during `starting` / `stopping` transitions.
- Add a small "open recordings folder" link / button next to it that does `os.startfile(base_dir)` on Windows / `xdg-open` on Linux.

Threading invariants:
- All UI updates from `RecordingWorker` arrive via `QueuedConnection` signals — same pattern the histogram worker already uses.
- The button slot only mutates `_recorder` references on the GUI thread. Worker creation/teardown all happens here; the worker itself never touches Qt widgets.

---

## 9 — Config additions

```toml
[recording]
# Optional. Default: ~/AirStackerRecordings (expanduser'd).
base_dir = "~/AirStackerRecordings"

# Encoder. Default: try h264_qsv (Intel QuickSync) then fall back to libx264.
# Set codec = "libx264" to skip QSV detection (forces software encode).
codec = "auto"          # "auto" | "h264_qsv" | "libx264"
qsv_global_quality = 22 # used when codec resolves to h264_qsv
x264_preset = "veryfast"
x264_crf = 17           # used when codec resolves to libx264

# Limits.
max_run_gb = 50
free_space_floor_gb = 5
queue_size = 120

# Whether to disable the camera options panel while recording (recommended).
lock_camera_settings_during_record = true
```

All keys optional; defaults baked into a `RecordingDefaults` dataclass next to `OUR_DEFAULTS`.

---

## 10 — Out of scope for v0

- Stage / heater event logging into `events.csv` (worth a follow-up — would be very useful for synchronizing video to controller state).
- Auto-rotate runs at N minutes / N GB.
- Live preview decoupled from record resolution.
- ROI cropping for record.
- Lossless mode (FFV1/MKV) — flag-able later if anyone wants pixel-exact for offline analysis.
- Audio (none).
- Per-frame thumbnails / scrubber.
- Annotations / labels mid-run.

---

## 11 — Open questions

1. **PyAV install on Windows**: PyAV ships with bundled ffmpeg on Linux; Windows wheels exist but are larger. Acceptable to add to `pyproject.toml`? Alternative is `imageio-ffmpeg` (subprocess-based — simpler install, less control over PTS / flush behavior).
2. **NVENC availability**: do the air-stacker PC(s) have an Nvidia GPU? If yes, NVENC essentially eliminates the encode-CPU risk.
3. **Disk on the air-stacker PC**: I assumed worst-case (slow SATA SSD). If we know the actual drive (NVMe? spinning?) the budget gets tighter or looser.
4. **Lock camera settings during recording — yes by default?** Strongly recommended; surfacing to confirm. The current Camera Options panel can change exposure / gain on the fly mid-record, which produces ugly seams in the encoded stream.
5. **Should adjustment changes mid-record be allowed?** Recording adjusted frames means slider changes alter the recorded stream visibly. Two options: (a) lock adjustments while recording (simpler, predictable output); (b) allow + log changes (more expressive). I'd default to allow + log, but flag the trade.
6. **Run-level user note**: do we want a "label this run" textbox before/at recording start? Cheap to add; very useful for downstream filing. Could be a 1-line field that ends up in `run.json` as `"label": "RPL18 4K test"`.
7. **Codec sanity**: should we include a quick "test encode" preflight on first launch — encode 60 frames synthetically and warn if the machine can't sustain real-time? Catches the CPU-bottleneck case before users hit it during a real run.
8. **stop hotkey**: bind `Space` or `R` while the camera window has focus? Useful if the operator's hand is on the stage.
