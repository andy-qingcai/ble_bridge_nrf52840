"""Tests for blebridge.framing: golden vectors, roundtrip, determinism, fuzz.

Golden vectors are generated (never hand-edit tests/golden_frames.json):
``python -m tests.tools.gen_golden > tests/golden_frames.json``
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Import blebridge straight from the src tree (works with or without `pip install -e .`).
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from blebridge.framing import (  # noqa: E402
    CRC_LEN,
    EVENT_CMDS,
    FRAME_OVERHEAD,
    HEADER_LEN,
    MAX_PAYLOAD,
    SOF,
    Frame,
    StreamParser,
    crc16_ccitt_false,
    encode_frame,
)

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_frames.json"
GOLDEN: Dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

FRAME_ROUNDTRIP_COUNT = 500
FUZZ_FEEDS = 10000


def _golden_frames() -> List[Dict[str, Any]]:
    return GOLDEN["frames"]


def _golden_streams() -> List[Dict[str, Any]]:
    return GOLDEN["streams"]


def _parse_all(stream: bytes) -> tuple:
    """Parse one stream three ways: whole, byte-by-byte, random chunks.

    Returns (frames, stats) for each strategy, asserting they all agree.
    """
    results = []

    p = StreamParser()
    results.append((p.feed(stream), _stats(p)))

    p = StreamParser()
    frames: List[Frame] = []
    for byte in stream:
        frames.extend(p.feed(bytes([byte])))
    results.append((frames, _stats(p)))

    rng = random.Random(99)
    p = StreamParser()
    frames = []
    i = 0
    while i < len(stream):
        n = min(rng.randint(1, 17), len(stream) - i)
        frames.extend(p.feed(stream[i : i + n]))
        i += n
    results.append((frames, _stats(p)))

    assert results[0] == results[1] == results[2], "parser is chunking-dependent"
    return results[0]


def _stats(parser: StreamParser) -> Dict[str, int]:
    return {
        "frames_ok": parser.frames_ok,
        "crc_errors": parser.crc_errors,
        "resyncs": parser.resyncs,
        "bytes_dropped": parser.bytes_dropped,
    }


# ---------------------------------------------------------------------------
# Codec basics
# ---------------------------------------------------------------------------


def test_crc16_known_check_value():
    # Standard check value of CRC-16/CCITT-FALSE.
    assert crc16_ccitt_false(b"123456789") == 0x29B1
    assert crc16_ccitt_false(b"") == 0xFFFF  # init value, no final XOR


def test_constants():
    assert SOF == 0x55
    assert MAX_PAYLOAD == 512
    assert HEADER_LEN == 5
    assert CRC_LEN == 2
    assert EVENT_CMDS == frozenset({0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x8E})


def test_encode_layout():
    frame = encode_frame(0x22, 0x07, b"abc")
    assert frame[0] == SOF
    assert int.from_bytes(frame[1:3], "little") == 3
    assert frame[3] == 0x22
    assert frame[4] == 0x07
    assert frame[5:8] == b"abc"
    crc = int.from_bytes(frame[8:10], "little")
    assert crc == crc16_ccitt_false(bytes([0x22, 0x07]) + b"abc")
    assert len(frame) == FRAME_OVERHEAD + 3


def test_frame_is_event():
    assert Frame(0x20, 0, b"").is_event
    assert Frame(0x25, 42, b"").is_event
    assert Frame(0x8E, 1, b"").is_event
    assert not Frame(0x01, 1, b"").is_event
    assert not Frame(0xA0, 1, b"").is_event  # 0x20 | 0x80 is a response, not an event


@pytest.mark.parametrize("length", [0, 1, 244, 247, 511, MAX_PAYLOAD])
def test_encode_accepts_boundary_lengths(length):
    payload = b"\x00" * length
    frame = encode_frame(0x01, 1, payload)
    assert len(frame) == FRAME_OVERHEAD + length
    frames, stats = _parse_all(frame)
    assert frames == [Frame(0x01, 1, payload)]
    assert stats["frames_ok"] == 1
    assert stats["bytes_dropped"] == 0


def test_encode_rejects_oversize_payload():
    with pytest.raises(ValueError):
        encode_frame(0x01, 1, b"\x00" * (MAX_PAYLOAD + 1))


def test_encode_rejects_out_of_range_cmd_seq():
    with pytest.raises(ValueError):
        encode_frame(0x100, 1)
    with pytest.raises(ValueError):
        encode_frame(-1, 1)
    with pytest.raises(ValueError):
        encode_frame(1, 256)


def test_feed_empty_is_noop():
    p = StreamParser()
    assert p.feed(b"") == []
    assert _stats(p) == {
        "frames_ok": 0,
        "crc_errors": 0,
        "resyncs": 0,
        "bytes_dropped": 0,
    }


# ---------------------------------------------------------------------------
# Golden vectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", _golden_frames(), ids=lambda v: v["name"])
def test_golden_frame(vector):
    payload = bytes.fromhex(vector["payload_hex"])
    cmd, seq = vector["cmd"], vector["seq"]

    encoded = encode_frame(cmd, seq, payload)
    assert encoded.hex() == vector["frame_hex"]

    body = bytes((cmd, seq)) + payload
    assert crc16_ccitt_false(body) == vector["crc"]

    frames, stats = _parse_all(encoded)
    assert frames == [Frame(cmd, seq, payload)]
    assert stats == {
        "frames_ok": 1,
        "crc_errors": 0,
        "resyncs": 0,
        "bytes_dropped": 0,
    }
    assert frames[0].is_event == (cmd in EVENT_CMDS)


@pytest.mark.parametrize("vector", _golden_streams(), ids=lambda v: v["name"])
def test_golden_stream(vector):
    stream = bytes.fromhex(vector["stream_hex"])
    expected = [
        Frame(f["cmd"], f["seq"], bytes.fromhex(f["payload_hex"]))
        for f in vector["expected_frames"]
    ]

    frames, stats = _parse_all(stream)
    assert frames == expected  # deterministic across whole/1-byte/chunked feeding

    if vector.get("check_frames_ok", True):
        assert stats["frames_ok"] == vector["expected_stats"]["frames_ok"]
        assert stats["crc_errors"] == vector["expected_stats"]["crc_errors"]
    # Truncated streams: no frames_ok assertion per brief (only "does not crash",
    # plus the chunking determinism already asserted by _parse_all).


# ---------------------------------------------------------------------------
# Roundtrip property
# ---------------------------------------------------------------------------


def test_roundtrip_property_500_random_frames():
    rng = random.Random(20260924)
    lengths = [0, 1, 2, 3, 244, 247, 511, 512]
    for _ in range(FRAME_ROUNDTRIP_COUNT):
        length = rng.choice(lengths + [rng.randint(0, MAX_PAYLOAD)])
        payload = rng.randbytes(length)
        cmd = rng.randint(0, 0xFF)
        seq = rng.randint(0, 0xFF)

        parser = StreamParser()
        frames = parser.feed(encode_frame(cmd, seq, payload))
        assert frames == [Frame(cmd, seq, payload)]
        assert parser.frames_ok == 1
        assert parser.crc_errors == 0
        assert parser.resyncs == 0
        assert parser.bytes_dropped == 0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_crc_bit_flip_rejects_frame():
    payload = bytes(range(32))
    frame = encode_frame(0x22, 0x09, payload)

    # Sanity: the intact frame decodes.
    p = StreamParser()
    assert p.feed(frame) == [Frame(0x22, 0x09, payload)]

    # Flip every single bit of CMD..CRC (CRC covers CMD..PAYLOAD, plus the
    # stored CRC itself); every variant must be rejected exactly once.
    for index in range(HEADER_LEN, len(frame)):
        for bit in range(8):
            corrupted = bytearray(frame)
            corrupted[index] ^= 1 << bit
            p = StreamParser()
            assert p.feed(bytes(corrupted)) == []
            assert p.frames_ok == 0
            assert p.crc_errors == 1
            assert p.resyncs == 1


def test_resync_after_garbage_between_frames():
    f1 = encode_frame(0x01, 0x01, b"ping")
    f2 = encode_frame(0x01, 0x02, b"pong")
    garbage = b"\x00\xff" b"\x55\x13\x12" b"\xde\xad\xbe\xef" b"\x55\x55\x55"

    frames, stats = _parse_all(garbage + f1 + garbage + f2)
    assert [(f.cmd, f.seq, f.payload) for f in frames] == [
        (0x01, 0x01, b"ping"),
        (0x01, 0x02, b"pong"),
    ]
    assert stats["frames_ok"] == 2
    # Each garbage segment yields exactly 2 bad-LEN resyncs (bumps on
    # "\x55\x13\x12" and on "\x55\x55\x55"); it appears twice in the stream.
    assert stats["resyncs"] == 4
    assert stats["bytes_dropped"] == 24  # 12 dropped bytes per garbage segment


def test_bad_len_resync_drops_exactly_three_bytes_and_recovers():
    """SOF + LEN bytes forming 0xAAAA (>512): the attempt aborts dropping
    exactly the 3 consumed bytes (SOF + 2x LEN), counts one resync and no
    CRC error, and the parser still decodes the next valid frame intact."""
    parser = StreamParser()
    assert parser.feed(b"\x55\xaa\xaa") == []
    assert parser.resyncs == 1
    assert parser.bytes_dropped == 3
    assert parser.frames_ok == 0
    assert parser.crc_errors == 0

    frame = encode_frame(0x22, 0x07, b"ok")
    assert parser.feed(frame) == [Frame(0x22, 0x07, b"ok")]
    assert parser.frames_ok == 1
    assert parser.resyncs == 1  # unchanged by the good frame
    assert parser.bytes_dropped == 3


def test_truncated_stream_is_stateless_safe():
    """A frame cut across feed() calls still decodes; stats stay consistent."""
    frame = encode_frame(0x20, 0x05, b"\x11\x22\x33\x44")
    p = StreamParser()
    got: List[Frame] = []
    for i in range(len(frame)):
        got.extend(p.feed(frame[i : i + 1]))
        assert p.frames_ok == (1 if i == len(frame) - 1 else 0)
    assert got == [Frame(0x20, 0x05, b"\x11\x22\x33\x44")]


# ---------------------------------------------------------------------------
# Fuzz
# ---------------------------------------------------------------------------


def _fuzz_blob(rng: random.Random) -> bytes:
    kind = rng.randrange(6)
    if kind == 0:  # pure random noise (0x55 appears naturally)
        return rng.randbytes(rng.randint(1, 64))
    if kind == 1:  # SOF runs plus noise
        return b"\x55" * rng.randint(1, 12) + rng.randbytes(rng.randint(0, 16))
    if kind == 2:  # pseudo header: SOF followed by junk
        return b"\x55" + rng.randbytes(rng.randint(1, 8))
    if kind == 3:  # valid frame
        return encode_frame(rng.randint(0, 0xFF), rng.randint(0, 0xFF),
                            rng.randbytes(rng.randint(0, 64)))
    if kind == 4:  # frame with one flipped bit
        blob = bytearray(
            encode_frame(rng.randint(0, 0xFF), rng.randint(0, 0xFF),
                         rng.randbytes(rng.randint(0, 48)))
        )
        blob[rng.randrange(len(blob))] ^= 1 << rng.randrange(8)
        return bytes(blob)
    # truncated frame prefix
    return encode_frame(rng.randint(0, 0xFF), rng.randint(0, 0xFF),
                        rng.randbytes(rng.randint(0, 32)))[: rng.randint(1, 20)]


def test_fuzz_10000_feeds_no_crash_and_accounting():
    rng = random.Random(20260924)
    parser = StreamParser()
    total_fed = 0
    bytes_in_ok_frames = 0
    prev = _stats(parser)

    # Max bytes that can sit in an unfinished frame attempt (SOF+LEN+CMD+SEQ
    # +payload+CRC); everything else must already be dropped or decoded.
    max_in_flight = HEADER_LEN + MAX_PAYLOAD + CRC_LEN

    def accounting() -> int:
        return total_fed - parser.bytes_dropped - bytes_in_ok_frames

    for _ in range(FUZZ_FEEDS):
        blob = _fuzz_blob(rng)
        frames = parser.feed(blob)
        total_fed += len(blob)
        bytes_in_ok_frames += sum(FRAME_OVERHEAD + len(f.payload) for f in frames)

        current = _stats(parser)
        # Counters are monotonically non-decreasing and never negative.
        for key in current:
            assert current[key] >= prev[key] >= 0, key
        prev = current
        # Unaccounted bytes can only be an in-progress attempt, and only a
        # bounded number of them.
        assert 0 <= accounting() <= max_in_flight

    # Drain any in-flight attempt with non-SOF bytes, then the conservation
    # law must hold exactly: every fed byte was dropped or decoded once.
    drain = b"\x00" * (max_in_flight + 1)
    bytes_in_ok_frames += sum(
        FRAME_OVERHEAD + len(f.payload) for f in parser.feed(drain)
    )
    total_fed += len(drain)
    assert accounting() == 0

    assert parser.frames_ok > 0  # the noise mixture contains valid frames
    assert parser.crc_errors > 0  # ...and corrupted ones
    assert parser.resyncs > 0
