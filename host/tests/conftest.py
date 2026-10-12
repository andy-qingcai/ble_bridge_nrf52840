"""Shared pytest fixtures: build the C protocol library for the L2 contract tests.

Compiles ``firmware/proto/bridge_proto.c`` into a shared library with the
host C compiler (macOS-bundled ``cc`` works; no CMake required), once per
session, and exposes a thin ctypes wrapper mirroring ``bridge_proto.h``.
This is layer L2 of the testing strategy (docs/adr/ADR-005): the C half of
the cross-language framing contract against ``blebridge.framing``.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sysconfig
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROTO_SRC = _REPO_ROOT / "firmware" / "proto" / "bridge_proto.c"

# Mirrors bridge_parser_t below; keep both in sync (guarded on the C side by
# _Static_assert in bridge_proto.c).
_EXPECTED_PARSER_SIZEOF = 512 + 28

# Mirror of BRIDGE_ERR_TOO_LONG in bridge_proto.h.
_BRIDGE_ERR_TOO_LONG = -1000


class BridgeFrame(ctypes.Structure):
    """Mirror of bridge_frame_t."""

    _fields_ = [
        ("cmd", ctypes.c_uint8),
        ("seq", ctypes.c_uint8),
        ("len", ctypes.c_uint16),
        ("payload", ctypes.POINTER(ctypes.c_uint8)),
    ]


class BridgeParserState(ctypes.Structure):
    """ctypes mirror of bridge_parser_t in bridge_proto.h (keep in sync!)."""

    _fields_ = [
        ("payload", ctypes.c_uint8 * 512),
        ("header_rest", ctypes.c_uint8 * 2),
        ("crc_bytes", ctypes.c_uint8 * 2),
        ("frame_len", ctypes.c_uint16),
        ("payload_pos", ctypes.c_uint16),
        ("len_low", ctypes.c_uint8),
        ("header_pos", ctypes.c_uint8),
        ("crc_pos", ctypes.c_uint8),
        ("state", ctypes.c_uint8),
        ("frames_ok", ctypes.c_uint32),
        ("crc_errors", ctypes.c_uint32),
        ("resyncs", ctypes.c_uint32),
        ("bytes_dropped", ctypes.c_uint32),
    ]


def _find_cc() -> Optional[str]:
    """Locate a C compiler: macOS-bundled ``cc`` first, then clang/gcc,
    finally the compiler recorded in sysconfig."""
    for name in ("cc", "clang", "gcc"):
        cc = shutil.which(name)
        if cc:
            return cc
    configured = sysconfig.get_config_var("CC")
    if configured:
        cc = shutil.which(configured.split()[0])
        if cc:
            return cc
    return None


def _compile_shared_library(build_dir: Path) -> Path:
    """Compile bridge_proto.c into a shared library inside ``build_dir``.

    Raises RuntimeError with an actionable message on any failure.
    """
    if not _PROTO_SRC.exists():
        raise RuntimeError(
            f"C source not found: {_PROTO_SRC}\n"
            "expected layout: firmware/proto/bridge_proto.c at the repo root"
        )
    cc = _find_cc()
    if cc is None:
        raise RuntimeError(
            "no C compiler found (tried 'cc', 'clang', 'gcc' on PATH and "
            "sysconfig CC). Install the Xcode command line tools: xcode-select --install"
        )

    out = build_dir / "libbridge_proto.so"
    cmd = [
        cc,
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-O2",
        "-fPIC",
        "-shared",
        "-o",
        str(out),
        str(_PROTO_SRC),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except OSError as exc:
        raise RuntimeError(f"failed to run compiler {cc!r}: {exc}") from exc
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(
            "compiling firmware/proto/bridge_proto.c failed "
            f"(compiler: {cc}, exit {proc.returncode}).\n"
            f"command: {' '.join(cmd)}\n"
            f"stderr:\n{proc.stderr}"
        )
    return out


@pytest.fixture(autouse=True)
def _disable_serial_open_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the SerialTransport open-settle window (U-1) at 0 in tests.

    The real-hardware settle silently drains post-open glitch bytes for a
    wall-clock window (default 0.6 s); the suite must stay deterministic and
    fast, so tests default to ``0`` and opt back in explicitly with
    ``monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", ...)``.  FakeTransport has
    no settle and is unaffected either way.
    """
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0")


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point ``~/.blebridge/config.json`` (blebridge.config) at a per-test scratch file.

    resolve_serial/load_config read the developer's real config otherwise;
    every test must see an empty config unless it writes one explicitly.
    """
    monkeypatch.setenv("BLEBRIDGE_CONFIG", str(tmp_path / "blebridge-config.json"))


@pytest.fixture(scope="session")
def proto_lib_path(tmp_path_factory) -> Path:
    """Compile the C protocol library once per session (no hardware, no net)."""
    build_dir = tmp_path_factory.mktemp("bridge_proto_build")
    try:
        return _compile_shared_library(build_dir)
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)


class BridgeProtoC:
    """ctypes wrapper around the compiled bridge_proto C library."""

    BRIDGE_SOF = 0x55
    BRIDGE_MAX_PAYLOAD = 512
    BRIDGE_ERR_TOO_LONG = _BRIDGE_ERR_TOO_LONG

    def __init__(self, lib_path: Path):
        assert ctypes.sizeof(BridgeParserState) == _EXPECTED_PARSER_SIZEOF, (
            "BridgeParserState mirror drifted from blebridge_parser_t "
            f"(sizeof={ctypes.sizeof(BridgeParserState)}, "
            f"expected={_EXPECTED_PARSER_SIZEOF})"
        )
        self.lib = ctypes.CDLL(str(lib_path))
        lib = self.lib
        lib.bridge_crc16.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint16,
        ]
        lib.bridge_crc16.restype = ctypes.c_uint16
        lib.bridge_encode.argtypes = [
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint16,
        ]
        lib.bridge_encode.restype = ctypes.c_int
        lib.bridge_parser_init.argtypes = [ctypes.POINTER(BridgeParserState)]
        lib.bridge_parser_init.restype = None
        lib.bridge_parser_feed.argtypes = [
            ctypes.POINTER(BridgeParserState),
            ctypes.c_uint8,
            ctypes.POINTER(BridgeFrame),
        ]
        lib.bridge_parser_feed.restype = ctypes.c_int

    @staticmethod
    def _buf(data: bytes) -> Optional[ctypes.Array]:
        if len(data) == 0:
            return None  # NULL: safe, the C side never dereferences at len 0
        return (ctypes.c_uint8 * len(data)).from_buffer_copy(data)

    def crc16(self, data: bytes) -> int:
        """uint16_t bridge_crc16(const uint8_t *data, uint16_t len)."""
        return int(self.lib.bridge_crc16(self._buf(data), len(data)))

    def encode(
        self, cmd: int, seq: int, payload: bytes = b"", cap: Optional[int] = None
    ) -> object:
        """Call bridge_encode(); returns frame bytes on success (rc >= 0),
        or the negative C return code (capacity report / BRIDGE_ERR_TOO_LONG).
        """
        payload = bytes(payload)
        needed = 5 + len(payload) + 2
        if cap is None:
            cap = needed
        out = (ctypes.c_uint8 * cap)() if cap > 0 else None
        rc = int(
            self.lib.bridge_encode(
                cmd, seq, self._buf(payload), len(payload), out, cap
            )
        )
        if rc >= 0:
            return bytes(out[:rc])
        return rc

    def parser(self) -> "CStreamParser":
        """A fresh bridge_parser_t wired through bridge_parser_feed()."""
        return CStreamParser(self.lib)


class CStreamParser:
    """Byte-by-byte driver over bridge_parser_feed with StreamParser-ish API."""

    def __init__(self, lib: ctypes.CDLL):
        self._feed = lib.bridge_parser_feed
        self._init = lib.bridge_parser_init
        self.state = BridgeParserState()
        self._frame = BridgeFrame()
        self._init(ctypes.byref(self.state))

    def feed_byte(self, byte: int) -> int:
        """Feed a single byte, returning the raw bridge_parser_feed() code
        (1 = frame, 0 = incomplete, -1 = resync drop)."""
        return int(
            self._feed(
                ctypes.byref(self.state),
                byte,
                ctypes.byref(self._frame),
            )
        )

    def feed(self, data: bytes) -> List[Tuple[int, int, bytes]]:
        """Consume bytes; return decoded frames as (cmd, seq, payload)."""
        frames: List[Tuple[int, int, bytes]] = []
        state_ref = ctypes.byref(self.state)
        frame_ref = ctypes.byref(self._frame)
        for byte in data:
            rc = int(self._feed(state_ref, byte, frame_ref))
            if rc == 1:
                frames.append(
                    (
                        int(self._frame.cmd),
                        int(self._frame.seq),
                        ctypes.string_at(self._frame.payload, self._frame.len),
                    )
                )
            elif rc not in (0, -1):
                raise AssertionError(f"bridge_parser_feed returned {rc!r}")
        return frames

    def reinit(self) -> None:
        """Re-run bridge_parser_init() on the same parser instance."""
        self._init(ctypes.byref(self.state))

    @property
    def stats(self) -> Dict[str, int]:
        s = self.state
        return {
            "frames_ok": int(s.frames_ok),
            "crc_errors": int(s.crc_errors),
            "resyncs": int(s.resyncs),
            "bytes_dropped": int(s.bytes_dropped),
        }


@pytest.fixture(scope="session")
def proto(proto_lib_path) -> BridgeProtoC:
    """Loaded C protocol library (session-cached CDLL wrapper)."""
    return BridgeProtoC(proto_lib_path)
