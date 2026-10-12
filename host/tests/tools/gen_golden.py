"""Generate the golden frame test vectors (deterministic).

Usage (from ``host/``)::

    python -m tests.tools.gen_golden > tests/golden_frames.json

Seeded with ``random.seed(20260924)`` so the output is reproducible.
This file is the only sanctioned way to produce ``golden_frames.json``;
never hand-edit the JSON.

Ground truth is by construction:
- ``frames``: ``frame_hex``/``crc`` come from ``encode_frame``/``crc16_ccitt_false``.
  The algorithm itself is anchored externally by the CRC check value
  ``crc16_ccitt_false(b"123456789") == 0x29B1`` (asserted in test_framing.py).
- ``streams``: ``expected_frames`` are the frames deliberately embedded in the
  stream; the generator re-parses each stream with StreamParser and refuses to
  emit if the parse disagrees (guards against RNG garbage accidentally
  forming or eating frames under the fixed seed).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Make blebridge importable straight from the src tree (works with or without
# `pip install -e .`).
_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from blebridge.framing import (  # noqa: E402
    EVENT_CMDS,
    SOF,
    Frame,
    StreamParser,
    crc16_ccitt_false,
    encode_frame,
)

SEED = 20260924


def _rand_payload(n: int) -> bytes:
    return bytes(random.getrandbits(8) for _ in range(n))


def _random_no_sof(n: int) -> bytes:
    """Random bytes guaranteed not to contain SOF (0x55)."""
    out = bytearray()
    while len(out) < n:
        b = random.getrandbits(8)
        if b != SOF:
            out.append(b)
    return bytes(out)


def _frame_vectors() -> List[Dict[str, Any]]:
    specs: List[Tuple[str, int, int, bytes]] = [
        # Requests, responses, events; payload sizes hit 0/1/244/247/512 etc.
        ("req_ping_empty", 0x01, 0x01, b""),
        ("rsp_ping_empty", 0x81, 0x01, b""),
        ("req_info_empty", 0x02, 0x02, b""),
        ("rsp_info_payload", 0x82, 0x02, b"\x00\x01\x02\x03"),
        ("req_payload_1", 0x40, 0x03, _rand_payload(1)),
        ("req_payload_2", 0x41, 0x04, _rand_payload(2)),
        ("req_payload_3", 0x42, 0x05, _rand_payload(3)),
        ("evt_scan_result_20", 0x20, 0x07, _rand_payload(31)),
        ("evt_conn_state_21", 0x21, 0x08, _rand_payload(8)),
        ("evt_notify_22_244", 0x22, 0x09, _rand_payload(244)),
        ("evt_flow_credit_23", 0x23, 0x0A, _rand_payload(4)),
        ("evt_error_24", 0x24, 0x0B, _rand_payload(16)),
        ("evt_dbg_25", 0x25, 0x0C, _rand_payload(6)),
        ("evt_debug_8E", 0x8E, 0x01, _rand_payload(20)),
        ("req_suota_chunk_244", 0x50, 0x10, _rand_payload(244)),
        ("rsp_payload_244", 0xD0, 0x10, _rand_payload(244)),
        ("req_mtu_data_247", 0x51, 0x11, _rand_payload(247)),
        ("req_payload_511", 0x52, 0x12, _rand_payload(511)),
        ("req_payload_512_max", 0x53, 0x13, _rand_payload(512)),
        ("rsp_payload_512_max", 0xD3, 0x13, _rand_payload(512)),
        ("req_seq_0", 0x01, 0x00, b""),
        ("req_seq_255", 0x01, 0xFF, b"\xAA"),
        ("req_ascii_payload", 0x03, 0x21, b"bridge-probe"),
        ("req_zeros_payload", 0x04, 0x22, b"\x00" * 16),
        ("req_ff_payload", 0x05, 0x23, b"\xFF" * 16),
        ("evt_empty_payload", 0x20, 0x00, b""),
    ]

    vectors: List[Dict[str, Any]] = []
    for name, cmd, seq, payload in specs:
        frame = encode_frame(cmd, seq, payload)
        crc = crc16_ccitt_false(bytes((cmd, seq)) + payload)
        assert len(frame) == 5 + len(payload) + 2
        assert frame[0] == SOF
        assert int.from_bytes(frame[1:3], "little") == len(payload)
        assert frame[-2] == (crc & 0xFF) and frame[-1] == (crc >> 8)
        vectors.append(
            {
                "name": name,
                "cmd": cmd,
                "seq": seq,
                "payload_hex": payload.hex(),
                "frame_hex": frame.hex(),
                "crc": crc,
            }
        )
    return vectors


def _stream_vectors() -> List[Dict[str, Any]]:
    frame_a = encode_frame(0x22, 0x07, bytes(range(16)))  # event frame
    frame_b = encode_frame(0x81, 0x33, b"response-payload")  # response frame
    frame_c = encode_frame(0x20, 0x08, b"\xaa\x55\xbb")  # event, 0x55 inside

    # A frame with a deliberately corrupted CRC (flip 1 bit of the last byte):
    # consumed fully by the parser, rejected, then resync.
    bad = bytearray(encode_frame(0x51, 0x09, _rand_payload(40)))
    bad[-1] ^= 0x01

    def frame_json(frame: bytes) -> Dict[str, Any]:
        return {
            "cmd": frame[3],
            "seq": frame[4],
            "payload_hex": frame[5:-2].hex(),
        }

    def stats(n_frames: int, crc_errors: int) -> Dict[str, Any]:
        return {"frames_ok": n_frames, "crc_errors": crc_errors}

    json_a = frame_json(frame_a)
    json_b = frame_json(frame_b)
    json_c = frame_json(frame_c)

    streams: List[Dict[str, Any]] = []

    # 1) Leading garbage (no SOF) + a clean 3-byte bad-LEN abort sequence
    #    (0x55 AA AA -> LEN 0xAAAA > 512) then a complete frame.
    stream = _random_no_sof(32) + b"\x55\xaa\xaa" + frame_a
    streams.append(
        {
            "name": "leading_garbage_then_frame",
            "stream_hex": stream.hex(),
            "expected_frames": [json_a],
            "expected_stats": stats(1, crc_errors=0),
            "check_frames_ok": True,
        }
    )

    # 2) A complete frame followed by a truncated one (cut mid-payload).
    #    frames_ok is intentionally not asserted (brief: only "does not crash").
    truncated_tail = frame_b[: len(frame_b) // 2]
    stream = frame_a + truncated_tail
    streams.append(
        {
            "name": "frame_with_truncated_tail",
            "stream_hex": stream.hex(),
            "expected_frames": [json_a],
            "expected_stats": stats(1, crc_errors=0),
            "check_frames_ok": False,
        }
    )

    # 3) Three frames glued with garbage: bad-LEN bump, then a full bad-CRC
    #    frame, exercising sliding resync between every pair of valid frames.
    stream = frame_a + b"\x55\xaa\xaa" + frame_b + bytes(bad) + frame_c
    streams.append(
        {
            "name": "multi_frames_with_garbage_gaps",
            "stream_hex": stream.hex(),
            "expected_frames": [json_a, json_b, json_c],
            "expected_stats": stats(3, crc_errors=1),
            "check_frames_ok": True,
        }
    )

    # Generation-time self-check: the parser must reproduce the ground truth.
    for vector in streams:
        stream_bytes = bytes.fromhex(vector["stream_hex"])
        expected = [
            Frame(f["cmd"], f["seq"], bytes.fromhex(f["payload_hex"]))
            for f in vector["expected_frames"]
        ]
        parser = StreamParser()
        got = parser.feed(stream_bytes)
        if got != expected:
            raise SystemExit(
                f"stream self-check failed for {vector['name']}: "
                f"RNG garbage under seed {SEED} broke the expected frames; "
                "adjust the stream construction"
            )
        if vector["check_frames_ok"]:
            if parser.frames_ok != vector["expected_stats"]["frames_ok"]:
                raise SystemExit(f"frames_ok self-check failed for {vector['name']}")
            if parser.crc_errors != vector["expected_stats"]["crc_errors"]:
                raise SystemExit(f"crc_errors self-check failed for {vector['name']}")

    return streams


def main() -> None:
    random.seed(SEED)

    frames = _frame_vectors()
    streams = _stream_vectors()

    # Coverage guards required by the brief.
    sizes = {len(bytes.fromhex(v["payload_hex"])) for v in frames}
    for required in (0, 1, 244, 247, 512):
        assert required in sizes, f"missing boundary payload size {required}"
    cmds = {v["cmd"] for v in frames}
    assert cmds & EVENT_CMDS, "no event frames in golden vectors"
    assert any(c & 0x80 and c not in EVENT_CMDS for c in cmds), "no response frames"
    assert len(frames) >= 20, "need at least 20 frame vectors"
    assert len(streams) == 3

    doc = {
        "meta": {
            "protocol": "blebridge uart bridge v2 (ADR-002)",
            "generator": "python -m tests.tools.gen_golden > tests/golden_frames.json",
            "seed": SEED,
            "note": "Generated file - do not hand-edit.",
        },
        "frames": frames,
        "streams": streams,
    }
    json.dump(doc, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
