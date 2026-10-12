#!/usr/bin/env python3
"""OTA 传输瓶颈测量工具（C-性能线：分环节基准的 SUOTA 全流程版）。

零侵入：不改动 ``blebridge.suota`` / ``blebridge.bridge`` / 固件——计时全部在包装层
（:class:`InstrumentedBridge` 重载 ``BridgeHost.request`` 外部计时）与独立模块
（本文件）实现。任何 SUOTA 设备的数据都可直接用本工具采集。

用法::

    python tools/ota_bench.py [--port <p>] --bank 1 [--image <img>] [--chunk 244]
                              [--json out.json] [--pings 20]
                              [--addr AA:BB:CC:DD:EE:FF] [--phy 2]
                              [--conn-interval-ms 10]

内部流程（同 /tmp/ota_run.py 的既有打法，独立实现）：
open（SerialTransport 自带 U-1 settle）→ best-effort disconnect → drain →
connect → dm 就绪等待（gatt_read suota_version 轮询）→ ping 基线（A 段裸 RTT）
→ ``SuotaSession.run``（progress_cb 每块一次）→ 汇总 → JSON。

测什么（对应四段链路模型）：

- **chunk RTT**：每次 PATCH_DATA write-without-response 的桥请求往返 =
  A(USB CDC 帧往返) + B(固件 GATT 路径) + BLE 入队/空口等待。
- **ping RTT**：裸 CMD.ping 往返 ≈ A + B(帧路径)，是 chunk RTT 的基线。
- **ack_wait**：块内最后一次 chunk 响应 → progress 回调（块 ACK notify 被
  host 消费）≈ D(设备侧处理/写 flash + ACK) + C_notify + conn event 对齐抖动。
- **每块时间线**：9 次 chunk RTT + 发送跨度 + ACK 等待 + 块总时长。

输出 JSON：meta / session / ping / blocks 逐块时间线 / summary（分位数 +
有效吞吐 + A/B/C/D 模型拆分估计）。B 段单独不可测（固件内部），以
``chunk_rtt_p50 − ping_p50`` 残差呈现；C 段走空口模型（可计算）；D 段 =
ack_wait − C_notify（黑盒估计，±1 个 conn interval 对齐抖动）。

退出码: 0 成功 / 2 OTA 失败 / 3 环境（端口打不开）。真机运行不在离线验收内；
单元测试（host/tests/test_ota_bench.py）用 FakeTransport + 可注入时钟驱动。
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "host" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from blebridge.bridge import CMD, BridgeHost, BridgeTimeout  # noqa: E402
from blebridge.config import SerialResolveError, resolve_serial  # noqa: E402
from blebridge.daemon import connect_payload  # noqa: E402
from blebridge.serial_scan import list_candidates  # noqa: E402
from blebridge.suota import (  # noqa: E402
    DEFAULT_BLOCK_SIZE,
    SUOTA_UUIDS,
    SuotaError,
    SuotaProgress,
    SuotaResult,
    SuotaSession,
    uuid16_le,
    uuid16_prefix,
)
from blebridge.transport import SerialTransport, TransportClosed  # noqa: E402

__all__ = [
    "InstrumentedBridge",
    "BenchRecorder",
    "RequestRecord",
    "ProgressPoint",
    "BlockTimeline",
    "ModelParams",
    "classify_request",
    "build_block_timelines",
    "summarize",
    "build_report",
    "write_report",
    "percentile",
    "pstats",
    "main",
]

DEFAULT_JSON = "ota_bench_report.json"

# ---------------------------------------------------------------- 空口模型
# BLE 空口时间模型（交付 1-C 的可计算形式；假设见报告 §2-C）:
# - 2M PHY 每字节 4 µs（1M 为 8 µs）；LL 包 = preamble(1)+AA(4)+hdr(2)+payload+CRC(3)。
# - 明文链路（FR-B5 前 SMP 未启用）无 MIC → 244B chunk 恰好填满 DLE=251 的
#   一个 LL 包（3+244+4=251）；启用加密后须按 +4 MIC 重算（可能裂成 2 包）。
# - conn event 内 central/peripheral 交替收发：我方每个数据包后是对端空包，
#   包间开销 = IFS(150) + 空包(40 @2M) + IFS(150) = 340 µs。
PHY_BYTE_US = {1: 8.0, 2: 4.0}
LL_OVERHEAD_BYTES = 10  # preamble + AA + LL header + CRC
IFS_US = 150.0
ATT_WRITE_HEADER = 3  # write command: opcode(1) + handle(2)
L2CAP_HEADER = 4
MIC_BYTES_PLAINTEXT = 0  # FR-B5 加密启用后改 4（模型自动裂包）
DLE_PAYLOAD_DEFAULT = 251
CONN_INTERVAL_MS_DEFAULT = 10.0  # bring-up 实测 10ms（8 units）


def empty_pdu_us(phy: int = 2) -> float:
    """LL 空包空中时间（10 字节 × 每字节时间）。"""
    return LL_OVERHEAD_BYTES * PHY_BYTE_US[phy]


def ll_packet_us(ll_payload: int, phy: int = 2) -> float:
    """一个 LL Data PDU 的空中时间（µs），payload 含 L2CAP 头与 MIC。"""
    return (ll_payload + LL_OVERHEAD_BYTES) * PHY_BYTE_US[phy]


def chunk_ll_payload(chunk_size: int, mic: int = MIC_BYTES_PLAINTEXT) -> int:
    """一个 PATCH_DATA chunk 走空口的 LL payload 字节数。"""
    return ATT_WRITE_HEADER + chunk_size + L2CAP_HEADER + mic


def notify_ll_payload(status_bytes: int = 1) -> int:
    """SERV_STATUS notify（opcode+handle+value）的 LL payload 字节数。"""
    return 3 + status_bytes + L2CAP_HEADER


def exchange_us(pkt_us: float, phy: int = 2) -> float:
    """conn event 内一个数据包占用的总时间（包 + 对端空包 + 两个 IFS）。"""
    return pkt_us + IFS_US + empty_pdu_us(phy) + IFS_US


def packets_per_event(conn_interval_ms: float, pkt_us: float, phy: int = 2) -> int:
    """一个 conn event 能塞下的数据包数（首包不计对端前置 IFS）。"""
    budget = conn_interval_ms * 1000.0 - pkt_us
    if budget <= 0:
        return 1
    return 1 + int(budget // exchange_us(pkt_us, phy))


def air_send_us(
    n_packets: int,
    pkt_us: float,
    conn_interval_ms: float,
    phy: int = 2,
) -> float:
    """n 个数据包跨 conn event 的纯空口发送时间（µs，事件对齐近似）。

    事件 e 的起点近似为 e × conn_interval；末事件内 k 包耗时 k×pkt + (k-1)×exchange。
    """
    if n_packets <= 0:
        return 0.0
    ppe = packets_per_event(conn_interval_ms, pkt_us, phy)
    events = int(math.ceil(n_packets / ppe))
    last_k = n_packets - (events - 1) * ppe
    gap_us = exchange_us(pkt_us, phy) - pkt_us  # IFS + 对端空包 + IFS
    span = (events - 1) * conn_interval_ms * 1000.0
    span += last_k * pkt_us + (last_k - 1) * gap_us
    return span


# ------------------------------------------------------------------ 统计


def percentile(values: List[float], q: float) -> float:
    """线性插值分位数（q ∈ [0, 100]）；空列表抛 ValueError。"""
    if not values:
        raise ValueError("percentile of empty list")
    if not 0 <= q <= 100:
        raise ValueError(f"q out of range [0,100]: {q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * (q / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def pstats(values: List[float]) -> Dict[str, Optional[float]]:
    """p50/p95/max 摘要（空列表 → 全 None 字段）。"""
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    return {
        "n": len(values),
        "p50": round(percentile(values, 50), 4),
        "p95": round(percentile(values, 95), 4),
        "max": round(max(values), 4),
    }


# -------------------------------------------------------------- 记录结构


@dataclass
class RequestRecord:
    """一次桥请求的外部计时记录。"""

    kind: str  # "chunk" | "control_write" | "info_read" | "gatt_read" | "ping" | cmd 名
    name: str  # 特征名 / 命令名（"patch_data" / "mem_dev" / "ping" / …）
    t_start: float  # clock() 单调时刻
    t_end: float
    value_size: int  # gatt_write 的 value 字节数（其余为 payload 长度）
    status: Optional[int]  # 桥响应 status；None = 超时

    @property
    def rtt_ms(self) -> float:
        return (self.t_end - self.t_start) * 1000.0


@dataclass
class ProgressPoint:
    """一次 progress 回调（= 一个块被 ACK 的时刻）。"""

    t: float
    block: int
    total_blocks: int
    bytes_cum: int


@dataclass
class ModelParams:
    """空口模型参数（汇总/报告中透出，供对账）。"""

    phy: int = 2
    conn_interval_ms: float = CONN_INTERVAL_MS_DEFAULT
    dle_payload: int = DLE_PAYLOAD_DEFAULT
    mic_bytes: int = MIC_BYTES_PLAINTEXT
    chunk_size: int = 244
    block_size: int = DEFAULT_BLOCK_SIZE


class BenchRecorder:
    """收集 InstrumentedBridge 的请求记录 + progress 回调时间点。

    ``clock`` 可注入（默认 time.monotonic）；测试用步进时钟得到确定性 RTT。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.records: List[RequestRecord] = []
        self.progress_points: List[ProgressPoint] = []

    def add_request(
        self,
        kind: str,
        name: str,
        t_start: float,
        t_end: float,
        value_size: int,
        status: Optional[int],
    ) -> None:
        self.records.append(
            RequestRecord(kind, name, t_start, t_end, value_size, status)
        )

    def progress(self, p: SuotaProgress) -> None:
        self.progress_points.append(
            ProgressPoint(self.clock(), p.block, p.total_blocks, p.bytes)
        )

    # ------------------------------------------------------------- 便捷视图

    def of_kind(self, kind: str) -> List[RequestRecord]:
        return [r for r in self.records if r.kind == kind]

    def chunk_records(self) -> List[RequestRecord]:
        return self.of_kind("chunk")

    def ping_rtts_ms(self) -> List[float]:
        return [r.rtt_ms for r in self.of_kind("ping") if r.status == 0]


# ------------------------------------------------------------- 分类与包装

_CHAR_NAMES: Dict[int, str] = {
    uuid16_prefix(u): n for n, u in SUOTA_UUIDS.items()
}
_INFO_CHARS = {"suota_version", "patch_data_char_size", "suota_mtu", "l2cap_psm"}
_CMD_NAMES = {int(c): c.name for c in CMD}


def classify_request(cmd: int, payload: bytes) -> Tuple[str, str]:
    """按桥命令/payload 前缀分类一次请求 → (kind, name)。"""
    if cmd == int(CMD.gatt_write) and len(payload) >= 3:
        (u16,) = struct.unpack_from("<H", payload, 1)
        name = _CHAR_NAMES.get(u16, f"0x{u16:04x}")
        write_type = payload[0]
        if write_type == 1 and name == "patch_data":
            return "chunk", name
        return "control_write", name
    if cmd in (int(CMD.subscribe), int(CMD.unsubscribe)):
        return "control_write", _CMD_NAMES.get(cmd, f"0x{cmd:02x}")
    if cmd == int(CMD.gatt_read) and len(payload) >= 2:
        (u16,) = struct.unpack("<H", payload[:2])
        name = _CHAR_NAMES.get(u16, f"0x{u16:04x}")
        if name in _INFO_CHARS:
            return "info_read", name
        return "gatt_read", name
    return _CMD_NAMES.get(cmd, f"0x{cmd:02x}"), _CMD_NAMES.get(cmd, f"0x{cmd:02x}")


def _value_size(cmd: int, payload: bytes) -> int:
    """gatt_write 记录 value 字节数（跳过 wtype+uuid16 前缀），其余记 payload。"""
    if cmd in (int(CMD.gatt_write),) and len(payload) >= 3:
        return len(payload) - 3
    return len(payload)


class InstrumentedBridge(BridgeHost):
    """带外部计时的 BridgeHost 包装层（零侵入：只重载 request）。

    每次请求记录 (分类, 起止时刻, value 大小, status)，超时也记录（status=None）
    后原样上抛。其余行为（事件环/credit/同步泵）全部继承。
    """

    def __init__(self, transport, recorder: BenchRecorder, **kwargs) -> None:
        super().__init__(transport, **kwargs)
        self.recorder = recorder

    def request(self, cmd, payload=b"", timeout=2.0) -> Tuple[int, bytes]:
        cmd = int(cmd)
        kind, name = classify_request(cmd, payload)
        size = _value_size(cmd, payload)
        t0 = self.recorder.clock()
        try:
            status, body = super().request(cmd, payload, timeout=timeout)
        except BridgeTimeout:
            self.recorder.add_request(
                kind, name, t0, self.recorder.clock(), size, None
            )
            raise
        self.recorder.add_request(kind, name, t0, self.recorder.clock(), size, status)
        return status, body


# --------------------------------------------------------------- 时间线


@dataclass
class BlockTimeline:
    """一个块的传输时间线（由 progress 回调切窗）。"""

    block: int
    total_blocks: int
    bytes_cum: int
    n_chunks: int
    chunk_sizes: List[int]
    chunk_rtts_ms: List[float]
    send_ms: float  # Σ chunk RTT（host 串行发送成本，A 段主体）
    control_ms: float  # 窗内控制写 RTT 合计（块 1 含 subscribe/MEM_DEV/GPIO_MAP）
    t_first_chunk: Optional[float]
    t_last_chunk_end: Optional[float]
    t_ack: float
    span_ms: Optional[float]  # 首块首 chunk 起 → ACK（含控制写）
    ack_wait_ms: Optional[float]  # 末 chunk 响应 → ACK（D + C_notify + 抖动）

    @property
    def block_ms(self) -> Optional[float]:
        """块总时长：首 chunk 发起 → ACK。"""
        if self.t_first_chunk is None:
            return None
        return (self.t_ack - self.t_first_chunk) * 1000.0


def build_block_timelines(
    records: List[RequestRecord], progress_points: List[ProgressPoint]
) -> Tuple[List[BlockTimeline], int]:
    """按 progress 回调时刻切窗，把 chunk/控制写归属到块。

    返回 (时间线列表, 未归属的尾部 chunk 数——失败中断时遗留的最后一块)。
    records 必须按时间升序（同步单线程保证）；窗为 [prev_ack, ack)。
    """
    timelines: List[BlockTimeline] = []
    for i, point in enumerate(progress_points):
        lo = progress_points[i - 1].t if i > 0 else float("-inf")
        window = [r for r in records if lo <= r.t_start < point.t]
        chunks = [r for r in window if r.kind == "chunk"]
        control_ms = sum(r.rtt_ms for r in window if r.kind == "control_write")
        first = chunks[0] if chunks else None
        last_end = chunks[-1].t_end if chunks else None
        timelines.append(
            BlockTimeline(
                block=point.block,
                total_blocks=point.total_blocks,
                bytes_cum=point.bytes_cum,
                n_chunks=len(chunks),
                chunk_sizes=[r.value_size for r in chunks],
                chunk_rtts_ms=[r.rtt_ms for r in chunks],
                send_ms=sum(r.rtt_ms for r in chunks),
                control_ms=control_ms,
                t_first_chunk=first.t_start if first else None,
                t_last_chunk_end=last_end,
                t_ack=point.t,
                span_ms=(
                    (point.t - first.t_start) * 1000.0 if first else None
                ),
                ack_wait_ms=(
                    (point.t - last_end) * 1000.0 if last_end is not None else None
                ),
            )
        )
    last_ack = progress_points[-1].t if progress_points else float("inf")
    tail_chunks = sum(1 for r in records if r.kind == "chunk" and r.t_start >= last_ack)
    return timelines, tail_chunks


# ----------------------------------------------------------------- 汇总


def _dominant(a_ms: float, c_ms: float, d_ms: float) -> str:
    """按 max(A, C, D) 判定主导项（并列时按 A > C > D 顺序报先者）。"""
    pairs = [
        ("A(host↔bridge 9×chunk RTT)", a_ms),
        ("C(BLE 空口发送)", c_ms),
        ("D(设备侧 ACK/处理)", d_ms),
    ]
    best = max(pairs, key=lambda kv: kv[1])
    return best[0]


def summarize(
    timelines: List[BlockTimeline],
    recorder: BenchRecorder,
    result: Optional[SuotaResult],
    params: ModelParams,
    tail_chunks: int = 0,
) -> Dict:
    """分位数汇总 + A/B/C/D 模型拆分（假设与量级见报告）。"""
    chunk_rtts = [v for t in timelines for v in t.chunk_rtts_ms]
    sends = [t.send_ms for t in timelines]
    acks = [t.ack_wait_ms for t in timelines if t.ack_wait_ms is not None]
    blocks_ms = [t.block_ms for t in timelines if t.block_ms is not None]
    controls = [t.control_ms for t in timelines[1:]]  # 块 1 含会话建立，剔除

    # --- 有效吞吐: 传输窗口 = 首 chunk 发起 → 末块 ACK
    firsts = [t.t_first_chunk for t in timelines if t.t_first_chunk is not None]
    if result is not None:
        payload_bytes = result.bytes_written
    elif timelines:
        payload_bytes = timelines[-1].bytes_cum
    else:
        payload_bytes = 0
    transfer_window_s = None
    effective_bps = None
    if firsts and timelines:
        transfer_window_s = timelines[-1].t_ack - min(firsts)
        if transfer_window_s > 0 and payload_bytes:
            effective_bps = payload_bytes / transfer_window_s

    # --- 模型拆分
    ping = pstats(recorder.ping_rtts_ms())
    chunk_p50 = percentile(chunk_rtts, 50) if chunk_rtts else None
    ack_p50 = percentile(acks, 50) if acks else None
    pkt_us = ll_packet_us(
        chunk_ll_payload(params.chunk_size, params.mic_bytes), params.phy
    )
    c_air_send_ms = air_send_us(
        timelines[0].n_chunks if timelines else 9, pkt_us,
        params.conn_interval_ms, params.phy,
    ) / 1000.0
    c_notify_ms = exchange_us(
        ll_packet_us(notify_ll_payload(), params.phy), params.phy
    ) / 1000.0
    a_9rtt_ms = 9.0 * chunk_p50 if chunk_p50 is not None else None
    b_residual_ms = None
    if chunk_p50 is not None and ping["p50"] is not None:
        b_residual_ms = chunk_p50 - float(ping["p50"])
    d_est_ms = None
    if ack_p50 is not None:
        d_est_ms = ack_p50 - c_notify_ms  # 黑盒估计: ±1 conn interval 对齐抖动

    dominant = None
    pred_pipelined_block_ms = None
    pred_pipelined_bps = None
    if None not in (a_9rtt_ms, d_est_ms):
        dominant = _dominant(a_9rtt_ms, c_air_send_ms, d_est_ms)
        # 流水化后块周期 → max(空口, D+notify)：A 可被完全隐藏
        pred_pipelined_block_ms = max(c_air_send_ms, d_est_ms + c_notify_ms)
        if params.block_size and pred_pipelined_block_ms > 0:
            pred_pipelined_bps = params.block_size / (pred_pipelined_block_ms / 1000.0)

    return {
        "chunk_rtt_ms": pstats(chunk_rtts),
        "ping_rtt_ms": ping,
        "block_send_ms": pstats(sends),
        "ack_wait_ms": pstats(acks),
        "block_ms": pstats(blocks_ms),
        "control_write_ms_p50": (
            round(percentile(controls, 50), 4) if controls else None
        ),
        "tail_chunks_unacked": tail_chunks,
        "throughput": {
            "payload_bytes": payload_bytes,
            "transfer_window_s": (
                round(transfer_window_s, 4) if transfer_window_s is not None else None
            ),
            "effective_bps": round(effective_bps, 2) if effective_bps else None,
            "session_bps": round(result.throughput, 2) if result else None,
        },
        "model": {
            "params": {
                "phy_mbps": params.phy,
                "conn_interval_ms": params.conn_interval_ms,
                "dle_payload": params.dle_payload,
                "mic_bytes": params.mic_bytes,
                "chunk_size": params.chunk_size,
                "block_size": params.block_size,
                "chunk_ll_packet_us": round(pkt_us, 1),
                "packets_per_event": packets_per_event(
                    params.conn_interval_ms, pkt_us, params.phy
                ),
            },
            "A_9rtt_ms": round(a_9rtt_ms, 3) if a_9rtt_ms is not None else None,
            "B_residual_ms": (
                round(b_residual_ms, 3) if b_residual_ms is not None else None
            ),
            "C_air_send_ms": round(c_air_send_ms, 3),
            "C_notify_ms": round(c_notify_ms, 3),
            "D_est_ms": round(d_est_ms, 3) if d_est_ms is not None else None,
            "dominant": dominant,
            "pipelined_pred_block_ms": (
                round(pred_pipelined_block_ms, 3)
                if pred_pipelined_block_ms is not None
                else None
            ),
            "pipelined_pred_throughput_bps": (
                round(pred_pipelined_bps, 2) if pred_pipelined_bps else None
            ),
            "notes": [
                "A=9×chunk_rtt_p50（host 串行发送成本，CDC RTT 主导）",
                "B=chunk_rtt_p50 − ping_rtt_p50（固件 GATT 路径+BLE 入队残差，不可单测）",
                "C=空口模型（见 params；明文链路 244B chunk 恰好 1 个 LL 包）",
                "D=ack_wait_p50 − C_notify（设备侧黑盒估计，±1 conn interval 对齐抖动）",
                "pipelined 预测 = 流水化深度≥1（不等块 ACK 串行发送）后的块周期下限",
            ],
        },
    }


def build_report(
    meta: Dict,
    result: Optional[SuotaResult],
    recorder: BenchRecorder,
    params: ModelParams,
    error: Optional[str] = None,
) -> Dict:
    """组装完整 JSON 报告（meta / session / blocks / summary）。"""
    timelines, tail_chunks = build_block_timelines(
        recorder.records, recorder.progress_points
    )
    report: Dict = {
        "tool": "ota_bench",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "meta": meta,
        "session": None,
        "error": error,
        "blocks": [
            {
                "block": t.block,
                "total_blocks": t.total_blocks,
                "bytes_cum": t.bytes_cum,
                "n_chunks": t.n_chunks,
                "chunk_sizes": t.chunk_sizes,
                "chunk_rtt_ms": [round(v, 4) for v in t.chunk_rtts_ms],
                "send_ms": round(t.send_ms, 4),
                "control_ms": round(t.control_ms, 4),
                "span_ms": round(t.span_ms, 4) if t.span_ms is not None else None,
                "ack_wait_ms": (
                    round(t.ack_wait_ms, 4) if t.ack_wait_ms is not None else None
                ),
                "block_ms": round(t.block_ms, 4) if t.block_ms is not None else None,
            }
            for t in timelines
        ],
    }
    if result is not None:
        report["session"] = {
            "blocks": result.blocks,
            "chunks": result.chunks,
            "elapsed_s": round(result.elapsed, 4),
            "throughput_bps": round(result.throughput, 2),
            "bytes_written": result.bytes_written,
        }
    report["summary"] = summarize(timelines, recorder, result, params, tail_chunks)
    return report


def write_report(report: Dict, path) -> Path:
    out = Path(path)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ------------------------------------------------------------------ 主流程


def wait_dm_ready(
    bridge: BridgeHost, timeout_s: float = 12.0, poll_s: float = 0.4
) -> bytes:
    """连上后等设备侧 dm/服务就绪：gatt_read suota_version 直到 status 0。"""
    deadline = time.monotonic() + timeout_s
    body = b""
    while True:
        status, body = bridge.request(
            int(CMD.gatt_read), uuid16_le(SUOTA_UUIDS["suota_version"]), timeout=8.0
        )
        if status == 0:
            return body
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"dm 就绪等待超时（suota_version status={status}）"
            )
        time.sleep(poll_s)


def run_ping_baseline(bridge: BridgeHost, n: int, timeout: float = 2.0) -> int:
    """裸 ping 基线（A 段）；失败不致命（返回成功次数）。"""
    ok = 0
    for _ in range(n):
        try:
            status, _body = bridge.request(int(CMD.ping), timeout=timeout)
        except BridgeTimeout:
            continue
        if status == 0:
            ok += 1
    return ok


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="OTA 传输瓶颈测量（零侵入包装层，任何 SUOTA 设备数据可直接采集）"
    )
    ap.add_argument("--port", default=None,
                    help="串口（CDC: /dev/cu.usbmodem*）；缺省时经 "
                         "blebridge.config.resolve_serial 按 显式 > 配置 > "
                         "自动探测 解析（macOS 复位后口会重编号，勿硬编码）")
    ap.add_argument("--bank", type=int, default=1, help="MEM_DEV bank（默认 1）")
    ap.add_argument("--image", required=True, help="镜像路径（必填）")
    ap.add_argument("--chunk", type=int, default=None, help="chunk 大小（默认协商）")
    ap.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    ap.add_argument("--json", default=DEFAULT_JSON, help="输出 JSON 路径")
    ap.add_argument("--addr", required=True, help="设备地址 AA:BB:CC:DD:EE:FF（必填）")
    ap.add_argument("--pings", type=int, default=20, help="ping 基线次数")
    ap.add_argument("--baud", type=int, default=115200, help="UART 形态波特率")
    ap.add_argument("--rtscts", action="store_true", help="启用 RTS/CTS（CDC 勿开）")
    ap.add_argument("--phy", type=int, default=2, choices=(1, 2), help="模型 PHY")
    ap.add_argument("--conn-interval-ms", type=float, default=CONN_INTERVAL_MS_DEFAULT)
    ap.add_argument("--ack-timeout", type=float, default=30.0)
    ap.add_argument("--pair", action="store_true",
                    help="OTA 入口需加密链路时：MEM_DEV 0x0F 自动配对并重试")
    ap.add_argument("--list-ports", action="store_true", help="列出候选串口退出")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_ports:
        for c in list_candidates():
            print(c)
        return 0

    # D5（task-dfu-a §9.5）: macOS CDC 口路径复位后偶发重编号（usbmodemNNNN），
    # --port 从必填改为可选——缺省时经 resolve_serial 按 显式 > 配置文件 >
    # usbmodem 自动探测 链解析；只取端口，bench 自己的 --baud/--rtscts 语义
    # 保持不变（CDC 形态下 baud 无实际意义）。
    if args.port is None:
        try:
            args.port, _cfg_baud, _cfg_rtscts = resolve_serial()
        except SerialResolveError as exc:
            print(f"[bench] 串口解析失败: {exc}", file=sys.stderr)
            return 3
        print(f"[bench] --port 未指定，自动探测: {args.port}", flush=True)
        port_source = "resolve_serial"
    else:
        port_source = "explicit"

    meta: Dict = {
        "port": args.port,
        "port_source": port_source,
        "baud": args.baud,
        "rtscts": args.rtscts,
        "bank": args.bank,
        "addr": args.addr,
        "image": str(args.image),
        "chunk_requested": args.chunk,
        "block_size": args.block_size,
        "model": {"phy_mbps": args.phy, "conn_interval_ms": args.conn_interval_ms},
    }
    recorder = BenchRecorder()
    params = ModelParams(
        phy=args.phy,
        conn_interval_ms=args.conn_interval_ms,
        chunk_size=args.chunk or 244,
        block_size=args.block_size,
    )
    result: Optional[SuotaResult] = None
    error: Optional[str] = None
    bridge: Optional[InstrumentedBridge] = None
    try:
        transport = SerialTransport(args.port, baudrate=args.baud, rtscts=args.rtscts)
        try:
            transport.open()
        except TransportClosed as exc:
            print(f"[bench] 串口打不开: {exc}", file=sys.stderr)
            return 3
        bridge = InstrumentedBridge(transport, recorder)
        try:
            bridge.request(int(CMD.disconnect), timeout=2.0)
        except (BridgeTimeout, TransportClosed):
            pass  # 首连前本就可能未连接
        time.sleep(1.0)
        bridge.drain()

        status, body = bridge.request(
            int(CMD.connect), connect_payload(addr=args.addr), timeout=8.0
        )
        print(f"[bench] connect status={status}", flush=True)
        if status != 0:
            raise RuntimeError(f"connect 失败 status={status} body={body.hex()}")
        # generic 固件不再自动发现：设备需唤醒 settle + 显式触发 0xFEF5 发现
        time.sleep(2.0)
        bridge.request(
            int(CMD.disc_svc), struct.pack("<H", 0xFEF5), timeout=8.0
        )
        version = wait_dm_ready(bridge)
        print(f"[bench] dm_ready suota_version={version.hex()}", flush=True)
        bridge.drain()

        ok = run_ping_baseline(bridge, args.pings)
        print(f"[bench] ping 基线 {ok}/{args.pings}", flush=True)

        session = SuotaSession(bridge, block_size=args.block_size)

        def on_progress(p: SuotaProgress) -> None:
            recorder.progress(p)
            print(
                f"[bench] block {p.block}/{p.total_blocks} bytes={p.bytes}",
                flush=True,
            )

        result = session.run(
            args.image,
            progress_cb=on_progress,
            chunk_size=args.chunk,
            ack_timeout=args.ack_timeout,
            bank=args.bank,
            pair=args.pair,
        )
        print(
            f"[bench] RESULT blocks={result.blocks} bytes={result.bytes_written} "
            f"elapsed={result.elapsed:.2f}s throughput={result.throughput:.1f}B/s",
            flush=True,
        )
        meta["negotiated"] = {"suota_version": version.hex()}
    except (SuotaError, RuntimeError, BridgeTimeout, TransportClosed) as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[bench] 失败: {error}", file=sys.stderr)
    finally:
        if bridge is not None and bridge.is_open:
            try:
                bridge.request(int(CMD.disconnect), timeout=3.0)
            except (BridgeTimeout, TransportClosed):
                pass
            bridge.close()

    report = build_report(meta, result, recorder, params, error=error)
    out = write_report(report, args.json)
    print(f"[bench] 报告: {out}", flush=True)
    summary = report["summary"]
    print(
        "[bench] chunk_rtt p50={}/p95={} ms | ack_wait p50={} ms | "
        "block p50={} ms | effective={} B/s".format(
            summary["chunk_rtt_ms"]["p50"],
            summary["chunk_rtt_ms"]["p95"],
            summary["ack_wait_ms"]["p50"],
            summary["block_ms"]["p50"],
            summary["throughput"]["effective_bps"],
        ),
        flush=True,
    )
    if error is not None:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
