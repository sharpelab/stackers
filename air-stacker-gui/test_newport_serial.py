"""Tests for the Newport serial transport (newport_serial.py).

Run: uv run python test_newport_serial.py   (plain asserts, no pytest dep)

A scripted fake stands in for pyserial. It delivers replies in wire order,
each after an optional delay, so late replies (the USB-stall case) can be
made to land during the *next* query exactly as they did on the rig.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable
from unittest import mock

import serial

from conex import ConexAxis, ConexError
from newport_serial import ANY, FLOAT, TEXT, TS_REPLY, NewportLink
from smc100 import SMC100Axis, SMC100Error

# (delay_s, bytes) — the fake delivers these strictly in order; each becomes
# readable ``delay_s`` after it was queued, but never before its predecessor.
Reply = tuple[float, bytes]
Responder = Callable[[bytes], list[Reply]]


class FakeSerial:
    """Enough of ``serial.Serial`` for NewportLink, with a scripted wire."""

    def __init__(self, responder: Responder | None = None, **kwargs) -> None:
        self.kwargs = kwargs
        self.timeout: float | None = kwargs.get("timeout")
        self.write_timeout: float | None = kwargs.get("write_timeout")
        self.is_open = True
        self.writes: list[bytes] = []
        self.responder = responder or (lambda _line: [])
        self.fail_next_write = False
        self._lock = threading.Lock()
        self._queue: deque[tuple[float, bytes]] = deque()  # (ready_at, data)
        self._last_ready = 0.0

    # --- scripting helpers ---------------------------------------------------

    def enqueue(self, replies: list[Reply]) -> None:
        now = time.monotonic()
        with self._lock:
            for delay, data in replies:
                ready = max(now + delay, self._last_ready)
                self._queue.append((ready, data))
                self._last_ready = ready

    def _available(self) -> bytes:
        now = time.monotonic()
        out = b""
        with self._lock:
            while self._queue and self._queue[0][0] <= now:
                out += self._queue.popleft()[1]
        return out

    # --- serial.Serial surface -----------------------------------------------

    @property
    def in_waiting(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(len(d) for r, d in self._queue if r <= now)

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout if self.timeout is not None else 10.0)
        buf = b""
        while len(buf) < size:
            buf += self._available()
            if len(buf) >= size:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        # Excess beyond ``size`` is put back at the head, like a real FIFO.
        head, rest = buf[:size], buf[size:]
        if rest:
            with self._lock:
                self._queue.appendleft((0.0, rest))
        return head

    def write(self, data: bytes) -> int:
        if self.fail_next_write:
            self.fail_next_write = False
            self.writes.append(data[: len(data) // 2])  # partial, like the OS
            raise serial.SerialTimeoutException("Write timeout")
        self.writes.append(data)
        self.enqueue(self.responder(data))
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def make_link(responder: Responder, timeout: float = 0.2) -> tuple[NewportLink, FakeSerial, _Capture]:
    holder: dict[str, FakeSerial] = {}

    def factory(**kwargs):
        holder["port"] = FakeSerial(responder, **kwargs)
        return holder["port"]

    log = logging.getLogger(f"test.newport.{id(holder)}")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    cap = _Capture()
    log.addHandler(cap)
    link = NewportLink("COMX", baud=57600, timeout=timeout, log=log, serial_factory=factory)
    link.open()
    return link, holder["port"], cap


def reply_to(table: dict[bytes, list[Reply]]) -> Responder:
    return lambda line: list(table.get(line, []))


# --- link-level tests --------------------------------------------------------


def test_query_healthy():
    link, port, _ = make_link(reply_to({b"1TP?\r\n": [(0.0, b"1TP12.5\r\n")]}))
    m = link.query("TP?", FLOAT)
    assert float(m.group(0)) == 12.5
    assert port.writes == [b"1TP?\r\n"]
    assert port.timeout == 0.2, "port timeout must be restored after a query"


def test_late_reply_is_discarded_by_next_query():
    # TP? answers late (after the 0.2 s budget); TS answers promptly but the
    # wire is FIFO, so the stale TP line lands first during the TS query.
    link, port, cap = make_link(reply_to({
        b"1TP?\r\n": [(0.3, b"1TP25.481\r\n")],
        b"1TS\r\n": [(0.0, b"1TS000033\r\n")],
    }))
    try:
        link.query("TP?", FLOAT)
        raise AssertionError("first query should have timed out")
    except RuntimeError as e:
        assert "no response" in str(e)
    m = link.query("TS", TS_REPLY)
    assert (m.group(1), m.group(2)) == ("0000", "33")
    stale = [msg for msg in cap.messages() if "stale reply" in msg]
    assert len(stale) == 1 and "1TP25.481" in stale[0] and "TS" in stale[0], stale


def test_mismatched_prefix_is_never_returned():
    # The rig failure: a TP reply arriving in answer to TS. Old code returned
    # "1TP2..." verbatim; now it is discarded and the query times out clean.
    link, _, cap = make_link(reply_to({b"1TS\r\n": [(0.0, b"1TP25.48103\r\n")]}))
    try:
        link.query("TS", TS_REPLY)
        raise AssertionError("must not return a wrong-prefix line")
    except RuntimeError as e:
        assert "no response" in str(e) and "1 stale" in str(e), e
    assert any("stale reply" in m for m in cap.messages())


def test_malformed_body_raises():
    link, _, _ = make_link(reply_to({b"1TP?\r\n": [(0.0, b"1TPabc\r\n")]}))
    try:
        link.query("TP?", FLOAT)
        raise AssertionError("malformed body must raise")
    except RuntimeError as e:
        assert "malformed" in str(e) and "'abc'" in str(e), e


def test_ts_pattern_rejects_garbage_and_short():
    for body in (b"1TS1TP2\r\n", b"1TS0\r\n", b"1TS00003\r\n", b"1TS0000331\r\n"):
        link, _, _ = make_link(reply_to({b"1TS\r\n": [(0.0, body)]}))
        try:
            link.query("TS", TS_REPLY)
            raise AssertionError(f"{body!r} must be rejected")
        except RuntimeError as e:
            assert "malformed" in str(e), (body, e)


def test_query_is_bounded_by_timeout_even_with_stale_lines():
    # A trickle of stale lines must not extend the wait past the budget.
    link, _, _ = make_link(reply_to({
        b"1TP?\r\n": [(0.05, b"1TSxx\r\n"), (0.05, b"1VA5\r\n"), (0.05, b"1AC1\r\n"), (0.5, b"1TP1\r\n")],
    }), timeout=0.2)
    t0 = time.monotonic()
    try:
        link.query("TP?", FLOAT)
        raise AssertionError("should time out")
    except RuntimeError as e:
        assert "3 stale" in str(e), e
    elapsed = time.monotonic() - t0
    assert elapsed < 0.35, f"query overran its budget: {elapsed:.3f}s"


def test_two_lines_in_one_chunk():
    link, _, cap = make_link(reply_to({b"1TS\r\n": [(0.0, b"1TP9.9\r\n1TS000A28\r\n")]}))
    m = link.query("TS", TS_REPLY)
    assert (m.group(1), m.group(2)) == ("000A", "28")
    assert any("1TP9.9" in msg for msg in cap.messages())
    assert not link._rx, "receive buffer must be empty after consuming both lines"


def test_write_timeout_marks_dirty_and_next_write_terminates_fragment():
    link, port, _ = make_link(reply_to({}))
    port.fail_next_write = True
    try:
        link.send("PR0.0001")
        raise AssertionError("write timeout must surface")
    except RuntimeError as e:
        assert "write timeout" in str(e), e
    assert link._dirty
    link.send("ST")
    assert port.writes[-1] == b"\r\n1ST\r\n", port.writes
    assert not link._dirty
    link.send("ST")
    assert port.writes[-1] == b"1ST\r\n", "clean writes carry no leading terminator"


def test_send_drains_pending_bytes_and_logs():
    link, port, cap = make_link(reply_to({}))
    port.enqueue([(0.0, b"1TP1.0\r\n")])
    time.sleep(0.005)
    link.send("ST")
    assert port.writes == [b"1ST\r\n"]
    drained = [m for m in cap.messages() if "pending byte" in m]
    assert len(drained) == 1 and "1TP1.0" in drained[0] and "ST" in drained[0], drained


def test_blank_line_is_skipped():
    link, _, _ = make_link(reply_to({b"1ID?\r\n": [(0.0, b"\r\n"), (0.0, b"1IDLTA-HS\r\n")]}))
    assert link.query("ID?", TEXT).group(0) == "LTA-HS"


def test_closed_port_raises_error_cls():
    class MyErr(RuntimeError):
        pass

    link = NewportLink("COMX", baud=1, error_cls=MyErr, serial_factory=lambda **kw: FakeSerial(**kw))
    try:
        link.query("TP?", ANY)
        raise AssertionError("closed link must raise")
    except MyErr as e:
        assert "not open" in str(e)


# --- driver-level tests -------------------------------------------------------


def _patched_serial(responder: Responder):
    return mock.patch("newport_serial.serial.Serial", lambda **kw: FakeSerial(responder, **kw))


def test_smc100_state_parses_and_uppercases():
    table = {
        b"1SL?\r\n": [(0.0, b"1SL0\r\n")],
        b"1SR?\r\n": [(0.0, b"1SR50\r\n")],
        b"1TS\r\n": [(0.0, b"1TS000a33\r\n")],
        b"1TP?\r\n": [(0.0, b"1TP25.4759899668\r\n")],
    }
    with _patched_serial(reply_to(table)):
        axis = SMC100Axis("COMX", position_limits=(0.0, 30.0))
        axis.open()
        assert axis.effective_limits == (0.0, 30.0)
        assert axis.state() == ("33", "000A")
        assert axis.position() == 25.4759899668


def test_smc100_garbage_ts_raises_driver_error():
    table = {
        b"1SL?\r\n": [(0.0, b"1SL0\r\n")],
        b"1SR?\r\n": [(0.0, b"1SR50\r\n")],
        b"1TS\r\n": [(0.0, b"1TP25.48\r\n")],  # the 8/18 failure shape
    }
    with _patched_serial(reply_to(table)):
        axis = SMC100Axis("COMX", timeout=0.1)
        axis.open()
        try:
            axis.state()
            raise AssertionError("garbage TS must not parse")
        except SMC100Error as e:
            assert "no response" in str(e) and "stale" in str(e), e


def test_smc100_open_tolerates_missing_limits():
    # CONFIGURATION state refuses SL/SR; open() must still succeed with no clamp.
    with _patched_serial(reply_to({})):
        axis = SMC100Axis("COMX", timeout=0.05)
        axis.open()
        assert axis.effective_limits is None


def test_conex_errors_are_conex_error():
    table = {b"1TS?\r\n": [(0.0, b"1TS000032\r\n")]}
    with _patched_serial(reply_to(table)):
        axis = ConexAxis("COMX", timeout=0.05)
        axis.open()
        assert axis.state() == ("32", "0000")
        try:
            axis.position()
            raise AssertionError("no TP reply must raise")
        except ConexError as e:
            assert "no response" in str(e)


def test_query_with_pattern_from_driver_surface():
    table = {b"1VE?\r\n": [(0.0, b"1VE SMC_CC - Controller-driver version  3. 1. 2\r\n")]}
    with _patched_serial(reply_to(table)):
        axis = SMC100Axis("COMX", timeout=0.05)
        axis.open()
        assert axis.firmware().startswith("SMC_CC")
        # Raw-body access for probe scripts still works.
        assert axis.query("VE?").startswith("SMC_CC")


if __name__ == "__main__":
    import sys

    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001 — report and continue
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
