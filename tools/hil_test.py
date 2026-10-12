#!/usr/bin/env python3
"""blebridge HIL 骨架 — 硬件在环验收自动化（Task 8，ADR-005 L3 层）。

一条命令跑全链验收::

    python3 tools/hil_test.py --bridge <port>            # quick（G1）
    python3 tools/hil_test.py --bridge auto --level full # full（G2）
    python3 tools/hil_test.py --selftest                 # 无硬件 L1 演练（FakeTransport）

结构（IO 解耦）: 所有 ``step_*`` 函数的第一参数是**桥操作句柄**——即
:class:`blebridge.bridge.BridgeHost` 接口（``request`` / ``drain`` / ``wait_for``
/ ``events``）。真实硬件时句柄由 ``SerialTransport`` 支撑；``--selftest`` 与
pytest（host/tests/test_hil_selftest.py）时由 ``HilSimTransport``（FakeTransport +
桥命令/SCAN/CONN/MTU/DISC 行为 + SUOTA 从机模型，复用 host/tests/suota_fake.py）
支撑。步骤函数自身绝不做 IO 探测，因此同一套步骤在两种世界里逐字复用。

步骤（ADR-005 L3 / REQUIREMENTS 验收门）:

- quick（G1）: ping → info → scan → connect(SuotaSim) → mtu ≥247 断言 →
  EVT 断言（SCAN/CONN/MTU/DISC 事件在各步内断言）→ disconnect。
- full（G2）: quick 链路检查 → SUOTA 上传（合成镜像或 --image）→ 零损坏断言
  （SuotaResult + 字节级比对[自测/仿真]）→ 吞吐断言 → REBOOT 后 SuotaSim
  重新广播可见 → 清理性断开。

吞吐基线: **14.8 KB/s 来自参考链路实测**（REQUIREMENTS G2）。本骨架默认阈值放宽到
**10 KB/s**（容忍 Kconfig/环境差异），可用 ``--min-throughput`` 覆盖——报告与
--help 均注明。

无硬件行为: ``--bridge`` 省略/auto 探测不到候选端口，或显式端口打不开 →
退出码 3 + 指引（tools/flash.sh / tools/list_hardware.sh / 检查 USB）。
绝不假成功；所有串口读经 BridgeHost 的超时轮询，绝不卡死。

退出码: 0 全过 / 1 断言失败 / 2 环境错（依赖缺失）/ 3 无硬件。

依赖: 仅标准库 + blebridge（sys.path 注入 host/src，或 pip install -e host）。
--selftest 额外只读访问 host/tests/suota_fake.py（懒加载，硬件路径不触碰）。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "host" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from blebridge.bridge import CMD, EVT, BridgeHost, BridgeTimeout
    from blebridge.daemon import connect_payload, parse_scan_payload
    from blebridge.framing import encode_frame
    from blebridge.image import build_payload
    from blebridge.serial_scan import list_candidates
    from blebridge.suota import SUOTA_UUIDS, SuotaError, SuotaResult, SuotaSession, uuid16_le
    from blebridge.transport import SerialTransport, TransportClosed

    BLEBRIDGE_IMPORT_ERROR: "str | None" = None
except ImportError as _exc:  # 依赖缺失 -> main() 以退出码 2 报告
    BLEBRIDGE_IMPORT_ERROR = (
        f"{_exc}\n  指引: 用 host/.venv 运行本脚本，或 pip install -e "
        f"{_REPO_ROOT / 'host'}（pyserial>=3.5）"
    )

__all__ = [
    "EXIT_OK",
    "EXIT_ASSERT",
    "EXIT_ENV",
    "EXIT_NOHW",
    "PEN_NAME",
    "MIN_THROUGHPUT_DEFAULT",
    "THROUGHPUT_BASELINE_KIB",
    "StepFailure",
    "step_ping",
    "step_info",
    "step_scan",
    "step_connect",
    "step_mtu",
    "step_disconnect",
    "step_suota",
    "step_readvertise",
    "run_link_checks",
    "run_quick",
    "run_full",
    "synthetic_bytes",
    "make_reporter",
    "make_sim_bridge",
    "HilSimTransport",
    "main",
]

# ----------------------------------------------------------------- constants

EXIT_OK = 0
EXIT_ASSERT = 1
EXIT_ENV = 2
EXIT_NOHW = 3

# suota_sim 默认广播名（firmware/suota_sim/Kconfig: SUOTA_SIM_ADV_NAME）。
PEN_NAME = "SuotaSim"

# 吞吐断言: 基线 14.8 KiB/s 来自参考链路实测（REQUIREMENTS G2 / ADR-005 原则 4）；
# 默认阈值放宽到 10 KiB/s 容忍 Kconfig/环境差异，--min-throughput 可覆盖。
THROUGHPUT_BASELINE_KIB = 14.8
MIN_THROUGHPUT_DEFAULT = 10.0

# G1 验收门: MTU >= 247（外设主动 MTU_REQ 由桥固件自动应答）。
MIN_MTU_DEFAULT = 247


class StepFailure(AssertionError):
    """一个验收步骤未达标（消息面向报告，含期望值与实测值）。"""


# ------------------------------------------------------------------ reporter


def make_reporter(echo: bool = True):
    """步骤记录器: ``record(name, ok, detail)``；``rows`` 属性拿全部结果。"""
    rows: list = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))
        if echo:
            mark = "PASS" if ok else "FAIL"
            line = f"  [{mark}] {name}"
            if detail:
                line += f" — {detail}"
            print(line, flush=True)

    record.rows = rows  # type: ignore[attr-defined]
    return record


def _attempt(record, name: str, fn, fmt=str, *args, **kwargs):
    """跑一个步骤函数: 记录 PASS/FAIL；FAIL 时中止后续步骤（异常上抛）。"""
    try:
        result = fn(*args, **kwargs)
    except StepFailure as exc:
        record(name, False, str(exc))
        raise
    except (SuotaError, BridgeTimeout, TransportClosed) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        record(name, False, detail)
        raise StepFailure(detail) from exc
    record(name, True, fmt(result))
    return result


# -------------------------------------------------------------- step functions
# 全部吃"桥操作句柄"（BridgeHost 接口）；绝不做端口探测/文件 IO。


def step_ping(bridge: "BridgeHost", timeout: float = 2.0) -> str:
    """桥应答 ping: status 0 且 body 以 "blebridge" 开头（Task 9 契约）。"""
    status, body = bridge.request(int(CMD.ping), b"", timeout=timeout)
    text = body.decode("ascii", "replace")
    if status != 0:
        raise StepFailure(f"ping status={status} body={body.hex()}")
    if not text.startswith("blebridge"):
        raise StepFailure(f"ping 响应不是 blebridge<version>: {text!r}")
    return text


def step_info(bridge: "BridgeHost", timeout: float = 2.0) -> dict:
    """桥应答 info: status 0、JSON 可解析、含 version/phy 键。"""
    status, body = bridge.request(int(CMD.info), b"", timeout=timeout)
    if status != 0:
        raise StepFailure(f"info status={status} body={body.hex()}")
    try:
        info = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise StepFailure(f"info body 不是 JSON: {body.hex()}（{exc}）") from exc
    if not isinstance(info, dict):
        raise StepFailure(f"info JSON 不是对象: {info!r}")
    missing = {"version", "phy"} - set(info)
    if missing:
        raise StepFailure(f"info 缺键: {sorted(missing)}（{sorted(info)}）")
    return info


def scan_collect(bridge: "BridgeHost", duration: float = 2.0) -> dict:
    """扫描窗口收集（EVT.SCAN -> 广播记录表）；scan_stop 尽力而为。

    scan_start 响应非零时直接根因化失败（fix round1 M-2）: 桥拒绝扫描属链路
    层问题，继续等窗口只会空转。
    """
    devices: dict = {}
    cursor = bridge.events.latest_seq()
    duration_ms = max(0, min(int(duration * 1000), 0xFFFF))
    status, body = bridge.request(
        int(CMD.scan_start), struct.pack("<H", duration_ms),
        timeout=max(duration + 2.0, 3.0),
    )
    if status != 0:
        raise StepFailure(
            f"scan_start status={status} body={body.hex()}"
            "（桥拒绝扫描: 蓝牙栈未就绪 / 前次扫描未停止 / 固件状态异常）"
        )
    deadline = time.monotonic() + duration
    while True:
        for event in bridge.drain(after=cursor):
            cursor = event.app_seq
            rec = parse_scan_payload(event)
            if rec is not None:
                devices[rec["addr"]] = rec
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.02, remaining))
    try:
        bridge.request(int(CMD.scan_stop), b"", timeout=2.0)
    except BridgeTimeout:
        pass  # 扫描结果已到手；stop 失败不追责（继续后续步骤验证）
    return devices


def step_scan(
    bridge: "BridgeHost",
    duration: float = 2.0,
    expect_name: "str | None" = PEN_NAME,
) -> dict:
    """扫描: 必须看到 EVT.SCAN，且（默认）发现 SuotaSim 广播名。返回命中记录。"""
    devices = scan_collect(bridge, duration)
    if not devices:
        raise StepFailure(f"扫描 {duration:.1f}s 看到 0 台设备（suota_sim 应持续广播）")
    matched = None
    if expect_name:
        needle = expect_name.encode("ascii")
        for rec in devices.values():
            if needle in bytes.fromhex(rec["adv_hex"]):
                matched = rec
                break
        if matched is None:
            raise StepFailure(
                f"扫描到 {len(devices)} 台但无 {expect_name!r} 广播名: "
                f"{sorted(devices)}"
            )
    return matched if matched is not None else next(iter(devices.values()))


def step_connect(
    bridge: "BridgeHost",
    addr: str,
    random: bool = False,
    timeout: float = 8.0,
) -> str:
    """连接广播地址: status 0 且随后出现 EVT.CONN。返回地址。"""
    payload = connect_payload(addr=addr, random=random)
    status, body = bridge.request(int(CMD.connect), payload, timeout=timeout)
    if status != 0:
        raise StepFailure(
            f"connect({addr}) status={status} body={body.hex()}"
        )
    bridge.wait_for(int(EVT.CONN), timeout=timeout)  # BridgeTimeout -> StepFailure(上层)
    return addr


def step_mtu(
    bridge: "BridgeHost",
    minimum: int = MIN_MTU_DEFAULT,
    settle: float = 5.0,
    timeout: float = 2.0,
) -> int:
    """MTU 断言（G1: >=247）: 轮询 CMD.mtu 响应 + EVT.MTU 事件取最大值。

    连接初期协商可能在途（固件自动应答外设的 MTU_REQ），故在 ``settle`` 秒内轮询，
    达标即刻返回。
    """
    deadline = time.monotonic() + settle
    best = 0
    while True:
        for event in bridge.drain():
            if event.evt == int(EVT.MTU) and len(event.payload) >= 2:
                best = max(best, int.from_bytes(event.payload[:2], "little"))
        status, body = bridge.request(int(CMD.mtu), b"", timeout=timeout)
        if status == 0 and len(body) >= 2:
            best = max(best, int.from_bytes(body[:2], "little"))
        if best >= minimum:
            return best
        if time.monotonic() >= deadline:
            raise StepFailure(
                f"MTU 协商未达标: best={best} < {minimum}（G1 要求 >={MIN_MTU_DEFAULT}）"
            )
        time.sleep(0.2)


def step_disconnect(
    bridge: "BridgeHost",
    timeout: float = 2.0,
    event_timeout: float = 5.0,
    tolerant: bool = False,
) -> str:
    """断开: status 0 且出现 EVT.DISC。

    ``tolerant``（full 尾部清理用）: SUOTA REBOOT 后链路已被设备断开，此时
    status != 0 / 无 DISC 事件均可容忍——如实写进返回明细，不计失败。
    """
    status, body = bridge.request(int(CMD.disconnect), b"", timeout=timeout)
    if status != 0:
        if tolerant:
            return f"status={status}（REBOOT 后链路已由设备断开，清理性断开容忍）"
        raise StepFailure(f"disconnect status={status} body={body.hex()}")
    try:
        bridge.wait_for(int(EVT.DISC), timeout=event_timeout)
        return "EVT.DISC 已收到"
    except BridgeTimeout:
        if tolerant:
            return "EVT.DISC 未到（容忍: 链路已被设备重启断开）"
        raise StepFailure(
            f"disconnect 已受理但 {event_timeout:.0f}s 内未见 EVT.DISC"
        ) from None


def step_suota(
    bridge: "BridgeHost",
    image_path,
    min_throughput_kib: float = MIN_THROUGHPUT_DEFAULT,
    sink=None,
    request_timeout: float = 2.0,
    ack_timeout: float = 30.0,
    progress_cb=None,
) -> "SuotaResult":
    """SUOTA 全流程: 零损坏 + 吞吐断言（G2）。

    - 零损坏: SuotaResult.bytes_written == 源 payload（文件 + 1B XOR）；
      ``sink``（仿真世界的 fake.received）非 None 时再做字节级全等比对；
      真实硬件上设备侧 XOR/大小校验由 suota_sim 固件执行（会话成功即通过）。
    - 吞吐: result.throughput >= ``min_throughput_kib`` * 1024
      （基线 14.8 KiB/s 为参考链路实测；默认阈值放宽到 10，--min-throughput 覆盖）。
    """
    payload = build_payload(image_path)
    session = SuotaSession(bridge, request_timeout=request_timeout)
    result = session.run(
        image_path, progress_cb=progress_cb, ack_timeout=ack_timeout
    )
    if result.bytes_written != len(payload):
        raise StepFailure(
            f"上传字节数不符: {result.bytes_written} != {len(payload)}"
        )
    if sink is not None and bytes(sink) != payload:
        raise StepFailure(
            f"零损坏校验失败: 接收端 {len(bytes(sink))}B 与源 payload "
            f"{len(payload)}B 不一致"
        )
    if result.throughput < min_throughput_kib * 1024:
        raise StepFailure(
            f"吞吐 {result.throughput / 1024:.1f} KiB/s < 阈值 "
            f"{min_throughput_kib} KiB/s"
        )
    return result


def step_readvertise(
    bridge: "BridgeHost",
    expect_name: str = PEN_NAME,
    timeout: float = 15.0,
    request_timeout: float = 2.0,
) -> dict:
    """REBOOT 后重新广播可见: 在 ``timeout`` 秒扫描窗内等到 SuotaSim 广播。"""
    duration_ms = max(0, min(int(timeout * 1000), 0xFFFF))
    cursor = bridge.events.latest_seq()
    status, body = bridge.request(
        int(CMD.scan_start), struct.pack("<H", duration_ms),
        timeout=request_timeout,
    )
    if status != 0:
        # fix round1 M-2: 根因化失败，而非空等整个扫描窗
        raise StepFailure(
            f"scan_start status={status} body={body.hex()}"
            "（REBOOT 后重新扫描被桥拒绝: 蓝牙栈未就绪）"
        )
    needle = expect_name.encode("ascii")
    deadline = time.monotonic() + timeout
    found = None
    while True:
        for event in bridge.drain(after=cursor):
            cursor = event.app_seq
            rec = parse_scan_payload(event)
            if rec is not None and needle in bytes.fromhex(rec["adv_hex"]):
                found = rec
                break
        if found is not None or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    try:
        bridge.request(int(CMD.scan_stop), b"", timeout=request_timeout)
    except BridgeTimeout:
        pass
    if found is None:
        raise StepFailure(
            f"REBOOT 后 {timeout:.0f}s 内未见 {expect_name!r} 重新广播"
            "（suota_sim 默认 REBOOT_DELAY_S=10s）"
        )
    return found


# ------------------------------------------------------------- flow runners


def run_link_checks(bridge: "BridgeHost", *, record, expect_name=PEN_NAME,
                    min_mtu=MIN_MTU_DEFAULT, scan_duration=2.0,
                    request_timeout=2.0, connect_timeout=8.0, mtu_settle=5.0) -> dict:
    """quick 的链路部分（G1，不含断开）: ping→info→scan→connect→mtu。"""
    _attempt(record, "ping", step_ping, str, bridge, timeout=request_timeout)
    info = _attempt(record, "info", step_info, str, bridge, timeout=request_timeout)
    target = _attempt(
        record, "scan", step_scan, _fmt_scan, bridge,
        duration=scan_duration, expect_name=expect_name,
    )
    addr = _attempt(
        record, "connect", step_connect, _fmt_connect, bridge,
        target["addr"], random=bool(target["addr_type"]), timeout=connect_timeout,
    )
    mtu = _attempt(
        record, "mtu", step_mtu, _fmt_mtu, bridge,
        minimum=min_mtu, settle=mtu_settle, timeout=request_timeout,
    )
    return {"addr": addr, "mtu": mtu, "info": info}


def run_quick(bridge: "BridgeHost", *, record, expect_name=PEN_NAME,
              min_mtu=MIN_MTU_DEFAULT, scan_duration=2.0, request_timeout=2.0,
              connect_timeout=8.0, mtu_settle=5.0, disconnect_event_timeout=5.0) -> dict:
    """quick = G1: 链路检查 + 断开（完整闭环）。"""
    summary = run_link_checks(
        bridge, record=record, expect_name=expect_name, min_mtu=min_mtu,
        scan_duration=scan_duration, request_timeout=request_timeout,
        connect_timeout=connect_timeout, mtu_settle=mtu_settle,
    )
    _attempt(
        record, "disconnect", step_disconnect, str, bridge,
        timeout=request_timeout, event_timeout=disconnect_event_timeout,
    )
    return summary


def run_full(bridge: "BridgeHost", *, record, image_path,
             min_throughput=MIN_THROUGHPUT_DEFAULT, sink=None,
             expect_name=PEN_NAME, min_mtu=MIN_MTU_DEFAULT, scan_duration=2.0,
             request_timeout=2.0, connect_timeout=8.0, mtu_settle=5.0,
             ack_timeout=30.0, readvertise_timeout=15.0, progress_cb=None) -> "SuotaResult":
    """full = G2: 链路检查 + SUOTA（零损坏/吞吐）+ 重新广播可见 + 清理断开。"""
    run_link_checks(
        bridge, record=record, expect_name=expect_name, min_mtu=min_mtu,
        scan_duration=scan_duration, request_timeout=request_timeout,
        connect_timeout=connect_timeout, mtu_settle=mtu_settle,
    )
    result = _attempt(
        record, "suota", step_suota,
        lambda r: _fmt_suota(r, min_throughput), bridge, image_path,
        min_throughput, sink=sink, request_timeout=request_timeout,
        ack_timeout=ack_timeout, progress_cb=progress_cb,
    )
    _attempt(
        record, "readvertise", step_readvertise, _fmt_scan, bridge,
        expect_name=expect_name, timeout=readvertise_timeout,
        request_timeout=request_timeout,
    )
    _attempt(
        record, "disconnect", step_disconnect, str, bridge,
        timeout=request_timeout, tolerant=True,
    )
    return result


# ------------------------------------------------------------------- formats


def _fmt_scan(rec) -> str:
    return f"addr={rec['addr']} type={rec['addr_type']} rssi={rec['rssi']} adv={rec['adv_hex'][:32]}"


def _fmt_connect(addr) -> str:
    return f"已连接 {addr}（EVT.CONN 已确认）"


def _fmt_mtu(mtu) -> str:
    return f"MTU={mtu}"


def _fmt_suota(result, min_throughput) -> str:
    kib = result.throughput / 1024.0
    return (
        f"bytes={result.bytes_written} blocks={result.blocks} "
        f"chunks={result.chunks} elapsed={result.elapsed:.2f}s "
        f"{kib:.1f} KiB/s（阈值 {min_throughput}; 基线 14.8 KiB/s 为参考链路实测）"
    )


# ------------------------------------------------------------------- helpers


def synthetic_bytes(length: int) -> bytes:
    """确定性合成镜像（与 host/tests/suota_fake.py 同公式，供真实硬件路径）。"""
    return bytes((i * 7 + 3) % 251 for i in range(length))


def _write_image(directory: Path, data: bytes) -> Path:
    path = directory / "synthetic.img"
    path.write_bytes(data)
    return path


# ------------------------------------------------------- selftest (no hardware)


def _suota_fake_module():
    """懒加载 host/tests/suota_fake.py（仅 selftest/pytest 用；硬件路径不触碰）。"""
    tests_dir = _REPO_ROOT / "host" / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    import suota_fake

    return suota_fake


_SIM_CLS = None


def HilSimTransport(**kwargs):  # noqa: N802 —— 工厂函数，形似类构造
    """构造仿真桥+设备传输层（缓存类定义; 首次调用时懒加载 suota_fake）。"""
    global _SIM_CLS
    if _SIM_CLS is None:
        fake = _suota_fake_module()

        class _HilSimTransport(fake.SuotaFakeTransport):
            """FakeTransport + 桥级命令行为模型（selftest 世界）。

            在 SuotaFakeTransport（SUOTA 从机模型）之上补齐桥命令:
            ping/info/scan_start/scan_stop/connect/disconnect/mtu 按桥固件
            契约应答；scan_start 注入一条 SuotaSim 广播（EVT.SCAN）；connect
            注入 EVT.CONN + EVT.MTU；disconnect 注入 EVT.DISC。注入事件按
            固件规则连续编排 wire SEQ。
            """

            def __init__(
                self,
                adv_name: str = PEN_NAME,
                link_mtu: int = 247,
                bridge_version: str = "0.1.0",
                **suota_kwargs,
            ):
                super().__init__(**suota_kwargs)
                self.adv_name = adv_name
                self.link_mtu = link_mtu
                self.connect_count = 0
                self.disconnect_count = 0
                self.scan_count = 0
                self._wire_seq = -1
                # 桥地址（suota_sim Kconfig 默认固定静态随机地址）
                self._addr_bytes = bytes.fromhex("C0FFEE123456")
                self._addr_type = 1
                self.responses.update(
                    {
                        int(CMD.ping): f"\x00blebridge{bridge_version}".encode(),
                        int(CMD.info): (
                            b'\x00{"version":"' + bridge_version.encode()
                            + b'","uptime_s":1,"events_sent":0,"events_dropped":0,'
                            + b'"credits":128,"current_log_level":3,"phy":"cdc"}'
                        ),
                        int(CMD.scan_start): b"\x00",
                        int(CMD.scan_stop): b"\x00",
                        int(CMD.connect): b"\x00",
                        int(CMD.disconnect): b"\x00",
                        int(CMD.mtu): b"\x00" + struct.pack("<H", link_mtu),
                    }
                )

            # -------------------------------------------------- event injection

            def _inject_evt(self, evt: int, payload_after_subtype: bytes) -> None:
                self._wire_seq = (self._wire_seq + 1) & 0xFF
                self.inject_raw(
                    encode_frame(
                        evt, self._wire_seq, b"\x00" + payload_after_subtype
                    )
                )

            def _advertise_once(self) -> None:
                name = self.adv_name.encode("ascii")
                adv = bytes([2, 0x01, 0x06]) + bytes(
                    [len(name) + 1, 0x09]
                ) + name  # AD: flags + complete local name
                payload = (
                    bytes([self._addr_type])
                    + self._addr_bytes
                    + struct.pack("<b", -55)  # rssi i8
                    + adv
                )
                self._inject_evt(int(EVT.SCAN), payload)

            # ------------------------------------------------------ hooks

            def _on_request(self, frame) -> None:
                if frame.cmd == int(CMD.scan_start):
                    self.scan_count += 1
                    self._advertise_once()
                elif frame.cmd == int(CMD.connect):
                    self.connect_count += 1
                    self._inject_evt(int(EVT.CONN), bytes([self._addr_type]) + self._addr_bytes)
                    self._inject_evt(int(EVT.MTU), struct.pack("<H", self.link_mtu))
                elif frame.cmd == int(CMD.disconnect):
                    self.disconnect_count += 1
                    self._inject_evt(int(EVT.DISC), bytes([0x16]))
                super()._on_request(frame)

            def _notify(self, status: int) -> None:
                self.notified.append(status)
                self._inject_evt(
                    int(EVT.GATTX),
                    uuid16_le(SUOTA_UUIDS["serv_status"]) + bytes([status]),
                )

        _HilSimTransport.__name__ = "HilSimTransport"
        _SIM_CLS = _HilSimTransport
    return _SIM_CLS(**kwargs)


def make_sim_bridge(**sim_kwargs):
    """打开仿真传输并包一层 BridgeHost，返回 (transport, bridge)。"""
    transport = HilSimTransport(**sim_kwargs)
    transport.open()
    return transport, BridgeHost(transport)


def selftest_main(args) -> int:
    """--selftest: 无硬件 L1 全流程演练（同一套步骤函数 × FakeTransport 世界）。"""
    _suota_fake_module()  # 提前导入自检（缺文件/语法错即刻暴露）
    record = make_reporter()
    print("== hil_test selftest: 无硬件 L1 演练")
    print("  世界: HilSimTransport = FakeTransport + 桥命令/SCAN/CONN/MTU/DISC"
          " + SUOTA 从机模型 (host/tests/suota_fake.py)")
    print(f"  吞吐阈值: {args.min_throughput} KiB/s（基线 14.8 来自参考链路实测，"
          "默认放宽到 10，--min-throughput 覆盖）")
    rc = EXIT_OK

    # --- 1) quick (G1) -------------------------------------------------------
    print("== 场景 1/2: quick（G1: ping→info→scan→connect→mtu>=247→EVT→disconnect）")
    sim, bridge = make_sim_bridge()
    try:
        run_quick(bridge, record=record, min_mtu=args.min_mtu)
    except StepFailure:
        rc = EXIT_ASSERT
    finally:
        bridge.close()
    if rc == EXIT_OK:
        if sim.connect_count != 1 or sim.disconnect_count != 1:
            record("sim-state", False,
                   f"connect={sim.connect_count} disconnect={sim.disconnect_count}")
            rc = EXIT_ASSERT

    # --- 2) full (G2) --------------------------------------------------------
    print("== 场景 2/2: full（G2: 链路检查→SUOTA 零损坏/吞吐→重新广播→清理断开）")
    sim2, bridge2 = make_sim_bridge()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = _write_image(Path(tmp), synthetic_bytes(3 * 2048 + 100))
            run_full(
                bridge2, record=record, image_path=image,
                min_throughput=args.min_throughput, sink=sim2.received,
                min_mtu=args.min_mtu,
            )
    except StepFailure:
        rc = EXIT_ASSERT
    finally:
        bridge2.close()
    if rc == EXIT_OK and not sim2.rebooted:
        record("sim-state", False, "full 结束后仿真设备未 REBOOT（MEM_DEV 0xFD 未写）")
        rc = EXIT_ASSERT

    rows = record.rows
    passed = sum(1 for _, ok, _ in rows if ok)
    print(f"== selftest 总结: {passed}/{len(rows)} 步通过 -> 退出码 {rc}")
    return rc


# --------------------------------------------------------------------- CLI

_NOHW_GUIDANCE = """
指引:
  1. 确认 DK 已上电并接 USB（macOS: ls /dev/cu.usbmodem* 应出现桥 CDC 节点）
  2. tools/list_hardware.sh —— 查看 J-Link/串口汇总与映射提示
  3. 固件未刷? 先 tools/flash.sh app 与 tools/flash.sh suota_sim
  4. 端口被占用? 关闭其它占用进程，或显式 --bridge <port>
"""


def build_parser() -> "argparse.ArgumentParser":
    epilog = (
        "退出码: 0 全过 / 1 断言失败 / 2 环境错（依赖缺失）/ 3 无硬件\n"
        "吞吐: 默认阈值 10 KiB/s（基线 14.8 KiB/s 来自参考链路实测，"
        "--min-throughput 可覆盖）\n"
        "无硬件时绝不假成功、绝不卡死（全部串口读带超时）"
    )
    parser = argparse.ArgumentParser(
        prog="hil_test.py",
        description="blebridge HIL 验收骨架（ADR-005 L3; 步骤函数与 IO 解耦）",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--bridge", default=None, metavar="PORT",
        help="桥串口设备节点; 省略或 'auto' 自动探测 /dev/cu.usbmodem* 等",
    )
    parser.add_argument(
        "--auto", action="store_true", help="强制自动探测桥串口（同 --bridge auto）"
    )
    parser.add_argument(
        "--sim-port", default=None, metavar="PORT",
        help="suota_sim 侧串口（仅诊断/报告记录，当前步骤集不使用）",
    )
    parser.add_argument(
        "--level", choices=("quick", "full"), default="quick",
        help="quick=G1 链路验收; full=G2 全链 OTA（默认 quick）",
    )
    parser.add_argument(
        "--min-throughput", type=float, default=MIN_THROUGHPUT_DEFAULT,
        metavar="KIB_S",
        help=f"吞吐阈值 KiB/s（默认 {MIN_THROUGHPUT_DEFAULT}; "
        f"基线 {THROUGHPUT_BASELINE_KIB} KiB/s 为参考链路实测）",
    )
    parser.add_argument(
        "--min-mtu", type=int, default=MIN_MTU_DEFAULT,
        help=f"MTU 断言下限（默认 {MIN_MTU_DEFAULT}，G1）",
    )
    parser.add_argument(
        "--image", default=None, metavar="PATH",
        help="full 的 OTA 镜像（缺省: 合成镜像 --image-bytes 字节）",
    )
    parser.add_argument(
        "--image-bytes", type=int, default=640 * 1024,
        help="合成镜像字节数（默认 640KiB, 对齐 G2 基准）",
    )
    parser.add_argument("--scan-duration", type=float, default=2.0)
    parser.add_argument("--connect-timeout", type=float, default=8.0)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument("--mtu-settle", type=float, default=5.0)
    parser.add_argument("--ack-timeout", type=float, default=30.0)
    parser.add_argument("--readvertise-timeout", type=float, default=15.0)
    parser.add_argument(
        "--selftest", action="store_true",
        help="无硬件 L1 自测: FakeTransport 世界跑同一套步骤函数（可入 pytest）",
    )
    return parser


def _resolve_bridge_port(args) -> "str | None":
    """返回桥串口；探测不到返回 None（--auto 且无候选）。"""
    if args.auto or not args.bridge or args.bridge.strip().lower() == "auto":
        candidates = list_candidates()
        if not candidates:
            return None
        return candidates[0]
    return args.bridge


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if BLEBRIDGE_IMPORT_ERROR is not None:
        print(f"FAIL 环境错: blebridge 包不可用\n{BLEBRIDGE_IMPORT_ERROR}", file=sys.stderr)
        return EXIT_ENV

    if args.selftest:
        return selftest_main(args)

    # ---- 硬件世界 ----------------------------------------------------------
    print(f"== hil_test level={args.level}")
    port = _resolve_bridge_port(args)
    if port is None:
        print("FAIL 无硬件: --auto 未探测到桥串口候选", file=sys.stderr)
        print(_NOHW_GUIDANCE, file=sys.stderr)
        return EXIT_NOHW
    print(f"  桥串口: {port}")
    if args.sim_port:
        print(f"  suota_sim 串口（仅记录）: {args.sim_port}")
    print(f"  吞吐阈值: {args.min_throughput} KiB/s（基线 14.8 来自参考链路实测，"
          "默认放宽到 10，--min-throughput 覆盖）")

    transport = SerialTransport(port)
    try:
        transport.open()
    except TransportClosed as exc:
        print(f"FAIL 无硬件: 桥串口打开失败 {port}（{exc}）", file=sys.stderr)
        print(_NOHW_GUIDANCE, file=sys.stderr)
        return EXIT_NOHW

    record = make_reporter()
    bridge = BridgeHost(transport)
    rc = EXIT_OK
    tmp_dir = None
    try:
        if args.level == "quick":
            run_quick(
                bridge, record=record, expect_name=PEN_NAME,
                min_mtu=args.min_mtu, scan_duration=args.scan_duration,
                request_timeout=args.request_timeout,
                connect_timeout=args.connect_timeout, mtu_settle=args.mtu_settle,
            )
        else:
            if args.image:
                image_path: Path = Path(args.image)
                if not image_path.is_file():
                    print(f"FAIL 环境错: --image 不存在: {image_path}",
                          file=sys.stderr)
                    bridge.close()
                    return EXIT_ENV
            else:
                tmp_dir = tempfile.TemporaryDirectory()
                image_path = _write_image(
                    Path(tmp_dir.name), synthetic_bytes(args.image_bytes)
                )
            run_full(
                bridge, record=record, image_path=image_path,
                min_throughput=args.min_throughput, min_mtu=args.min_mtu,
                scan_duration=args.scan_duration,
                request_timeout=args.request_timeout,
                connect_timeout=args.connect_timeout,
                mtu_settle=args.mtu_settle, ack_timeout=args.ack_timeout,
                readvertise_timeout=args.readvertise_timeout,
            )
    except StepFailure:
        rc = EXIT_ASSERT
    finally:
        bridge.close()
        if tmp_dir is not None:
            tmp_dir.cleanup()

    rows = record.rows
    passed = sum(1 for _, ok, _ in rows if ok)
    print(f"== 总结: {passed}/{len(rows)} 步通过 -> 退出码 {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
