"""Shared serial transport for Newport's ASCII controller family (SMC100, CONEX-CC).

Both controllers speak the same line protocol: ``<address><CMD>[arg]\\r\\n``
out, ``<address><CMD><value>\\r\\n`` back for queries, nothing back for
fire-and-forget commands. This module owns everything below the command
vocabulary — port lifecycle, the lock, framing, and reply validation — so
the per-instrument drivers reduce to "which commands exist and what shape
their replies have."

Why this exists: with a bare ``read_until`` a reply that arrives late (USB
stall, controller busy) is consumed by the *next* query, and a
prefix-mismatched line handed back verbatim parses into garbage — an
error register of ``1TP2``, a state of ``5.``, a position of 33000 µm.
:meth:`NewportLink.query` instead treats a wrong-prefix line as stale
(discard, log, keep waiting) and a right-prefix line whose body fails the
declared pattern as malformed (raise, don't guess).

Timeouts: the constructor value bounds one whole query, stale discards
included. The port's own read timeout is a short fixed quantum set once at
open and never changed afterwards: reconfiguring a pyserial port timeout
goes through ``SetCommState`` on Windows, and the FTDI driver loses or
delays bytes that arrive during that call — which is exactly when a reply
is in flight. The link polls in quanta against its own deadline instead.
The port object is private to the link; nothing else touches it.

Write timeouts: pyserial raises ``SerialTimeoutException`` when the OS
accepted only part of a line. The controller then holds an unterminated
fragment, and the next command's text would be appended to it. The link
records that state and leads the next write with a bare terminator so the
fragment is closed off as its own (invalid) line instead of corrupting a
real command.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

import serial

DEFAULT_ADDRESS = 1
TERMINATOR = b"\r\n"
# Port read timeout: how long one blocking read waits for a first byte
# before the deadline loop gets to check the clock again. Comfortably above
# the FTDI latency timer (16 ms) so a reply arrives within one wait.
READ_QUANTUM_S = 0.05

# Reply-body patterns. Drivers pass one per command so the shape is declared
# next to the command that produces it.
FLOAT = re.compile(r"^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")
# TS reply body: 4 hex chars of positioner error register + 2 of state.
TS_REPLY = re.compile(r"^([0-9A-Fa-f]{4})([0-9A-Fa-f]{2})$")
# Free-text replies (ID?, VE?): printable ASCII, non-empty.
TEXT = re.compile(r"^[ -~]+$")
# Anything printable, including empty — for callers that want the raw body.
ANY = re.compile(r"^[ -~]*$")


class NewportLink:
    """One controller on one serial port.

    ``error_cls`` is the exception type raised for protocol-level failures
    (no response, malformed reply, write timeout) so each driver's callers
    keep catching their own error class. Transport failures from pyserial
    other than write timeouts (port vanished, permission) propagate as
    ``serial.SerialException`` unchanged.
    """

    def __init__(
        self,
        port: str,
        *,
        baud: int,
        address: int = DEFAULT_ADDRESS,
        timeout: float = 0.5,
        error_cls: type[Exception] = RuntimeError,
        log: logging.Logger | None = None,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0 (got {timeout})")
        self.port = port
        self.baud = baud
        self.address = address
        self.timeout = timeout
        self._error_cls = error_cls
        self._log = log or logging.getLogger(__name__)
        self._serial_factory = serial_factory
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        # Bytes read from the port but not yet consumed as a line. Only ever
        # non-empty when one read returned more than one line.
        self._rx = bytearray()
        # Set when a write timed out and the controller may be holding an
        # unterminated fragment. Cleared by the next successful write.
        self._dirty = False

    # --- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        if self._serial is not None:
            return
        # Resolved at open() rather than bound at import so tests can patch
        # ``newport_serial.serial.Serial`` without constructing links by hand.
        factory = self._serial_factory or serial.Serial
        self._serial = factory(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=min(READ_QUANTUM_S, self.timeout),
            write_timeout=self.timeout,
        )
        self._rx.clear()
        self._dirty = False

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                finally:
                    self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    # --- transport -----------------------------------------------------------

    def send(self, cmd: str) -> None:
        """Fire-and-forget command. No reply is expected or read."""
        with self._lock:
            ser = self._require_port()
            self._drain_pending(ser, awaiting=cmd)
            self._write(ser, cmd)

    def query(self, cmd: str, pattern: re.Pattern[str] = ANY) -> re.Match[str]:
        """Send ``cmd`` and return the match of ``pattern`` against the reply body.

        Lines whose prefix doesn't belong to ``cmd`` are stale replies from
        an earlier query; they are discarded with a warning and the wait
        continues. A line with the right prefix whose body fails ``pattern``
        raises immediately. The whole exchange, discards included, is
        bounded by the constructor timeout.
        """
        prefix = f"{self.address}{cmd.rstrip('?')}"
        with self._lock:
            ser = self._require_port()
            self._drain_pending(ser, awaiting=cmd)
            self._write(ser, cmd)
            deadline = time.monotonic() + self.timeout
            discarded = 0
            while True:
                text = self._read_line(ser, deadline)
                if text is None:
                    suffix = f" ({discarded} stale line(s) discarded)" if discarded else ""
                    raise self._error_cls(f"no response to {cmd!r}{suffix}")
                if not text:
                    continue  # blank line; the deadline still bounds us
                if not text.startswith(prefix):
                    discarded += 1
                    self._log.warning(
                        "%s: discarded stale reply %r while awaiting %s",
                        self.port, text, cmd,
                    )
                    continue
                # Strip: VE? and friends put a space after the mnemonic.
                body = text[len(prefix):].strip()
                m = pattern.match(body)
                if m is None:
                    raise self._error_cls(f"malformed {cmd!r} reply: {body!r}")
                return m

    # --- internals -----------------------------------------------------------

    def _require_port(self) -> serial.Serial:
        if self._serial is None:
            raise self._error_cls("serial port not open")
        return self._serial

    def _write(self, ser: serial.Serial, cmd: str) -> None:
        line = f"{self.address}{cmd}".encode("ascii") + TERMINATOR
        if self._dirty:
            # Close off whatever fragment the last failed write left behind.
            line = TERMINATOR + line
        try:
            ser.write(line)
            ser.flush()
        except serial.SerialTimeoutException as e:
            self._dirty = True
            raise self._error_cls(f"write timeout sending {cmd!r}: {e}") from e
        self._dirty = False

    def _drain_pending(self, ser: serial.Serial, *, awaiting: str) -> None:
        """Discard bytes already delivered before we send. Logged, never silent."""
        stale = bytes(self._rx)
        self._rx.clear()
        waiting = ser.in_waiting
        if waiting:
            stale += ser.read(waiting)
        if stale:
            self._log.warning(
                "%s: discarded %d pending byte(s) %r before sending %s",
                self.port, len(stale), stale, awaiting,
            )

    def _read_line(self, ser: serial.Serial, deadline: float) -> str | None:
        """One terminator-delimited line, or None if the deadline passes first.

        Serves from the link's receive buffer first. Otherwise reads whatever
        the port already has without blocking, or blocks one quantum for a
        first byte, and re-checks the deadline between reads. The port
        timeout is never modified. An unterminated fragment left at the
        deadline stays buffered and is reported by the next
        ``_drain_pending``.
        """
        while True:
            idx = self._rx.find(TERMINATOR)
            if idx >= 0:
                line = bytes(self._rx[:idx])
                del self._rx[: idx + len(TERMINATOR)]
                return line.decode("ascii", errors="replace").strip()
            if time.monotonic() >= deadline:
                return None
            waiting = ser.in_waiting
            chunk = ser.read(max(1, waiting))
            if chunk:
                self._rx += chunk
