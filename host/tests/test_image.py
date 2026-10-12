"""Tests for blebridge.image: header families + XOR upload payload (FR-C4).

The fixture-image assertions are locked to ``synthetic_image.img``, a
self-consistent DA1469x-family image generated deterministically by
``tests/tools/gen_test_image.py`` (no third-party binary involved).
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from blebridge.image import (  # noqa: E402
    ImageInfo,
    UnknownImageFormat,
    build_payload,
    parse,
    verify_payload_crc,
    xor_byte,
)

FIXTURES = Path(__file__).parent / "fixtures"
SYNTH = FIXTURES / "synthetic_image.img"


# ------------------------------------------------------- fixture image


def test_parse_synthetic_measured_fields() -> None:
    info = parse(SYNTH)
    assert info.family == "DA1469x"
    assert info.payload_len == 8192
    assert info.version == "9.9.9.9"
    # header_hex is the raw 34-byte DA1469x header (signature 51 71 first)
    raw = SYNTH.read_bytes()
    assert len(info.header_hex) == 68
    assert bytes.fromhex(info.header_hex) == raw[:34]
    assert info.header_hex.startswith("5171")
    # the fixture's declared CRC32 is the real CRC32 of its payload region
    assert info.crc32 == (zlib.crc32(raw[34:]) & 0xFFFFFFFF)


def test_synthetic_payload_region_matches_header() -> None:
    """Cross-check the fixture: payload region ends exactly at EOF."""
    raw = SYNTH.read_bytes()
    assert len(raw) == 34 + 8192
    pointer_to_ivt = struct.unpack_from("<I", raw, 30)[0]
    assert pointer_to_ivt == 34
    info = parse(SYNTH)
    assert pointer_to_ivt + info.payload_len == len(raw)


def test_synthetic_declared_crc32_matches_payload_region() -> None:
    info = parse(SYNTH)
    assert verify_payload_crc(SYNTH.read_bytes(), info) is True


def test_build_payload_synthetic_xor_tail() -> None:
    # Upload body = file + 1 whole-file XOR checksum byte (AN-B-022).
    raw = SYNTH.read_bytes()
    payload = build_payload(SYNTH)
    assert len(payload) == len(raw) + 1
    assert payload[-1] == xor_byte(raw)
    assert payload[:-1] == raw


def test_xor_byte_basics() -> None:
    assert xor_byte(b"") == 0
    assert xor_byte(b"\x01\x02\x04") == 0x07
    assert xor_byte(b"\xff\xff") == 0x00


# ------------------------------------------------------- unknown / damaged


def test_unknown_signature_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.img"
    bad.write_bytes(b"\x12\x34" + b"\x00" * 100)
    with pytest.raises(UnknownImageFormat):
        parse(bad)


def test_empty_file_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.img"
    empty.write_bytes(b"")
    with pytest.raises(UnknownImageFormat):
        parse(empty)


def test_one_byte_file_rejected(tmp_path: Path) -> None:
    one = tmp_path / "one.img"
    one.write_bytes(b"\x51")
    with pytest.raises(UnknownImageFormat):
        parse(one)


def test_truncated_69x_header_rejected(tmp_path: Path) -> None:
    short = tmp_path / "short.img"
    short.write_bytes(b"\x51\x71" + b"\x00" * 10)  # needs 34 bytes
    with pytest.raises(UnknownImageFormat):
        parse(short)


# --------------------------------------------------------- other families


def _da1458x_image(code_size: int, crc: int, version: bytes, payload: bytes) -> bytes:
    """DA1458x layout (AN-B-022): 64-byte header + payload."""
    header = b"\x70\x51"  # signature
    header += bytes([0x01])  # valid_flag
    header += bytes([0x00])  # image_id
    header += struct.pack("<II", code_size, crc)  # code_size, CRC @4/@8
    header += version.ljust(16, b"\x00")[:16]  # version @12
    header += struct.pack("<I", 0x6AB251A4)  # timestamp @28
    header += bytes([0x00])  # encryption @32
    header += b"\x00" * 31  # reserved @33..64
    assert len(header) == 64
    return header + payload


def test_parse_da1458x(tmp_path: Path) -> None:
    payload = b"\xaa" * 128
    img = tmp_path / "f58x.img"
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    img.write_bytes(_da1458x_image(128, crc, b"9.9.9.9", payload))
    info = parse(img)
    assert info.family == "DA1458x"
    assert info.payload_len == 128
    assert info.crc32 == crc
    assert info.version == "9.9.9.9"
    assert len(info.header_hex) == 128  # 64-byte header
    assert verify_payload_crc(img.read_bytes(), info) is True


def _da1468x_image(code_size: int, crc: int, version: bytes, payload: bytes) -> bytes:
    """DA1468x layout: 36-byte header, payload at ``exec_location``.

    The official HeaderInfo68x.getPayloadOffset() returns ``exec_location``,
    so a self-consistent synthetic file places the payload right after the
    header and sets exec_location = 36.
    """
    header = b"\x70\x61"  # signature
    header += struct.pack("<H", 0x0001)  # flags @2
    header += struct.pack("<II", code_size, crc)  # code_size, CRC @4/@8
    header += version.ljust(16, b"\x00")[:16]  # version @12
    header += struct.pack("<I", 0x6AB251A4)  # timestamp @28
    header += struct.pack("<I", 36)  # exec_location = payload start @32
    assert len(header) == 36
    return header + payload


def test_parse_da1468x(tmp_path: Path) -> None:
    payload = b"\x55" * 64
    img = tmp_path / "f68x.img"
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    img.write_bytes(_da1468x_image(64, crc, b"2.0.0.0", payload))
    info = parse(img)
    assert info.family == "DA1468x"
    assert info.payload_len == 64
    assert info.crc32 == crc
    assert info.version == "2.0.0.0"
    assert verify_payload_crc(img.read_bytes(), info) is True


def test_synthetic_69x_version_truncated_at_ff(tmp_path: Path) -> None:
    """Version field stops at the first 0x00 or 0xFF byte."""
    img = tmp_path / "v69x.img"
    header = b"\x51\x71"
    header += struct.pack("<II", 8, zlib.crc32(b"payload!"))  # size, crc @2/@6
    header += b"1.2.3".ljust(16, b"\xff")  # version padded with 0xFF @10
    header += struct.pack("<I", 0x6AB251A4)  # timestamp @26
    header += struct.pack("<I", 34)  # pointer_to_ivt = payload start @30
    img.write_bytes(header + b"payload!\x00")
    info = parse(img)
    assert info.family == "DA1469x"
    assert info.version == "1.2.3"
    assert info.payload_len == 8
    assert verify_payload_crc(img.read_bytes(), info) is True


def test_image_info_is_the_brief_contract() -> None:
    fields = set(ImageInfo.__dataclass_fields__)
    assert fields == {"family", "version", "payload_len", "crc32", "header_hex"}
