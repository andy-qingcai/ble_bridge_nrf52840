"""一键设备信息（UX 简化波痛点 3）。

:func:`collect_device_info` 一趟流程拿齐一台 BLE 设备的关键信息：

    connect（addr / --name / --random，addr_type 默认 public）→ settle 2s →
    disc_svc(0x1800)（GAP；可选 ``--suota`` 加发现 0xFEF5）→ 读 0x2A00 设备名
    + 0x2A19 电量（如存在）+（suota 时）全部 SUOTA info 特征 → 结构化 dict。

:func:`format_device_info` 渲染人话汇总；CLI（``blebridge device-info``）是薄壳。
单次读失败/服务不暴露不中断收集（"如存在"语义），逐条记入 ``errors``。
全部等待经模块级 :data:`sleep`（测试注入 no-op 保持零真实等待）。
"""

from __future__ import annotations

import time
from typing import Any, Optional, Tuple

from .bridge import CMD, BridgeHost, BridgeTimeout
from .daemon import connect_payload
from .suota import SUOTA_SVC_UUID16, SUOTA_UUIDS, uuid16_prefix

__all__ = [
    "UUID_GAP_SERVICE",
    "UUID_DEVICE_NAME",
    "UUID_BATTERY_LEVEL",
    "SUOTA_INFO_CHARS",
    "collect_device_info",
    "format_device_info",
]

UUID_GAP_SERVICE = 0x1800
UUID_DEVICE_NAME = 0x2A00
UUID_BATTERY_LEVEL = 0x2A19

SUOTA_INFO_CHARS = ("suota_version", "patch_data_char_size", "suota_mtu", "l2cap_psm")
"""0xFEF5 服务的全部 info 特征（AN-B-022；协商用的可读信息，非控制面）。"""

sleep = time.sleep
"""模块级等待注入点（默认 time.sleep；测试 monkeypatch 成 no-op）。"""


def collect_device_info(
    bridge: BridgeHost,
    *,
    addr: Optional[str] = None,
    name: Optional[str] = None,
    random: bool = False,
    suota: bool = False,
    connect_timeout: float = 8.0,
    settle_s: float = 2.0,
) -> dict[str, Any]:
    """连接目标设备并收集信息，返回结构化 dict（见模块 docstring）。

    ``addr``/``name`` 至少给一个（地址或广播名二选一）。
    ``connected=False`` 时其余字段无意义。
    无论收集结果如何，只要连上就断开链路（不留半开链）。
    """
    if addr is None and name is None:
        raise ValueError("device-info 需要 --addr 或 --name 之一")
    info: dict[str, Any] = {
        "addr": addr,
        "name_target": name,
        "connected": False,
        "addr_type": 1 if random else 0,
        "device_name": None,
        "battery": None,
        "suota": None,
        "errors": [],
    }
    payload = connect_payload(addr=addr, name=name, random=random)
    try:
        status, body = bridge.request(int(CMD.connect), payload, timeout=connect_timeout)
    except BridgeTimeout as exc:
        info["errors"].append(f"连接超时: {exc}")
        return info
    info["connect_status"] = status
    if status != 0:
        info["errors"].append(f"连接失败 status={status}")
        return info
    info["connected"] = True
    if len(body) >= 7:  # 成功 body = 1B addr_type + 6B 地址（显示序）
        info["addr_type"] = body[0]
        info["addr"] = ":".join(f"{b:02X}" for b in body[1:7])
    try:
        sleep(settle_s)
        _discover(bridge, UUID_GAP_SERVICE, "GAP(0x1800)", info)
        if suota:
            _discover(bridge, SUOTA_SVC_UUID16, "SUOTA(0xFEF5)", info)
        status, body = _read(bridge, UUID_DEVICE_NAME)
        if status == 0:
            info["device_name"] = body.decode("utf-8", "replace")
        else:
            info["errors"].append(f"读设备名 0x2A00 失败 status={status}")
        status, body = _read(bridge, UUID_BATTERY_LEVEL)
        if status == 0 and body:
            info["battery"] = body[0]
        elif status != 0:
            info["errors"].append(
                f"读电量 0x2A19 失败 status={status}（设备不暴露电量服务属正常）"
            )
        if suota:
            info["suota"] = {
                char_name: _suota_entry(*_read(bridge, uuid16_prefix(SUOTA_UUIDS[char_name])))
                for char_name in SUOTA_INFO_CHARS
            }
    finally:
        try:
            bridge.request(int(CMD.disconnect), timeout=3.0)
        except Exception:  # noqa: BLE001 — 收尾尽力而为
            pass
    return info


def format_device_info(info: dict[str, Any]) -> str:
    """把 :func:`collect_device_info` 的结果渲染成人话汇总（多行文本）。"""
    lines = []
    if info.get("connected"):
        kind = "random" if info.get("addr_type") == 1 else "public"
        lines.append(f"地址:   {info.get('addr')} ({kind})")
    else:
        target = info.get("addr") or info.get("name_target") or "?"
        lines.append(f"连接失败: {target}")
    name = info.get("device_name")
    lines.append(f"设备名: {name if name is not None else '不可用'}")
    battery = info.get("battery")
    lines.append(f"电量:   {battery}%" if battery is not None else "电量:   不可用")
    suota = info.get("suota")
    if suota is not None:
        lines.append("SUOTA:")
        for char_name, entry in suota.items():
            lines.append(f"  {char_name}: {_format_suota_value(char_name, entry)}")
    errors = info.get("errors") or []
    if errors:
        lines.append("备注:")
        for err in errors:
            lines.append(f"  - {err}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 内部


def _discover(bridge: BridgeHost, svc_uuid16: int, label: str, info: dict[str, Any]) -> None:
    try:
        status, _nchars = bridge.disc_svc(svc_uuid16)
    except BridgeTimeout as exc:
        info["errors"].append(f"发现 {label} 超时: {exc}")
        return
    if status != 0:
        info["errors"].append(f"发现 {label} 失败 status={status}")


def _read(bridge: BridgeHost, uuid16: int) -> Tuple[int, bytes]:
    """读一个特征；超时归一化为 status -1（单点失败不中断汇总收集）。"""
    try:
        return bridge.gatt_read(uuid16)
    except BridgeTimeout:
        return -1, b""


def _suota_entry(status: int, body: bytes) -> dict[str, Any]:
    return {"status": status, "hex": body.hex()}


def _format_suota_value(char_name: str, entry: dict[str, Any]) -> str:
    if entry["status"] != 0:
        return f"不可用 (status {entry['status']})"
    raw = bytes.fromhex(entry["hex"])
    if char_name in ("patch_data_char_size", "suota_mtu"):
        if len(raw) >= 2:
            return str(int.from_bytes(raw[:2], "little"))
        return entry["hex"] or "(空)"
    # suota_version / l2cap_psm：原样 hex（版本无固定宽度，PSM 本就按字节看）
    return entry["hex"] or "(空)"
