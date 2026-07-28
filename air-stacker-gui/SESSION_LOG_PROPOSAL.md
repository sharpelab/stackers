# Air Stacker GUI — Session Log Proposal

Status: DRAFT — for review.
Companion to: `RECORDING_PROPOSAL.md` (the H.264 video stream).
Scope: persisting the full state-and-event timeline of a GUI session so that
recordings can be reconstructed, replayed, and consumed by an ML automation
pipeline.

---

## TL;DR

Per-stream native-rate tables, plus a sparse event log, plus a per-session
provenance file. No fixed-cadence snapshots — they're derivable post-hoc.

| File | Cadence | Shape | Source of truth for |
|---|---|---|---|
| `session.json` | once at start, finalized at end | object | provenance, hardware identity, calibration, code version, axis inventory |
| `events.jsonl` | sparse | append-only JSONL | commands, settings changes, lifecycle, errors, annotations |
| `axes.csv` | native (10 Hz today) | append-only CSV, long-format with `axis` column | per-axis position / state / error history |
| `heater.csv` | native (1 Hz today) | append-only CSV | heater process / setpoint / output history |
| `run_*/frames.csv` | native (~60 Hz) | append-only CSV | per-frame host + camera timestamps |
| `run_*/video.mp4` | ~60 Hz | H.264 (QSV) | post-adjustment video |
| `run_*/run.json` | once per run | object | run-scoped totals (frame counts, drops, codec settings) |

UI inputs are not captured — only their state-effects. Events distinguish
**commands** (intent issued) from **changes** (observed result), which is the
distinction an imitation-learning pipeline cares about.

---

## 1 — Goals

Ranked.

1. **ML automation training data.** Per-stream native rates, time-aligned to one monotonic clock. Long-format tables. Provenance pinned. Observations cleanly separable from actions.
2. **QC / audit / training aid for humans.** Same artifacts; human-greppable. `events.jsonl` reads like a session timeline.
3. **Scientific artifact.** Sessions are durable, reproducible, self-describing. Pair with the H.264 video to explain a capture months later.

Non-goal: replaying GUI gestures. UI events are not recorded.

---

## 2 — ML data-collection principles applied here

The schema is shaped by these (in order of how much they constrained the design):

1. **Native-rate per-stream over fixed-cadence wide snapshots.** Capture as fast as each source produces. Downsampling at training time is cheap; un-recording is impossible.
2. **One monotonic clock.** Every row of every stream stamped with `t = perf_counter() - session_t0`. Wall clock is metadata only.
3. **Long format, axis as a column.** New axes (we're adding more) appear as new values in the `axis` column — no new files, no schema change.
4. **Absolute state, not deltas.** `position = 12.84`, never `delta = +0.5`. Deltas are computable.
5. **Observations vs. actions split.** `*.command` (intent issued) vs. `*.changed` (state observed after the fact). Imitation learning needs both.
6. **Provenance pinned hard.** Hardware serials, firmware, code git SHA, calibration constants, units, axis directions — all in `session.json` at start.
7. **Failure annotations are training signal.** Drops, gaps, comm errors are first-class events, not log noise.
8. **Append-only, crash-tolerant.** CSV / JSONL on the hot path; convert to Parquet at session-end if downstream wants it.
9. **Human annotations welcome.** A small annotation API (`annotation.label`, `annotation.bookmark`) so operators can tag good/bad/interesting moments inline.

---

## 3 — Streams in detail

### 3.1 `session.json` — provenance + inventory

Written at session start, finalized at session end. Object, not list. Fields:

```json
{
  "schema_version": 1,
  "session_id": "session_2026-05-09_14-23-05",
  "start_iso": "2026-05-09T14:23:05.418-07:00",
  "end_iso": null,
  "status": "running",
  "host": {
    "hostname": "AIR-STACKER-PC",
    "username": "GGG-User",
    "platform": "Windows-10-...",
    "python": "3.10.13"
  },
  "app": {
    "name": "air-stacker-gui",
    "git_sha": "e53a0cc",
    "git_dirty": false,
    "version": "0.0.0"
  },
  "camera": {
    "vendor": "FLIR",
    "model": "Flea3 FL3-U3-32S2C-CS",
    "serial": "12345678",
    "firmware": "1.16.0",
    "width": 1600, "height": 1200,
    "pixel_format": "BayerRG8",
    "calibration": null
  },
  "axes": [
    {
      "name": "Rotation Stage",
      "controller": "Newport CONEX-CC",
      "port": "COM4",
      "address": 1,
      "units": "°",
      "step": 0.01,
      "poll_hz": 10,
      "firmware": "...",
      "serial": "...",
      "origin_offset": null,
      "direction": "+CCW"
    }
  ],
  "heater": {
    "controller": "Omega Platinum",
    "port": "COM7",
    "slave_id": 1,
    "units": "°C",
    "poll_hz": 1,
    "firmware": "..."
  },
  "runs": []
}
```

Notes:
- `calibration: null` placeholder — extend when we have intrinsics or stage→camera transforms.
- `origin_offset` / `direction` are placeholders for axis frames once we agree on conventions. Fine to ship as `null` in v0; the field exists so consumers can future-proof.
- Hardware serials / firmware: read from PySpin and from CONEX/heater on connect. Best-effort — null if unreadable.
- `runs` is appended to as runs complete; carries the run_id, start/end t, frame counts.

Status values: `running` | `completed` | `aborted_crash` (set on next launch by scanning for sessions left in `running`).

### 3.2 `events.jsonl` — sparse transitions

One JSON object per line. Fired only when something actually happens.

```jsonl
{"t":     0.000, "wall":"2026-05-09T14:23:05.418-07:00", "name":"session.start", "data":{"schema_version":1}}
{"t":     0.012, "name":"camera.changed",        "data":{"changed":["gain_db","exposure_us","frame_rate_hz"], "gain_db":0.0, "exposure_us":8000, "frame_rate_hz":59.98}}
{"t":     0.013, "name":"adjustments.changed",   "data":{"brightness":100, "contrast":100, "saturation":100, "r_range":[0,255], "g_range":[0,255], "b_range":[0,255]}}
{"t":    18.421, "name":"adjustments.changed",   "data":{"brightness":110, "contrast":100, "saturation":100, "r_range":[0,255], "g_range":[0,255], "b_range":[0,255]}}
{"t":    24.005, "name":"axis.command",          "axis":"Rotation Stage", "data":{"target":12.840, "kind":"absolute"}}
{"t":    24.612, "name":"axis.move.done",        "axis":"Rotation Stage", "data":{"position":12.840, "state":"READY"}}
{"t":    72.500, "name":"heater.setpoint.command","data":{"from":25.0, "to":30.0}}
{"t":   120.500, "name":"recording.start",       "data":{"run_id":"run_001_2026-05-09_14-25-05", "video":"run_001_…/video.mp4", "codec":"h264_qsv"}}
{"t":   720.500, "name":"recording.stop",        "data":{"run_id":"run_001_…", "frames_written":36000, "frames_dropped_queue":0, "end_status":"completed"}}
{"t":   903.211, "name":"error.camera",          "data":{"message":"GetNextImage timeout", "kind":"transient"}}
{"t":  1100.000, "name":"annotation.bookmark",   "data":{"label":"good exfoliation", "note":"flake landed clean"}}
{"t":  1800.000, "name":"session.end",           "data":{"runs":1, "events":24}}
```

Conventions:
- `t` = `perf_counter() - session_t0`. Consistent across all streams.
- `wall` only on `session.start`, `session.end`, and a periodic `time.checkpoint` event (every 60 s) for drift detection.
- `name` is dotted: `<source>.<verb>` or `<source>.<noun>`.
- Multi-instance sources (axes) carry an `axis` field alongside `data`.

Taxonomy:

| Family | Examples | Meaning |
|---|---|---|
| `session.*` | `session.start`, `session.end`, `time.checkpoint` | Lifecycle |
| `*.command` | `axis.command`, `heater.setpoint.command` | **Action** issued (intent) |
| `*.changed` | `adjustments.changed`, `camera.changed` | **Observation** that a setting moved (full new state in payload) |
| `*.done` / `*.result` / `*.timeout` | `axis.move.done` | Command outcome |
| `recording.*` | `recording.start`, `recording.stop`, `recording.error` | Video lifecycle |
| `error.*` / `warning.*` | `error.camera`, `warning.serial` | Logged exceptions / WARN+ |
| `annotation.*` | `annotation.bookmark`, `annotation.label`, `annotation.note` | Operator-or-script-emitted labels |

What's NOT here: UI events. The state-change events tell automation everything it needs.

### 3.3 `axes.csv` — per-axis poll history (long format)

One row per `PollWorker` tick, across all axes. Single file by design — a new axis joins as a new value in the `axis` column, no schema change.

```csv
t,axis,position,state,error
0.105,Rotation Stage,12.340,READY,
0.105,XY Stage,5.670,READY,
0.205,Rotation Stage,12.340,READY,
0.205,XY Stage,5.671,MOVING,
0.305,Rotation Stage,12.340,READY,
...
```

- `t` is the perf-counter timestamp of the read, taken at the moment the controller's response is received.
- `state` is the raw CONEX state symbol (`READY`, `MOVING`, `DISABLED`, etc.).
- `error` is empty on success; carries the controller's error code/string if the poll failed but produced a partial read.
- Native rate: 10 Hz today (`POLL_MS = 100`). New axes can run at different rates; rows just interleave by `t`.
- A missed poll (controller timeout, serial dropout) emits an `error.axis` event in `events.jsonl` and *does not* write a CSV row. Consumers detect gaps from `t` deltas.

For axes with a fundamentally different schema (e.g., a future 6-DOF stage with multiple position fields), add columns and leave them empty for axes that don't use them. If the schema mismatch gets ugly, split that axis into its own file (`force_sensor.csv`) and add to the `session.json` axis inventory with a `file` pointer. v0 keeps everything in `axes.csv`.

### 3.4 `heater.csv` — heater poll history

Separate file (different schema from axes — setpoint / process / output, not position / state).

```csv
t,setpoint_c,process_c,output_pct,connected
1.000,25.0,24.81,0.0,true
2.000,25.0,24.83,0.0,true
3.000,25.0,24.84,0.0,true
...
```

- Native 1 Hz (`poll_interval_ms = 1000`).
- `connected = false` row when the serial port drops; surface via `error.heater` event too.
- Missed polls → no row; gap detectable from `t` deltas.

### 3.5 Per-run `frames.csv` — frame timestamps

Run-scoped, in `run_NNN_<TS>/frames.csv`. ~60 Hz, dominates the rest combined; lives with its run rather than at session level.

```csv
frame_index,t,host_perf_ns,cam_ts_ns,exposure_us,gain_db
0,120.512,120512000000,143258000000000,8000,0.0
1,120.529,120528690000,143274690000000,8000,0.0
...
```

- `t` consistent with the session monotonic clock. `host_perf_ns` is the same value × 1e9 (kept for precision-sensitive consumers).
- `cam_ts_ns` from `image.GetTimeStamp()` — camera-side hardware clock. Critical if we ever hardware-trigger.
- `exposure_us`, `gain_db`: pulled from cached camera state at frame time. Lets a training pipeline correlate exposure with frame content without joining to events.

---

## 4 — Source tap points

Where each piece of data comes from in the existing code:

| Data | Origin | Tap |
|---|---|---|
| `events: camera.changed` | `CameraOptionsPanel` slider/checkbox handlers | Already mutate via `_node_*_set`; emit event after the set returns true |
| `events: adjustments.changed` | `ImageAdjustments.update()` / `reset()` | Tap the methods directly; the panel already calls them |
| `events: axis.command`, `axis.move.done` | `ConexAxisPanel.move_*` methods | Wrap the move call: emit command before, done/timeout after settle |
| `events: heater.setpoint.command` | `HeaterPanel` setpoint write | Already a discrete UI action |
| `events: recording.*` | new `RecordingWorker` | Lifecycle hooks |
| `events: error.*`, `warning.*` | `logging.Logger("airstacker")` | Custom `logging.Handler` that funnels WARN+ to events writer |
| `events: annotation.*` | new annotation widget (textbox + bookmark hotkey) | UI hook → logger.event |
| `axes.csv` rows | `PollWorker` for each `ConexAxis` | Existing poll callback also writes to logger |
| `heater.csv` rows | `OmegaPlatinum` poll loop | Existing poll callback also writes to logger |
| `frames.csv` rows | `RecordingWorker` per encoded frame | Already required for v0 |

Implementation pattern: a `SessionLogger` on `CameraWindow`, passed by reference to the panels and workers that produce data. All writes go through it; it queues them to a writer thread.

---

## 5 — Threading / I/O

```
main thread + worker threads
         │  logger.event(name, data)            (non-blocking queue.put_nowait)
         │  logger.poll("axes", row)            (non-blocking queue.put_nowait)
         │  logger.poll("heater", row)          (non-blocking queue.put_nowait)
         ▼
     SessionLogger queue (bounded, e.g. 8192 records)
         │
         ▼
     SessionLoggerWorker (own QThread)
         - dispatches each record by stream → matching writer (events.jsonl, axes.csv, heater.csv)
         - line-buffered, fsync every ~1 s
         - flush + close on session.end
```

- Time stamping at call site (`t = perf_counter() - t0`) so queue latency doesn't smear.
- One writer thread, multiple file handles. fsync interleaves; cost negligible at our rates.
- Bounded queue: overflow is essentially impossible at our event rates (events ≪ 100/s, axes 10/s, heater 1/s). If it ever happens, drop with a counter and emit `logger.dropped` when there's room.
- Writer thread is the only thing that touches files. No fsync on the GUI / acq / proc threads.

---

## 6 — Filesystem layout

```
~/AirStackerRecordings/
└── session_2026-05-09_14-23-05/
    ├── session.json
    ├── events.jsonl
    ├── axes.csv
    ├── heater.csv
    ├── run_001_2026-05-09_14-25-12/
    │   ├── run.json
    │   ├── video.mp4
    │   └── frames.csv
    └── run_002_2026-05-09_14-32-40/
        └── ...
```

Session dir is created lazily on first event after `session.start` (avoids littering on launch-and-quit).

---

## 7 — Consumption

For an automation / ML pipeline:

```python
import pandas as pd
import json

axes  = pd.read_csv("axes.csv")
heat  = pd.read_csv("heater.csv")
frames = pd.read_csv("run_001_…/frames.csv")
events = [json.loads(l) for l in open("events.jsonl")]

# What was the rotation stage doing at each video frame?
rot = axes[axes["axis"] == "Rotation Stage"].sort_values("t")
frames["rotation_at_frame"] = pd.merge_asof(
    frames.sort_values("t"), rot[["t", "position"]],
    on="t", direction="backward"
)["position"]

# Which frames were inside a recording?
rec_starts = [e for e in events if e["name"] == "recording.start"]
rec_stops  = [e for e in events if e["name"] == "recording.stop"]
```

For a human:

```bash
jq -r '"\(.t)  \(.name)  \(.data)"' events.jsonl | less
```

For converting to Parquet at session end (optional, post-shutdown step):

```python
pd.read_csv("axes.csv").to_parquet("axes.parquet")
# events.jsonl → parquet via json_normalize
```

---

## 8 — Failure modes

| Failure | Behavior |
|---|---|
| App crash | All files valid up to last fsync. `session.json` left in `status="running"` → next launch marks `aborted_crash`. Rows in `axes.csv` / `heater.csv` are line-atomic; no partial rows after crash. |
| Disk full | Writer catches `OSError`, surfaces to UI, logging goes silent rather than crashing the app. |
| Source goes silent (camera disconnect, heater serial drop) | No new rows. `error.*` event names the cause. Consumers detect via `t` deltas. |
| Logger queue overflow | Drop with counter; emit `logger.dropped` event when drained. Should never happen at our rates. |
| Two GUI instances | Each gets its own session dir (timestamp + PID). No locking. |
| New axis added mid-session | Not supported in v0 — `session.json` axis inventory is fixed at start. (Adding a new axis means restarting the GUI.) |

---

## 9 — Out of scope for v0

- **Pre-record buffer.** Common ML capture trick: keep last N seconds in RAM, persist on a hotkey. Worth doing later.
- **Phase / state-machine annotations.** "Calibrating" / "scanning" / "exfoliating" mode tags. Inferable post-hoc; explicit is better but not required for v0.
- **Raw-video stream.** Recording is post-adjustment. For ML training, raw post-debayer is often more useful — adjustments are display LUTs that bake in a transform the model could otherwise learn. Worth re-litigating; covered in *Open questions*.
- **Image features per frame** (focus, brightness, edges). Derivable later.
- **Hardware-trigger sync metadata.** Camera is free-running today.
- **Auto-conversion to Parquet at session end.** Trivial post-process; can ship as a separate script.

---

## 10 — Open questions

1. **Raw-video stream alongside the adjusted MP4?** ML training usually wants the un-LUT'd sensor data. Two streams (`raw.mp4` + `adjusted.mp4`) would require encoding both — ~2× the encoder load (QSV can handle it; libx264 can't at 60 fps). Or: drop the adjusted stream entirely and let display-LUTs replay from `events.jsonl` at consumption time. This is a recording-proposal-level decision, not session-log-level.
2. **Annotation API surface.** Hotkey + textbox? Right-click "bookmark this moment"? Both? What labels? Free-form vs. enum?
3. **Camera-state-per-frame in `frames.csv`.** I included `exposure_us` / `gain_db` per row. Worth doing because they can change mid-record and a training pipeline needs them. Add more (gamma, balance ratios) at the same cost. Where do we cut?
4. **Time-checkpoint cadence.** Proposed every 60 s. Larger? Smaller? (Only matters for wall-clock drift, which is nanoseconds per minute.)
5. **Logging-handler bridge.** Funnel `logging.WARN+` into `events.jsonl` as `error.*`/`warning.*`? Yes by default unless you disagree.
6. **Annotation file split.** Keep annotations in `events.jsonl`, or split into `annotations.jsonl`? I'd argue keep in events — they're sparse and time-correlated with everything else.
7. **Privacy.** `host`/`username` end up in `session.json`. Lab-only, no concern today; flag for future cloud-sync.
8. **Schema versioning.** `schema_version: 1` in `session.json`. No formal contract beyond that for v0; freeze when a downstream tool actually depends on it.

---

## 11 — Relationship to `RECORDING_PROPOSAL.md`

This proposal supersedes:
- The earlier `session.jsonl` sketch (now: `events.jsonl` + per-stream CSVs).
- Fixed-cadence snapshots (dropped — derivable from per-stream native-rate logs).
- The `ui.slider` / `ui.button` event family (dropped — UI inputs not captured).

`run.json` (per-run) keeps its shape from the recording proposal but gets slimmer: most context lives in the session-level streams.

The encoder choice (QSV) is updated in `RECORDING_PROPOSAL.md` directly.
