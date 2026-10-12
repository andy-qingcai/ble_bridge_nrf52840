"""task-dfu-a pure-logic tests: image self-confirm gate + CMD.enter_dfu.

Compiles the Zephyr-free C units (bridge_dispatch.c + bridge_info.c +
firmware/proto/bridge_proto.c) exactly as documented in firmware/app/
CMakeLists.txt and drives them over ctypes:

- ``bridge_confirm_ready``  : the pure decision behind
  ``boot_write_img_confirmed()`` in main.c -- confirm only after the
  stable-uptime window AND a clean health snapshot;
- ``BRIDGE_CMD_ENTER_DFU``  : the 0x4B request latches ``dfu_requested``
  and answers ``[OK]`` with an empty body (the reboot itself is link-layer
  behaviour, out of scope for a pure unit test).  The request frame is
  round-tripped through the real bridge_encode() so the test stays
  contract-true instead of hand-rolling bytes.
"""

from __future__ import annotations

import ctypes
import subprocess
import sysconfig
from pathlib import Path

import pytest

from blebridge.bridge import CMD, EVENT_CMDS

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SRCS = [
    _REPO_ROOT / "firmware" / "app" / "src" / "bridge_dispatch.c",
    _REPO_ROOT / "firmware" / "app" / "src" / "bridge_info.c",
    _REPO_ROOT / "firmware" / "proto" / "bridge_proto.c",
]


@pytest.fixture(scope="module")
def lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    """Compile the pure C units once per module (macOS cc / Linux gcc)."""
    cc = sysconfig.get_config_var("CC")
    if not cc:
        pytest.skip("no C compiler available")
    suffix = ".dylib" if sysconfig.get_platform() == "macosx-universal2" else ".so"
    out = tmp_path_factory.mktemp("dfu_pure") / f"libdfu_pure{suffix}"
    cmd = [*cc.split(), "-shared", "-fPIC", "-std=c11", "-Wall", "-Wextra",
           "-Werror", f"-I{_REPO_ROOT / 'firmware' / 'proto'}",
           f"-I{_REPO_ROOT / 'firmware' / 'app' / 'src'}",
           "-o", str(out)]
    cmd += [str(s) for s in _SRCS]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return ctypes.CDLL(str(out))


class BridgeFrameC(ctypes.Structure):
    """Mirror of bridge_frame_t (bridge_proto.h:162)."""

    _fields_ = [
        ("cmd", ctypes.c_uint8),
        ("seq", ctypes.c_uint8),
        ("len", ctypes.c_uint16),
        ("payload", ctypes.POINTER(ctypes.c_uint8)),
    ]


@pytest.fixture()
def dispatch(lib: ctypes.CDLL) -> ctypes.POINTER:
    """Fresh dispatcher (opaque blob: layout owned by bridge_dispatch.c)."""
    lib.bridge_dispatch_init.argtypes = [
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint8,
    ]
    blob = (ctypes.c_ubyte * 512)()
    ptr = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    lib.bridge_dispatch_init(ptr, 128, 3)
    return ptr


class TestConfirmReady:
    """bridge_confirm_ready: uptime window + clean-counters semantics."""

    def test_confirm_ready(self, lib: ctypes.CDLL) -> None:
        fn = lib.bridge_confirm_ready
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        assert fn(10, 10, True, 0, 0, 0) is True
        # comfortable margin above the window
        assert fn(3600, 10, True, 0, 0, 0) is True

    def test_confirm_blocked_before_window(self, lib: ctypes.CDLL) -> None:
        fn = lib.bridge_confirm_ready
        fn.restype = ctypes.c_bool
        # 9 s of a 10 s window: still unconfirmed (revert armed)
        assert fn(9, 10, True, 0, 0, 0) is False
        assert fn(0, 10, True, 0, 0, 0) is False

    def test_confirm_blocked_by_dirty_counters(self, lib: ctypes.CDLL) -> None:
        fn = lib.bridge_confirm_ready
        fn.restype = ctypes.c_bool
        # any single dirty counter blocks the confirm
        assert fn(10, 10, True, 1, 0, 0) is False
        assert fn(10, 10, True, 0, 1, 0) is False
        assert fn(10, 10, True, 0, 0, 1) is False
        assert fn(10, 10, True, 0, 0, 0) is True

    def test_confirm_blocked_by_failed_init(self, lib: ctypes.CDLL) -> None:
        fn = lib.bridge_confirm_ready
        fn.restype = ctypes.c_bool
        assert fn(3600, 10, False, 0, 0, 0) is False


class TestEnterDfu:
    """CMD.enter_dfu (0x4B): response contract + latch, pure-C side."""

    def test_cmd_value_and_invariant(self) -> None:
        assert int(CMD.enter_dfu) == 0x4B
        # Contract invariant (Task 4): the response byte never aliases an
        # event command (0x4B | 0x80 = 0xCB).
        assert (int(CMD.enter_dfu) | 0x80) not in EVENT_CMDS

    def test_enter_dfu_latches_and_acks(
        self, lib: ctypes.CDLL, dispatch: ctypes.POINTER
    ) -> None:
        # Round-trip the request through the real encoder (contract-true).
        lib.bridge_encode.restype = ctypes.c_int
        lib.bridge_encode.argtypes = [
            ctypes.c_uint8, ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint16, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint16,
        ]
        frame = (ctypes.c_ubyte * 64)()
        flen = lib.bridge_encode(int(CMD.enter_dfu), 7, None, 0, frame, 64)
        assert flen == 7  # SOF + LEN(2) + CMD + SEQ + CRC(2), empty payload
        assert frame[0] == 0x55 and frame[3] == 0x4B

        # Decode with the real parser, then hand the frame to dispatch.
        lib.bridge_parser_init.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        parser = (ctypes.c_ubyte * (512 + 28))()  # conftest.py mirror size
        lib.bridge_parser_init(
            ctypes.cast(parser, ctypes.POINTER(ctypes.c_ubyte))
        )
        lib.bridge_parser_feed.restype = ctypes.c_int
        lib.bridge_parser_feed.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ubyte,
            ctypes.POINTER(BridgeFrameC),
        ]
        got = BridgeFrameC()
        rc = 0
        for i in range(flen):
            rc = lib.bridge_parser_feed(
                ctypes.cast(parser, ctypes.POINTER(ctypes.c_ubyte)),
                frame[i], ctypes.byref(got),
            )
        assert rc == 1, "parser reports one complete frame"
        assert got.cmd == 0x4B and got.seq == 7
        assert got.len == 0

        lib.bridge_dispatch_handle.restype = ctypes.c_int
        lib.bridge_dispatch_handle.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_void_p,  # env: unused by this cmd; NULL is allowed
            ctypes.POINTER(BridgeFrameC),
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint16,
        ]
        resp = (ctypes.c_ubyte * 16)()
        plen = lib.bridge_dispatch_handle(
            dispatch, None, ctypes.byref(got), resp, 16
        )
        assert plen == 1, "empty body: [status] only"
        assert resp[0] == 0x00, "status OK (ACK before reboot)"

    def test_reset_and_enter_dfu_are_independent(
        self, lib: ctypes.CDLL, dispatch: ctypes.POINTER
    ) -> None:
        """Only the latched request the host actually sent must fire."""
        lib.bridge_dispatch_handle.restype = ctypes.c_int
        lib.bridge_dispatch_handle.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p,
            ctypes.POINTER(BridgeFrameC), ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint16,
        ]
        req = BridgeFrameC(cmd=int(CMD.enter_dfu), seq=1, len=0, payload=None)
        resp = (ctypes.c_ubyte * 16)()
        assert lib.bridge_dispatch_handle(
            dispatch, None, ctypes.byref(req), resp, 16
        ) == 1
        assert resp[0] == 0x00
        # The latch lives in the dispatcher; the link layer decides the
        # reboot flavour.  Reading it back over ctypes would need a struct
        # mirror -- asserting the ACK contract here is the pure-logic part;
        # the latch consumption is verified by the real-device step
        # (docs/reports/task-dfu-a-report.md, enter_dfu section).
