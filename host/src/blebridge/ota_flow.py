"""一键 OTA 流程（UX 简化波痛点 2）——SUOTA 设备升级的产品化编排。

:func:`run_ota` 把完整 OTA 验证流程固化为可导入函数，完整编排
（CLI ``blebridge ota`` 与 daemon/测试共用，CLI 只是薄壳）：

    断旧链（容错）→ connect → 设备唤醒 settle 2s → disc_svc(0xFEF5) →
    dm 轮询（读 suota_version 判就绪：8s 超时/次、12s 总上限）→ drain →
    :meth:`SuotaSession.run`（pair/bank/pipelined 全部复用 SuotaSession
    既有参数，不重复造轮子）→ disconnect → 设备存活验证（等重广播 →
    重连 → 读 suota_version；参考设备实测 OTA 重启后 ~10s 重广播）。

:class:`SameImageError` 原样上抛（CLI 转译为人话："设备已是同版本，无需升级"）；
上传成功但设备未回来抛 :class:`DeviceNotBackError`。

全部等待经模块级 :data:`sleep`（默认 ``time.sleep``）——测试注入 no-op 即可
保持零真实等待；其余时序参数均有带实战默认值的关键字参数。
"""

from __future__ import annotations

import struct
import time
from typing import Callable, Optional, Tuple, Union

from .bridge import CMD, BridgeHost, BridgeTimeout
from .daemon import connect_payload, parse_scan_payload
from .suota import (
    SUOTA_SVC_UUID16,
    SUOTA_UUIDS,
    SameImageError,
    SuotaProgress,
    SuotaResult,
    SuotaSession,
    uuid16_prefix,
)
from .transport import Transport, TransportClosed

__all__ = [
    "DeviceNotBackError",
    "OtaFlowError",
    "run_ota",
    "verify_device_alive",
]

sleep = time.sleep
"""模块级等待注入点（默认 time.sleep；测试 monkeypatch 成 no-op）。"""


class OtaFlowError(Exception):
    """OTA 编排步骤失败（连接/发现/dm 未就绪等），message 为人话指引。"""


class DeviceNotBackError(Exception):
    """镜像上传成功，但设备重启后存活验证未通过（未重广播/重连失败/GATT 静默）。"""


def run_ota(
    bridge_or_transport: Union[BridgeHost, Transport],
    image: str,
    bank: int = 1,
    pair: bool = True,
    # 回调由 SuotaSession.run 以 SuotaProgress 调用（原 object 是宽松占位；
    # Callable 参数型逆变，占位宽类型反而拒绝真实的窄类型回调）。
    progress_cb: Optional[Callable[[SuotaProgress], None]] = None,
    *,
    addr: str,
    addr_type: int = 0,
    chunk_size: Optional[int] = None,
    ack_timeout: float = 30.0,
    connect_timeout: float = 8.0,
    post_disconnect_settle_s: float = 1.0,
    settle_s: float = 2.0,
    dm_poll_timeout_s: float = 12.0,
    dm_read_timeout_s: float = 8.0,
    dm_poll_interval_s: float = 0.4,
    verify: bool = True,
    verify_wait_s: float = 8.0,
    verify_scan_window_s: float = 15.0,
    verify_timeout_s: float = 60.0,
    verify_settle_s: float = 2.0,
    verify_connect_timeout: float = 8.0,
) -> SuotaResult:
    """对 ``addr`` 的设备执行一次完整 OTA（流程见模块 docstring）。

    ``bridge_or_transport`` 传已连接串口的 :class:`~blebridge.bridge.BridgeHost`
    （生命周期归调用方）或裸 :class:`~blebridge.transport.Transport`（此处代为
    open/wrap/close）。``bank`` 默认 1（多数设备从 bank0 运行，写运行 bank 会被
    SAME_IMAGE 拒绝）；``pair`` 默认 True（安全默认：MEM_DEV 加密门 +
    重启后授权门都靠配对/事务解锁）。其余关键字参数暴露实战脚本的全部时序
    常量，测试取小值即可零等待。

    Raises:
        SameImageError: 设备已在运行该镜像（0x15），无需升级。
        DeviceNotBackError: 上传成功但设备重启后未恢复。
        OtaFlowError: 连接/发现/dm 就绪等编排步骤失败。
        SuotaError: SUOTA 会话协议失败（SuotaSession.run 透传）。
    """
    bridge, owns_bridge = _as_bridge(bridge_or_transport)
    try:
        _drop_stale_link(bridge, post_disconnect_settle_s)
        _connect_device(bridge, addr, addr_type, connect_timeout)
        sleep(settle_s)  # 设备唤醒 settle（连接后立刻 GATT 会答非所问）
        status, _nchars = bridge.disc_svc(SUOTA_SVC_UUID16, timeout=connect_timeout)
        if status != 0:
            raise OtaFlowError(
                f"服务发现 disc_svc(0xFEF5) 失败 status={status}"
                "（设备不在此链路上暴露 SUOTA 服务？）"
            )
        version_hex = _wait_dm_ready(
            bridge, dm_poll_timeout_s, dm_read_timeout_s, dm_poll_interval_s
        )
        _log(f"dm 就绪 (suota_version={version_hex})")
        bridge.drain()
        session = SuotaSession(bridge)
        try:
            result = session.run(
                image,
                progress_cb=progress_cb,
                chunk_size=chunk_size,
                ack_timeout=ack_timeout,
                bank=bank,
                pair=pair,
            )
        except SameImageError:
            _disconnect_quiet(bridge)  # 设备已给结论，正常收尾、无 verify
            raise
        _disconnect_quiet(bridge)
        if verify:
            ok, detail = verify_device_alive(
                bridge,
                addr=addr,
                addr_type=addr_type,
                wait_s=verify_wait_s,
                scan_window_s=verify_scan_window_s,
                timeout_s=verify_timeout_s,
                settle_s=verify_settle_s,
                connect_timeout=verify_connect_timeout,
            )
            if not ok:
                raise DeviceNotBackError(
                    f"镜像上传成功（{result.blocks} 块 / {result.bytes_written} 字节），"
                    f"但设备重启后未恢复: {detail}"
                )
            _log(f"设备存活验证通过 ({detail})")
        return result
    finally:
        if owns_bridge:
            bridge.close()


def verify_device_alive(
    bridge: BridgeHost,
    *,
    addr: str,
    addr_type: int = 0,
    wait_s: float = 8.0,
    scan_window_s: float = 15.0,
    timeout_s: float = 60.0,
    settle_s: float = 2.0,
    connect_timeout: float = 8.0,
) -> Tuple[bool, str]:
    """OTA 重启后的设备存活验证：等重广播 → 重连 → 读 suota_version。

    返回 ``(ok, detail)``，不抛异常（验证是尽力而为的观测，结论交调用方）。
    """
    sleep(wait_s)  # 实测 REBOOT 后 ~10s 才重广播，先留静默窗
    if not _scan_for_device(bridge, addr, scan_window_s, timeout_s):
        return False, f"{timeout_s:.0f}s 内未扫到 {addr} 的重广播"
    try:
        _connect_device(bridge, addr, addr_type, connect_timeout)
    except OtaFlowError as exc:
        return False, str(exc)
    sleep(settle_s)
    try:
        bridge.disc_svc(SUOTA_SVC_UUID16, timeout=connect_timeout)
        status, body = bridge.gatt_read(
            uuid16_prefix(SUOTA_UUIDS["suota_version"]), timeout=connect_timeout
        )
    except (BridgeTimeout, TransportClosed) as exc:
        return False, f"重连后 GATT 探测失败: {exc}"
    _disconnect_quiet(bridge)
    if status != 0:
        return False, f"重连后读 suota_version 失败 status={status}"
    return True, f"suota_version={body.hex() or '(空)'}"


# ------------------------------------------------------------------ 步骤


def _as_bridge(bridge_or_transport: Union[BridgeHost, Transport]) -> Tuple[BridgeHost, bool]:
    """BridgeHost 原样借用（不代管生命周期）；Transport 则 open + wrap。"""
    if isinstance(bridge_or_transport, BridgeHost):
        return bridge_or_transport, False
    transport = bridge_or_transport
    if not transport.is_open:
        transport.open()
    return BridgeHost(transport), True


def _drop_stale_link(bridge: BridgeHost, settle_s: float) -> None:
    """断旧链（容错）：上一会话可能遗留半开 BLE 链，失败不阻塞新流程。"""
    try:
        bridge.request(int(CMD.disconnect), timeout=2.0)
    except Exception:  # noqa: BLE001 — 断旧链尽力而为
        pass
    sleep(settle_s)
    bridge.drain()


def _connect_device(bridge: BridgeHost, addr: str, addr_type: int, timeout: float) -> None:
    payload = connect_payload(addr=addr, random=bool(addr_type))
    try:
        status, _body = bridge.request(int(CMD.connect), payload, timeout=timeout)
    except BridgeTimeout as exc:
        raise OtaFlowError(
            f"连接 {addr} 超时（{timeout:.0f}s）— 设备在广播吗？"
            "（阵发广播可重试，稍后再连）"
        ) from exc
    if status != 0:
        raise OtaFlowError(f"连接 {addr} 失败 status={status}")


def _wait_dm_ready(
    bridge: BridgeHost, poll_timeout_s: float, read_timeout_s: float, interval_s: float
) -> str:
    """轮询读 suota_version 判定设备侧 GATT 就绪（实战节奏）。

    每次读至多 ``read_timeout_s``（8s/次），整体 ``poll_timeout_s``（12s 总
    上限）——设备复位/唤醒后 dm 未就绪时读会失败，就绪即过。返回版本 hex。
    """
    version_u16 = uuid16_prefix(SUOTA_UUIDS["suota_version"])
    deadline = time.monotonic() + poll_timeout_s
    while True:
        try:
            status, body = bridge.gatt_read(version_u16, timeout=read_timeout_s)
        except BridgeTimeout:
            status, body = -1, b""
        if status == 0:
            return body.hex() or "(空)"
        if time.monotonic() >= deadline:
            raise OtaFlowError(
                "设备 GATT 未就绪（suota_version 轮询超时）— 设备可能仍在唤醒/重启；"
                "稍后重试"
            )
        sleep(interval_s)


def _scan_for_device(bridge: BridgeHost, addr: str, window_s: float, timeout_s: float) -> bool:
    """在 ``timeout_s`` 总预算内按 ``window_s`` 窗口反复扫描，直到目标地址出现。"""
    target = addr.upper()
    cursor = bridge.events.latest_seq()
    deadline = time.monotonic() + timeout_s
    window_ms = max(1, min(int(window_s * 1000), 0xFFFF))
    while time.monotonic() < deadline:
        bridge.request(int(CMD.scan_start), struct.pack("<H", window_ms))
        window_end = time.monotonic() + window_s
        seen = False
        while time.monotonic() < window_end:
            for event in bridge.drain(after=cursor):
                cursor = event.app_seq
                record = parse_scan_payload(event)
                if record is not None and record["addr"].upper() == target:
                    seen = True
            if seen:
                break
            sleep(min(0.05, max(0.0, window_end - time.monotonic())))
        try:
            bridge.request(int(CMD.scan_stop))
        except Exception:  # noqa: BLE001 — stop 尽力而为
            pass
        if seen:
            return True
    return False


def _disconnect_quiet(bridge: BridgeHost) -> None:
    try:
        bridge.request(int(CMD.disconnect), timeout=3.0)
    except Exception:  # noqa: BLE001 — 收尾尽力而为
        pass


def _log(message: str) -> None:
    print(f"[ota] {message}", flush=True)
