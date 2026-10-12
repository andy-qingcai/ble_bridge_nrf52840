"""持久化用户配置与串口参数解析（UX 简化波痛点 1）。

``~/.blebridge/config.json`` 保存上次使用的串口参数（port/baud/rtscts）；
:func:`resolve_serial` 按 **显式参数 > 配置文件 > 自动探测** 的优先级解析三个
串口参数，让既有子命令在 ``blebridge config`` 配置一次之后零参数可用。

自动探测规则（仅当 port 既无显式值、配置中也没有时）：

- ``serial.tools.list_ports`` 枚举本机串口，仅保留设备名含 ``usbmodem`` /
  ``usbserial`` 的候选（桥的 CDC 形态 / USB-UART 转接板）；
- 恰一个候选 → 自动选用；多个 → 报错并列出候选；零个 → 报错提示插设备。

rtscts 自动规则（既无显式值也无配置值时）：端口名含 ``usbmodem``（CDC 类，
桥链路没有可用流控线；WCH-Link CDC 上 ``rtscts=True`` 会静默扣发写）→
False；否则 True（UART 转接板保留硬件流控）。

配置文件路径可用环境变量 ``BLEBRIDGE_CONFIG`` 覆盖（测试隔离用；旧名
``BRIDGE_CONFIG`` 仍被识别，仅作向后兼容回退）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

__all__ = [
    "DEFAULT_BAUD",
    "SerialResolveError",
    "config_path",
    "load_config",
    "save_config",
    "clear_config",
    "list_usb_candidates",
    "resolve_serial",
]

DEFAULT_BAUD = 921600
"""FR-A5 默认波特率（配置文件与显式参数都未给时；CDC 形态下该值无实际意义）。"""

_CONFIG_ENV = "BLEBRIDGE_CONFIG"
_LEGACY_CONFIG_ENV = "BRIDGE_CONFIG"  # 旧包名环境变量，向后兼容回退

_KNOWN_KEYS = {"port": str, "baud": int, "rtscts": bool}


class SerialResolveError(ValueError):
    """串口参数无法解析（自动探测失败/配置值非法）；message 为人话指引。"""


def config_path() -> Path:
    """配置文件路径（``BLEBRIDGE_CONFIG`` 覆盖 → ``~/.blebridge/config.json``）。"""
    override = os.environ.get(_CONFIG_ENV) or os.environ.get(_LEGACY_CONFIG_ENV)
    if override:
        return Path(override)
    return Path.home() / ".blebridge" / "config.json"


def load_config() -> dict[str, Any]:
    """读配置；不存在返回空 dict，损坏时 WARN + 返回空（绝不崩）。"""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[blebridge] 警告: 配置文件损坏，已忽略 ({path}): {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"[blebridge] 警告: 配置文件不是 JSON 对象，已忽略 ({path})", file=sys.stderr)
        return {}
    return data


def save_config(**kv: Any) -> dict[str, Any]:
    """合并写入配置（只更新给出的键），返回合并后的完整配置。

    只接受已知键（port: str / baud: int / rtscts: bool），其余 ValueError。
    """
    for key, value in kv.items():
        expect = _KNOWN_KEYS.get(key)
        if expect is None:
            raise ValueError(f"未知配置键 {key!r}（支持: port/baud/rtscts）")
        if expect is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"配置键 baud 应为整数，得到 {value!r}")
        elif not isinstance(value, expect):
            raise ValueError(f"配置键 {key} 应为 {expect.__name__}，得到 {value!r}")
    merged = load_config()
    merged.update(kv)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return merged


def clear_config() -> bool:
    """删除配置文件；返回是否真的删了东西。"""
    try:
        config_path().unlink()
        return True
    except FileNotFoundError:
        return False


def list_usb_candidates() -> list[str]:
    """枚举 usbmodem*/usbserial* 形态的串口设备名（排序、去重）。"""
    try:
        from serial.tools.list_ports import comports
    except ImportError:  # pragma: no cover — pyserial 是硬依赖，防御兜底
        return []
    found = []
    for info in comports():
        device = getattr(info, "device", "") or ""
        low = device.lower()
        if "usbmodem" in low or "usbserial" in low:
            found.append(device)
    return sorted(set(found))


def resolve_serial(
    port: Optional[str] = None,
    baud: Optional[int] = None,
    rtscts: Optional[bool] = None,
) -> Tuple[str, int, bool]:
    """按 显式参数 > 配置文件 > 自动探测 解析串口三参数。

    baud 缺省回落 :data:`DEFAULT_BAUD`；rtscts 缺省按端口名自动判定
    （usbmodem → False，其它 → True）。自动探测失败抛
    :class:`SerialResolveError`（ValueError 子类，CLI 归一为一行报错）。
    """
    cfg = load_config()
    if port is None:
        port = cfg.get("port")
    if baud is None:
        baud = cfg.get("baud")
    if rtscts is None:
        rtscts = cfg.get("rtscts")
    if baud is None:
        baud = DEFAULT_BAUD
    if isinstance(baud, bool) or not isinstance(baud, int) or baud <= 0:
        raise SerialResolveError(f"配置中的 baud 非法: {baud!r}（应为正整数）")
    if rtscts is not None and not isinstance(rtscts, bool):
        raise SerialResolveError(f"配置中的 rtscts 非法: {rtscts!r}（应为布尔）")
    if port is None:
        candidates = list_usb_candidates()
        if not candidates:
            raise SerialResolveError(
                "未找到 USB 串口设备（usbmodem*/usbserial*）: 请插上桥/转接板后重试，"
                "或用 --port（或 blebridge config --port）显式指定"
            )
        if len(candidates) > 1:
            raise SerialResolveError(
                "找到多个候选串口: " + ", ".join(candidates)
                + " — 请用 --port 指定其一，或 blebridge config --port <首选> 保存默认"
            )
        port = candidates[0]
    if rtscts is None:
        # 自动规则: CDC（usbmodem）无真实流控线 → 关; 其它（USB-UART 转接板）保留开。
        rtscts = "usbmodem" not in str(port).lower()
    return str(port), int(baud), bool(rtscts)
