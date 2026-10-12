"""Tests for tools/ota_bench.py: the OTA bottleneck measurement layer (C-性能线).

Zero-hardware: every scenario drives the scripted SUOTA peripheral from
``suota_fake`` through the bench's :class:`InstrumentedBridge` wrapper (the
same zero-intrusion wrapping the real tool applies to ``BridgeHost``).  A
stepping fake clock makes RTT/ACK timelines fully deterministic, so p50/p95
math and the A/B/C/D model decomposition are asserted on exact values.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import ota_bench as bench  # noqa: E402
from blebridge.bridge import CMD, BridgeHost, BridgeTimeout  # noqa: E402
from blebridge.image import build_payload  # noqa: E402
from blebridge.suota import (  # noqa: E402
    SUOTA_UUIDS,
    SuotaSession,
    uuid16_le,
)
from blebridge.transport import FakeTransport  # noqa: E402
from suota_fake import SuotaFakeTransport, synthetic_image  # noqa: E402


# ----------------------------------------------------------------- helpers


class FakeClock:
    """Deterministic clock: every call advances by ``step_s`` and returns it."""

    def __init__(self, step_s: float = 0.0005) -> None:
        self.step_s = step_s
        self.now = 0.0
        self.calls = 0

    def __call__(self) -> float:
        self.now += self.step_s
        self.calls += 1
        return self.now


def mk_timeline(
    block: int,
    n_chunks: int,
    rtt_ms: float,
    ack_ms: float,
    t0: float,
    total_blocks: int = 2,
    bytes_cum: int = 2048,
) -> bench.BlockTimeline:
    """Synthetic block timeline: ``n_chunks`` serial chunks then the ACK."""
    rtts = [rtt_ms / 1000.0] * n_chunks
    first = t0
    last_end = t0 + n_chunks * rtt_ms / 1000.0
    ack = last_end + ack_ms / 1000.0
    return bench.BlockTimeline(
        block=block,
        total_blocks=total_blocks,
        bytes_cum=bytes_cum * block,
        n_chunks=n_chunks,
        chunk_sizes=[244] * n_chunks,
        chunk_rtts_ms=[r * 1000.0 for r in rtts],
        send_ms=n_chunks * rtt_ms,
        control_ms=0.0,
        t_first_chunk=first,
        t_last_chunk_end=last_end,
        t_ack=ack,
        span_ms=(ack - first) * 1000.0,
        ack_wait_ms=(ack - last_end) * 1000.0,
    )


def recorder_with_ping(p50_ms: float, n: int = 5) -> bench.BenchRecorder:
    rec = bench.BenchRecorder()
    for i in range(n):
        rtt = p50_ms / 1000.0
        rec.add_request("ping", "ping", i * 0.1, i * 0.1 + rtt, 0, 0)
    return rec


def run_fake_ota(tmp_path: Path, clock=None, **run_kwargs):
    """Full SUOTA run over SuotaFakeTransport through InstrumentedBridge.

    ``pipelined=False``: the bench exists to decompose the *legacy*
    per-chunk request/response round trip (chunk_rtt -> send_ms / A 段), so
    the helper pins the blocking path unless a test opts in explicitly.  A
    pipelined run bypasses ``request`` entirely (fire-and-forget chunks),
    which would leave these timelines without chunk records.
    """
    run_kwargs.setdefault("pipelined", False)
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(**run_kwargs.pop("image_kwargs", {})))
    fake = SuotaFakeTransport()
    fake.open()
    recorder = bench.BenchRecorder(clock) if clock else bench.BenchRecorder()
    bridge = bench.InstrumentedBridge(fake, recorder)
    session = SuotaSession(bridge)
    result = session.run(path, progress_cb=recorder.progress, ack_timeout=0.5,
                         **run_kwargs)
    return fake, recorder, result


# ------------------------------------------------------------- percentiles


def test_percentile_linear_interpolation() -> None:
    values = list(range(1, 101))
    assert bench.percentile(values, 50) == pytest.approx(50.5)
    assert bench.percentile(values, 95) == pytest.approx(95.05)
    assert bench.percentile(values, 0) == 1.0
    assert bench.percentile(values, 100) == 100.0


def test_percentile_injected_rtt_sequence() -> None:
    """Injected RTT histogram: [10,20,30,40] ms -> p50=25, p95=38.5, max=40."""
    rtts = [10.0, 20.0, 30.0, 40.0]
    assert bench.percentile(rtts, 50) == pytest.approx(25.0)
    assert bench.percentile(rtts, 95) == pytest.approx(38.5)
    assert bench.pstats(rtts) == {"n": 4, "p50": 25.0, "p95": 38.5, "max": 40.0}


def test_percentile_single_value_and_errors() -> None:
    assert bench.percentile([7.0], 50) == 7.0
    assert bench.percentile([7.0], 100) == 7.0
    with pytest.raises(ValueError):
        bench.percentile([], 50)
    with pytest.raises(ValueError):
        bench.percentile([1.0], 101)


def test_pstats_empty() -> None:
    assert bench.pstats([]) == {"n": 0, "p50": None, "p95": None, "max": None}


# -------------------------------------------------------------- 空口模型


def test_ll_packet_air_time_2m() -> None:
    """244B chunk fills one DLE=251 LL packet exactly (plaintext, no MIC)."""
    assert bench.chunk_ll_payload(244) == 251  # 3 ATT + 244 value + 4 L2CAP
    pkt = bench.ll_packet_us(bench.chunk_ll_payload(244), phy=2)
    assert pkt == pytest.approx(1044.0)  # (251+10 overhead) * 4 us/byte @2M
    assert bench.ll_packet_us(251, phy=1) == pytest.approx(2088.0)  # 1M: 8 us/B


def test_packets_per_event_and_block_air_time() -> None:
    pkt = bench.ll_packet_us(bench.chunk_ll_payload(244), phy=2)
    # 10ms event: 1st packet 1044us, each further +150+40+150 = +1384us -> 7
    assert bench.packets_per_event(10.0, pkt, phy=2) == 7
    # 7 chunks in one event: 7*1044 + 6*340 = 9348us (< 10ms interval)
    assert bench.air_send_us(7, pkt, 10.0, phy=2) == pytest.approx(9348.0)
    # 9 chunks -> 7 in event 1, 2 in event 2: 10ms + 2*1044 + 1*340 = 12428us
    assert bench.air_send_us(9, pkt, 10.0, phy=2) == pytest.approx(12428.0)
    assert bench.air_send_us(0, pkt, 10.0) == 0.0


def test_notify_exchange_time() -> None:
    """1-byte SERV_STATUS notify: 8B LL payload -> 72us packet, 412us exchange."""
    assert bench.notify_ll_payload() == 8
    pkt = bench.ll_packet_us(bench.notify_ll_payload(), phy=2)
    assert pkt == pytest.approx(72.0)
    assert bench.exchange_us(pkt, phy=2) == pytest.approx(412.0)


# ------------------------------------------------------------ 请求分类


def test_classify_request_unit() -> None:
    mem_dev = uuid16_le(SUOTA_UUIDS["mem_dev"])
    patch_data = uuid16_le(SUOTA_UUIDS["patch_data"])
    assert bench.classify_request(int(CMD.gatt_write), b"\x00" + mem_dev + b"\x01" * 4) == (
        "control_write",
        "mem_dev",
    )
    assert bench.classify_request(int(CMD.gatt_write), b"\x01" + patch_data + b"\x00" * 244) == (
        "chunk",
        "patch_data",
    )
    assert bench.classify_request(int(CMD.subscribe), patch_data) == (
        "control_write",
        "subscribe",
    )
    assert bench.classify_request(
        int(CMD.gatt_read), uuid16_le(SUOTA_UUIDS["patch_data_char_size"])
    ) == ("info_read", "patch_data_char_size")
    assert bench.classify_request(
        int(CMD.gatt_read), uuid16_le(SUOTA_UUIDS["serv_status"])
    ) == ("gatt_read", "serv_status")
    assert bench.classify_request(int(CMD.ping), b"") == ("ping", "ping")
    assert bench.classify_request(0x33, b"") == ("0x33", "0x33")


def test_value_size_skips_write_prefix() -> None:
    payload = b"\x01" + uuid16_le(SUOTA_UUIDS["patch_data"]) + b"\x00" * 244
    assert bench._value_size(int(CMD.gatt_write), payload) == 244
    assert bench._value_size(int(CMD.ping), b"") == 0


def test_instrumented_bridge_classifies_and_times(tmp_path: Path) -> None:
    fake, recorder, result = run_fake_ota(
        tmp_path, image_kwargs={"full_blocks": 0, "tail": 101}
    )
    kinds = {r.kind for r in recorder.records}
    assert {"chunk", "control_write", "info_read"} <= kinds
    names = {r.name for r in recorder.records if r.kind == "control_write"}
    assert {"mem_dev", "gpio_map", "patch_len", "subscribe"} <= names
    chunks = recorder.chunk_records()
    # 101B image -> 102B payload -> 1 block of 1 chunk
    assert result.blocks == 1
    assert [r.value_size for r in chunks] == [102]
    assert all(r.status == 0 for r in recorder.records)


def test_instrumented_bridge_records_timeout() -> None:
    """No responder configured -> BridgeTimeout still leaves a record (status None)."""
    fake = FakeTransport()
    fake.open()
    recorder = bench.BenchRecorder()
    bridge = bench.InstrumentedBridge(fake, recorder)
    with pytest.raises(BridgeTimeout):
        bridge.request(int(CMD.ping), timeout=0.02)
    (record,) = recorder.records
    assert record.kind == "ping"
    assert record.status is None
    assert record.rtt_ms >= 0.0


def test_default_run_is_pipelined_no_chunk_records(tmp_path: Path) -> None:
    """默认行为钉子：run() 不传 pipelined 即流水化 —— chunk 走
    fire-and-forget、不再产生 request 计时记录（对照测量须显式
    pipelined=False），传输仍字节完整。"""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=101))
    fake = SuotaFakeTransport()
    fake.open()
    recorder = bench.BenchRecorder()
    session = SuotaSession(bench.InstrumentedBridge(fake, recorder))
    result = session.run(path, progress_cb=recorder.progress, ack_timeout=0.5)

    assert result.blocks == 1 and result.chunks == 1
    assert recorder.chunk_records() == []  # chunks never touched request()
    assert recorder.records  # control writes / info reads still recorded
    assert bytes(fake.received) == build_payload(path)


# ------------------------------------------------------------ 块时间线


def test_build_block_timelines_window_boundaries() -> None:
    """A chunk starting exactly at an ACK belongs to the *next* block window."""
    records: List[bench.RequestRecord] = [
        bench.RequestRecord("chunk", "patch_data", 1.0, 2.0, 244, 0),  # block 1
        bench.RequestRecord("chunk", "patch_data", 3.0, 4.0, 244, 0),  # boundary
        bench.RequestRecord("chunk", "patch_data", 3.5, 4.5, 244, 0),  # block 2
    ]
    progress = [
        bench.ProgressPoint(3.0, 1, 2, 2048),
        bench.ProgressPoint(6.0, 2, 2, 4096),
    ]
    timelines, tail = bench.build_block_timelines(records, progress)
    assert tail == 0
    assert [t.n_chunks for t in timelines] == [1, 2]
    assert timelines[0].ack_wait_ms == pytest.approx(1000.0)  # 2.0 -> 3.0
    assert timelines[1].ack_wait_ms == pytest.approx(1500.0)  # 4.5 -> 6.0
    assert timelines[1].send_ms == pytest.approx(2000.0)  # 1.0s + 1.0s


def test_build_block_timelines_tail_chunks_unacked() -> None:
    records = [
        bench.RequestRecord("chunk", "patch_data", 1.0, 2.0, 244, 0),
        bench.RequestRecord("chunk", "patch_data", 5.0, 6.0, 244, 0),  # after ACK
    ]
    progress = [bench.ProgressPoint(3.0, 1, 1, 2048)]
    _timelines, tail = bench.build_block_timelines(records, progress)
    assert tail == 1


def test_full_fake_run_block_grouping(tmp_path: Path) -> None:
    """3 full blocks + 101B remainder: 9/9/9/1 chunks, byte-cumulative progress."""
    _fake, recorder, result = run_fake_ota(tmp_path)
    timelines, tail = bench.build_block_timelines(recorder.records,
                                                  recorder.progress_points)
    assert tail == 0
    assert result.blocks == 4
    assert [t.n_chunks for t in timelines] == [9, 9, 9, 1]
    assert timelines[3].chunk_sizes == [101]
    assert [t.bytes_cum for t in timelines] == [2048, 4096, 6144, 6245]
    assert sum(t.send_ms for t in timelines) > 0
    assert all(t.ack_wait_ms >= 0 for t in timelines)
    # Every full block carries exactly 8*244 + 96 bytes
    assert all(sum(t.chunk_sizes) == 2048 for t in timelines[:3])


def test_deterministic_rtts_with_stepping_clock(tmp_path: Path) -> None:
    """Fake clock steps 0.5ms/call: every RTT == 0.5ms, ACK wait == one step.

    Each request consumes exactly two clock calls (start/end); the progress
    callback consumes one — so ack_wait is exactly one step on the fake.
    """
    clock = FakeClock(step_s=0.0005)
    _fake, recorder, result = run_fake_ota(tmp_path, clock=clock)
    timelines, _tail = bench.build_block_timelines(recorder.records,
                                                   recorder.progress_points)
    assert result.blocks == 4
    for timeline in timelines:
        assert all(r == pytest.approx(0.5) for r in timeline.chunk_rtts_ms)
        assert timeline.send_ms == pytest.approx(0.5 * timeline.n_chunks)
        assert timeline.ack_wait_ms == pytest.approx(0.5)
        # Stepping clock burns 2 calls/request: elapsed >= sum of RTTs.
        assert timeline.block_ms >= timeline.send_ms + timeline.ack_wait_ms


# ----------------------------------------------------------------- 汇总


def test_summary_dominant_d_pen_ack(tmp_path: Path) -> None:
    """Realistic device numbers (RTT 3ms, ACK 130ms): D dominates, A is 2nd."""
    timelines = [mk_timeline(1, 9, rtt_ms=3.0, ack_ms=130.0, t0=0.0)]
    summary = bench.summarize(timelines, recorder_with_ping(3.5), None,
                              bench.ModelParams())
    model = summary["model"]
    assert model["A_9rtt_ms"] == pytest.approx(27.0)
    assert model["C_air_send_ms"] == pytest.approx(12.428)
    assert model["C_notify_ms"] == pytest.approx(0.412)
    assert model["D_est_ms"] == pytest.approx(130.0 - 0.412)
    assert model["dominant"].startswith("D(")
    # Pipelining hides A entirely: block period -> D + notify = 130ms
    assert model["pipelined_pred_block_ms"] == pytest.approx(130.0)
    assert model["pipelined_pred_throughput_bps"] == pytest.approx(
        2048 / 0.130, rel=1e-3
    )
    assert summary["ping_rtt_ms"]["p50"] == pytest.approx(3.5)


def test_summary_dominant_a_serial_rtt() -> None:
    """Slow CDC / fast device (RTT 8ms, ACK 5ms): the 9xRTT term dominates."""
    timelines = [mk_timeline(1, 9, rtt_ms=8.0, ack_ms=5.0, t0=0.0)]
    summary = bench.summarize(timelines, recorder_with_ping(3.5), None,
                              bench.ModelParams())
    model = summary["model"]
    assert model["A_9rtt_ms"] == pytest.approx(72.0)
    assert model["D_est_ms"] == pytest.approx(5.0 - 0.412)
    assert model["dominant"].startswith("A(")
    assert model["pipelined_pred_block_ms"] == pytest.approx(12.428)


def test_summary_dominant_c_air() -> None:
    """Fast everything (RTT 0.4ms, ACK 5ms): pure air time is the floor."""
    timelines = [mk_timeline(1, 9, rtt_ms=0.4, ack_ms=5.0, t0=0.0)]
    summary = bench.summarize(timelines, recorder_with_ping(0.3), None,
                              bench.ModelParams())
    model = summary["model"]
    assert model["A_9rtt_ms"] == pytest.approx(3.6)
    assert model["C_air_send_ms"] == pytest.approx(12.428)
    assert model["dominant"].startswith("C(")
    assert model["pipelined_pred_block_ms"] == pytest.approx(12.428)


def test_summary_effective_throughput(tmp_path: Path) -> None:
    _fake, recorder, result = run_fake_ota(
        tmp_path, clock=FakeClock(step_s=0.001)
    )
    timelines, tail = bench.build_block_timelines(recorder.records,
                                                  recorder.progress_points)
    summary = bench.summarize(timelines, recorder, result, bench.ModelParams(), tail)
    assert summary["throughput"]["payload_bytes"] == 6245
    assert summary["throughput"]["effective_bps"] > 0
    assert summary["throughput"]["session_bps"] == pytest.approx(
        result.throughput, rel=1e-3
    )
    # fake clock, 0.5ms steps; transfer window = last ACK − first chunk start
    # = 18+19+19+5 = 61 steps (chunk pairs + progress calls per block window)
    assert summary["throughput"]["transfer_window_s"] == pytest.approx(0.061)


def test_summary_without_progress_and_ping() -> None:
    """Empty recorder degrades gracefully (all None, no crash)."""
    summary = bench.summarize([], bench.BenchRecorder(), None, bench.ModelParams())
    assert summary["chunk_rtt_ms"]["n"] == 0
    assert summary["ack_wait_ms"]["p50"] is None
    assert summary["throughput"]["effective_bps"] is None
    assert summary["model"]["A_9rtt_ms"] is None
    assert summary["model"]["dominant"] is None


# ----------------------------------------------------------------- 报告


def test_build_report_full_fake_run(tmp_path: Path) -> None:
    _fake, recorder, result = run_fake_ota(tmp_path)
    meta = {"port": "fake", "bank": 1, "image": "synth.img"}
    report = bench.build_report(meta, result, recorder, bench.ModelParams())
    assert report["meta"] == meta
    assert report["error"] is None
    assert report["session"]["blocks"] == 4
    assert report["session"]["bytes_written"] == 6245
    assert [b["block"] for b in report["blocks"]] == [1, 2, 3, 4]
    assert report["blocks"][3]["n_chunks"] == 1
    assert report["summary"]["tail_chunks_unacked"] == 0
    assert report["summary"]["model"]["params"]["chunk_size"] == 244
    # JSON round-trip must be lossless (ensure_ascii=False content included)
    encoded = json.dumps(report, ensure_ascii=False)
    assert json.loads(encoded)["summary"]["model"]["dominant"] is not None


def test_write_report_to_file(tmp_path: Path) -> None:
    _fake, recorder, result = run_fake_ota(tmp_path)
    report = bench.build_report({"port": "fake"}, result, recorder,
                                bench.ModelParams())
    out = bench.write_report(report, tmp_path / "report.json")
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == report


def test_build_report_records_error_field(tmp_path: Path) -> None:
    _fake, recorder, _result = run_fake_ota(tmp_path)
    report = bench.build_report({"port": "fake"}, None, recorder,
                                bench.ModelParams(), error="SuotaError: boom")
    assert report["error"] == "SuotaError: boom"
    assert report["session"] is None


# ------------------------------------------------------------------ CLI


def test_parse_args_defaults_and_overrides() -> None:
    args = bench.parse_args(["--port", "/dev/cu.test", "--bank", "1",
                             "--image", "fw.img", "--addr", "AA:BB:CC:DD:EE:FF"])
    assert args.port == "/dev/cu.test"
    assert args.bank == 1
    assert args.chunk is None  # negotiate
    assert args.pings == 20
    assert args.phy == 2
    assert args.conn_interval_ms == pytest.approx(10.0)
    assert args.rtscts is False  # CDC 默认关流控
    tuned = bench.parse_args(
        ["--port", "p", "--bank", "0", "--chunk", "244", "--phy", "1",
         "--conn-interval-ms", "7.5", "--rtscts",
         "--image", "fw.img", "--addr", "AA:BB:CC:DD:EE:FF"]
    )
    assert tuned.chunk == 244
    assert tuned.bank == 0
    assert tuned.phy == 1
    assert tuned.conn_interval_ms == pytest.approx(7.5)
    assert tuned.rtscts is True


def test_instrumented_bridge_is_a_bridge_host() -> None:
    """The wrapper must stay substitutable for BridgeHost (SuotaSession contract)."""
    fake = SuotaFakeTransport()
    fake.open()
    bridge = bench.InstrumentedBridge(fake, bench.BenchRecorder())
    assert isinstance(bridge, BridgeHost)
    # Substitutability: info reads answer through the inherited request path.
    status, body = bridge.request(
        int(CMD.gatt_read), uuid16_le(SUOTA_UUIDS["suota_mtu"])
    )
    assert status == 0
    assert int.from_bytes(body[:2], "little") == 251
