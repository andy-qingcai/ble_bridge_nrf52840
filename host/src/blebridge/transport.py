"""Transport layer for the blebridge UART bridge (ADR-003 layering).

A :class:`Transport` is a framed-byte pipe: it carries opaque bytes in both
directions and knows nothing about the frame protocol (see ``blebridge.framing``).
Two backends:

- :class:`SerialTransport` — real UART via pyserial, opened lazily on the
  first :meth:`~Transport.open` call.  ``open`` finishes with a *settle*
  window (default 0.6 s, ``BRIDGE_OPEN_SETTLE_S``): bytes arriving right
  after the port opens — the bridge firmware emits one glitch frame there,
  U-1 — are silently read and discarded before any caller I/O.
- :class:`FakeTransport` — scripted in-memory backend for tests.  It answers
  request frames from a ``cmd -> payload`` table, replays a script of raw
  frame bytes, records everything written, and accepts ``inject_raw`` for
  resynchronisation tests.  Reads are instantaneous and deterministic: when
  its queue is empty, ``read`` returns ``b""`` immediately (``timeout`` is
  accepted for interface compatibility, never slept on).

Error model: any transport that is not open raises :class:`TransportClosed`
from ``open``/``write``/``read``; a :class:`SerialTransport` whose port
cannot be opened or dies mid-session (pyserial ``SerialException`` /
``OSError``) also surfaces as :class:`TransportClosed`, so callers handle one
exception type.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from types import TracebackType
from typing import Dict, Iterable, Optional

import serial

from .framing import EVENT_CMDS, StreamParser, encode_frame

__all__ = [
    "Transport",
    "TransportClosed",
    "SerialTransport",
    "FakeTransport",
    "DEFAULT_OPEN_SETTLE_S",
    "open_settle_seconds",
]

DEFAULT_OPEN_SETTLE_S = 0.6
"""Default post-open settle window in seconds (U-1 open-glitch drain)."""

_OPEN_SETTLE_ENV = "BRIDGE_OPEN_SETTLE_S"


def open_settle_seconds() -> float:
    """Resolve the post-open settle window (U-1) from the environment.

    ``BRIDGE_OPEN_SETTLE_S`` overrides :data:`DEFAULT_OPEN_SETTLE_S` (``0``
    disables the settle entirely); invalid or negative values fall back to
    the default / clamp to ``0``.  Read at ``open()`` time, not import time,
    so operators and tests can toggle it per process without reloading.
    """
    raw = os.environ.get(_OPEN_SETTLE_ENV)
    if raw is None:
        return DEFAULT_OPEN_SETTLE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_OPEN_SETTLE_S


class TransportClosed(ConnectionError):
    """Raised when a transport operation is attempted on a closed transport."""


class Transport(ABC):
    """Abstract byte pipe between host and bridge firmware."""

    @abstractmethod
    def open(self) -> None:
        """Open the transport.  Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Close the transport.  Idempotent."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """True while the transport can carry bytes."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write raw bytes (blocking).  Raises TransportClosed when closed."""

    @abstractmethod
    def read(self, n: int, timeout: float) -> bytes:
        """Read up to ``n`` bytes, blocking at most ``timeout`` seconds.

        Returns ``b""`` on timeout (fewer bytes may be returned when some are
        already available).  Raises TransportClosed when closed.
        """

    def _require_open(self) -> None:
        if not self.is_open:
            raise TransportClosed("transport is not open")

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


class SerialTransport(Transport):
    """pyserial backend; the port is opened lazily on the first ``open()``.

    Default line settings follow FR-A5: 921600 8N1 with RTS/CTS hardware flow
    control.  ``timeout`` is the default read timeout; individual
    :meth:`read` calls override it per call.

    ``settle_s`` (U-1) is the post-open settle window: ``open()`` silently
    reads and discards bytes for up to that long before returning, so the
    bridge firmware's open-glitch frame cannot corrupt the first request.
    ``None`` resolves the window from ``BRIDGE_OPEN_SETTLE_S`` (default
    0.6 s) at open time; an explicit value (including ``0``) wins over the
    environment.  :class:`FakeTransport` has no settle — host-layer tests
    stay synchronous and zero-latency.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 921600,
        rtscts: bool = True,
        timeout: float = 0.1,
        settle_s: Optional[float] = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.rtscts = rtscts
        self.timeout = timeout
        self.settle_s = settle_s
        self._serial: Optional[serial.Serial] = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        # Construct fresh (re)serial handle each open so a close/reopen cycle
        # gets a clean file descriptor.  Construction failure (port missing,
        # busy, permission denied) is normalised like write/read below.
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                rtscts=self.rtscts,
                timeout=self.timeout,
            )
        except (serial.SerialException, OSError) as exc:
            raise TransportClosed(f"serial open failed: {exc}") from exc
        settle = (
            self.settle_s if self.settle_s is not None else open_settle_seconds()
        )
        if settle > 0:
            self._drain_settle(settle)

    def _drain_settle(self, settle: float) -> None:
        """Silently read and discard bytes for up to ``settle`` seconds (U-1).

        Bounded by the deadline even under a never-ending byte stream; a
        quiet port ends the window early (read timeout).  Read failures here
        never raise — a port that dies mid-settle surfaces
        :class:`TransportClosed` from the next write/read instead.
        """
        assert self._serial is not None
        deadline = time.monotonic() + settle
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._serial.timeout = remaining
                try:
                    if not self._serial.read(4096):
                        return  # quiet for the rest of the window: settled
                except (serial.SerialException, OSError):
                    return  # dead port: let later I/O raise TransportClosed
        finally:
            self._serial.timeout = self.timeout  # read() re-sets it per call

    def close(self) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None

    def write(self, data: bytes) -> None:
        self._require_open()
        assert self._serial is not None  # narrows type for mypy-style checkers
        try:
            self._serial.write(bytes(data))
        except (serial.SerialException, OSError) as exc:
            raise TransportClosed(f"serial write failed: {exc}") from exc

    def read(self, n: int, timeout: float) -> bytes:
        self._require_open()
        assert self._serial is not None
        # timeout=0 -> non-blocking poll (returns whatever is buffered now).
        self._serial.timeout = max(0.0, float(timeout))
        try:
            return bytes(self._serial.read(n))
        except (serial.SerialException, OSError) as exc:
            raise TransportClosed(f"serial read failed: {exc}") from exc


class FakeTransport(Transport):
    """Scripted in-memory transport for deterministic tests.

    Args:
        responses: mapping ``cmd -> payload``.  When a *request* frame (cmd
            not in EVENT_CMDS, bit 7 clear) whose cmd appears here is written,
            a response frame ``CMD | 0x80`` echoing the request SEQ and
            carrying ``payload`` verbatim (status byte included) is queued for
            the next ``read``.  Commands absent from the table get no
            response, which is how timeout paths are exercised.
        script: raw frame bytes (as produced by ``encode_frame``) queued for
            delivery in order, before anything else.  Typically event frames.

    Attributes:
        written: every byte ever passed to :meth:`write`, for assertions.

    ``read`` never blocks: queued bytes are handed out immediately (chunked by
    ``n``); when the queue is empty it returns ``b""`` at once regardless of
    ``timeout``.  This keeps host-layer tests free of real waiting.
    """

    def __init__(
        self,
        responses: Optional[Dict[int, bytes]] = None,
        script: Optional[Iterable[bytes]] = None,
    ) -> None:
        self.written = bytearray()
        self._rx = bytearray()
        self._open = False
        self._responses: Dict[int, bytes] = dict(responses or {})
        self._parser = StreamParser()
        for chunk in script or []:
            self._rx += bytes(chunk)

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        self._require_open()
        data = bytes(data)
        self.written += data
        # Answer matching request frames immediately (queued for later read).
        for frame in self._parser.feed(data):
            if (
                not frame.is_event
                and not (frame.cmd & 0x80)
                and frame.cmd in self._responses
            ):
                self._rx += encode_frame(
                    frame.cmd | 0x80, frame.seq, self._responses[frame.cmd]
                )

    def read(self, n: int, timeout: float) -> bytes:
        self._require_open()
        if n <= 0 or not self._rx:
            return b""
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    def inject_raw(self, data: bytes) -> None:
        """Append arbitrary bytes to the receive queue (resync tests etc.)."""
        self._rx += bytes(data)

    def pending(self) -> int:
        """Bytes still queued for delivery (diagnostics/tests)."""
        return len(self._rx)
