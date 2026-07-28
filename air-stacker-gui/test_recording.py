"""Tests for the recording core (recording.py).

Run: uv run python test_recording.py   (plain asserts, no pytest dep)

Also pytest-compatible (`uv run pytest test_recording.py` if pytest is
ever added): tests that need a scratch dir take a ``tmp_path`` argument,
which the __main__ runner fills with a TemporaryDirectory.

Exercises the libx264 path only — QSV is probed but not assumed to exist
on the dev box. Frames are small (320x240) and clips short (~30 frames)
so the suite stays fast.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import av
import numpy as np
from PySide6.QtCore import Qt

from recording import (
    RecordingConfig,
    RecordingWorker,
    RecordQueue,
    RunWriter,
    free_space_gb,
    mark_stale_runs_aborted,
    new_run_dir,
    new_session_dir,
    select_codec,
)

W, H = 320, 240


def _cfg(base: Path, codec: str = "libx264") -> RecordingConfig:
    # codec="libx264" skips the QSV probe so most tests stay fast.
    return RecordingConfig(base_dir=base, codec=codec)


def _red_frame() -> np.ndarray:
    rgb = np.zeros((H, W, 3), dtype=np.uint8)
    rgb[..., 0] = 255
    return rgb


def _writer(run_dir: Path, cfg: RecordingConfig, metadata: dict | None = None) -> RunWriter:
    return RunWriter(
        run_dir, cfg, width=W, height=H, fps_hint=30.0,
        metadata={"session_id": "session_test", **(metadata or {})},
    )


def _decode_frames(path: Path) -> list[np.ndarray]:
    with av.open(str(path)) as container:
        return [f.to_ndarray(format="rgb24") for f in container.decode(video=0)]


def test_happy_path(tmp_path: Path) -> None:
    n = 30
    run_dir = tmp_path / "run_001_x"
    run_dir.mkdir()
    w = _writer(run_dir, _cfg(tmp_path))
    w.open()
    frame = _red_frame()
    for i in range(n):
        w.write_frame(frame, host_ns=1_000_000 * i)
    w.close("completed")

    video = run_dir / "video.mp4"
    assert video.exists() and video.stat().st_size > 0
    assert len(_decode_frames(video)) >= n - 2

    doc = json.loads((run_dir / "run.json").read_text())
    assert doc["status"] == "completed"
    assert doc["frame_count_written"] == n
    assert doc["session_id"] == "session_test"
    assert doc["video"]["codec"] in ("libx264", "h264_qsv")
    assert doc["end_iso"] is not None
    assert doc["disk_at_end_gb_free"] is not None

    lines = (run_dir / "timestamps.csv").read_text().splitlines()
    assert lines[0] == "frame_index,host_perf_ns"
    assert len(lines) == n + 1
    assert lines[1] == "0,0"
    assert lines[-1] == f"{n - 1},{1_000_000 * (n - 1)}"


def test_color_sanity(tmp_path: Path) -> None:
    # Pure red in → red-dominant out. Catches RGB/BGR swaps and
    # colormatrix mistakes in the encode path.
    run_dir = tmp_path / "run_001_x"
    run_dir.mkdir()
    w = _writer(run_dir, _cfg(tmp_path))
    w.open()
    frame = _red_frame()
    for i in range(30):
        w.write_frame(frame, host_ns=i)
    w.close("completed")

    frames = _decode_frames(run_dir / "video.mp4")
    assert frames
    mean = frames[len(frames) // 2].reshape(-1, 3).mean(axis=0)
    r, g, b = mean
    assert r > 150, f"red channel too weak: {mean}"
    assert g < 80 and b < 80, f"not red-dominant: {mean}"


def test_record_queue() -> None:
    q = RecordQueue(maxsize=3)
    frames = [np.full((2, 2, 3), i, dtype=np.uint8) for i in range(4)]
    assert q.put_nowait(frames[0], 100)
    assert q.put_nowait(frames[1], 101)
    assert q.put_nowait(frames[2], 102)
    assert not q.put_nowait(frames[3], 103)  # overflow drops
    assert q.dropped_count == 1

    # FIFO order preserved, host_ns rides along.
    for i in range(3):
        item = q.take(timeout=0.1)
        assert item is not None
        frame, host_ns = item
        assert frame[0, 0, 0] == i
        assert host_ns == 100 + i

    # Empty take times out to None.
    t0 = time.perf_counter()
    assert q.take(timeout=0.05) is None
    assert time.perf_counter() - t0 >= 0.04

    # wake() unblocks a pending take well before its timeout.
    result: list[object] = []
    taker = threading.Thread(target=lambda: result.append(q.take(timeout=5.0)))
    taker.start()
    time.sleep(0.05)
    q.wake()
    taker.join(timeout=1.0)
    assert not taker.is_alive()
    assert result == [None]


def test_dir_numbering(tmp_path: Path) -> None:
    with mock.patch("recording.time.strftime", return_value="2026-01-01_00-00-00"):
        session = new_session_dir(tmp_path, {"hostname": "testbox"})
        assert session.name == "session_2026-01-01_00-00-00"
        assert json.loads((session / "session.json").read_text())["hostname"] == "testbox"

        # Same-second collision gets a numeric suffix.
        session2 = new_session_dir(tmp_path, {})
        assert session2.name == "session_2026-01-01_00-00-00_2"

        run1 = new_run_dir(session)
        run2 = new_run_dir(session)
    assert run1.name.startswith("run_001_")
    assert run2.name.startswith("run_002_")
    assert run1.is_dir() and run2.is_dir()


def test_mark_stale_runs_aborted(tmp_path: Path) -> None:
    run_dir = tmp_path / "session_x" / "run_001_y"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"status": "running", "end_iso": None}))
    (run_dir / "video.mp4").write_bytes(b"\x00" * 16)
    done_dir = tmp_path / "session_x" / "run_002_z"
    done_dir.mkdir()
    (done_dir / "run.json").write_text(json.dumps({"status": "completed"}))

    assert mark_stale_runs_aborted(tmp_path) == 1
    doc = json.loads((run_dir / "run.json").read_text())
    assert doc["status"] == "aborted_crash"
    assert doc["end_iso"] is not None
    assert json.loads((done_dir / "run.json").read_text())["status"] == "completed"

    # Second scan finds nothing; missing base is a no-op.
    assert mark_stale_runs_aborted(tmp_path) == 0
    assert mark_stale_runs_aborted(tmp_path / "does_not_exist") == 0


def test_close_idempotent(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_001_x"
    run_dir.mkdir()
    w = _writer(run_dir, _cfg(tmp_path))
    w.open()
    w.write_frame(_red_frame(), host_ns=0)
    w.close("completed")
    w.close("error")  # no-op: must not raise or rewrite status
    assert json.loads((run_dir / "run.json").read_text())["status"] == "completed"


def test_select_codec_auto(tmp_path: Path) -> None:
    name, opts = select_codec(_cfg(tmp_path, codec="auto"))
    assert name in ("libx264", "h264_qsv")
    if name == "libx264":
        assert opts["crf"] == "17"
        assert opts["preset"] == "veryfast"
    else:
        assert opts["global_quality"] == "22"


def test_free_space_gb(tmp_path: Path) -> None:
    assert free_space_gb(tmp_path) > 0
    # Nonexistent path falls back to nearest existing ancestor.
    assert free_space_gb(tmp_path / "not" / "yet" / "created") > 0


def test_worker_end_to_end(tmp_path: Path) -> None:
    # Real queue + writer + worker on a plain threading.Thread — no
    # QThread/event loop needed for direct-connection signals.
    run_dir = tmp_path / "run_001_x"
    run_dir.mkdir()
    writer = _writer(run_dir, _cfg(tmp_path))
    queue = RecordQueue(maxsize=64)
    state = {"v": 0}
    worker = RecordingWorker(queue, writer, state_probe=lambda: state["v"])

    # DirectConnection: the worker QObject lives on this (main) thread but
    # emits from the plain thread; auto-connections would queue and need an
    # event loop. The GUI uses moveToThread + queued as usual.
    direct = Qt.ConnectionType.DirectConnection
    started: list[str] = []
    finishes: list[tuple[str, str]] = []
    worker.started_run.connect(started.append, direct)
    worker.finished.connect(lambda status, msg: finishes.append((status, msg)), direct)

    thread = threading.Thread(target=worker.run)
    thread.start()
    frame = _red_frame()
    try:
        for i in range(15):
            assert queue.put_nowait(frame, host_ns=i)
        deadline = time.monotonic() + 10
        while writer.frame_count < 15 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.frame_count >= 15

        state["v"] = 1  # state_probe change → adjustment_changes entry
        for i in range(15, 30):
            assert queue.put_nowait(frame, host_ns=i)
        while writer.frame_count < 30 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop("completed")
        thread.join(timeout=10)
    assert not thread.is_alive()

    assert started == [str(run_dir)]
    assert finishes and finishes[0][0] == "completed"
    assert len(_decode_frames(run_dir / "video.mp4")) >= 28

    doc = json.loads((run_dir / "run.json").read_text())
    assert doc["status"] == "completed"
    assert doc["frame_count_written"] == 30
    assert doc["frame_count_dropped_queue"] == 0
    changes = doc["adjustment_changes"]
    assert len(changes) == 1
    assert changes[0]["snapshot"] == 1
    assert 15 <= changes[0]["frame_index"] <= 30


def _run_all() -> None:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        t0 = time.perf_counter()
        if "tmp_path" in inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))
        else:
            fn()
        print(f"{name} OK ({time.perf_counter() - t0:.2f}s)")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
