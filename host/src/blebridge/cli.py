"""Scripted CLI over the bridge — ``blebridge`` console script (FR-D3, Task 4).

Subcommands drive a :class:`~blebridge.bridge.BridgeHost` directly (no TCP
detour): the daemon is the long-lived shareable facade, the CLI is the
one-shot/scripting face.  Byte-level payload encoders are imported from
:mod:`blebridge.daemon` so there is exactly one definition of each wire format.

Run ``blebridge --help`` or ``blebridge <cmd> --help`` for details.  串口三参数
（``--port/--baud/--rtscts``）全部可省略：未显式给时经
:func:`blebridge.config.resolve_serial` 解析（显式参数 > ``~/.blebridge/config.json``
> USB 串口自动探测），``blebridge config`` 一次配置后所有命令零参数可用。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional

from . import config as config_mod
from . import device_info as device_info_mod
from . import ota_flow
from .bridge import CMD, EVT, BridgeHost, BridgeTimeout
from .config import resolve_serial
from .daemon import (
    DEFAULT_SCAN_DURATION,
    DaemonError,
    connect_payload,
    parse_scan_payload,
    uuid16_arg,
)
from .suota import SameImageError, SuotaError, SuotaProgress, SuotaSession
from .transport import SerialTransport, TransportClosed

__all__ = ["build_parser", "main", "open_bridge"]


def open_bridge(
    port: Optional[str] = None,
    baud: Optional[int] = None,
    rtscts: Optional[bool] = None,
) -> BridgeHost:
    """Open a synchronous BridgeHost; None 参数经 :func:`resolve_serial` 解析。

    解析优先级：显式参数 > ``~/.blebridge/config.json`` > USB 串口自动探测
    （恰一个 usbmodem*/usbserial* 候选时自动选用）。rtscts 未给时按端口名
    自动判定（usbmodem/CDC → 关，其它 → 开；bringup-state.md 进展3 注意①）。
    """
    if port is None or baud is None or rtscts is None:
        port, baud, rtscts = resolve_serial(port, baud, rtscts)
    transport = SerialTransport(port, baudrate=baud, rtscts=rtscts)
    transport.open()
    return BridgeHost(transport)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blebridge",
        description="One-shot CLI for the blebridge nRF52840 BLE SUOTA bridge.",
    )
    parser.add_argument(
        "--port",
        default=None,
        help="serial device node (default: ~/.blebridge/config.json 或 USB 串口自动探测)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="serial baud rate (default: 配置值或 921600 per FR-A5; the "
        "bring-up WCH-Link bridge link needs 115200)",
    )
    parser.add_argument(
        "--rtscts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="RTS/CTS hardware flow control (三态: 不给 = 自动 — usbmodem/CDC "
        "端口关、其它开；可用 blebridge config 保存固定值)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "config",
        help="保存/查看/清除串口默认配置（~/.blebridge/config.json）；配置一次，之后零参数",
    )
    p.add_argument("--port", default=None, help="保存默认串口设备节点")
    p.add_argument("--baud", type=int, default=None, help="保存默认波特率")
    p.add_argument(
        "--rtscts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="保存默认流控开关（--rtscts / --no-rtscts；不给 = 不修改该项）",
    )
    p.add_argument(
        "--show",
        action="store_true",
        help="只查看当前配置与解析结果（无保存参数时这也是默认行为）",
    )
    p.add_argument("--clear", action="store_true", help="清除配置文件后显示状态")
    p.set_defaults(func=None)  # main() 对 config 特判：本地操作，不开桥

    p = sub.add_parser("info", help="read bridge board info")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("scan", help="scan for BLE adverts (synchronous window)")
    p.add_argument("--duration", type=float, default=DEFAULT_SCAN_DURATION)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("connect", help="connect to a device by address or name")
    p.add_argument("addr", nargs="?", default=None, help="AA:BB:CC:DD:EE:FF")
    p.add_argument("--name", default=None, help="connect by advertised name")
    p.add_argument("--random", action="store_true", help="address is random type")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("disconnect", help="drop the BLE link")
    p.set_defaults(func=cmd_disconnect)

    p = sub.add_parser("write", help="GATT write to a characteristic")
    p.add_argument("--uuid", default=None, help="uuid16 prefix or 128-bit UUID")
    p.add_argument(
        "--handle",
        type=lambda x: int(x, 0),
        default=None,
        help="raw attribute handle (handle addressing; overrides --uuid)",
    )
    p.add_argument(
        "--handle-type",
        type=int,
        choices=(0, 1),
        default=0,
        help="handle kind with --handle: 0 value / 1 descriptor (default 0)",
    )
    p.add_argument("--data", required=True, help="value as hex")
    p.add_argument(
        "--without-response",
        action="store_true",
        help="write without response (default: with response)",
    )
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("read", help="GATT read a characteristic")
    p.add_argument("--uuid", default=None)
    p.add_argument(
        "--handle",
        type=lambda x: int(x, 0),
        default=None,
        help="raw attribute handle (handle addressing; overrides --uuid)",
    )
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("subscribe", help="enable notifications (CCCD write)")
    p.add_argument("--uuid", default=None)
    p.add_argument(
        "--handle",
        type=lambda x: int(x, 0),
        default=None,
        help="value handle (handle addressing; requires --cccd)",
    )
    p.add_argument(
        "--cccd",
        type=lambda x: int(x, 0),
        default=None,
        help="CCCD descriptor handle (with --handle)",
    )
    p.set_defaults(func=cmd_subscribe)

    p = sub.add_parser("unsubscribe", help="disable notifications")
    p.add_argument("--uuid", default=None)
    p.add_argument(
        "--handle",
        type=lambda x: int(x, 0),
        default=None,
        help="value handle of a handle-mode subscription",
    )
    p.set_defaults(func=cmd_unsubscribe)

    p = sub.add_parser(
        "disc-svc",
        help="run GATT discovery for a 16-bit service UUID (host-triggered)",
    )
    p.add_argument(
        "--uuid",
        default="0",
        help="16-bit service UUID, e.g. fef5 (default 0 = re-discover the "
        "last service)",
    )
    p.set_defaults(func=cmd_disc_svc)

    p = sub.add_parser(
        "conn-update",
        help="request an LE connection parameter update (CMD 0x44)",
    )
    p.add_argument("--interval-min", type=lambda x: int(x, 0), default=24,
                   help="min interval, 1.25 ms units (default 24)")
    p.add_argument("--interval-max", type=lambda x: int(x, 0), default=40,
                   help="max interval, 1.25 ms units (default 40)")
    p.add_argument("--latency", type=lambda x: int(x, 0), default=0,
                   help="slave latency (default 0)")
    p.add_argument("--timeout", type=lambda x: int(x, 0), default=400,
                   help="supervision timeout, 10 ms units (default 400)")
    p.set_defaults(func=cmd_conn_update)

    p = sub.add_parser("phy", help="request an LE PHY change (CMD 0x45)")
    p.add_argument("phy", type=int, choices=(1, 2), help="1 = 1M, 2 = 2M")
    p.set_defaults(func=cmd_phy)

    p = sub.add_parser(
        "adv-start", help="start peripheral advertising (FR-H, CMD 0x46)"
    )
    p.add_argument(
        "--interval",
        type=lambda x: int(x, 0),
        default=None,
        help="advertising interval in ms (default: firmware default)",
    )
    p.add_argument(
        "--name",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include the local name 'blebridge' (default: on)",
    )
    p.set_defaults(func=cmd_adv_start)

    p = sub.add_parser(
        "adv-stop", help="stop peripheral advertising (FR-H, CMD 0x47)"
    )
    p.set_defaults(func=cmd_adv_stop)

    p = sub.add_parser(
        "notify",
        help="push notification content of a local characteristic (FR-H, CMD 0x48)",
    )
    p.add_argument("--data", default="", help="value as hex (default empty)")
    p.add_argument(
        "--uuid",
        default=None,
        help="uuid16 of a local characteristic (default: preset notify "
        "characteristic ffe2)",
    )
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser(
        "set-char",
        help="set a local characteristic value (FR-H, CMD 0x49)",
    )
    p.add_argument("--data", default="", help="value as hex (default empty)")
    p.add_argument(
        "--uuid",
        default=None,
        help="uuid16 of a local characteristic (default: preset write "
        "characteristic ffe1)",
    )
    p.set_defaults(func=cmd_set_char)

    p = sub.add_parser(
        "gatt-define",
        help="register a host-defined GATT service (FR-H5, CMD 0x4A)",
    )
    p.add_argument("--uuid", required=True, help="16-bit service UUID, e.g. ffa0")
    p.add_argument(
        "--chars",
        required=True,
        help='JSON table, e.g. \'[{"uuid16": "ffa1", "read": true, '
        '"notify": true, "max_len": 64}]\' (properties: read, '
        "write_wo_resp, write, notify; max_len 1..244)",
    )
    p.set_defaults(func=cmd_gatt_define)

    p = sub.add_parser("drain", help="print ring events after a cursor")
    p.add_argument("--after", type=int, default=-1)
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("events", help="alias of drain (legacy name)")
    p.add_argument("--after", type=int, default=-1)
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("suota", help="run a full SUOTA update (connect first)")
    p.add_argument("image", help="path to the firmware image file")
    p.add_argument("--chunk-size", type=int, default=None)
    p.add_argument(
        "--pair",
        action="store_true",
        help="SMP-pair the link before the update (FR-B5; a MEM_DEV 0x0F "
        "encryption guard also triggers pairing automatically)",
    )
    p.set_defaults(func=cmd_suota)

    p = sub.add_parser(
        "ota",
        help="一键 OTA: 断旧链→连接→dm 就绪轮询→"
        "SUOTA 上传→断开→设备存活验证",
    )
    p.add_argument("image", help="path to the firmware image file")
    p.add_argument(
        "--bank",
        type=int,
        default=1,
        help="目标 bank（默认 1；设备通常从 bank0 运行，写运行 bank 会被 SAME_IMAGE 拒绝）",
    )
    p.add_argument(
        "--pair",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="上传前 SMP 配对（默认开，安全默认；--no-pair 关闭）",
    )
    p.add_argument(
        "--chunk",
        dest="chunk_size",
        type=int,
        default=None,
        help="PATCH_DATA 片大小（默认协商 min(244, mtu-3)）",
    )
    p.add_argument(
        "--ack-timeout",
        type=float,
        default=30.0,
        help="块/END ACK 等待上限秒（默认 30，设备实测值）",
    )
    p.add_argument("--addr", required=True,
                   help="设备地址（AA:BB:CC:DD:EE:FF，必填）")
    p.add_argument("--random", action="store_true", help="地址为 random 类型")
    p.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="成功后做设备存活验证（等重广播→重连→读 suota_version；--no-verify 跳过）",
    )
    p.set_defaults(func=cmd_ota)

    p = sub.add_parser(
        "device-info",
        help="一键设备信息: 连接→GAP/SUOTA 发现→设备名/电量/SUOTA info 汇总→断开",
    )
    p.add_argument("addr", nargs="?", default=None,
                   help="设备地址（AA:BB:CC:DD:EE:FF；或用 --name 按广播名连接）")
    p.add_argument("--name", default=None, help="按广播名连接（与 addr 二选一）")
    p.add_argument("--random", action="store_true", help="地址为 random 类型")
    p.add_argument(
        "--suota",
        action="store_true",
        help="同时发现 0xFEF5 并读取全部 SUOTA info 特征（suota_version 等）",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="post-connect wake-up settle seconds (default 2)",
    )
    p.set_defaults(func=cmd_device_info)

    p = sub.add_parser(
        "enter-dfu",
        help="CMD.enter_dfu (0x4B): reboot into the MCUboot serial recovery console",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="ACK wait seconds (firmware flushes + tears down BLE first)",
    )
    p.set_defaults(func=cmd_enter_dfu)

    p = sub.add_parser("monitor", help="stream bridge events until Ctrl+C")
    p.add_argument("--duration", type=float, default=None, help="auto-stop (tests)")
    p.set_defaults(func=cmd_monitor)

    return parser


# ------------------------------------------------------------ command impls


def cmd_config(args: argparse.Namespace) -> int:
    """``blebridge config``：本地操作（不开桥）— 保存/清除/查看串口默认配置。"""
    if args.clear:
        removed = config_mod.clear_config()
        state = "已清除" if removed else "无配置可清除"
        print(f"{state} ({config_mod.config_path()})")
    to_save = {}
    if args.port is not None:
        to_save["port"] = args.port
    if args.baud is not None:
        to_save["baud"] = args.baud
    if args.rtscts is not None:
        to_save["rtscts"] = args.rtscts
    if to_save:
        merged = config_mod.save_config(**to_save)
        print(
            f"已保存 {config_mod.config_path()}: "
            + json.dumps(merged, ensure_ascii=False, sort_keys=True)
        )
    current = config_mod.load_config()
    print(f"配置文件: {config_mod.config_path()}")
    print(
        "内容: "
        + (json.dumps(current, ensure_ascii=False, sort_keys=True) if current else "(空)")
    )
    try:
        port, baud, rtscts = resolve_serial()
        print(f"当前解析: port={port} baud={baud} rtscts={rtscts}")
    except ValueError as exc:  # 自动探测失败属正常展示，不影响 rc
        print(f"当前解析: 失败（{exc}）")
        return 1
    return 0


def cmd_info(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, body = bridge.request(int(CMD.info))
    print(f"status={status} hex={body.hex()}")
    text = body.decode("ascii", "replace")
    if text.isprintable():
        print(f"text={text}")
    return 0 if status == 0 else 1


def cmd_enter_dfu(bridge: BridgeHost, args: argparse.Namespace) -> int:
    """CMD.enter_dfu (0x4B, task-dfu-a): ACK -> GPREGRET boot mode -> reboot.

    Success = the bridge ACKed and reboots into the MCUboot serial recovery
    console: the USB port re-enumerates (default MCUboot VID:PID
    1915:8523) and the bridge protocol is offline until the next normal
    reset.  Recovery flashing from there: smpclient / nrfutil device xfer
    against the bootloader port (docs/reports/task-dfu-a-report.md).
    """
    status, body = bridge.request(int(CMD.enter_dfu), timeout=args.timeout)
    if status != 0:
        print(f"enter_dfu rejected: status={status} body={body.hex()}")
        return 1
    print(
        "ACK ok; device is rebooting into MCUboot serial recovery "
        "(bridge CDC goes down, bootloader CDC re-enumerates; "
        "recovery: smpclient / nrfutil device program on that port)."
    )
    return 0


def cmd_scan(bridge: BridgeHost, args: argparse.Namespace) -> int:
    duration_ms = max(0, min(int(args.duration * 1000), 0xFFFF))
    bridge.request(int(CMD.scan_start), duration_ms.to_bytes(2, "little"))
    devices = {}
    cursor = -1
    deadline = time.monotonic() + args.duration
    while True:
        for event in bridge.drain(after=cursor):
            cursor = event.app_seq
            record = parse_scan_payload(event)
            if record is not None:
                devices[record["addr"]] = record
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.02, remaining))
    try:
        bridge.request(int(CMD.scan_stop))
    except Exception:  # noqa: BLE001 — stop is best effort
        pass
    for record in sorted(devices.values(), key=lambda d: d["rssi"], reverse=True):
        print(
            f"{record['addr']}  type={record['addr_type']}  "
            f"rssi={record['rssi']:>4}  adv={record['adv_hex']}"
        )
    print(f"{len(devices)} device(s)")
    return 0


def cmd_connect(bridge: BridgeHost, args: argparse.Namespace) -> int:
    payload = connect_payload(addr=args.addr, name=args.name, random=args.random)
    status, body = bridge.request(int(CMD.connect), payload, timeout=args.timeout)
    print(f"connect status={status} hex={body.hex()}")
    for event in bridge.drain():
        if event.evt == int(EVT.CONN) or event.evt == int(EVT.DISC):
            print(f"evt 0x{event.evt:02X} sub=0x{event.subtype:02X} {event.payload.hex()}")
    return 0 if status == 0 else 1


def cmd_disconnect(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, _ = bridge.request(int(CMD.disconnect))
    print(f"disconnect status={status}")
    return 0 if status == 0 else 1


def cmd_write(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, _ = bridge.gatt_write(
        bytes.fromhex(args.data),
        uuid16=None if args.handle is not None else uuid16_arg(_require_arg(args, "uuid")),
        handle=args.handle,
        handle_type=args.handle_type,
        with_response=not args.without_response,
    )
    print(f"write status={status}")
    return 0 if status == 0 else 1


def cmd_read(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, body = bridge.gatt_read(
        uuid16=None if args.handle is not None else uuid16_arg(_require_arg(args, "uuid")),
        handle=args.handle,
    )
    print(f"status={status} hex={body.hex()}")
    return 0 if status == 0 else 1


def cmd_subscribe(bridge: BridgeHost, args: argparse.Namespace) -> int:
    if args.handle is not None or args.cccd is not None:
        if args.handle is None or args.cccd is None:
            raise ValueError("subscribe --handle needs --cccd (and vice versa)")
        value_handle, cccd_handle = args.handle, args.cccd
    else:
        value_handle = cccd_handle = None
    status, _ = bridge.subscribe(
        uuid16=None if value_handle is not None else uuid16_arg(_require_arg(args, "uuid")),
        value_handle=value_handle,
        cccd_handle=cccd_handle,
    )
    print(f"subscribe status={status}")
    return 0 if status == 0 else 1


def cmd_unsubscribe(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, _ = bridge.unsubscribe(
        uuid16=None if args.handle is not None else uuid16_arg(_require_arg(args, "uuid")),
        handle=args.handle,
    )
    print(f"unsubscribe status={status}")
    return 0 if status == 0 else 1


def cmd_disc_svc(bridge: BridgeHost, args: argparse.Namespace) -> int:
    svc_uuid16 = uuid16_arg(args.uuid) if args.uuid != "0" else 0
    status, nchars = bridge.disc_svc(svc_uuid16)
    print(f"disc_svc status={status} nchars={nchars}")
    return 0 if status == 0 else 1


def cmd_conn_update(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, _ = bridge.conn_update(
        interval_min=args.interval_min,
        interval_max=args.interval_max,
        latency=args.latency,
        supervision_timeout=args.timeout,
    )
    print(f"conn_update status={status}")
    return 0 if status == 0 else 1


def cmd_phy(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, _ = bridge.phy_update(args.phy)
    print(f"phy status={status}")
    return 0 if status == 0 else 1


def cmd_adv_start(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, _ = bridge.adv_start(interval_ms=args.interval, name=args.name)
    print(f"adv_start status={status}")
    return 0 if status == 0 else 1


def cmd_adv_stop(bridge: BridgeHost, args: argparse.Namespace) -> int:
    status, _ = bridge.adv_stop()
    print(f"adv_stop status={status}")
    return 0 if status == 0 else 1


def cmd_notify(bridge: BridgeHost, args: argparse.Namespace) -> int:
    try:
        data = bytes.fromhex(args.data)
    except ValueError as exc:
        raise ValueError(f"--data must be hex: {exc}") from exc
    uuid16 = uuid16_arg(args.uuid) if args.uuid is not None else None
    status, _ = bridge.notify(data, uuid16=uuid16)
    print(f"notify status={status}")
    return 0 if status == 0 else 1


def cmd_set_char(bridge: BridgeHost, args: argparse.Namespace) -> int:
    try:
        data = bytes.fromhex(args.data)
    except ValueError as exc:
        raise ValueError(f"--data must be hex: {exc}") from exc
    uuid16 = uuid16_arg(args.uuid) if args.uuid is not None else None
    status, _ = bridge.set_char(data, uuid16=uuid16)
    print(f"set_char status={status}")
    return 0 if status == 0 else 1


def cmd_gatt_define(bridge: BridgeHost, args: argparse.Namespace) -> int:
    """FR-H5.  The characteristic table is a JSON string (not per-flag CLI
    options): five dimensions per characteristic (uuid + 4 property flags +
    max_len) would explode into repetitive positional/keyword flag pairs
    with index coupling, while one JSON array maps 1:1 onto the daemon
    ``gatt_define`` request and stays copy-pasteable between both surfaces."""
    try:
        chars = json.loads(args.chars)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--chars must be JSON: {exc}") from exc
    if not isinstance(chars, list) or not chars:
        raise ValueError("--chars must be a nonempty JSON array")
    status, base = bridge.gatt_define(uuid16_arg(args.uuid), chars)
    print(f"gatt_define status={status} base_handle={base}")
    return 0 if status == 0 else 1


def _require_arg(args: argparse.Namespace, key: str) -> str:
    value: str = getattr(args, key)
    if value is None:
        raise ValueError(f"--{key.replace('_', '-')} is required without --handle")
    return value


def cmd_drain(bridge: BridgeHost, args: argparse.Namespace) -> int:
    for event in bridge.drain(after=args.after):
        print(
            f"{event.app_seq:6d}  evt=0x{event.evt:02X}  sub=0x{event.subtype:02X}  "
            f"{event.payload.hex()}"
        )
    return 0


def cmd_suota(bridge: BridgeHost, args: argparse.Namespace) -> int:
    session = SuotaSession(bridge)

    def progress_cb(progress: SuotaProgress) -> None:
        print(
            f"block {progress.block}/{progress.total_blocks} "
            f"({progress.bytes} bytes)",
            flush=True,
        )

    result = session.run(
        args.image, progress_cb=progress_cb, chunk_size=args.chunk_size, pair=args.pair
    )
    print(
        f"OK bytes={result.bytes_written} blocks={result.blocks} "
        f"chunks={result.chunks} elapsed={result.elapsed:.2f}s "
        f"throughput={result.throughput:.0f}B/s"
    )
    return 0


def cmd_ota(bridge: BridgeHost, args: argparse.Namespace) -> int:
    """``blebridge ota`` 薄壳：编排单源 ota_flow.run_ota。"""

    def progress_cb(progress: SuotaProgress) -> None:
        # 每 10 块一行 + 末块必打（进度实时可见又不刷屏）
        if progress.block % 10 == 0 or progress.block == progress.total_blocks:
            print(
                f"[ota] block {progress.block}/{progress.total_blocks} "
                f"bytes={progress.bytes} phase={progress.phase}",
                flush=True,
            )

    try:
        result = ota_flow.run_ota(
            bridge,
            args.image,
            bank=args.bank,
            pair=args.pair,
            progress_cb=progress_cb,
            addr=args.addr,
            addr_type=1 if args.random else 0,
            chunk_size=args.chunk_size,
            ack_timeout=args.ack_timeout,
            verify=args.verify,
        )
    except SameImageError:
        print(
            "[ota] 设备已是同版本，无需升级（SAME_IMAGE：目标镜像与运行镜像一致，"
            "或目标 bank 即运行 bank）",
            flush=True,
        )
        return 0
    except ota_flow.DeviceNotBackError as exc:
        print(f"[ota] 错误: {exc}", file=sys.stderr)
        return 1
    print(
        f"[ota] RESULT: blocks={result.blocks} bytes={result.bytes_written} "
        f"elapsed={result.elapsed:.1f}s throughput={result.throughput:.0f}B/s",
        flush=True,
    )
    return 0


def cmd_device_info(bridge: BridgeHost, args: argparse.Namespace) -> int:
    """``blebridge device-info`` 薄壳：实现单源 device_info.collect_device_info。"""
    info = device_info_mod.collect_device_info(
        bridge,
        addr=args.addr,
        name=args.name,
        random=args.random,
        suota=args.suota,
        settle_s=args.settle,
    )
    print(device_info_mod.format_device_info(info), flush=True)
    return 0 if info["connected"] else 1


def cmd_monitor(bridge: BridgeHost, args: argparse.Namespace) -> int:
    cursor = -1
    deadline = None if args.duration is None else time.monotonic() + args.duration
    try:
        while deadline is None or time.monotonic() < deadline:
            for event in bridge.drain(after=cursor):
                cursor = event.app_seq
                print(
                    f"{event.app_seq:6d}  evt=0x{event.evt:02X}  "
                    f"sub=0x{event.subtype:02X}  {event.payload.hex()}",
                    flush=True,
                )
            if deadline is None:
                time.sleep(0.05)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "config":
        # 本地文件操作：不占用串口、不开桥（串口甚至可以不在）。
        try:
            return cmd_config(args)
        except (ValueError, OSError) as exc:
            print(f"blebridge: error: {exc}", file=sys.stderr)
            return 2
    try:
        bridge = open_bridge(args.port, baud=args.baud, rtscts=args.rtscts)
    except (ValueError, TransportClosed) as exc:
        # 串口解析/打开失败（无候选/多候选/端口打不开）同样一行报错。
        print(f"blebridge: error: {exc}", file=sys.stderr)
        return 2
    try:
        # args.func 由 build_parser 的 set_defaults 注入（Namespace 属性在类型
        # 系统之外）；命令处理器的契约是返回退出码 int — 显式标注承接。
        rc: int = args.func(bridge, args)
        return rc
    except KeyboardInterrupt:
        return 130
    except (
        ValueError,
        DaemonError,
        SuotaError,
        BridgeTimeout,
        TransportClosed,
        ota_flow.OtaFlowError,
    ) as exc:
        # One-line error like the daemon's JSON error surface, no traceback:
        # bad user input (malformed hex/address/uuid), command-level failures,
        # SUOTA session errors, bridge timeouts and transport loss (fix
        # round 3: the failure families are normalised to rc=2).
        print(f"blebridge: error: {exc}", file=sys.stderr)
        return 2
    finally:
        bridge.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
