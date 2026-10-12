"""Frame protocol codec for the blebridge UART bridge protocol v2 (ADR-002).

Frame layout (the single authority is task-1-brief.md / REQUIREMENTS FR-A1)::

    offset  field    size  notes
    0       SOF      1B    fixed 0x55
    1       LEN      2B LE PAYLOAD length, 0..512
    3       CMD      1B    request 0x01..0x7F; response = request CMD | 0x80;
                           events 0x20..0x2F and 0x8E
    4       SEQ      1B    request sequence; responses echo it; events carry
                           the event ring sequence number here
    5       PAYLOAD  LEN B <= 512
    5+LEN   CRC      2B LE CRC16-CCITT-FALSE (poly 0x1021, init 0xFFFF,
                           no reflection, no final XOR) over CMD..PAYLOAD

This module is pure protocol: no transport, no command semantics.  It is the
Python half of a cross-language contract; the firmware C implementation
(Task 6) must agree byte-for-byte, enforced via the golden vectors in
``tests/golden_frames.json``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List

__all__ = [
    "SOF",
    "MAX_PAYLOAD",
    "HEADER_LEN",
    "CRC_LEN",
    "FRAME_OVERHEAD",
    "EVENT_CMDS",
    "Frame",
    "crc16_ccitt_false",
    "encode_frame",
    "StreamParser",
]

SOF: int = 0x55
MAX_PAYLOAD: int = 512
HEADER_LEN: int = 5  # SOF(1) + LEN(2) + CMD(1) + SEQ(1)
CRC_LEN: int = 2
FRAME_OVERHEAD: int = HEADER_LEN + CRC_LEN

# Assigned event commands (event window is 0x20..0x2F plus debug 0x8E;
# only these are currently assigned per the task-1 brief).
EVENT_CMDS: frozenset[int] = frozenset({0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x8E})

_CRC16_POLY = 0x1021
_CRC16_INIT = 0xFFFF
_HEADER = struct.Struct("<BH")  # SOF placeholder not covered; LEN as u16 LE
_CS = struct.Struct("<BB")  # CMD, SEQ
_CRC = struct.Struct("<H")  # CRC as u16 LE


def _build_crc16_table() -> List[int]:
    """Build the 256-entry CRC16-CCITT-FALSE table (once, at import)."""
    table: List[int] = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ _CRC16_POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC_TABLE = _build_crc16_table()


def crc16_ccitt_false(data: bytes) -> int:
    """CRC16-CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final XOR.

    Check value: ``crc16_ccitt_false(b"123456789") == 0x29B1``.
    """
    crc = _CRC16_INIT
    table = _CRC_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ byte) & 0xFF]
    return crc


@dataclass
class Frame:
    """One decoded frame.  ``cmd``/``seq`` are 0..255, ``payload`` is raw bytes."""

    cmd: int
    seq: int
    payload: bytes

    @property
    def is_event(self) -> bool:
        """True when ``cmd`` belongs to the event command space (EVENT_CMDS)."""
        return self.cmd in EVENT_CMDS


def encode_frame(cmd: int, seq: int, payload: bytes = b"") -> bytes:
    """Encode one frame.

    Raises:
        ValueError: if ``cmd`` or ``seq`` is outside 0..255, or
            ``len(payload)`` exceeds MAX_PAYLOAD (512).
    """
    if not 0 <= cmd <= 0xFF:
        raise ValueError(f"cmd must be in 0..255, got {cmd:#04x}")
    if not 0 <= seq <= 0xFF:
        raise ValueError(f"seq must be in 0..255, got {seq:#04x}")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            f"payload length {len(payload)} exceeds MAX_PAYLOAD ({MAX_PAYLOAD})"
        )
    payload = bytes(payload)
    body = _CS.pack(cmd, seq) + payload
    crc = crc16_ccitt_false(body)
    return _HEADER.pack(SOF, len(payload)) + body + _CRC.pack(crc)


class StreamParser:
    """Synchronous byte-by-byte frame parser.

    Pure state machine: no IO, no threads, no callbacks.  Feed arbitrary
    chunks (or single bytes) via :meth:`feed` and collect decoded frames.
    State is kept across calls, so chunk boundaries never affect the result.

    On any invalid step (LEN > 512, CRC mismatch) the bytes consumed by the
    failed frame attempt are dropped, ``resyncs`` is incremented, and the
    parser returns to WAIT_SOF resuming from the *next* incoming byte
    (sliding resynchronisation, ADR-002 / FR-A4).  A non-SOF byte while in
    WAIT_SOF is dropped immediately.

    Counters (monotonically non-decreasing):

    - ``frames_ok``: frames decoded and CRC-verified.
    - ``crc_errors``: attempts rejected because the CRC did not match.
    - ``resyncs``: aborted frame attempts (bad LEN or bad CRC).
    - ``bytes_dropped``: bytes consumed that did not become part of a
      valid frame.  Invariant for any byte stream::

          len(stream) == bytes_dropped
                           + sum(HEADER_LEN + len(f.payload) + CRC_LEN
                                 for f in decoded_frames)
    """

    # States
    WAIT_SOF = 0  # scanning for SOF
    GOT_SOF = 1  # SOF seen, expecting LEN byte 0
    READ_LEN = 2  # LEN byte 0 seen, expecting LEN byte 1
    READ_HEADER_REST = 3  # expecting CMD then SEQ
    READ_PAYLOAD = 4  # collecting LEN payload bytes
    CHECK_CRC = 5  # collecting the 2 CRC bytes

    def __init__(self) -> None:
        self.frames_ok = 0
        self.crc_errors = 0
        self.resyncs = 0
        self.bytes_dropped = 0
        self._reset_attempt()

    def _reset_attempt(self) -> None:
        """Return to WAIT_SOF and clear all per-attempt buffers."""
        self._state = self.WAIT_SOF
        self._len_low = 0
        self._frame_len = 0
        self._header_rest = bytearray()  # CMD, SEQ
        self._payload = bytearray()
        self._crc_bytes = bytearray()

    def feed(self, data: bytes) -> List[Frame]:
        """Consume ``data`` (any chunking) and return the frames decoded from it."""
        frames: List[Frame] = []
        for byte in data:
            self._feed_byte(byte, frames)
        return frames

    def _abort_attempt(self, attempt_bytes: int, crc_error: bool = False) -> None:
        """Drop the current attempt's bytes and slide back to WAIT_SOF."""
        self.bytes_dropped += attempt_bytes
        self.resyncs += 1
        if crc_error:
            self.crc_errors += 1
        self._reset_attempt()

    def _feed_byte(self, byte: int, frames: List[Frame]) -> None:
        state = self._state

        if state == self.WAIT_SOF:
            if byte == SOF:
                self._state = self.GOT_SOF
            else:
                self.bytes_dropped += 1
            return

        if state == self.GOT_SOF:
            self._len_low = byte
            self._state = self.READ_LEN
            return

        if state == self.READ_LEN:
            frame_len = self._len_low | (byte << 8)
            if frame_len > MAX_PAYLOAD:
                # SOF + 2 LEN bytes are bogus: drop and rescan from next byte.
                self._abort_attempt(attempt_bytes=3)
            else:
                self._frame_len = frame_len
                self._state = self.READ_HEADER_REST
            return

        if state == self.READ_HEADER_REST:
            self._header_rest.append(byte)
            if len(self._header_rest) == 2:
                # Zero-length payload skips straight to the CRC bytes.
                self._state = self.CHECK_CRC if self._frame_len == 0 else self.READ_PAYLOAD
            return

        if state == self.READ_PAYLOAD:
            self._payload.append(byte)
            if len(self._payload) == self._frame_len:
                self._state = self.CHECK_CRC
            return

        # state == self.CHECK_CRC
        self._crc_bytes.append(byte)
        if len(self._crc_bytes) == CRC_LEN:
            cmd = self._header_rest[0]
            seq = self._header_rest[1]
            body = _CS.pack(cmd, seq) + bytes(self._payload)
            rx_crc = self._crc_bytes[0] | (self._crc_bytes[1] << 8)
            if rx_crc == crc16_ccitt_false(body):
                self.frames_ok += 1
                frames.append(Frame(cmd=cmd, seq=seq, payload=bytes(self._payload)))
                self._reset_attempt()
            else:
                self._abort_attempt(
                    attempt_bytes=HEADER_LEN + self._frame_len + CRC_LEN,
                    crc_error=True,
                )
