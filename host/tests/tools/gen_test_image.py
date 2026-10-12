"""Generate the synthetic SUOTA image fixture (deterministic).

Usage (from ``host/``)::

    python -m tests.tools.gen_test_image > tests/fixtures/synthetic_image.img

The fixture is a self-consistent DA1469x-family image: 34-byte header
(signature ``51 71``) + a deterministic pseudo-random payload region.  It
replaces the former third-party real-device firmware fixture so the header
parsing / XOR upload-body tests run without shipping any proprietary binary.

Ground truth is by construction:
- ``payload_len`` = len(payload), ``crc32`` = CRC32 of the payload region,
- ``pointer_to_ivt`` = 34 (payload starts right after the header),
- ``version`` = b"9.9.9.9".

Determinism: the payload bytes come from a seeded LCG (xorshift), so
re-running the generator reproduces the exact fixture bytes.
"""

from __future__ import annotations

import struct
import sys
import zlib

HEADER_LEN = 34
PAYLOAD_LEN = 8192
VERSION = b"9.9.9.9"
TIMESTAMP = 0x6AB251A4  # arbitrary fixed value


def _payload(n: int) -> bytes:
    """Deterministic pseudo-random bytes (xorshift32, nonzero)."""
    out = bytearray()
    state = 0x9E3779B9
    for _ in range(n):
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        out.append(state & 0xFF)
    return bytes(out)


def build_image() -> bytes:
    payload = _payload(PAYLOAD_LEN)
    header = b"\x51\x71"  # DA1469x signature (bytes on the wire: 51 71)
    header += struct.pack("<II", PAYLOAD_LEN, zlib.crc32(payload) & 0xFFFFFFFF)
    header += VERSION.ljust(16, b"\x00")[:16]  # version @10
    header += struct.pack("<I", TIMESTAMP)  # timestamp @26
    header += struct.pack("<I", HEADER_LEN)  # pointer_to_ivt = payload start @30
    assert len(header) == HEADER_LEN
    return header + payload


def main() -> None:
    sys.stdout.buffer.write(build_image())


if __name__ == "__main__":
    main()
