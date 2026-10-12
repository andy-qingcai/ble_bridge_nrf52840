"""Tests for blebridge.bridge: request/response, event ring, credits, lifecycle.

Everything runs on FakeTransport (synchronous pump model): fully
deterministic, no background threads, and the only real waiting is the tiny
timeout paths (0.05 s).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import List

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from blebridge.bridge import (  # noqa: E402
    CMD,
    EVT,
    GATTX_NOTIFY,
    GATTX_PERIPH_WRITE,
    ROLE_CENTRAL,
    ROLE_PERIPHERAL,
    BridgeHost,
    BridgeTimeout,
    EventRing,
    adv_start_payload,
    conn_update_payload,
    gatt_define_payload,
    gatt_read_payload,
    gatt_write_payload,
    notify_payload,
    phy_payload,
    set_char_payload,
    subscribe_payload,
)
from blebridge.framing import EVENT_CMDS, StreamParser, encode_frame  # noqa: E402
from blebridge.transport import FakeTransport, TransportClosed  # noqa: E402


def frames_of(data: bytes) -> list:
    return StreamParser().feed(data)


def written_frames(t: FakeTransport) -> list:
    return frames_of(bytes(t.written))


def evt_frame(wire_seq: int, subtype: int, payload: bytes = b"", evt: int = EVT.SCAN) -> bytes:
    return encode_frame(int(evt), wire_seq, bytes([subtype]) + payload)


# ------------------------------------------------------------------ commands


def test_ping_roundtrip() -> None:
    host = BridgeHost(FakeTransport(responses={int(CMD.ping): b"\x00\x01\x02"}))
    host.transport.open()
    status, body = host.request(CMD.ping, timeout=0.5)
    assert (status, body) == (0, b"\x01\x02")


def test_request_payload_written_verbatim() -> None:
    t = FakeTransport(responses={int(CMD.gatt_write): b"\x00"})
    host = BridgeHost(t)
    t.open()
    host.request(CMD.gatt_write, payload=b"\x0b\x00\x03abc", timeout=0.5)
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.gatt_write)]
    assert len(reqs) == 1
    assert reqs[0].payload == b"\x0b\x00\x03abc"


def test_nonzero_status_returned_not_raised() -> None:
    host = BridgeHost(
        FakeTransport(responses={int(CMD.connect): b"\x03no such device"})
    )
    host.transport.open()
    status, body = host.request(CMD.connect, payload=b"\x01\x02", timeout=0.5)
    assert status == 3
    assert body == b"no such device"


def test_empty_response_payload_maps_to_status_zero() -> None:
    host = BridgeHost(FakeTransport(responses={int(CMD.reset): b""}))
    host.transport.open()
    assert host.request(CMD.reset, timeout=0.5) == (0, b"")


def test_seq_rings_1_to_255_then_back_to_1() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    host = BridgeHost(t)
    t.open()
    for _ in range(255):
        host.request(CMD.ping, timeout=0.5)
    seqs = [f.seq for f in written_frames(t)]
    # Frame 1 is the session-start credit resync (the SEQ ring is shared by
    # all request-direction frames); the 255 requests then run 2..255 and
    # wrap around to 1.
    assert seqs == [1] + list(range(2, 256)) + [1]
    host.request(CMD.ping, timeout=0.5)  # wraparound continues: 2
    host.request(CMD.ping, timeout=0.5)  # then 3
    seqs = [f.seq for f in written_frames(t)]
    assert seqs[-2:] == [2, 3]


def test_request_accepts_plain_int_cmd() -> None:
    t = FakeTransport(responses={0x02: b"\x07"})
    host = BridgeHost(t)
    t.open()
    assert host.request(0x02, timeout=0.5) == (7, b"")
    assert written_frames(t)[0].cmd == 0x02


# ------------------------------------------------------------ CMD.pair (FR-B5)


def test_cmd_pair_value_and_response_invariant() -> None:
    """CMD.pair sits in the safe slot 0x43 (0x40-0x42 SUOTA placeholders,
    0x50 credits) and its response byte never collides with an event."""
    assert int(CMD.pair) == 0x43
    assert (int(CMD.pair) | 0x80) not in EVENT_CMDS


def test_pair_command_encoding_empty_payload() -> None:
    """pair(): empty-payload CMD.pair request; body = [encrypted, sec_level]."""
    t = FakeTransport(responses={int(CMD.pair): b"\x00\x01\x02"})
    host = BridgeHost(t)
    t.open()
    status, body = host.pair(timeout=0.5)
    assert (status, body) == (0, b"\x01\x02")  # encrypted=1, sec_level=L2
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.pair)]
    assert len(reqs) == 1
    assert reqs[0].payload == b""


def test_pair_failure_status_0x75_returned_not_raised() -> None:
    """Pairing failure surfaces as status 0x75 (ERR_PAIR_FAILED), verbatim."""
    host = BridgeHost(FakeTransport(responses={int(CMD.pair): b"\x75"}))
    host.transport.open()
    status, body = host.pair(timeout=0.5)
    assert status == 0x75
    assert body == b""


def test_timeout_raises_bridge_timeout_and_next_request_unpolluted() -> None:
    # info is NOT in the response table -> silence -> timeout.
    t = FakeTransport(responses={int(CMD.ping): b"\x00\x2a"})
    host = BridgeHost(t)
    t.open()
    with pytest.raises(BridgeTimeout) as excinfo:
        host.request(CMD.info, timeout=0.05)
    assert excinfo.value.cmd == CMD.info
    # The very next request on a fresh SEQ works normally.
    assert host.request(CMD.ping, timeout=0.5) == (0, b"\x2a")


# ------------------------------------------ generic BLE surface (0x0C/0x44/0x45)


def test_generic_cmd_values_and_response_invariant() -> None:
    """CMD.conn_update=0x44 / CMD.phy=0x45; their response bytes never
    collide with event frames."""
    assert int(CMD.conn_update) == 0x44
    assert int(CMD.phy) == 0x45
    for value in (int(CMD.conn_update), int(CMD.phy), int(CMD.disc_svc)):
        assert (value | 0x80) not in EVENT_CMDS


def test_disc_svc_sends_service_uuid_and_parses_nchars() -> None:
    """disc_svc(0xFEF5): payload f5 fe; body = discovered count u16 LE."""
    t = FakeTransport(responses={int(CMD.disc_svc): b"\x00\x0a\x00"})
    host = BridgeHost(t)
    t.open()
    status, nchars = host.disc_svc(0xFEF5, timeout=0.5)
    assert (status, nchars) == (0, 10)
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.disc_svc)]
    assert reqs[0].payload == b"\xf5\xfe"


def test_disc_svc_zero_means_rediscover_last() -> None:
    t = FakeTransport(responses={int(CMD.disc_svc): b"\x00\x03\x00"})
    host = BridgeHost(t)
    t.open()
    assert host.disc_svc(0, timeout=0.5) == (0, 3)
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.disc_svc)]
    assert reqs[0].payload == b"\x00\x00"


def test_gatt_read_handle_mode_wire_payload() -> None:
    """handle addressing: uuid16 field 0x0000 + raw handle (u16 LE)."""
    t = FakeTransport(responses={int(CMD.gatt_read): b"\x00\xaa"})
    host = BridgeHost(t)
    t.open()
    status, body = host.gatt_read(handle=0x1234, timeout=0.5)
    assert (status, body) == (0, b"\xaa")
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.gatt_read)]
    assert reqs[0].payload == b"\x00\x00\x34\x12"


def test_gatt_write_handle_mode_wire_payload() -> None:
    """handle write: [type][00 00][handle_type][handle LE][value]."""
    t = FakeTransport(responses={int(CMD.gatt_write): b"\x00"})
    host = BridgeHost(t)
    t.open()
    host.gatt_write(
        b"\x01\x02", handle=0x0011, handle_type=1, with_response=False, timeout=0.5
    )
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.gatt_write)]
    assert reqs[0].payload == b"\x01\x00\x00\x01\x11\x00\x01\x02"


def test_subscribe_handle_mode_wire_payload() -> None:
    """handle subscribe: 0x0000 + value handle + cccd handle (u16 LE x2)."""
    t = FakeTransport(responses={int(CMD.subscribe): b"\x00"})
    host = BridgeHost(t)
    t.open()
    host.subscribe(value_handle=0x002A, cccd_handle=0x002B, timeout=0.5)
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.subscribe)]
    assert reqs[0].payload == b"\x00\x00\x2a\x00\x2b\x00"


def test_unsubscribe_handle_mode_wire_payload() -> None:
    t = FakeTransport(responses={int(CMD.unsubscribe): b"\x00"})
    host = BridgeHost(t)
    t.open()
    host.unsubscribe(handle=0x002A, timeout=0.5)
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.unsubscribe)]
    assert reqs[0].payload == b"\x00\x00\x2a\x00"


def test_conn_update_wire_payload_and_defaults() -> None:
    """4 x u16 LE; defaults mirror the firmware connect parameters."""
    t = FakeTransport(responses={int(CMD.conn_update): b"\x00"})
    host = BridgeHost(t)
    t.open()
    status, _ = host.conn_update(timeout=0.5)
    assert status == 0
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.conn_update)]
    assert reqs[0].payload == struct.pack("<HHHH", 24, 40, 0, 400)


def test_phy_wire_payload() -> None:
    t = FakeTransport(responses={int(CMD.phy): b"\x00"})
    host = BridgeHost(t)
    t.open()
    host.phy_update(2, timeout=0.5)
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.phy)]
    assert reqs[0].payload == b"\x02"


def test_payload_encoder_validation_errors() -> None:
    with pytest.raises(ValueError):
        gatt_read_payload()  # neither addressing given
    with pytest.raises(ValueError):
        gatt_read_payload(0x1234, handle=0x12)  # both given
    with pytest.raises(ValueError):
        phy_payload(4)  # only 1M / 2M exist
    with pytest.raises(ValueError):
        gatt_write_payload(b"", handle=1, handle_type=7)
    with pytest.raises(ValueError):
        subscribe_payload(value_handle=1)  # cccd_handle missing
    with pytest.raises(ValueError):
        conn_update_payload(interval_min=0x10000)  # u16 overflow


# ------------------------------------------------- request_noreply


def test_request_noreply_writes_frame_returns_seq_without_waiting() -> None:
    """fire-and-forget：请求帧照常上线（SEQ 正常分配），不泵不等待；响应
    留在传输层直到下一次 pump 被消费为孤儿计数，绝不进入匹配表。"""
    t = FakeTransport(responses={int(CMD.ping): b"\x00\x2a"})
    host = BridgeHost(t)
    t.open()
    seq = host.request_noreply(CMD.ping, b"\x07")
    assert 1 <= seq <= 255
    reqs = [f for f in written_frames(t) if f.cmd == int(CMD.ping)]
    assert len(reqs) == 1
    assert reqs[0].seq == seq
    assert reqs[0].payload == b"\x07"
    assert host.orphan_responses == 0  # nobody pumped yet
    host.drain()  # next pump: the response is consumed as an orphan
    assert host.orphan_responses == 1
    assert host._responses == {}  # nothing survived into the match table


def test_request_noreply_orphans_do_not_pollute_following_request() -> None:
    """连发 3 个 fire-and-forget 后紧跟一个普通 request：后者首轮 pump 就
    必须匹配到自己的响应（不同命令，逐字节校验 body）。"""
    t = FakeTransport(
        responses={int(CMD.ping): b"\x00\x01", int(CMD.info): b"\x07body"}
    )
    host = BridgeHost(t)
    t.open()
    for i in range(3):
        host.request_noreply(CMD.ping, bytes([i]))
    assert host.request(CMD.info, timeout=0.5) == (7, b"body")
    assert host.orphan_responses == 3  # the three ping responses
    assert host._responses == {}


def test_request_noreply_same_cmd_request_matches_only_own_response() -> None:
    """同命令场景：孤儿响应（旧 body）不得被后续普通 request 抢先匹配。"""
    t = FakeTransport(responses={int(CMD.ping): b"\x00\x01"})
    host = BridgeHost(t)
    t.open()
    for _ in range(3):
        host.request_noreply(CMD.ping)
    t._responses[int(CMD.ping)] = b"\x00\x02"  # the normal request's body
    assert host.request(CMD.ping, timeout=0.5) == (0, b"\x02")
    assert host.orphan_responses == 3


def test_request_noreply_silent_seq_recycle_keeps_request_matching() -> None:
    """静默 noreply（响应永不来）留下的 pending 标记在 SEQ 环回卷复用时被
    request 清除——回收后普通 request 的合法响应不被误吞为孤儿。"""
    t = FakeTransport(responses={int(CMD.ping): b"\x00\x2a"})
    host = BridgeHost(t)
    t.open()
    host.request_noreply(CMD.info)  # not in the response table: silent
    for _ in range(300):  # wrap the SEQ ring well past the marked SEQ
        assert host.request(CMD.ping, timeout=0.5) == (0, b"\x2a")
    assert host.orphan_responses == 0  # no legitimate response was swallowed


def test_request_noreply_never_swallows_event_frames() -> None:
    """嫌疑1排除（fix round 1）：孤儿判别只作用于响应分支（cmd|0x80），
    EVT 帧（bit7 清零）永不进孤儿路径——即使其 wire SEQ 恰与 pending
    noreply 的 SEQ 相同（真机事件帧 wire SEQ 0..255 会与请求环重合）。"""
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    host = BridgeHost(t)
    t.open()
    seq = host.request_noreply(CMD.ping)
    # notify 帧故意撞 pending noreply 的 SEQ，且排在响应帧之前入队。
    t.inject_raw(evt_frame(seq, 7, b"\xaa\xbb", evt=EVT.GATTX))
    t.inject_raw(encode_frame(int(CMD.ping) | 0x80, seq, b"\x00"))
    events = host.drain()
    assert [(e.evt, e.subtype, e.payload) for e in events] == [
        (EVT.GATTX, 7, b"\xaa\xbb")
    ]
    assert host.orphan_responses == 1  # 响应按孤儿消费
    assert host.orphan_errors == 0
    assert host.join_noreply(seq, timeout=0.5) == 0  # status 已记录可查
    assert host.events.wire_gaps == 0  # EVT wire SEQ 追踪未被孤儿逻辑扰动


def test_join_noreply_surfaces_orphan_error_status() -> None:
    """孤儿响应的 status 字节必须可查（桥把 bt_gatt_write_without_response
    的失败码放进响应 status，bridge_ble.c:1000）——这是流水化唯一的片级
    遥测，丢弃它 = 实机首跑"30s 静默无响应"的直接 host 侧缺陷。"""
    t = FakeTransport(responses={int(CMD.gatt_write): b"\x69"})  # e.g. ENOBUFS
    host = BridgeHost(t)
    t.open()
    seq = host.request_noreply(CMD.gatt_write, b"\x01\x00\x02\x03")
    assert host.join_noreply(seq, timeout=0.5) == 0x69
    assert host.orphan_responses == 1
    assert host.orphan_errors == 1


def test_join_noreply_times_out_on_silent_noreply() -> None:
    t = FakeTransport()  # no responder at all: silent
    host = BridgeHost(t)
    t.open()
    seq = host.request_noreply(CMD.ping)
    with pytest.raises(BridgeTimeout):
        host.join_noreply(seq, timeout=0.05)
    # 片帧已在传输层，稍后响应到达仍按孤儿记账（计数不丢）。
    t.inject_raw(encode_frame(int(CMD.ping) | 0x80, seq, b"\x00"))
    host.drain()
    assert host.orphan_responses == 1


def test_join_noreply_not_in_flight_raises_runtime_error() -> None:
    """M-2："SEQ 不在飞"（已 join / 从未发出 / SEQ 已回收）是 host 簿记违例
    → RuntimeError（消息带 seq），立即抛出且不是 BridgeTimeout——host bug
    不得伪装成"响应没来"的超时。"""
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    host = BridgeHost(t)
    t.open()
    seq = host.request_noreply(CMD.ping)
    assert host.join_noreply(seq, timeout=0.5) == 0
    with pytest.raises(RuntimeError, match=str(seq)):  # already joined
        host.join_noreply(seq, timeout=0.05)
    with pytest.raises(RuntimeError, match="not in flight"):  # never issued
        host.join_noreply(42, timeout=0.05)
    # 真超时路径语义不受影响：在飞的 SEQ 静默（无应答命令）仍是 BridgeTimeout。
    pending = host.request_noreply(CMD.info)  # not in the response table
    with pytest.raises(BridgeTimeout):
        host.join_noreply(pending, timeout=0.05)


def test_request_noreply_closed_transport_raises() -> None:
    host = BridgeHost(FakeTransport(responses={int(CMD.ping): b"\x00"}))
    host.transport.open()
    host.close()
    with pytest.raises(TransportClosed):
        host.request_noreply(CMD.ping)


# -------------------------------------------------------------------- events


def test_drain_first_call_returns_all_with_contiguous_seq() -> None:
    script = [evt_frame(100, 1, b"aa"), evt_frame(101, 2), evt_frame(102, 1, b"cc")]
    host = BridgeHost(FakeTransport(script=script))
    host.transport.open()
    events = host.drain()
    assert [e.app_seq for e in events] == [0, 1, 2]
    assert all(e.evt == EVT.SCAN for e in events)
    assert [e.subtype for e in events] == [1, 2, 1]
    assert [e.payload for e in events] == [b"aa", b"", b"cc"]


def test_drain_incremental_after_cursor() -> None:
    t = FakeTransport(script=[evt_frame(0, 1, b"a"), evt_frame(1, 1, b"b")])
    host = BridgeHost(t)
    t.open()
    assert [e.app_seq for e in host.drain()] == [0, 1]
    t.inject_raw(evt_frame(2, 1, b"c"))
    assert [e.app_seq for e in host.drain(after=1)] == [2]  # only the new one
    assert host.drain(after=2) == []
    assert [e.app_seq for e in host.drain(after=-1)] == [0, 1, 2]


def test_event_fields_split_subtype_and_mixed_evt_types() -> None:
    script = [
        evt_frame(0, 5, b"x", evt=EVT.CONN),
        evt_frame(1, 6, b"yz", evt=EVT.DEBUG),
    ]
    host = BridgeHost(FakeTransport(script=script))
    host.transport.open()
    e0, e1 = host.drain()
    assert (e0.evt, e0.subtype, e0.payload) == (EVT.CONN, 5, b"x")
    assert (e1.evt, e1.subtype, e1.payload) == (EVT.DEBUG, 6, b"yz")


def test_empty_payload_event_gets_subtype_zero() -> None:
    host = BridgeHost(FakeTransport(script=[encode_frame(int(EVT.MTU), 4, b"")]))
    host.transport.open()
    (e,) = host.drain()
    assert (e.subtype, e.payload) == (0, b"")


def test_event_ring_capacity_default_evicts_oldest_counts_dropped() -> None:
    # wire SEQ is u8; it wraps (host ignores it, numbering is host-side)
    script = [evt_frame(i % 256, 1, b"!") for i in range(1030)]
    host = BridgeHost(FakeTransport(script=script))
    host.transport.open()
    events = host.drain()
    assert host.events.dropped == 6
    assert len(events) == EventRing.CAPACITY
    assert [e.app_seq for e in events] == list(range(6, 1030))  # contiguous window


def test_event_ring_small_capacity_unit() -> None:
    ring = EventRing(capacity=4)
    for i in range(6):
        ring.append(int(EVT.SCAN), i, bytes([i]))
    assert ring.dropped == 2
    assert [e.app_seq for e in ring.drain()] == [2, 3, 4, 5]
    assert ring.latest_seq() == 5
    assert len(ring) == 4


def test_wait_for_hits_existing_event_with_subtype() -> None:
    script = [
        evt_frame(0, 1, b"a"),
        evt_frame(1, 7, b"needle"),
        evt_frame(2, 7, b"second"),
    ]
    host = BridgeHost(FakeTransport(script=script))
    host.transport.open()
    hit = host.wait_for(EVT.SCAN, 7, timeout=0.5)
    assert (hit.app_seq, hit.subtype, hit.payload) == (1, 7, b"needle")


def test_wait_for_subtype_none_matches_any() -> None:
    script = [evt_frame(0, 3, b"x", evt=EVT.DISC)]
    host = BridgeHost(FakeTransport(script=script))
    host.transport.open()
    hit = host.wait_for(EVT.DISC, None, timeout=0.5)
    assert hit.subtype == 3


def test_wait_for_times_out_without_match() -> None:
    host = BridgeHost(FakeTransport(script=[evt_frame(0, 1, b"a")]))
    host.transport.open()
    with pytest.raises(BridgeTimeout) as excinfo:
        host.wait_for(EVT.CONN, None, timeout=0.05)
    assert excinfo.value.cmd == EVT.CONN


def test_wait_for_picks_up_event_injected_after_start() -> None:
    t = FakeTransport()
    host = BridgeHost(t)
    t.open()
    t.inject_raw(evt_frame(0, 9, b"late"))
    hit = host.wait_for(EVT.SCAN, 9, timeout=0.5)
    assert hit.payload == b"late"


# ------------------------------------------------------------------ resync


def test_garbage_resync_then_valid_events_decoded() -> None:
    t = FakeTransport()
    host = BridgeHost(t)
    t.open()
    good1 = evt_frame(0, 1, b"a")
    good2 = evt_frame(1, 2, b"b")
    # Deterministic garbage (Task 1 convention): 55 AA AA has LEN=0xAAAA>512,
    # so the parser drops exactly those 3 bytes and good1's SOF survives.
    t.inject_raw(b"\x00\x13\x37\x55\xaa\xaa" + good1 + good2)
    events = host.drain()
    assert [(e.app_seq, e.subtype) for e in events] == [(0, 1), (1, 2)]
    assert host._parser.resyncs == 1  # garbage skipped, both frames intact


# ------------------------------------------------------------------ credits


def test_credits_auto_grant_after_100_events() -> None:
    # balance starts at 128; event k leaves 128-k; first balance < 32 at k=97.
    script = [evt_frame(i, 1, b"") for i in range(100)]
    t = FakeTransport(script=script)
    host = BridgeHost(t)
    t.open()
    host.drain()
    grants = [f for f in written_frames(t) if f.cmd == int(CMD.credits)]
    assert [f.payload for f in grants] == [(128).to_bytes(2, "little")] * 2
    # (1) session-start resync grant, (2) the automatic top-up


def test_no_credits_written_without_events() -> None:
    t = FakeTransport()
    host = BridgeHost(t)
    t.open()
    host.drain()
    # Only the session-start resync grant goes out; no automatic top-ups.
    frames = written_frames(t)
    assert [f.cmd for f in frames] == [int(CMD.credits)]
    assert frames[0].payload == (128).to_bytes(2, "little")


def test_grant_credits_manual_writes_frame_and_offsets_threshold() -> None:
    t = FakeTransport()
    host = BridgeHost(t)
    t.open()
    host.grant_credits(3)
    # balance 131; event k leaves 131-k; <32 first at k=100 (31 left).
    for i in range(100):
        t.inject_raw(evt_frame(i, 1, b""))
    host.drain()
    grants = [f for f in written_frames(t) if f.cmd == int(CMD.credits)]
    assert [f.payload for f in grants] == [
        (3).to_bytes(2, "little"),  # manual grant (written before first pump)
        (128).to_bytes(2, "little"),  # session-start resync on first pump
        (128).to_bytes(2, "little"),  # automatic top-up
    ]


def test_custom_threshold_and_grant_size() -> None:
    script = [evt_frame(i, 1, b"") for i in range(10)]
    t = FakeTransport(script=script)
    host = BridgeHost(t, credit_threshold=8, credit_grant=20)
    t.open()
    host.drain()
    # balance 20 -> event k leaves 20-k; <8 first at k=13; 10 events -> quiet.
    grants = [f for f in written_frames(t) if f.cmd == int(CMD.credits)]
    assert [f.payload for f in grants] == [(20).to_bytes(2, "little")]  # resync
    t.inject_raw(evt_frame(10, 1, b""))
    t.inject_raw(evt_frame(11, 1, b""))
    t.inject_raw(evt_frame(12, 1, b""))
    host.drain()  # 13 events total -> balance 7 -> grant 20 -> balance 27
    grants = [f for f in written_frames(t) if f.cmd == int(CMD.credits)]
    assert [f.payload for f in grants] == [
        (20).to_bytes(2, "little"),  # session-start resync
        (20).to_bytes(2, "little"),  # automatic top-up
    ]


# ------------------------------------------------- cross-session credit sync


class OrderingTransport(FakeTransport):
    """FakeTransport recording write/read call order (resync assertions)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.log: List[tuple] = []

    def write(self, data: bytes) -> None:
        self.log.append(("write", frames_of(bytes(data))))
        super().write(data)

    def read(self, n: int, timeout: float) -> bytes:
        self.log.append(("read", n))
        return super().read(n, timeout)


def test_session_start_credit_resync_grants_before_first_event() -> None:
    """P0 跨会话 credit 失步：固件余额跨串口重连持久（实测残留 81），而新
    BridgeHost 每会话假设满 128 —— 首用必须先把满额 grant 打出去、再消费
    首波事件，否则第一波广播把余额打到 0 掉单（实测 events_dropped+215）。"""
    script = [evt_frame(i, 1, b"") for i in range(200)]
    t = OrderingTransport(script=script)
    host = BridgeHost(t)
    t.open()
    events = host.drain()
    assert len(events) == 200
    assert host.events.dropped == 0  # nothing fell off the host ring either
    op, frames = t.log[0]
    assert op == "write"  # grant written before any event byte was read
    assert len(frames) == 1
    assert frames[0].cmd == int(CMD.credits)
    assert frames[0].payload == (128).to_bytes(2, "little")
    assert t.log[1][0] == "read"


def test_session_start_credit_resync_fires_exactly_once() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    host = BridgeHost(t)
    t.open()
    host.drain()
    host.drain()
    host.request(int(CMD.ping), timeout=0.5)
    grants = [f for f in written_frames(t) if f.cmd == int(CMD.credits)]
    assert len(grants) == 1  # session-start grant, not repeated per use
    assert grants[0].payload == (128).to_bytes(2, "little")


def test_session_start_credit_resync_also_on_request_first_use() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    host = BridgeHost(t)
    t.open()
    host.request(int(CMD.ping), timeout=0.5)  # first use is a request
    grants = [f for f in written_frames(t) if f.cmd == int(CMD.credits)]
    assert [f.payload for f in grants] == [(128).to_bytes(2, "little")]


# ---------------------------------------------------------------- lifecycle


def test_close_idempotent_and_requests_raise_gracefully() -> None:
    host = BridgeHost(FakeTransport(responses={int(CMD.ping): b"\x00"}))
    host.transport.open()
    host.close()
    host.close()  # idempotent
    assert host.is_open is False
    with pytest.raises(TransportClosed):
        host.request(CMD.ping, timeout=0.5)
    with pytest.raises(TransportClosed):
        host.drain()


def test_wait_for_after_close_raises_gracefully() -> None:
    host = BridgeHost(FakeTransport(script=[evt_frame(0, 1, b"a")]))
    host.transport.open()
    host.close()
    with pytest.raises(TransportClosed):
        host.wait_for(EVT.SCAN, None, timeout=0.05)


def test_grant_credits_after_close_raises_gracefully() -> None:
    host = BridgeHost(FakeTransport())
    host.transport.open()
    host.close()
    with pytest.raises(TransportClosed):
        host.grant_credits(8)


def test_transport_closed_under_host_surfaces_gracefully() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    host = BridgeHost(t)
    t.open()
    t.close()  # transport dies behind the host's back
    with pytest.raises(TransportClosed):
        host.request(CMD.ping, timeout=0.5)


def test_reconnect_with_new_transport_recovers() -> None:
    dead = FakeTransport(responses={int(CMD.ping): b"\x00"})
    host = BridgeHost(dead)
    dead.open()
    dead.close()
    with pytest.raises(TransportClosed):
        host.request(CMD.ping, timeout=0.5)

    fresh = FakeTransport(responses={int(CMD.ping): b"\x00\x77"})
    fresh.open()
    host2 = BridgeHost(fresh)
    assert host2.request(CMD.ping, timeout=0.5) == (0, b"\x77")


def test_bridge_host_context_manager() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    with BridgeHost(t) as host:
        assert t.is_open is True
        assert host.request(CMD.ping, timeout=0.5) == (0, b"")
    assert t.is_open is False
    assert host.is_open is False


# ------------------------------------------------- FR-H peripheral commands


def test_adv_start_payload_encodings() -> None:
    assert adv_start_payload() == b"\xff\xff\x01"
    assert adv_start_payload(None, name=False) == b"\xff\xff\x00"
    assert adv_start_payload(100) == struct.pack("<H", 100) + b"\x01"
    assert adv_start_payload(250, name=False) == struct.pack("<H", 250) + b"\x00"
    with pytest.raises(ValueError):
        adv_start_payload(-1)
    with pytest.raises(ValueError):
        adv_start_payload(0x10000)
    with pytest.raises(ValueError):
        adv_start_payload(True)  # bool is not an interval


def test_notify_and_set_char_payload_encodings() -> None:
    # default characteristic (preset): 0x00 selector + data
    assert notify_payload(b"\xaa\xbb") == b"\x00\xaa\xbb"
    assert set_char_payload() == b"\x00"
    # uuid16 addressing: 0x01 selector + u16 LE uuid16 + data
    assert notify_payload(b"hi", uuid16=0xFFE2) == b"\x01\xe2\xffhi"
    assert set_char_payload(b"\x01\x02", uuid16=0xFFA2) == b"\x01\xa2\xff\x01\x02"
    with pytest.raises(ValueError):
        notify_payload(b"x", uuid16=0x10000)
    with pytest.raises(ValueError):
        set_char_payload(b"x", uuid16=-1)


def test_gatt_define_payload_encoding() -> None:
    payload = gatt_define_payload(
        0xFFA0,
        [
            {"uuid16": 0xFFA1, "read": True, "notify": True, "max_len": 64},
            {"uuid16": 0xFFA2, "write_wo_resp": True},
        ],
    )
    assert payload == (
        b"\xa0\xff"  # svc uuid16 LE
        + b"\x02"  # n_chars
        + b"\xa1\xff" + bytes([0x01 | 0x08]) + struct.pack("<H", 64)
        + b"\xa2\xff" + bytes([0x02]) + struct.pack("<H", 244)
    )


def test_gatt_define_payload_validation() -> None:
    with pytest.raises(ValueError):
        gatt_define_payload(0, [{"uuid16": 1, "read": True}])  # svc 0 forbidden
    with pytest.raises(ValueError):
        gatt_define_payload(0xFFA0, [])  # no characteristics
    with pytest.raises(ValueError):
        gatt_define_payload(0xFFA0, [{"uuid16": 1, "read": True}] * 9)  # > 8
    with pytest.raises(ValueError):
        gatt_define_payload(0xFFA0, [{"read": True}])  # uuid16 missing
    with pytest.raises(ValueError):
        gatt_define_payload(0xFFA0, [{"uuid16": 1}])  # no properties
    with pytest.raises(ValueError):
        gatt_define_payload(0xFFA0, [{"uuid16": 1, "read": 1}])  # non-bool
    with pytest.raises(ValueError):
        gatt_define_payload(0xFFA0, [{"uuid16": 1, "read": True, "max_len": 0}])
    with pytest.raises(ValueError):
        gatt_define_payload(0xFFA0, [{"uuid16": 1, "read": True, "max_len": 245}])
    with pytest.raises(ValueError):
        gatt_define_payload(
            0xFFA0,
            [{"uuid16": 1, "read": True}, {"uuid16": 1, "write": True}],
        )  # duplicate uuid16


def test_peripheral_methods_write_command_frames() -> None:
    responses = {
        int(CMD.adv_start): b"\x00",
        int(CMD.adv_stop): b"\x00",
        int(CMD.notify): b"\x00",
        int(CMD.set_char): b"\x00",
        int(CMD.gatt_define): b"\x00\x2a\x00",
    }
    t = FakeTransport(responses=responses)
    host = BridgeHost(t)
    t.open()
    assert host.adv_start(timeout=0.5) == (0, b"")
    assert host.adv_start(interval_ms=150, name=False, timeout=0.5) == (0, b"")
    assert host.adv_stop(timeout=0.5) == (0, b"")
    assert host.notify(b"\xaa", timeout=0.5) == (0, b"")
    assert host.set_char(b"\xbb", uuid16=0xFFA2, timeout=0.5) == (0, b"")
    assert host.gatt_define(0xFFA0, [{"uuid16": 0xFFA1, "read": True}], timeout=0.5) == (0, 42)
    frames = written_frames(t)
    by_cmd: dict = {}
    for frame in frames:
        by_cmd.setdefault(frame.cmd, []).append(frame.payload)
    assert by_cmd[int(CMD.adv_start)] == [b"\xff\xff\x01", struct.pack("<H", 150) + b"\x00"]
    assert by_cmd[int(CMD.adv_stop)] == [b""]
    assert by_cmd[int(CMD.notify)] == [b"\x00\xaa"]
    assert by_cmd[int(CMD.set_char)] == [b"\x01\xa2\xff\xbb"]
    assert by_cmd[int(CMD.gatt_define)] == [b"\xa0\xff\x01\xa1\xff\x01\xf4\x00"]


def test_notify_status_074_returned_not_raised() -> None:
    host = BridgeHost(FakeTransport(responses={int(CMD.notify): b"\x74"}))
    host.transport.open()
    status, body = host.notify(b"\xaa", timeout=0.5)
    assert status == 0x74
    assert body == b""


def test_gattx_subtypes_and_role_bytes_pinned() -> None:
    """FR-H wire constants: the peripheral role byte and the GATTX subtype
    split (0x00 central notify pinned long ago, 0x01 peripheral write)."""
    assert int(EVT.GATTX) == 0x23
    assert GATTX_NOTIFY == 0x00
    assert GATTX_PERIPH_WRITE == 0x01
    assert ROLE_CENTRAL == 0x00
    assert ROLE_PERIPHERAL == 0x01
