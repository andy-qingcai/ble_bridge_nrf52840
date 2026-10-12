"""SUOTA firmware image parsing: header families + XOR upload payload (FR-C4).

Authority: Dialog/Renesas AN-B-022 and the suotalib reference (header
families, field layouts) plus the SUOTA spec's one-byte XOR checksum
appended to the upload.  Three header families are recognised by the first
two file bytes read as a big-endian signature pair:

=======  ========  ==========  ============================================
sig      family    header len  payload region
=======  ========  ==========  ============================================
0x5171   DA1469x   34          starts at ``pointer_to_ivt`` (u32 LE @30)
0x7061   DA1468x   36          starts at ``exec_location`` (u32 LE @32)
0x7051   DA1458x   64          fixed offset 64
=======  ========  ==========  ============================================

The DA1469x signature is checked first (AN-B-022 order; note
the signature *bytes* are ``51 71`` — the pair value is 0x5171, not 0x7151).

``parse`` is deliberately parse-only: it reports what the header declares
(payload size, CRC32) and performs no file consistency check.  The payload
CRC32 is verified by the device after the END signal; a host-side helper is
:func:`verify_payload_crc` for preflight use.

The uploaded payload is the whole image file plus ONE trailing byte: the
XOR of every file byte (whole-file XOR checksum per AN-B-022).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Union

__all__ = [
    "ImageInfo",
    "UnknownImageFormat",
    "parse",
    "build_payload",
    "xor_byte",
    "verify_payload_crc",
]

PathLike = Union[str, Path]


@dataclass
class ImageInfo:
    """Parsed SUOTA image header (fields per the header-family layout)."""

    family: str  # "DA1469x" | "DA1468x" | "DA1458x"
    version: str  # ASCII version string, truncated at NUL / 0xFF
    payload_len: int  # header-declared payload size in bytes
    crc32: int  # header-declared CRC32 of the payload region
    header_hex: str  # hex of the raw header bytes (header_len * 2 chars)


class UnknownImageFormat(ValueError):
    """The file does not start with any known SUOTA image signature."""


def xor_byte(data: bytes) -> int:
    """XOR of every byte of ``data`` (0 for empty input)."""
    value = 0
    for byte in data:
        value ^= byte
    return value


def build_payload(path: PathLike) -> bytes:
    """Whole file plus the trailing one-byte XOR checksum (the upload body).

    The SUOTA receiver checks this byte per transfer (the whole-file XOR
    checksum per AN-B-022).
    """
    data = Path(path).read_bytes()
    return data + bytes([xor_byte(data)])


def verify_payload_crc(data: bytes, info: ImageInfo) -> bool:
    """Preflight CRC32 check of the payload region against the header.

    ``data`` must be the raw image file bytes.  Optional convenience — the
    authoritative check happens on the device after the END signal.
    """
    header = bytes.fromhex(info.header_hex)
    start = _payload_offset(header)
    region = data[start : start + info.payload_len]
    return len(region) == info.payload_len and (
        zlib.crc32(region) & 0xFFFFFFFF
    ) == info.crc32


def parse(path: PathLike) -> ImageInfo:
    """Parse the image header of ``path``.

    Raises:
        UnknownImageFormat: unknown signature, empty/short file, or a
            header truncated before its family's header length.
    """
    return _parse_bytes(Path(path).read_bytes())


# ----------------------------------------------------------------- internals


def _version_at(data: bytes, offset: int, length: int) -> str:
    """ASCII version field, truncated at the first NUL or 0xFF byte."""
    raw = data[offset : offset + length]
    valid = 0
    for byte in raw:
        if byte == 0x00 or byte == 0xFF:
            break
        valid += 1
    return raw[:valid].decode("ascii", errors="replace")


def _payload_offset(header: bytes) -> int:
    """Payload start offset for an already-recognised header."""
    signature = (header[0] << 8) | header[1]
    if signature == 0x5171:
        offset: int = struct.unpack_from("<I", header, 30)[0]  # pointer_to_ivt
        return offset
    if signature == 0x7061:
        offset = struct.unpack_from("<I", header, 32)[0]  # exec_location
        return offset
    return 64  # 0x7051 / DA1458x: payload always starts right after the header


def _parse_bytes(data: bytes) -> ImageInfo:
    if len(data) < 2:
        raise UnknownImageFormat(f"file too short for a SUOTA header ({len(data)} B)")

    signature = (data[0] << 8) | data[1]

    if signature == 0x5171:  # DA1469x (SUOTA 1.1), 34-byte header
        header_len = 34
        if len(data) < header_len:
            raise UnknownImageFormat("truncated DA1469x header")
        payload_len, payload_crc = struct.unpack_from("<II", data, 2)
        # offset 26: timestamp (u32 LE); offset 30: pointer_to_ivt
        return ImageInfo(
            family="DA1469x",
            version=_version_at(data, 10, 16),
            payload_len=payload_len,
            crc32=payload_crc,
            header_hex=data[:header_len].hex(),
        )

    if signature == 0x7061:  # DA1468x, 36-byte header
        header_len = 36
        if len(data) < header_len:
            raise UnknownImageFormat("truncated DA1468x header")
        payload_len, payload_crc = struct.unpack_from("<II", data, 4)
        # offset 28: timestamp; offset 32: exec_location = payload start
        return ImageInfo(
            family="DA1468x",
            version=_version_at(data, 12, 16),
            payload_len=payload_len,
            crc32=payload_crc,
            header_hex=data[:header_len].hex(),
        )

    if signature == 0x7051:  # DA1458x, 64-byte header
        header_len = 64
        if len(data) < header_len:
            raise UnknownImageFormat("truncated DA1458x header")
        payload_len, payload_crc = struct.unpack_from("<II", data, 4)
        # offset 28: timestamp; payload fixed at offset 64
        return ImageInfo(
            family="DA1458x",
            version=_version_at(data, 12, 16),
            payload_len=payload_len,
            crc32=payload_crc,
            header_hex=data[:header_len].hex(),
        )

    raise UnknownImageFormat(f"unknown image signature {signature:#06x}")
