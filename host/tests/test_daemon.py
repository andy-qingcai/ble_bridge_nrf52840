"""Tests for blebridge.daemon (TCP-JSON facade) + the Task 4 bridge additions.

Everything runs on FakeTransport descendants (synchronous pump model): no
hardware, no background threads except the explicitly-tested serve loop, and
no real waiting beyond sub-0.1 s windows.

Covers:

- bridge additions: wire-SEQ gap detection (EventRing.wire_gaps + injected
  EVT.DEBUG/0xFF) and inject_local_event (pure addition, no credit use);
- handle_command contract for the whole command set (legacy 15 + suota
  family + raw escape hatch), envelope shape and ``_ms``;
- drain/events incremental semantics over the daemon-level global seq that
  survives reconnects;
- reconnect: fault-injected transport -> rebuild -> retry once ->
  one-shot ``reconnected`` marker; double failure -> ok=false;
- suota_start full success on the SuotaFake device model, failure
  eventing, suota_abort;
- TCP serve layer: bad JSON lines, socketpair connection, real loopback
  serve_forever round trip on an ephemeral port.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Dict

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from blebridge.bridge import CMD, EVT, BridgeHost, EventRing  # noqa: E402
from blebridge.daemon import (  # noqa: E402
    SUOTA_DONE,
    SUOTA_FAILED,
    SUOTA_PROGRESS,
    BridgeDaemon,
    connect_payload,
    parse_gattx_payload,
    parse_scan_payload,
    uuid16_arg,
)
from blebridge.framing import EVENT_CMDS, StreamParser, encode_frame  # noqa: E402
from blebridge.suota import SUOTA_UUIDS, uuid16_prefix  # noqa: E402
from blebridge.transport import FakeTransport  # noqa: E402

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS))

from suota_fake import SuotaFakeTransport, synthetic_image  # noqa: E402

GOOD_RESPONSES: Dict[int, bytes] = {
    int(CMD.ping): b"\x00\x01",
    int(CMD.info): b"\x00" + b"blebridge-v2",
    int(CMD.scan_start): b"\x00",
    int(CMD.scan_stop): b"\x00",
    int(CMD.disconnect): b"\x00",
    int(CMD.mtu): b"\x00",
    int(CMD.gatt_read): b"\x00\xaa\xbb",
    int(CMD.gatt_write): b"\x00",
    int(CMD.subscribe): b"\x00",
    int(CMD.unsubscribe): b"\x00",
    int(CMD.disc_svc): b"\x00\x18\x00\xf5\xfe",
    int(CMD.conn_update): b"\x00",
    int(CMD.phy): b"\x00",
    int(CMD.adv_start): b"\x00",
    int(CMD.adv_stop): b"\x00",
    int(CMD.notify): b"\x00",
    int(CMD.set_char): b"\x00",
    int(CMD.gatt_define): b"\x00\x40\x00",
    int(CMD.reset): b"\x00",
    int(CMD.log_level): b"\x00",
    int(CMD.suota_status): b"\x00\x02",
}


class GoodFake(FakeTransport):
    """FakeTransport answering every documented bridge command with status 0."""

    def __init__(self) -> None:
        super().__init__(responses=dict(GOOD_RESPONSES))


class FlakyFake(GoodFake):
    """GoodFake whose ``read`` raises OSError for the first N calls."""

    def __init__(self, fail_reads: int) -> None:
        super().__init__()
        self.fail_reads = fail_reads

    def read(self, n: int, timeout: float) -> bytes:
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise OSError("usb re-enumerated")
        return super().read(n, timeout)


def make_daemon(fake, **kwargs) -> BridgeDaemon:
    kwargs.setdefault("reconnect_delay", 0.0)
    return BridgeDaemon(
        port="FAKEPORT", transport_factory=lambda port: fake, **kwargs
    )


def written_frames(fake: FakeTransport) -> list:
    return StreamParser().feed(bytes(fake.written))


def scan_adv(
    addr: str = "E0:F1:00:02:03:04",
    rssi: int = -70,
    addr_type: int = 1,
    adv_type: int = 0,
    adv: bytes = b"\x02\x01\x06",
    wire_seq: int = 0,
) -> bytes:
    """Wire frame for one advert (v1.4.1): subtype + addr_type + addr +
    rssi + adv_type + adv."""
    digits = bytes.fromhex(addr.replace(":", ""))
    payload = (
        bytes([0x00, addr_type]) + digits + struct.pack("<b", rssi)
        + bytes([adv_type]) + adv
    )
    return encode_frame(int(EVT.SCAN), wire_seq, payload)


# ------------------- daemon baud/rtscts passthrough (bring-up 链路欠账) -------


def test_daemon_serial_params_defaults_and_passthrough(monkeypatch) -> None:
    # UX 波: 构造参数三态化（None = 延迟到首次使用时经 resolve_serial 解析）。
    daemon = BridgeDaemon(port="FAKEPORT", reconnect_delay=0.0)
    assert daemon.baud is None
    assert daemon.rtscts is None
    fake = FakeTransport(responses={int(CMD.ping): b"\x00"})
    daemon = BridgeDaemon(
        port="FAKEPORT",
        transport_factory=lambda port: fake,
        reconnect_delay=0.0,
    )
    daemon.handle_command({"cmd": "ping"})
    # 显式 port + 未给 baud/rtscts → 解析为 FR-A5 默认（FAKEPORT 非 usbmodem → 流控开）。
    assert daemon.baud == 921600
    assert daemon.rtscts is True

    opened = {}

    class RecordingSerial:
        def __init__(self, port, baudrate=921600, rtscts=True, timeout=0.1):
            opened["kwargs"] = (port, baudrate, rtscts)
            self.port = port
            self.baudrate = baudrate
            self.rtscts = rtscts
            self.is_open = False

        def open(self):
            self.is_open = True

        def close(self):
            self.is_open = False

        def write(self, data):
            pass

        def read(self, n, timeout):
            return b""

    monkeypatch.setattr("blebridge.daemon.SerialTransport", RecordingSerial)
    # Custom line settings reach the default factory (bring-up link case).
    daemon = BridgeDaemon(port="FAKEPORT", baud=115200, rtscts=False, reconnect_delay=0.0)
    daemon.handle_command({"cmd": "ping"})
    assert opened["kwargs"] == ("FAKEPORT", 115200, False)
    # ...and survive a reconnect cycle (factory rebuilt with the same params).
    daemon._reconnect()
    daemon.handle_command({"cmd": "ping"})
    assert opened["kwargs"] == ("FAKEPORT", 115200, False)
    daemon.shutdown()


def test_daemon_auto_resolves_port_when_none(monkeypatch) -> None:
    """UX 波: port=None 时 daemon 也走 resolve_serial 自动探测（含 rtscts 自动）。"""
    monkeypatch.setattr(
        "blebridge.config.list_usb_candidates", lambda: ["/dev/cu.usbmodemAuto"]
    )
    daemon = BridgeDaemon(reconnect_delay=0.0)
    monkeypatch.setattr(
        "blebridge.daemon.SerialTransport",
        lambda port, **kwargs: FakeTransport(
            responses={int(CMD.ping): b"\x00"}, script=[]
        ),
    )
    assert daemon._ensure_bridge() is not None
    assert daemon.port == "/dev/cu.usbmodemAuto"
    assert daemon.baud == 921600
    assert daemon.rtscts is False  # usbmodem → CDC 自动关流控
    daemon.shutdown()


# ======================= bridge additions (Task 4 前置增补) ===================


def evt_frame(wire_seq: int, subtype: int = 1, payload: bytes = b"") -> bytes:
    return encode_frame(int(EVT.SCAN), wire_seq, bytes([subtype]) + payload)


def test_wire_gap_counts_missing_and_injects_debug_event() -> None:
    fake = FakeTransport(script=[evt_frame(0), evt_frame(1), evt_frame(5)])
    host = BridgeHost(fake)
    fake.open()
    events = host.drain()
    scans = [e for e in events if e.evt == int(EVT.SCAN)]
    assert [e.subtype for e in scans] == [1, 1, 1]
    assert host.events.wire_gaps == 3  # wire SEQ 2,3,4 never arrived
    gap = events[-1]
    assert gap.evt == int(EVT.DEBUG)
    assert gap.subtype == 0xFF
    assert b"prev=1" in gap.payload and b"got=5" in gap.payload
    assert b"missing=3" in gap.payload


def test_wire_gap_wraparound_is_contiguous_no_gap() -> None:
    script = [evt_frame(253), evt_frame(254), evt_frame(255), evt_frame(0)]
    host = BridgeHost(FakeTransport(script=script))
    host.transport.open()
    host.drain()
    assert host.events.wire_gaps == 0
    assert not [e for e in host.events if e.subtype == 0xFF]


def test_wire_gap_first_event_and_repeat() -> None:
    # Any starting SEQ is fine (host may attach mid-stream)...
    host = BridgeHost(FakeTransport(script=[evt_frame(137)]))
    host.transport.open()
    host.drain()
    assert host.events.wire_gaps == 0
    # ...but a repeated wire SEQ counts every skipped slot (255).
    fake = FakeTransport(script=[evt_frame(9), evt_frame(9)])
    host = BridgeHost(fake)
    fake.open()
    host.drain()
    assert host.events.wire_gaps == 255


def test_wire_gap_survives_across_pumps_and_accumulates() -> None:
    fake = FakeTransport(script=[evt_frame(0)])
    host = BridgeHost(fake)
    fake.open()
    host.drain()
    fake.inject_raw(evt_frame(7))
    host.drain()
    fake.inject_raw(evt_frame(9))
    host.drain()
    assert host.events.wire_gaps == 6 + 1
    gaps = [e for e in host.events if e.evt == int(EVT.DEBUG) and e.subtype == 0xFF]
    assert len(gaps) == 2


def test_inject_local_event_appends_without_consuming_credits() -> None:
    fake = GoodFake()
    host = BridgeHost(fake)
    fake.open()
    host.request(int(CMD.ping), timeout=0.5)
    credits_before = host._credits
    written_before = len(fake.written)

    event = host.inject_local_event(int(EVT.SUOTA), 0x00, b"\x01\x02")
    assert (event.evt, event.subtype, event.payload) == (int(EVT.SUOTA), 0, b"\x01\x02")
    assert host.drain()[-1].payload == b"\x01\x02"
    assert host._credits == credits_before  # local events consume no credit
    assert len(fake.written) == written_before  # and write nothing


def test_event_ring_wire_gaps_defaults_to_zero() -> None:
    ring = EventRing(capacity=4)
    assert ring.wire_gaps == 0


def test_cmd_response_space_never_collides_with_events() -> None:
    """Protocol invariant (Task 4 fix round 1, controller ruling).

    A response frame carries ``cmd | 0x80``; that byte must never fall into
    the event command space, or responses and events become ambiguous.
    Guards every future CMD addition: CMD.log_level moved 0x0E -> 0x0F
    because 0x0E | 0x80 = 0x8E collided with EVT.DEBUG (0x0E is RESERVED).
    """
    for member in CMD:
        assert (int(member) | 0x80) not in (EVENT_CMDS | {0x8E}), (
            f"CMD.{member.name} ({int(member):#04x}) response byte "
            f"{int(member) | 0x80:#04x} collides with the event space"
        )


def test_log_level_uses_0x0f_and_0x0e_is_reserved() -> None:
    assert int(CMD.log_level) == 0x0F
    assert 0x0E not in {int(m) for m in CMD}  # RESERVED slot stays unassigned
    # Round trip on the new command value: response 0x8F matches, and a
    # genuine EVT.DEBUG frame is unaffected.
    fake = GoodFake()
    host = BridgeHost(fake)
    fake.open()
    status, body = host.request(int(CMD.log_level), b"\x03", timeout=0.5)
    assert (status, body) == (0, b"")
    fake.inject_raw(encode_frame(int(EVT.DEBUG), 7, b"\x01hello"))
    (event,) = host.drain()
    assert (event.evt, event.subtype, event.payload) == (int(EVT.DEBUG), 1, b"hello")
    assert all(f.cmd != 0x0E for f in written_frames(fake))


# ============================ handle_command contract ========================


def test_ping_envelope_ok_data_and_ms() -> None:
    daemon = make_daemon(GoodFake())
    resp = daemon.handle_command({"cmd": "ping"})
    assert resp["ok"] is True
    assert resp["data"]["pong"] is True
    assert resp["data"]["status"] == 0
    assert resp["_ms"] > 0


def test_unknown_cmd() -> None:
    daemon = make_daemon(GoodFake())
    resp = daemon.handle_command({"cmd": "time_travel"})
    assert resp["ok"] is False
    assert resp["error"] == "unknown_cmd"
    assert resp["_ms"] > 0


def test_missing_arg_and_bad_hex() -> None:
    daemon = make_daemon(GoodFake())
    assert daemon.handle_command({"cmd": "read"})["error"] == "missing_arg:uuid"
    resp = daemon.handle_command({"cmd": "write", "uuid": "f5fe", "data": "zz"})
    assert resp["ok"] is False and "bad_arg:data" in resp["error"]


@pytest.mark.parametrize(
    "request_obj,expected",
    [
        ({"cmd": "info"}, {"status": 0, "hex": b"blebridge-v2".hex()}),
        ({"cmd": "disconnect"}, {"status": 0}),
        ({"cmd": "mtu"}, {"status": 0}),
        ({"cmd": "read", "uuid": "f5fe"}, {"status": 0, "hex": "aabb"}),
        ({"cmd": "read", "uuid": SUOTA_UUIDS["mem_dev"]}, {"status": 0, "hex": "aabb"}),
        ({"cmd": "read", "handle": 40}, {"status": 0, "hex": "aabb"}),
        ({"cmd": "write", "handle": 40, "data": "0102"}, {"status": 0}),
        ({"cmd": "write", "handle": 40, "handle_type": 1, "data": "01"}, {"status": 0}),
        ({"cmd": "subscribe", "handle": 42, "cccd_handle": 43}, {"status": 0}),
        ({"cmd": "unsubscribe", "handle": 42}, {"status": 0}),
        ({"cmd": "conn_update"}, {"status": 0, "hex": ""}),
        ({"cmd": "conn_update", "interval_min": 16, "interval_max": 32, "latency": 1, "supervision_timeout": 500}, {"status": 0, "hex": ""}),
        ({"cmd": "phy", "phy": 2}, {"status": 0, "hex": ""}),
        ({"cmd": "adv_start"}, {"status": 0}),
        ({"cmd": "adv_start", "interval_ms": 250, "name": False}, {"status": 0}),
        ({"cmd": "adv_stop"}, {"status": 0}),
        ({"cmd": "notify", "data": "aabb"}, {"status": 0}),
        ({"cmd": "notify", "data": "aa", "uuid16": "ffe2"}, {"status": 0}),
        ({"cmd": "set_char", "data": "aabb"}, {"status": 0}),
        (
            {
                "cmd": "gatt_define",
                "svc_uuid16": "ffa0",
                "chars": [{"uuid16": "ffa1", "read": True, "notify": True, "max_len": 64}],
            },
            {"status": 0, "base_handle": 0x40},
        ),
        ({"cmd": "write", "uuid": "f5fe", "data": "0102"}, {"status": 0}),
        (
            {"cmd": "write", "uuid": "f5fe", "data": "01", "with_response": False},
            {"status": 0},
        ),
        ({"cmd": "subscribe", "uuid": "df94"}, {"status": 0}),
        ({"cmd": "enable_notify", "uuid": "df94"}, {"status": 0}),
        ({"cmd": "unsubscribe", "uuid": "df94"}, {"status": 0}),
        ({"cmd": "disc_svc", "uuid": "f5fe"}, {"status": 0, "hex": "1800f5fe"}),
        ({"cmd": "disc_all"}, {"status": 0, "hex": "1800f5fe"}),
        ({"cmd": "reset"}, {"status": 0}),
        ({"cmd": "log_level", "level": 3}, {"status": 0}),
        ({"cmd": "suota_status"}, {"status": 0, "hex": "02"}),
        (
            {"cmd": "raw", "raw_cmd": "0x08", "payload_hex": "fe f5"},
            {"status": 0, "hex": "aabb"},
        ),
        ({"cmd": "raw", "raw_cmd": 1}, {"status": 0, "hex": "01"}),
    ],
)
def test_command_contract_table(request_obj: dict, expected: dict) -> None:
    daemon = make_daemon(GoodFake())
    resp = daemon.handle_command(request_obj)
    assert resp["ok"] is True, resp
    assert resp["_ms"] > 0
    for key, value in expected.items():
        assert resp["data"][key] == value


def test_handle_addressing_and_generic_cmd_wire_payloads() -> None:
    """Generic-bridge addressing: uuid16 field 0x0000 switches read/write/
    subscribe onto raw attribute handles; conn_update/phy encodings."""
    fake = GoodFake()
    daemon = make_daemon(fake)
    daemon.handle_command({"cmd": "read", "handle": 0x1234})
    daemon.handle_command({"cmd": "write", "handle": 0x11, "data": "0a0b"})
    daemon.handle_command(
        {"cmd": "write", "handle": 0x11, "handle_type": 1, "data": "0a", "with_response": False}
    )
    daemon.handle_command({"cmd": "subscribe", "handle": 42, "cccd_handle": 43})
    daemon.handle_command({"cmd": "unsubscribe", "handle": 42})
    daemon.handle_command({"cmd": "conn_update", "interval_min": 16, "latency": 2})
    daemon.handle_command({"cmd": "phy", "phy": 1})
    payloads = {f.cmd: f.payload for f in written_frames(fake)}
    assert payloads[int(CMD.gatt_read)] == b"\x00\x00\x34\x12"
    writes = [f for f in written_frames(fake) if f.cmd == int(CMD.gatt_write)]
    assert writes[0].payload == b"\x00\x00\x00\x00\x11\x00\x0a\x0b"  # type 0 + h 0x11
    assert writes[1].payload == b"\x01\x00\x00\x01\x11\x00\x0a"  # type 1 (desc), wo resp
    assert payloads[int(CMD.subscribe)] == b"\x00\x00\x2a\x00\x2b\x00"
    assert payloads[int(CMD.unsubscribe)] == b"\x00\x00\x2a\x00"
    assert payloads[int(CMD.conn_update)] == struct.pack("<HHHH", 16, 40, 2, 400)
    assert payloads[int(CMD.phy)] == b"\x01"


def test_generic_cmd_bad_args() -> None:
    daemon = make_daemon(GoodFake())
    assert daemon.handle_command({"cmd": "phy", "phy": 3})["ok"] is False
    assert daemon.handle_command({"cmd": "phy", "phy": "fast"})["ok"] is False
    assert daemon.handle_command({"cmd": "subscribe", "handle": 4})["ok"] is False
    resp = daemon.handle_command({"cmd": "conn_update", "interval_min": 99999})
    assert resp["ok"] is False and "bad_arg" in resp["error"]


def test_request_payload_encodings_on_the_wire() -> None:
    fake = GoodFake()
    daemon = make_daemon(fake)
    daemon.handle_command({"cmd": "read", "uuid": "F5FE"})
    daemon.handle_command({"cmd": "write", "uuid": "f5fe", "data": "0a0b"})
    daemon.handle_command(
        {"cmd": "write", "uuid": "f5fe", "data": "0a", "with_response": False}
    )
    daemon.handle_command({"cmd": "subscribe", "uuid": SUOTA_UUIDS["serv_status"]})
    daemon.handle_command({"cmd": "log_level", "level": 2})
    cmds = [f.cmd for f in written_frames(fake)]
    payloads = {f.cmd: f.payload for f in written_frames(fake)}
    assert cmds.count(int(CMD.gatt_read)) == 1
    assert payloads[int(CMD.gatt_read)] == b"\xfe\xf5"
    writes = [f for f in written_frames(fake) if f.cmd == int(CMD.gatt_write)]
    assert writes[0].payload == b"\x00\xfe\xf5\x0a\x0b"  # with response
    assert writes[1].payload == b"\x01\xfe\xf5\x0a"  # without response
    assert payloads[int(CMD.subscribe)] == uuid16_prefix(SUOTA_UUIDS["serv_status"]).to_bytes(2, "little")
    assert payloads[int(CMD.log_level)] == b"\x02"


# ----------------------------- FR-H peripheral mode -------------------------


def test_peripheral_cmd_wire_payloads() -> None:
    """adv_start/notify/set_char/gatt_define request encodings (FR-H)."""
    fake = GoodFake()
    daemon = make_daemon(fake)
    daemon.handle_command({"cmd": "adv_start"})  # defaults
    daemon.handle_command({"cmd": "adv_start", "interval_ms": 250, "name": False})
    daemon.handle_command({"cmd": "notify", "data": "aabb"})  # preset char
    daemon.handle_command({"cmd": "notify", "data": "aa", "uuid16": "ffe2"})
    daemon.handle_command({"cmd": "set_char", "data": "aabb"})
    daemon.handle_command(
        {
            "cmd": "gatt_define",
            "svc_uuid16": "ffa0",
            "chars": [
                {"uuid16": "ffa1", "read": True, "notify": True, "max_len": 64},
                {"uuid16": 0xFFA2, "write_wo_resp": True},
            ],
        }
    )
    frames = written_frames(fake)
    payloads = {f.cmd: f.payload for f in frames}
    starts = [f for f in frames if f.cmd == int(CMD.adv_start)]
    assert [f.payload for f in starts] == [b"\xff\xff\x01", struct.pack("<H", 250) + b"\x00"]
    notifies = [f for f in frames if f.cmd == int(CMD.notify)]
    assert [f.payload for f in notifies] == [b"\x00\xaa\xbb", b"\x01\xe2\xff\xaa"]
    (set_char,) = [f for f in frames if f.cmd == int(CMD.set_char)]
    assert set_char.payload == b"\x00\xaa\xbb"
    define = payloads[int(CMD.gatt_define)]
    assert define == (
        b"\xa0\xff"  # svc uuid16 ffa0 LE
        + b"\x02"  # n_chars
        + b"\xa1\xff\x09\x40\x00"  # ffa1: read|notify(0x09), max_len 64
        + b"\xa2\xff\x02\xf4\x00"  # ffa2: write_wo_resp, max_len 244
    )


def test_gatt_define_redefinition_sends_reproducible_frame() -> None:
    """FR-H5 redefinition is a host-visible overwrite: the second define of
    the same svc_uuid16 carries a byte-identical payload (the firmware
    unregisters the old service and rebuilds its slot)."""
    fake = GoodFake()
    daemon = make_daemon(fake)
    req = {
        "cmd": "gatt_define",
        "svc_uuid16": "ffa0",
        "chars": [{"uuid16": "ffa1", "read": True, "notify": True, "max_len": 32}],
    }
    assert daemon.handle_command(req)["ok"] is True
    assert daemon.handle_command(req)["ok"] is True
    defines = [f for f in written_frames(fake) if f.cmd == int(CMD.gatt_define)]
    assert len(defines) == 2
    assert defines[0].payload == defines[1].payload


def test_gatt_define_busy_status_surfaced_not_raised() -> None:
    """FR-H5: BUSY (0x73, peripheral link up / no slot) is the firmware's
    verdict and reaches the caller verbatim as status, never an exception."""
    fake = FakeTransport(responses={int(CMD.gatt_define): b"\x73"})
    daemon = make_daemon(fake)
    resp = daemon.handle_command(
        {
            "cmd": "gatt_define",
            "svc_uuid16": "ffa0",
            "chars": [{"uuid16": "ffa1", "read": True}],
        }
    )
    assert resp["ok"] is True
    assert resp["data"] == {"status": 0x73, "base_handle": 0}


def test_gatt_define_bad_args() -> None:
    daemon = make_daemon(GoodFake())
    missing = daemon.handle_command({"cmd": "gatt_define"})
    assert missing["ok"] is False and missing["error"] == "missing_arg:svc_uuid16"
    zero = daemon.handle_command(
        {"cmd": "gatt_define", "svc_uuid16": 0, "chars": [{"uuid16": 1, "read": True}]}
    )
    assert zero["ok"] is False and "bad_arg:svc_uuid16" in zero["error"]
    empty = daemon.handle_command({"cmd": "gatt_define", "svc_uuid16": "ffa0", "chars": []})
    assert empty["ok"] is False and "bad_arg:chars" in empty["error"]
    no_uuid = daemon.handle_command(
        {"cmd": "gatt_define", "svc_uuid16": "ffa0", "chars": [{"read": True}]}
    )
    assert no_uuid["ok"] is False and "bad_arg:chars" in no_uuid["error"]
    no_props = daemon.handle_command(
        {"cmd": "gatt_define", "svc_uuid16": "ffa0", "chars": [{"uuid16": 1}]}
    )
    assert no_props["ok"] is False and "bad_arg" in no_props["error"]


def test_notify_status_074_no_subscriber_passthrough() -> None:
    """FR-H: notify without a subscribed peer answers 0x74 verbatim."""
    fake = FakeTransport(responses={int(CMD.notify): b"\x74"})
    daemon = make_daemon(fake)
    resp = daemon.handle_command({"cmd": "notify", "data": "aa"})
    assert resp["ok"] is True
    assert resp["data"]["status"] == 0x74


def test_parse_gattx_payload_central_and_peripheral() -> None:
    """EVT.GATTX decoding: subtype 0x00 central-role notify (unchanged
    format) vs 0x01 peripheral-role write with the role marker."""
    central = SimpleNamespace(evt=int(EVT.GATTX), subtype=0x00, payload=b"\xe2\xff\x02ok")
    assert parse_gattx_payload(central) == {
        "role": "central",
        "uuid16": 0xFFE2,
        "hex": b"\x02ok".hex(),
    }
    periph = SimpleNamespace(
        evt=int(EVT.GATTX), subtype=0x01, payload=b"\xe1\xff\x03\x00\xaa\xbb"
    )
    assert parse_gattx_payload(periph) == {
        "role": "peripheral",
        "uuid16": 0xFFE1,
        "handle": 3,
        "hex": "aabb",
    }
    assert parse_gattx_payload(SimpleNamespace(evt=int(EVT.SCAN), subtype=0, payload=b"")) is None
    short = SimpleNamespace(evt=int(EVT.GATTX), subtype=0x01, payload=b"\xe1")
    assert parse_gattx_payload(short) is None
    other = SimpleNamespace(evt=int(EVT.GATTX), subtype=0x7F, payload=b"")
    assert parse_gattx_payload(other) is None


def test_peripheral_gattx_event_flows_through_daemon_drain() -> None:
    """A peripheral-role write event injected on the wire surfaces in the
    daemon event log (host receive path of the FR-H write echo)."""
    fake = GoodFake()
    daemon = make_daemon(fake)
    fake.inject_raw(
        encode_frame(int(EVT.GATTX), 9, b"\x01" + b"\xe1\xff" + b"\x03\x00" + b"\xde\xad")
    )
    daemon.handle_command({"cmd": "drain"})
    events = daemon.handle_command({"cmd": "drain", "after": -1})["data"]["events"]
    gattx = [e for e in events if e["evt"] == int(EVT.GATTX)]
    assert len(gattx) == 1
    assert gattx[0]["subtype"] == 0x01
    assert gattx[0]["hex"] == (b"\xe1\xff\x03\x00\xde\xad").hex()


def test_raw_escape_hatch_bad_cmd() -> None:
    daemon = make_daemon(GoodFake())
    resp = daemon.handle_command({"cmd": "raw", "raw_cmd": "nothex"})
    assert resp["ok"] is False and resp["error"] == "bad_arg:raw_cmd"
    resp = daemon.handle_command({"cmd": "raw", "raw_cmd": 0x1FF})
    assert resp["ok"] is False


def test_sec_placeholder_not_implemented() -> None:
    daemon = make_daemon(GoodFake())
    resp = daemon.handle_command({"cmd": "sec"})
    assert resp["ok"] is False
    assert resp["error"] == "smp_not_implemented"


def test_uuid16_arg_accepts_all_spellings() -> None:
    assert uuid16_arg("f5fe") == 0xF5FE
    assert uuid16_arg("0xF5FE") == 0xF5FE
    assert uuid16_arg("8082caa8-41a6-4021-91c6-56f9b954cc34") == 0x8082


def test_connect_payload_encodings() -> None:
    assert connect_payload(addr="E0:F1:00:02:03:04") == b"\x00" + bytes.fromhex(
        "e0f100020304"
    )
    assert connect_payload(addr="E0:F1:00:02:03:04", random=True)[:1] == b"\x01"
    named = connect_payload(name="suotasim")
    assert named[0] == 0x02
    assert named[1:] == b"suotasim"


# ============================== drain / events ==============================


def test_drain_incremental_semantics_and_aliases() -> None:
    fake = GoodFake()
    fake.inject_raw(scan_adv(wire_seq=0))
    daemon = make_daemon(fake)

    first = daemon.handle_command({"cmd": "drain"})
    assert first["ok"] is True
    events = first["data"]["events"]
    assert [e["seq"] for e in events] == [0]
    assert events[0]["evt"] == int(EVT.SCAN)
    assert first["data"]["last_seq"] == 0
    assert "wire_gaps" in first["data"] and first["data"]["wire_gaps"] == 0

    empty = daemon.handle_command({"cmd": "events", "after": 0})  # legacy alias
    assert empty["data"]["events"] == []
    assert empty["data"]["last_seq"] == 0

    fake.inject_raw(scan_adv(addr="11:22:33:44:55:66", rssi=-40, wire_seq=1))
    incremental = daemon.handle_command({"cmd": "drain", "after": 0})
    assert [e["seq"] for e in incremental["data"]["events"]] == [1]
    whole = daemon.handle_command({"cmd": "drain"})["data"]["events"]
    assert [e["seq"] for e in whole] == [0, 1]


def test_mtu_response_reports_mtu_events() -> None:
    """Fix round 2 regression: the events field must contain the EVT.MTU.

    The snapshot must be ``next_seq - 1`` (last handed out); snapshotting
    ``next_seq`` itself filtered out the first — typically the only —
    EVT.MTU event and always answered ``events: []``.
    """
    fake = GoodFake()
    fake.inject_raw(encode_frame(int(EVT.MTU), 0, b"\x00\xfb\x00"))  # 251 LE
    daemon = make_daemon(fake)
    resp = daemon.handle_command({"cmd": "mtu", "timeout": 0.05})
    assert resp["ok"] is True, resp
    events = resp["data"]["events"]
    assert len(events) == 1
    assert events[0]["evt"] == int(EVT.MTU)
    assert bytes.fromhex(events[0]["hex"]) == b"\xfb\x00"


def test_mtu_events_snapshot_with_prior_events() -> None:
    """Same fix, non-empty log: only events newer than the snapshot return."""
    fake = GoodFake()
    fake.inject_raw(encode_frame(int(EVT.SCAN), 0, b"\x00\x01"))  # seq 0: old
    daemon = make_daemon(fake)
    daemon.handle_command({"cmd": "drain"})  # seq 0 now in the daemon log
    fake.inject_raw(encode_frame(int(EVT.MTU), 1, b"\x00\xf7\x00"))
    resp = daemon.handle_command({"cmd": "mtu", "timeout": 0.05})
    assert [e["evt"] for e in resp["data"]["events"]] == [int(EVT.MTU)]


def test_scan_events_parse_into_device_records() -> None:
    fake = GoodFake()
    fake.inject_raw(scan_adv(wire_seq=0))
    daemon = make_daemon(fake)
    daemon.handle_command({"cmd": "ping"})
    records = daemon.handle_command({"cmd": "drain"})["data"]["events"]
    record = parse_scan_payload(
        type("E", (), {"evt": records[0]["evt"], "payload": bytes.fromhex(records[0]["hex"])})()
    )
    assert record["addr"] == "E0:F1:00:02:03:04"
    assert record["rssi"] == -70
    assert record["addr_type"] == 1
    assert record["adv_type"] == 0  # v1.4.1 E2-T: connectable undirected
    assert record["adv_hex"] == "020106"


def test_scan_payload_adv_type_distinguishes_nonconnectable() -> None:
    """E2-T / v1.4.1: scannable and non-connectable adverts surface their
    raw PDU type so the host can refuse connect attempts on them."""
    for adv_type in (0, 2, 3, 4):
        fake = GoodFake()
        fake.inject_raw(scan_adv(wire_seq=0, adv_type=adv_type))
        daemon = make_daemon(fake)
        records = daemon.handle_command({"cmd": "drain"})["data"]["events"]
        record = parse_scan_payload(
            type("E", (), {"evt": records[0]["evt"], "payload": bytes.fromhex(records[0]["hex"])})()
        )
        assert record["adv_type"] == adv_type
        assert record["adv_hex"] == "020106"


# ================================== scan ====================================


def test_scan_start_collects_devices_then_stops() -> None:
    fake = GoodFake()
    fake.inject_raw(scan_adv(wire_seq=0, rssi=-70))
    fake.inject_raw(scan_adv(addr="11:22:33:44:55:66", rssi=-40, wire_seq=1))
    daemon = make_daemon(fake)
    resp = daemon.handle_command({"cmd": "scan_start", "duration": 0.06})
    assert resp["ok"] is True
    devices = resp["data"]["devices"]
    # Daemon returns devices sorted by address string.
    assert [d["addr"] for d in devices] == ["11:22:33:44:55:66", "E0:F1:00:02:03:04"]
    assert resp["data"]["status"] == 0
    starts = [f for f in written_frames(fake) if f.cmd == int(CMD.scan_start)]
    assert len(starts) == 1
    assert starts[0].payload == (60).to_bytes(2, "little")  # duration_ms u16 LE
    assert any(f.cmd == int(CMD.scan_stop) for f in written_frames(fake))

    # Accumulation across scans: one repeat + one new device.
    fake.inject_raw(scan_adv(addr="11:22:33:44:55:66", rssi=-33, wire_seq=2))
    fake.inject_raw(scan_adv(addr="aa:bb:cc:dd:ee:ff", rssi=-90, wire_seq=3))
    resp2 = daemon.handle_command({"cmd": "scan_start", "duration": 0.06})
    addrs = [d["addr"] for d in resp2["data"]["devices"]]
    assert addrs == ["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF", "E0:F1:00:02:03:04"]


# ================================ reconnect =================================


def test_reconnect_once_retries_and_marks_response() -> None:
    fake = FlakyFake(fail_reads=1)
    daemon = make_daemon(fake)

    resp = daemon.handle_command({"cmd": "ping"})
    assert resp["ok"] is True, resp
    assert resp["data"]["reconnected"] is True  # one-shot marker
    assert resp["data"]["pong"] is True

    follow_up = daemon.handle_command({"cmd": "ping"})
    assert follow_up["ok"] is True
    assert "reconnected" not in follow_up["data"]  # exactly once


def test_reconnect_twice_failing_reports_error() -> None:
    fake = FlakyFake(fail_reads=2)
    daemon = make_daemon(fake)
    resp = daemon.handle_command({"cmd": "ping"})
    assert resp["ok"] is False
    assert "transport_down" in resp["error"]
    assert fake.fail_reads == 0  # both attempts consumed


def test_daemon_event_seq_survives_reconnect() -> None:
    fake = FlakyFake(fail_reads=0)
    fake.inject_raw(scan_adv(wire_seq=0))
    daemon = make_daemon(fake)

    before = daemon.handle_command({"cmd": "drain"})["data"]["events"]
    assert [e["seq"] for e in before] == [0]

    fake.fail_reads = 1  # the *next* command hits the dead transport
    resp = daemon.handle_command({"cmd": "ping"})  # dies, rebuilds, retries
    assert resp["ok"] is True
    assert resp["data"]["reconnected"] is True

    fake.inject_raw(scan_adv(addr="22:22:22:22:22:22", wire_seq=1))
    after = daemon.handle_command({"cmd": "events"})["data"]["events"]
    # The pre-reconnect event keeps seq 0; the post-reconnect one continues
    # at 1 — the daemon-level counter never restarts (bridge app_seq does).
    assert [e["seq"] for e in after] == [0, 1]
    assert "22:22:22:22:22:22".replace(":", "").lower() in after[1]["hex"]


def test_wire_gap_visible_through_daemon_drain() -> None:
    fake = GoodFake()
    fake.inject_raw(evt_frame(0))
    fake.inject_raw(evt_frame(5))
    daemon = make_daemon(fake)
    resp = daemon.handle_command({"cmd": "drain"})
    assert resp["data"]["wire_gaps"] == 4
    evts = resp["data"]["events"]
    assert evts[-1]["evt"] == int(EVT.DEBUG)
    assert evts[-1]["subtype"] == 0xFF
    assert b"missing=4" in bytes.fromhex(evts[-1]["hex"])


# =================================== suota ===================================


def test_suota_start_full_success_on_fake_pen(tmp_path) -> None:
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=3, tail=100))  # 6245 B body
    fake = SuotaFakeTransport()
    daemon = make_daemon(fake, suota_ack_timeout=5.0)

    resp = daemon.handle_command({"cmd": "suota_start", "image_path": str(image)})
    assert resp["ok"] is True, resp
    assert resp["data"]["bytes_written"] == 3 * 2048 + 101
    assert resp["data"]["blocks"] == 4
    assert resp["data"]["chunks"] == 3 * 9 + 1  # 244-byte chunks
    assert "reconnected" not in resp["data"]  # no reconnect happened here
    assert fake.rebooted is True
    assert resp["_ms"] > 0

    drained = daemon.handle_command({"cmd": "drain"})["data"]["events"]
    suota = [e for e in drained if e["evt"] == int(EVT.SUOTA)]
    progress = [e for e in suota if e["subtype"] == SUOTA_PROGRESS]
    done = [e for e in suota if e["subtype"] == SUOTA_DONE]
    assert [struct.unpack("<HHI", bytes.fromhex(e["hex"]))[0] for e in progress] == [1, 2, 3, 4]
    assert len(done) == 1
    written, _elapsed = struct.unpack("<If", bytes.fromhex(done[0]["hex"]))
    assert written == 3 * 2048 + 101


def test_suota_start_failure_injects_failed_event(tmp_path) -> None:
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    fake = SuotaFakeTransport(block_ack=None)  # device never ACKs -> timeout
    daemon = make_daemon(fake, suota_ack_timeout=0.05)

    resp = daemon.handle_command({"cmd": "suota_start", "image_path": str(image)})
    assert resp["ok"] is False
    assert "suota_failed" in resp["error"]
    drained = daemon.handle_command({"cmd": "drain"})["data"]["events"]
    failed = [e for e in drained if e["evt"] == int(EVT.SUOTA) and e["subtype"] == SUOTA_FAILED]
    assert len(failed) == 1
    assert bytes.fromhex(failed[0]["hex"]) == b"-1:block"  # STATUS_TIMEOUT


def test_suota_start_missing_image_arg() -> None:
    daemon = make_daemon(SuotaFakeTransport())
    resp = daemon.handle_command({"cmd": "suota_start"})
    assert resp["ok"] is False and resp["error"] == "missing_arg:image_path"


def test_suota_abort_writes_mem_dev_abort() -> None:
    fake = SuotaFakeTransport()
    daemon = make_daemon(fake)
    resp = daemon.handle_command({"cmd": "suota_abort"})
    assert resp["ok"] is True and resp["data"]["aborted"] is True
    assert fake.aborted is True  # MEM_DEV 0xFF000000 reached the device


def test_suota_start_pair_flag_forwarded(monkeypatch) -> None:
    """suota_start{pair: true} reaches SuotaSession.run(pair=True) (FR-B5)."""
    import blebridge.daemon as daemon_mod
    from blebridge.suota import SuotaResult

    calls: Dict[str, dict] = {}

    class SpySession:
        def __init__(self, bridge, **kwargs) -> None:
            calls["init"] = kwargs

        def run(self, image_path, **kwargs) -> SuotaResult:
            calls["run"] = kwargs
            return SuotaResult(
                bytes_written=1, blocks=1, chunks=1, elapsed=0.1, throughput=10.0
            )

    monkeypatch.setattr(daemon_mod, "SuotaSession", SpySession)
    daemon = make_daemon(SuotaFakeTransport())

    resp = daemon.handle_command(
        {"cmd": "suota_start", "image_path": "x.bin", "pair": True}
    )
    assert resp["ok"] is True, resp
    assert calls["run"]["pair"] is True

    resp = daemon.handle_command({"cmd": "suota_start", "image_path": "x.bin"})
    assert resp["ok"] is True, resp
    assert calls["run"]["pair"] is False  # default: no pairing


# =============================== TCP serve layer =============================


def test_handle_line_bad_json_and_non_object() -> None:
    daemon = make_daemon(GoodFake())
    resp = json.loads(daemon.handle_line("{not json"))
    assert resp["ok"] is False and resp["error"].startswith("bad_json")
    assert "_ms" in resp
    resp = json.loads(daemon.handle_line("[1, 2, 3]"))
    assert resp["ok"] is False and resp["error"].startswith("bad_json")


def test_serve_connection_over_socketpair() -> None:
    daemon = make_daemon(GoodFake())
    client, server = socket.socketpair()
    thread = threading.Thread(target=daemon._serve_connection, args=(server,), daemon=True)
    thread.start()
    client.sendall(b'{"cmd":"ping"}\n\n{"cmd":"bogus"}\nnot json\n')
    client.settimeout(2.0)
    reader = client.makefile("rb")
    lines = [reader.readline() for _ in range(3)]
    r0, r1, r2 = (json.loads(line) for line in lines)
    assert r0["ok"] is True and r0["data"]["pong"] is True and "_ms" in r0
    assert r1["ok"] is False and r1["error"] == "unknown_cmd"
    assert r2["ok"] is False and r2["error"].startswith("bad_json")
    reader.close()
    client.shutdown(socket.SHUT_RDWR)  # propagate EOF to the serve thread
    client.close()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_serve_forever_loopback_roundtrip_and_shutdown() -> None:
    daemon = make_daemon(GoodFake())
    daemon.tcp_port = 0  # ephemeral
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    for _ in range(200):  # wait for the bound socket (max ~2 s)
        server = daemon._server
        if server is not None:
            break
        time.sleep(0.01)
    port = daemon.tcp_port
    assert port > 0

    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as client:
        client.sendall(b'{"cmd":"ping"}\n')
        client.settimeout(2.0)
        resp = json.loads(client.makefile("rb").readline())
        client.shutdown(socket.SHUT_RDWR)  # release the serve thread
    assert resp["ok"] is True and resp["data"]["pong"] is True

    daemon.shutdown()
    thread.join(timeout=2.0)
    assert not thread.is_alive()

