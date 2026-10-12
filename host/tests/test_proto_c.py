"""L2 cross-language contract tests (ADR-005): firmware/proto C library vs blebridge.framing.

Layered like test_framing.py, but every vector is checked against BOTH
implementations:

1. Golden vectors (tests/golden_frames.json): C encode output must equal the
   recorded frame bytes, the C CRC must equal the recorded CRC, and feeding
   the frame back through the C parser must decode the original triple.
2. Differential fuzz (the core of this task): the same seeded random byte
   streams (valid frames, noise, SOF bursts, bad CRC, bad LEN) are fed to the
   Python StreamParser and to the C parser byte by byte.  The decoded frame
   sequences AND the four counters (frames_ok / crc_errors / resyncs /
   bytes_dropped) must match exactly, and the byte-conservation invariant
   must hold for both.
3. Encode boundaries: payload 0/512/513 and capacity exactly/one short.

No hardware, no network, no CMake: the session fixture in conftest.py
compiles firmware/proto/bridge_proto.c with the host ``cc``.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

# Import blebridge straight from the src tree (works with or without
# `pip install -e .`); conftest.py provides the compiled C library fixtures.
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from blebridge.framing import (  # noqa: E402
    FRAME_OVERHEAD,
    MAX_PAYLOAD,
    StreamParser,
    crc16_ccitt_false,
    encode_frame,
)

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_frames.json"
GOLDEN: Dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

FUZZ_STREAMS = 2048
FUZZ_SEED = 20260924

# Non-SOF drain: fed after a stream it can never *start* an attempt, so after
# at most (pending payload + 2 CRC) bytes both parsers are back in WAIT_SOF
# and the framing.py conservation law holds over all bytes consumed.
_DRAIN = b"\x00" * (FRAME_OVERHEAD + MAX_PAYLOAD + 5)


def _golden_frames() -> List[Dict[str, Any]]:
    return GOLDEN["frames"]


def _golden_streams() -> List[Dict[str, Any]]:
    return GOLDEN["streams"]


def _stats_dict(
    frames_ok: int, crc_errors: int, resyncs: int, bytes_dropped: int
) -> Dict[str, int]:
    return {
        "frames_ok": frames_ok,
        "crc_errors": crc_errors,
        "resyncs": resyncs,
        "bytes_dropped": bytes_dropped,
    }


def _parse_both(proto, stream: bytes) -> Tuple[
    StreamParser,
    "CStreamParser",
    List[Tuple[int, int, bytes]],
    Dict[str, int],
    List[Tuple[int, int, bytes]],
    Dict[str, int],
]:
    """Feed one stream to both implementations; return (py_parser, c_parser,
    py_frames, py_stats, c_frames, c_stats)."""
    py_parser = StreamParser()
    py_frames = [(f.cmd, f.seq, f.payload) for f in py_parser.feed(stream)]

    c_parser = proto.parser()
    c_frames = c_parser.feed(stream)
    return py_parser, c_parser, py_frames, _py_stats(py_parser), c_frames, c_parser.stats


def _py_stats(parser: StreamParser) -> Dict[str, int]:
    return {
        "frames_ok": parser.frames_ok,
        "crc_errors": parser.crc_errors,
        "resyncs": parser.resyncs,
        "bytes_dropped": parser.bytes_dropped,
    }


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------


def test_crc_anchor_and_python_agreement(proto):
    """Standard CRC-16/CCITT-FALSE check value plus C==Python over a sweep of
    deterministic lengths (C's len parameter is uint16)."""
    assert proto.crc16(b"123456789") == 0x29B1
    assert proto.crc16(b"") == 0xFFFF  # init value, no final XOR

    rng = random.Random(FUZZ_SEED)
    for length in (0, 1, 2, 3, 8, 31, 244, 247, 255, 256, 511, 512, 514, 1000, 4096):
        data = rng.randbytes(length)
        assert proto.crc16(data) == crc16_ccitt_false(data), length


# ---------------------------------------------------------------------------
# Golden frames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", _golden_frames(), ids=lambda v: v["name"])
def test_golden_frame_encode_and_parse(proto, vector):
    payload = bytes.fromhex(vector["payload_hex"])
    cmd, seq = vector["cmd"], vector["seq"]

    encoded = proto.encode(cmd, seq, payload)
    assert isinstance(encoded, bytes)  # success, not a negative code
    assert encoded.hex() == vector["frame_hex"]

    assert proto.crc16(bytes((cmd, seq)) + payload) == vector["crc"]

    parser = proto.parser()
    assert parser.feed(encoded) == [(cmd, seq, payload)]
    assert parser.stats == _stats_dict(frames_ok=1, crc_errors=0, resyncs=0, bytes_dropped=0)


@pytest.mark.parametrize("vector", _golden_streams(), ids=lambda v: v["name"])
def test_golden_stream_cross_language(proto, vector):
    """Golden streams: C parser must reproduce the recorded frames and agree
    with the Python parser on all four counters."""
    stream = bytes.fromhex(vector["stream_hex"])
    expected = [
        (f["cmd"], f["seq"], bytes.fromhex(f["payload_hex"]))
        for f in vector["expected_frames"]
    ]

    py_parser, c_parser, py_frames, py_stats, c_frames, c_stats = _parse_both(proto, stream)

    assert c_frames == expected
    assert c_frames == py_frames
    assert c_stats == py_stats

    if vector.get("check_frames_ok", True):
        assert c_stats["frames_ok"] == vector["expected_stats"]["frames_ok"]
        assert c_stats["crc_errors"] == vector["expected_stats"]["crc_errors"]

    # Conservation: drain any in-flight attempt (e.g. the truncated tail),
    # then every consumed byte must be dropped or decoded exactly once.
    py_frames += [(f.cmd, f.seq, f.payload) for f in py_parser.feed(_DRAIN)]
    c_frames += c_parser.feed(_DRAIN)
    assert c_frames == py_frames
    frame_bytes = sum(FRAME_OVERHEAD + len(p) for _, _, p in c_frames)
    total = len(stream) + len(_DRAIN)
    assert c_parser.stats["bytes_dropped"] + frame_bytes == total
    assert _py_stats(py_parser)["bytes_dropped"] + frame_bytes == total


# ---------------------------------------------------------------------------
# Parser semantics pinned against framing.py
# ---------------------------------------------------------------------------


def test_feed_return_code_semantics(proto):
    """bridge_parser_feed: 1 = frame done, 0 = keep going, -1 = resync drop."""
    parser = proto.parser()
    frame = encode_frame(0x22, 0x07, b"\x01\x02")

    # A good frame: 0 on every byte, 1 on the last.
    assert [parser.feed_byte(b) for b in frame] == [0] * (len(frame) - 1) + [1]
    assert parser.stats == _stats_dict(1, 0, 0, 0)

    # Bad LEN (0xAAAA > 512): the third byte triggers the resync.
    assert [parser.feed_byte(b) for b in b"\x55\xaa\xaa"] == [0, 0, -1]
    assert parser.stats == _stats_dict(1, 0, resyncs=1, bytes_dropped=3)

    # CRC mismatch: the last CRC byte triggers the resync (drops 5+LEN+2).
    bad = bytearray(encode_frame(0x51, 0x09, b"\x00" * 8))
    bad[-1] ^= 0x01
    assert [parser.feed_byte(b) for b in bad] == [0] * (len(bad) - 1) + [-1]
    assert parser.stats == _stats_dict(1, 1, resyncs=2, bytes_dropped=3 + len(bad))

    # Non-SOF garbage in WAIT_SOF: dropped and counted, but NOT a resync (0).
    assert parser.feed_byte(0x00) == 0
    assert parser.stats == _stats_dict(1, 1, 2, 3 + len(bad) + 1)


def test_bad_len_513_drops_exactly_three_bytes(proto):
    """LEN 513 (> 512): 3-byte abort, one resync, no CRC error, then recovery."""
    parser = proto.parser()
    assert parser.feed(b"\x55\x01\x02") == []  # LEN = 0x0201 = 513
    assert parser.stats == _stats_dict(0, 0, resyncs=1, bytes_dropped=3)

    frame = encode_frame(0x20, 0x01, b"ok")
    assert parser.feed(frame) == [(0x20, 0x01, b"ok")]
    assert parser.stats == _stats_dict(1, 0, resyncs=1, bytes_dropped=3)


def test_parser_reinitialisation_clears_everything(proto):
    """bridge_parser_init() on a used parser resets state, buffers and all
    four counters (mirrors StreamParser.__init__)."""
    frame = encode_frame(0x21, 0x02, b"\xaa" * 16)
    parser = proto.parser()
    parser.feed(b"\x55\xaa\xaa")  # leave a resync + dropped bytes behind
    parser.feed(frame)
    assert parser.stats == _stats_dict(1, 0, 1, 3)

    parser.reinit()
    assert parser.stats == _stats_dict(0, 0, 0, 0)
    # The recycled parser must behave like a fresh one, byte for byte.
    assert parser.feed(frame) == [(0x21, 0x02, b"\xaa" * 16)]
    assert parser.stats == _stats_dict(1, 0, 0, 0)


# ---------------------------------------------------------------------------
# Encode boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 244, 247, 511, MAX_PAYLOAD])
def test_encode_matches_python_at_boundaries(proto, length):
    payload = b"\x5a" * length
    expected = encode_frame(0x44, 0x33, payload)

    rc = proto.encode(0x44, 0x33, payload)
    assert rc == expected
    assert len(rc) == FRAME_OVERHEAD + length


def test_encode_payload_too_long(proto):
    rc = proto.encode(0x01, 0x01, b"\x00" * (MAX_PAYLOAD + 1))
    assert rc == proto.BRIDGE_ERR_TOO_LONG


@pytest.mark.parametrize("length", [0, 3, 244, MAX_PAYLOAD])
def test_encode_capacity_report(proto, length):
    payload = b"\x77" * length
    needed = FRAME_OVERHEAD + length

    # Exactly enough: succeeds.
    assert proto.encode(0x55, 0xAA, payload, cap=needed) == encode_frame(0x55, 0xAA, payload)
    # One byte short: negative required length, nothing written.
    assert proto.encode(0x55, 0xAA, payload, cap=needed - 1) == -needed
    # Zero capacity: same report.
    assert proto.encode(0x55, 0xAA, payload, cap=0) == -needed


def test_encode_roundtrip_500_random_frames(proto):
    rng = random.Random(FUZZ_SEED)
    lengths = [0, 1, 2, 3, 244, 247, 511, 512]
    for _ in range(500):
        length = rng.choice(lengths + [rng.randint(0, MAX_PAYLOAD)])
        payload = rng.randbytes(length)
        cmd = rng.randint(0, 0xFF)
        seq = rng.randint(0, 0xFF)

        frame = proto.encode(cmd, seq, payload)
        assert frame == encode_frame(cmd, seq, payload)

        parser = proto.parser()
        assert parser.feed(frame) == [(cmd, seq, payload)]
        assert parser.stats == _stats_dict(1, 0, 0, 0)


# ---------------------------------------------------------------------------
# Differential fuzz (the core of the L2 contract)
# ---------------------------------------------------------------------------


def _fuzz_stream(rng: random.Random) -> bytes:
    """One random stream mixing valid frames, noise, SOF bursts, bad LEN,
    bad CRC and truncated frames (superset of the L0 _fuzz_blob mixture)."""
    parts: List[bytes] = []
    for _ in range(rng.randint(1, 6)):
        kind = rng.randrange(8)
        if kind == 0:  # plain noise (0x55 appears naturally)
            parts.append(rng.randbytes(rng.randint(1, 96)))
        elif kind == 1:  # SOF runs
            parts.append(b"\x55" * rng.randint(1, 16))
        elif kind == 2:  # canonical bad LEN (0xAAAA > 512)
            parts.append(b"\x55\xaa\xaa")
        elif kind == 3:  # random LEN > 512
            lo = rng.randrange(256)
            hi = rng.randrange(2, 256)
            while (hi << 8) | lo <= MAX_PAYLOAD:
                lo = rng.randrange(256)
                hi = rng.randrange(2, 256)
            parts.append(b"\x55" + bytes((lo, hi)))
        elif kind == 4:  # valid frame, boundary payload sizes included
            length = rng.choice((0, 1, 2, 3, 31, 244, 247, 511, 512))
            parts.append(
                encode_frame(rng.randrange(256), rng.randrange(256), rng.randbytes(length))
            )
        elif kind == 5:  # frame with one flipped bit (always a CRC error,
            # sometimes a different LEN)
            blob = bytearray(
                encode_frame(
                    rng.randrange(256), rng.randrange(256), rng.randbytes(rng.randint(0, 96))
                )
            )
            blob[rng.randrange(len(blob))] ^= 1 << rng.randrange(8)
            parts.append(bytes(blob))
        elif kind == 6:  # truncated frame prefix (possibly complete)
            blob = encode_frame(
                rng.randrange(256), rng.randrange(256), rng.randbytes(rng.randint(0, 128))
            )
            parts.append(blob[: rng.randint(1, len(blob))])
        else:  # SOF + junk header/payload/CRC
            parts.append(b"\x55" + rng.randbytes(rng.randint(1, 24)))
    return b"".join(parts)


def test_differential_fuzz_2048_streams(proto):
    """Same seeded random streams into both parsers: identical frame
    sequences AND identical frames_ok/crc_errors/resyncs/bytes_dropped."""
    rng = random.Random(FUZZ_SEED)
    totals = _stats_dict(0, 0, 0, 0)

    for i in range(FUZZ_STREAMS):
        stream = _fuzz_stream(rng)
        py_parser = StreamParser()
        py_frames = [(f.cmd, f.seq, f.payload) for f in py_parser.feed(stream)]

        c_parser = proto.parser()
        c_frames = c_parser.feed(stream)

        assert c_frames == py_frames, f"stream #{i} decoded frames differ"
        assert c_parser.stats == _py_stats(py_parser), f"stream #{i} counters differ"

        # Drain any in-flight attempt on both sides, re-check the frame
        # agreement, then verify byte conservation over everything consumed
        # (framing.py docstring invariant).
        py_frames += [(f.cmd, f.seq, f.payload) for f in py_parser.feed(_DRAIN)]
        c_frames += c_parser.feed(_DRAIN)
        assert c_frames == py_frames, f"stream #{i} frames differ after drain"
        assert c_parser.stats == _py_stats(py_parser), f"stream #{i} counters differ after drain"

        frame_bytes = sum(FRAME_OVERHEAD + len(p) for _, _, p in c_frames)
        total = len(stream) + len(_DRAIN)
        assert c_parser.stats["bytes_dropped"] + frame_bytes == total, f"stream #{i}"
        assert _py_stats(py_parser)["bytes_dropped"] + frame_bytes == total, f"stream #{i}"

        for key in totals:
            totals[key] += c_parser.stats[key]

    # Coverage guards (fixed seed, so these are deterministic; measured
    # baseline: ~770 ok frames, ~1020 crc errors, ~6000 resyncs).
    assert totals["frames_ok"] > 500
    assert totals["crc_errors"] > 500
    assert totals["resyncs"] > totals["crc_errors"]  # bad-LEN aborts included
    assert totals["bytes_dropped"] > 100000
