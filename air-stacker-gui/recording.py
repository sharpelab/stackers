"""Video-recording core for the Air Stacker GUI (v0 of RECORDING_PROPOSAL.md).

Encodes post-adjustment RGB frames to H.264 in fragmented MP4 via PyAV,
with a per-run sidecar ``run.json`` and per-frame ``timestamps.csv``.
Plain-Python pieces (:class:`RunWriter`, :class:`RecordQueue`, the dir
helpers) have no Qt dependency; only :class:`RecordingWorker` is a
QObject, following the same moveToThread pattern as the workers in
``main.py``.

Settings come from the ``[recording]`` table in ``config.toml`` via
:meth:`RecordingConfig.from_toml`. Expected keys (all optional):

  - ``base_dir`` (str, default ``"~/AirStackerRecordings"``, expanduser'd)
  - ``codec`` (str, default ``"auto"``) — ``"auto"`` | ``"h264_qsv"`` |
    ``"libx264"``. Auto probes QuickSync and falls back to software.
  - ``qsv_global_quality`` (int, default 22) — QSV quality scale
  - ``x264_preset`` (str, default ``"veryfast"``)
  - ``x264_crf`` (int, default 17)
  - ``max_run_gb`` (float, default 50) — hard stop per run
  - ``free_space_floor_gb`` (float, default 5) — abort below this
  - ``queue_size`` (int, default 120) — record-queue depth (~2 s @ 60 fps)
  - ``record_fps`` (float, default 0 = record every frame) — optional
    pacing target: frames arriving faster than this are skipped *by
    design* (counted separately from queue drops). Useful for smaller
    files / timelapse-style runs; the zero-copy write path sustains
    full camera rate on the rig (~194 fps capability at 1600×1200,
    bench 2026-07-28).

Filesystem layout (Windows-safe names, no colons)::

    <base>/session_<YYYY-MM-DD_HH-MM-SS>/
        session.json
        run_NNN_<YYYY-MM-DD_HH-MM-SS>/
            video.mp4
            run.json
            timestamps.csv
"""

from __future__ import annotations

import dataclasses
import errno
import io
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np
from av.video.stream import VideoStream
from PySide6.QtCore import QObject, Signal, Slot

log = logging.getLogger("airstacker.recording")

# Fragmented MP4: a crash truncates to a still-playable file instead of
# leaving an MP4 with no moov atom.
_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof"
_CONTAINER_FLAGS = ["frag_keyframe", "empty_moov", "default_base_moof"]

# Hardware encoders want their native layout; libx264 takes planar 4:2:0.
_PIX_FMT = {"h264_qsv": "nv12", "libx264": "yuv420p"}

_TS_FORMAT = "%Y-%m-%d_%H-%M-%S"  # Windows-safe: no colons

_VALID_CODECS = ("auto", "h264_qsv", "libx264")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json_default(obj: object) -> object:
    """Serialize sidecar payloads we don't control (adjustment snapshots
    are frozen dataclasses; camera settings may carry numpy scalars)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    asdict_fn = getattr(obj, "_asdict", None)  # namedtuple
    if callable(asdict_fn):
        return asdict_fn()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


def _write_json_atomic(path: Path, doc: dict) -> None:
    """Write tmp + os.replace so a crash mid-write never leaves a
    truncated run.json."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(doc, indent=2, default=_json_default) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


@dataclass(frozen=True)
class RecordingConfig:
    base_dir: Path
    codec: str = "auto"  # "auto" | "h264_qsv" | "libx264"
    qsv_global_quality: int = 22
    x264_preset: str = "veryfast"
    x264_crf: int = 17
    max_run_gb: float = 50.0
    free_space_floor_gb: float = 5.0
    queue_size: int = 120
    record_fps: float = 0.0

    @classmethod
    def from_toml(cls, section: dict | None) -> "RecordingConfig":
        """Tolerant parse of the ``[recording]`` table. Unknown keys are
        ignored; malformed values fall back to defaults with a warning."""
        section = dict(section or {})

        def _num(key: str, cast, default):
            if key not in section:
                return default
            try:
                return cast(section[key])
            except (TypeError, ValueError):
                log.warning("[recording] %s=%r is not a number; using %r",
                            key, section[key], default)
                return default

        base_dir = Path(
            str(section.get("base_dir", "~/AirStackerRecordings"))
        ).expanduser()
        codec = str(section.get("codec", "auto")).lower()
        if codec not in _VALID_CODECS:
            log.warning("[recording] codec=%r unknown; using 'auto'", codec)
            codec = "auto"
        return cls(
            base_dir=base_dir,
            codec=codec,
            qsv_global_quality=_num("qsv_global_quality", int, 22),
            x264_preset=str(section.get("x264_preset", "veryfast")),
            x264_crf=_num("x264_crf", int, 17),
            max_run_gb=_num("max_run_gb", float, 50.0),
            free_space_floor_gb=_num("free_space_floor_gb", float, 5.0),
            queue_size=_num("queue_size", int, 120),
            record_fps=_num("record_fps", float, 0.0),
        )


class RecordQueue:
    """Bounded FIFO of ``(rgb_frame, host_ns)`` with drop accounting.

    Distinct from the GUI's latest-wins FrameMailbox: recording wants
    *every* frame, so this is a real deque with a hard cap. put_nowait
    runs on the camera proc hot path at 60 fps — it never blocks; on
    overflow the frame drops here and the counter ticks.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = max(1, int(maxsize))
        self._items: deque[tuple[np.ndarray, int]] = deque()
        self._cond = threading.Condition()
        self._dropped = 0
        self._woken = False

    def put_nowait(self, frame: np.ndarray, host_ns: int) -> bool:
        with self._cond:
            if len(self._items) >= self._maxsize:
                self._dropped += 1
                return False
            self._items.append((frame, host_ns))
            self._cond.notify()
        return True

    def take(self, timeout: float) -> tuple[np.ndarray, int] | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._items:
                if self._woken:
                    self._woken = False  # one-shot, like an Event we clear
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._items.popleft()

    def wake(self) -> None:
        """Unblock a pending take() — used to break the wait at stop."""
        with self._cond:
            self._woken = True
            self._cond.notify_all()

    @property
    def dropped_count(self) -> int:
        with self._cond:
            return self._dropped


def _probe_encoder(codec: str, options: dict[str, str], pix_fmt: str) -> None:
    """Open a tiny in-memory encoder and push one frame through it.

    Encoder open is lazy in PyAV (happens at first encode), so we must
    actually encode a frame — merely adding the stream would report
    QSV "available" on boxes with no QuickSync device. Raises on failure.
    """
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mp4") as container:
        stream = container.add_stream(codec, rate=30)
        assert isinstance(stream, VideoStream)  # h264 codecs are video
        stream.width = 128
        stream.height = 96
        stream.pix_fmt = pix_fmt
        stream.codec_context.options = dict(options)
        frame = av.VideoFrame.from_ndarray(
            np.zeros((96, 128, 3), dtype=np.uint8), format="rgb24"
        ).reformat(format=pix_fmt)
        frame.pts = 0
        for packet in stream.encode(frame):
            container.mux(packet)


def select_codec(cfg: RecordingConfig) -> tuple[str, dict[str, str]]:
    """Resolve the configured codec to ``(name, encoder_options)``.

    ``"auto"`` probes h264_qsv and falls back to libx264 on any failure.
    Explicit ``"h264_qsv"`` still probes so a missing QuickSync device
    surfaces at record start (RuntimeError), not mid-run. Explicit
    ``"libx264"`` skips probing.
    """
    # look_ahead=0: global_quality alone enables LA-ICQ, which kills the
    # rig's Intel driver mid-stream after ~20 s of sustained encode
    # (MFX_ERR_DEVICE_FAILED -17, isolated 2026-07-28). Plain ICQ is
    # stable at the same quality setting.
    qsv_opts = {
        "global_quality": str(cfg.qsv_global_quality),
        "look_ahead": "0",
    }
    x264_opts = {"preset": cfg.x264_preset, "crf": str(cfg.x264_crf)}
    codec = cfg.codec if cfg.codec in _VALID_CODECS else "auto"

    if codec == "libx264":
        return "libx264", x264_opts
    try:
        _probe_encoder("h264_qsv", qsv_opts, _PIX_FMT["h264_qsv"])
    except Exception as e:  # noqa: BLE001 — av raises assorted FFmpegError types
        if codec == "h264_qsv":
            raise RuntimeError(f"h264_qsv requested but unusable: {e}") from e
        log.info("h264_qsv probe failed (%s); falling back to libx264", e)
        return "libx264", x264_opts
    return "h264_qsv", qsv_opts


def free_space_gb(path: Path) -> float:
    """Free space (decimal GB) on the filesystem holding ``path``.

    Walks up to the nearest existing ancestor so it works before the
    directory has been created.
    """
    p = Path(path)
    while not p.exists():
        parent = p.parent
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free / 1e9


def new_session_dir(base: Path, metadata: dict) -> Path:
    """Create ``base/session_<TS>/`` + ``session.json`` from metadata.

    Two GUIs launched in the same second collide on the timestamp name;
    the loser gets a numeric suffix.
    """
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    stem = f"session_{time.strftime(_TS_FORMAT)}"
    path = base / stem
    n = 2
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            path = base / f"{stem}_{n}"
            n += 1
    doc = dict(metadata)
    doc.setdefault("session_id", path.name)
    doc.setdefault("start_iso", _now_iso())
    _write_json_atomic(path / "session.json", doc)
    log.info("session dir %s", path)
    return path


def new_run_dir(session_dir: Path) -> Path:
    """Create ``session_dir/run_NNN_<TS>/``, NNN continuing from the
    highest existing run number in the session."""
    session_dir = Path(session_dir)
    highest = 0
    for entry in session_dir.iterdir():
        m = re.match(r"run_(\d{3})_", entry.name)
        if m and entry.is_dir():
            highest = max(highest, int(m.group(1)))
    path = session_dir / f"run_{highest + 1:03d}_{time.strftime(_TS_FORMAT)}"
    path.mkdir()
    return path


def mark_stale_runs_aborted(base: Path) -> int:
    """Launch-time crash scan: rewrite any ``*/*/run.json`` left at
    status="running" to "aborted_crash" (end_iso best-effort from the
    video.mp4 mtime). Returns the number rewritten. Never raises."""
    count = 0
    try:
        base = Path(base)
        if not base.is_dir():
            return 0
        for run_json in base.glob("*/*/run.json"):
            try:
                doc = json.loads(run_json.read_text(encoding="utf-8"))
                if doc.get("status") != "running":
                    continue
                doc["status"] = "aborted_crash"
                video = run_json.parent / "video.mp4"
                if doc.get("end_iso") is None and video.exists():
                    doc["end_iso"] = (
                        datetime.fromtimestamp(video.stat().st_mtime)
                        .astimezone()
                        .isoformat(timespec="milliseconds")
                    )
                _write_json_atomic(run_json, doc)
                count += 1
                log.warning("stale run marked aborted_crash: %s", run_json.parent)
            except Exception:
                log.exception("crash scan: could not process %s", run_json)
    except Exception:
        log.exception("crash scan failed under %s", base)
    return count


class RunWriter:
    """Owns one run dir: video.mp4 + run.json + timestamps.csv.

    Plain Python, no Qt. Constructed on the GUI thread (no I/O in
    __init__); open()/write_frame()/close() are called from the
    recording thread only. ``frame_count_dropped_queue`` is a plain
    attribute the worker fills from the queue before close().
    """

    _BYTES_CACHE_S = 1.0

    def __init__(
        self,
        run_dir: Path,
        cfg: RecordingConfig,
        width: int,
        height: int,
        fps_hint: float,
        metadata: dict,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.cfg = cfg
        self.frame_count_dropped_queue = 0
        self.frame_count_skipped_pacing = 0
        self._width = int(width)
        self._height = int(height)
        self._fps = self._clamp_fps(fps_hint)
        self._metadata = dict(metadata)
        self._video_path = self.run_dir / "video.mp4"
        self._container: av.container.OutputContainer | None = None
        self._stream: VideoStream | None = None
        self._csv = None
        self._codec_name = ""
        self._codec_opts: dict[str, str] = {}
        self._pix_fmt = ""
        self._frame_count = 0
        self._adjustment_changes: list[dict] = []
        self._start_iso = ""
        self._closed = False
        self._opened = False
        self._disk_at_start_gb: float | None = None
        self._bytes_cached = 0
        self._bytes_cached_at = 0.0

    @staticmethod
    def _clamp_fps(fps_hint: float) -> int:
        try:
            fps = round(float(fps_hint))
        except (TypeError, ValueError):
            return 60
        if not math.isfinite(fps_hint) or fps < 1:
            return 60
        return min(fps, 120)

    def open(self) -> None:
        self._codec_name, self._codec_opts = select_codec(self.cfg)
        pix_fmt = _PIX_FMT[self._codec_name]
        self._pix_fmt = pix_fmt
        # write_frame's I420 layout math and 4:2:0 subsampling both
        # need even dimensions; every Flea3 mode (binned or not) is.
        assert self._width % 2 == 0 and self._height % 2 == 0
        self._container = av.open(
            str(self._video_path),
            mode="w",
            format="mp4",
            options={"movflags": _MOVFLAGS},
        )
        stream = self._container.add_stream(self._codec_name, rate=self._fps)
        assert isinstance(stream, VideoStream)  # h264 codecs are video
        stream.width = self._width
        stream.height = self._height
        stream.pix_fmt = pix_fmt
        ctx = stream.codec_context
        opts = dict(self._codec_opts)
        # Explicit 2 s GOP for both codecs. frag_keyframe cuts a
        # fragment per IDR, so this bounds both crash loss and the
        # on-disk size staleness (bytes_written) to ~2 s — encoder
        # default GOPs are long/adaptive enough that video.mp4 stayed
        # at 0 bytes for an entire run.
        opts["g"] = str(max(1, int(2 * self._fps)))
        ctx.options = opts
        if self._codec_name == "libx264":
            # PyAV defaults to a single encode thread; x264 scales well
            # across cores and needs them at this resolution.
            ctx.thread_count = 0  # auto
        ctx.time_base = Fraction(1, self._fps)
        # Tag BT.601 / limited range explicitly — cv2's RGB→I420 in
        # write_frame uses the 601 matrix, and the tags must match the
        # actual conversion (encoder defaults are "unspecified", which
        # makes players guess).
        ctx.color_primaries = 6  # smpte170m
        ctx.color_trc = 6
        ctx.colorspace = 6
        ctx.color_range = 1  # MPEG / limited
        self._stream = stream

        # v0 records host perf_counter_ns only — camera timestamps
        # (image.GetTimeStamp()) are deliberately omitted; the proposal's
        # camera_ts_ns column returns when the acq tap forwards them.
        self._csv = open(
            self.run_dir / "timestamps.csv", "w", encoding="utf-8", newline=""
        )
        self._csv.write("frame_index,host_perf_ns\n")

        self._start_iso = _now_iso()
        self._disk_at_start_gb = free_space_gb(self.run_dir)
        self._opened = True
        _write_json_atomic(self.run_dir / "run.json", self._run_doc("running"))
        log.info(
            "run %s open: %s %s %dx%d @ %d fps",
            self.run_dir.name, self._codec_name, self._codec_opts,
            self._width, self._height, self._fps,
        )

    def _run_doc(self, status: str) -> dict:
        # Caller metadata (camera_settings, adjustments_at_start,
        # session_id, …) lands verbatim; core fields win on collision.
        doc = dict(self._metadata)
        doc.update(
            {
                "run_id": self.run_dir.name,
                "session_id": self._metadata.get("session_id"),
                "start_iso": self._start_iso,
                "end_iso": None if status == "running" else _now_iso(),
                "status": status,
                "video": {
                    "path": "video.mp4",
                    "codec": self._codec_name,
                    "options": self._codec_opts,
                    "pix_fmt": _PIX_FMT.get(self._codec_name),
                    "container_flags": _CONTAINER_FLAGS,
                },
                "fps_target": self._fps,
                "frame_count_written": self._frame_count,
                "frame_count_dropped_queue": self.frame_count_dropped_queue,
                "frame_count_skipped_pacing": self.frame_count_skipped_pacing,
                "frame_count_dropped_encoder": 0,
                "adjustment_changes": self._adjustment_changes,
                # Captured once at open() — recomputing here would make
                # the close-time rewrite report end-of-run disk as
                # "at start", losing the consumption delta.
                "disk_at_start_gb_free": self._disk_at_start_gb,
                "disk_at_end_gb_free": (
                    None if status == "running" else free_space_gb(self.run_dir)
                ),
            }
        )
        return doc

    def write_frame(self, rgb: np.ndarray, host_ns: int) -> None:
        # Only callable between open() and close() — narrow for the checker.
        assert self._stream is not None and self._container is not None
        assert self._csv is not None
        if not rgb.flags["C_CONTIGUOUS"]:
            rgb = np.ascontiguousarray(rgb)
        # cv2 SIMD conversion into a fresh numpy buffer + zero-copy
        # AVFrame wrap. The from_ndarray + swscale-reformat path cost
        # ~36 ms/frame on the rig (per-call AVFrame alloc + single-
        # threaded swscale, bench 2026-07-28 in TODO.md); this is ~5 ms.
        # Fresh buffer per frame, never reused — the encoder holds
        # references to submitted frames (x264 lookahead, QSV async),
        # so a recycled buffer would be mutated under it.
        # astype(copy=False) is a no-op at runtime (already uint8); it
        # narrows cv2's loosely-typed return for the checker.
        i420 = cv2.cvtColor(rgb, cv2.COLOR_RGB2YUV_I420).astype(
            np.uint8, copy=False
        )
        if self._pix_fmt == "nv12":
            # QSV wants NV12: same Y plane, U/V interleaved. Cheap
            # strided shuffle vs. swscale's full reconversion.
            h, w = self._height, self._width
            nv12 = np.empty_like(i420)
            nv12[:h] = i420[:h]
            u = i420[h : h + h // 4].reshape(h // 2, w // 2)
            v = i420[h + h // 4 :].reshape(h // 2, w // 2)
            uv = nv12[h:].reshape(h // 2, w)
            uv[:, 0::2] = u
            uv[:, 1::2] = v
            frame = av.VideoFrame.from_numpy_buffer(nv12, format="nv12")
        else:
            frame = av.VideoFrame.from_numpy_buffer(i420, format="yuv420p")
        # CFR: pts is just the frame index in a 1/fps time base.
        frame.pts = self._frame_count
        frame.time_base = Fraction(1, self._fps)
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        self._csv.write(f"{self._frame_count},{host_ns}\n")
        self._frame_count += 1

    def note_adjustments(self, snapshot: object) -> None:
        self._adjustment_changes.append(
            {
                "frame_index": self._frame_count,
                "host_ns": time.perf_counter_ns(),
                "snapshot": snapshot,
            }
        )

    def close(self, status: str) -> None:
        """Flush + finalize everything, best-effort; idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._stream is not None and self._container is not None:
            try:
                for packet in self._stream.encode(None):
                    self._container.mux(packet)
            except Exception:
                log.exception("encoder flush failed (%s)", self.run_dir.name)
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                log.exception("container close failed (%s)", self.run_dir.name)
        if self._csv is not None:
            try:
                self._csv.close()
            except Exception:
                log.exception("timestamps.csv close failed (%s)", self.run_dir.name)
        if self._opened:
            try:
                _write_json_atomic(self.run_dir / "run.json", self._run_doc(status))
            except Exception:
                log.exception("run.json finalize failed (%s)", self.run_dir.name)
        log.info(
            "run %s closed: status=%s frames=%d dropped=%d",
            self.run_dir.name, status, self._frame_count,
            self.frame_count_dropped_queue,
        )

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def bytes_written(self) -> int:
        """Size of video.mp4 on disk, stat cached ~1 s (called from the
        progress tick and the size-cap check)."""
        now = time.monotonic()
        if now - self._bytes_cached_at >= self._BYTES_CACHE_S:
            self._bytes_cached_at = now
            try:
                self._bytes_cached = os.stat(self._video_path).st_size
            except OSError:
                self._bytes_cached = 0
        return self._bytes_cached


class RecordingWorker(QObject):
    """Consumes a RecordQueue on its own QThread, drives a RunWriter.

    Same moveToThread pattern as the other workers in ``main.py``:
    ``thread.started -> run``, GUI connects the signals queued. The GUI
    owns the lifecycle and calls :meth:`stop` explicitly — there is no
    starvation self-stop in v0. run() never propagates an exception.
    """

    started_run = Signal(str)           # run dir path, after open() succeeds
    progress = Signal(int, int, float)  # frames_written, dropped, mb_written
    finished = Signal(str, str)         # (status, human message)

    DISK_CHECK_S = 5.0
    PROGRESS_S = 1.0
    DRAIN_S = 2.0  # bound on post-stop queue drain

    def __init__(
        self,
        queue: RecordQueue,
        writer: RunWriter,
        state_probe: Callable[[], object] | None = None,
    ) -> None:
        super().__init__()
        self._queue = queue
        self._writer = writer
        self._probe = state_probe
        self._stop_event = threading.Event()
        self._stop_status = "completed"
        # Pacing: skip frames arriving faster than record_fps. The
        # air-stacker PC can't push full camera rate through the
        # convert+encode path (~25 fps ceiling at 1600×1200), so v0
        # records a paced subset by design rather than dropping at the
        # queue under pressure.
        fps = writer.cfg.record_fps
        self._pace_interval_ns = int(1e9 / fps) if fps and fps > 0 else 0
        self._next_due_ns = 0
        self._skipped_pacing = 0

    @Slot()
    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001 — worker must die clean, never propagate
            log.exception("recording worker crashed")
            self._close_best_effort("error")
            self.finished.emit("error", f"Recording failed: {e}")

    def _run(self) -> None:
        writer = self._writer
        try:
            writer.open()
        except Exception as e:  # noqa: BLE001
            log.exception("could not start recording")
            self._close_best_effort("error")
            self.finished.emit("error", f"Could not start recording: {e}")
            return
        self.started_run.emit(str(writer.run_dir))

        # Baseline for change detection — only *changes* after start are
        # logged; the start state is already in metadata.
        prev_state = self._probe_state()
        now = time.monotonic()
        last_disk = now
        last_progress = now

        while not self._stop_event.is_set():
            item = self._queue.take(timeout=0.5)
            if item is not None and self._should_write(item[1]):
                if not self._write_one(item):
                    return  # ENOSPC path already finished the run
                state = self._probe_state()
                if state != prev_state:
                    writer.note_adjustments(state)
                    prev_state = state
            now = time.monotonic()
            if now - last_disk >= self.DISK_CHECK_S:
                last_disk = now
                free = free_space_gb(writer.run_dir)
                if free < self._writer.cfg.free_space_floor_gb:
                    self._finish(
                        "aborted_disk_full",
                        f"Stopped: {free:.1f} GB free, floor is "
                        f"{writer.cfg.free_space_floor_gb:g} GB",
                    )
                    return
                if writer.bytes_written > writer.cfg.max_run_gb * 1e9:
                    self._finish(
                        "aborted_size_cap",
                        f"Stopped: run reached {writer.cfg.max_run_gb:g} GB cap",
                    )
                    return
            if now - last_progress >= self.PROGRESS_S:
                last_progress = now
                self.progress.emit(
                    writer.frame_count,
                    self._queue.dropped_count,
                    writer.bytes_written / 1e6,
                )

        # Stop requested: drain what's already queued, bounded.
        deadline = time.monotonic() + self.DRAIN_S
        while time.monotonic() < deadline:
            item = self._queue.take(timeout=0.05)
            if item is None:
                break
            if self._should_write(item[1]) and not self._write_one(item):
                return
        self._finish(
            self._stop_status,
            f"Recording stopped: {writer.frame_count} frames, "
            f"{self._queue.dropped_count} dropped",
        )

    def _should_write(self, host_ns: int) -> bool:
        """Pacing gate. Schedule-based (next-due, not last-written) so the
        effective rate averages record_fps instead of one camera-frame
        slower; catch-up after a stall is capped at one interval so a
        pause never causes a burst of consecutive writes."""
        if self._pace_interval_ns == 0:
            return True
        if host_ns < self._next_due_ns:
            self._skipped_pacing += 1
            return False
        self._next_due_ns += self._pace_interval_ns
        if self._next_due_ns < host_ns:
            # Fell behind schedule (stall / slow encode) — re-anchor at
            # now instead of letting the backlog admit a write burst.
            self._next_due_ns = host_ns + self._pace_interval_ns
        return True

    def _write_one(self, item: tuple[np.ndarray, int]) -> bool:
        """Encode one frame; on ENOSPC finish the run as aborted_disk_full
        and return False. Other errors propagate to run()'s catch-all."""
        try:
            self._writer.write_frame(*item)
        except OSError as e:  # av errors subclass OSError with errno set
            if e.errno == errno.ENOSPC:
                log.error("ENOSPC while writing %s", self._writer.run_dir)
                self._finish("aborted_disk_full", "Stopped: disk full")
                return False
            raise
        return True

    def _probe_state(self) -> object:
        if self._probe is None:
            return None
        try:
            return self._probe()
        except Exception:
            log.exception("state_probe raised; disabling for this run")
            self._probe = None
            return None

    def _finish(self, status: str, message: str) -> None:
        self._close_best_effort(status)
        self.finished.emit(status, message)

    def _close_best_effort(self, status: str) -> None:
        try:
            self._writer.frame_count_dropped_queue = self._queue.dropped_count
            self._writer.frame_count_skipped_pacing = self._skipped_pacing
            self._writer.close(status)
        except Exception:
            log.exception("writer close failed")

    def stop(self, status: str = "completed") -> None:
        """Thread-safe, callable from the GUI thread. The run loop drains
        the queue (bounded ~2 s), closes with ``status``, then emits
        finished."""
        self._stop_status = status
        self._stop_event.set()
        self._queue.wake()
