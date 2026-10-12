"""MCP server: AI tool facade over the BLE bridge (FR-D3, Task 5).

A thin, stateful wrapper driving :class:`~blebridge.bridge.BridgeHost`
in-process — the same layering the CLI uses, and byte-compatible with the
daemon (payload encoders are imported from :mod:`blebridge.daemon` so each wire
format keeps exactly one definition).  ``backend="serial"`` opens a real
:class:`~blebridge.transport.SerialTransport`; ``backend="sim"`` opens
``SuotaFakeTransport`` (host/tests/suota_fake.py) dressed with canned bridge
answers and one scripted advertisement — **the sim backend is the
no-hardware self-check and CI carrier**: the whole tool surface incl. a full
SUOTA update runs with zero hardware and (like the fake transport itself)
zero real waiting.

Concurrency (review ruling, single-threaded): MCP stdio requests arrive
strictly one at a time, so tools drive :class:`BridgeHost` in-process
directly (never via the daemon TCP port); a module lock additionally
guarantees one-handler-at-a-time even if the MCP library dispatches sync
tools on worker threads.  For several concurrent clients a per-process
bridge is the wrong shape — attach them to the blebridge daemon's TCP port
instead.

Return contract: every tool returns a JSON *string*
``{"ok": true, "data": ...}`` or ``{"ok": false, "error": "..."}``; all
exceptions are caught at the :func:`call_tool` boundary so no traceback ever
reaches the MCP client.

Architecture note: the tool handlers are plain functions registered in the
mcp-independent description table :data:`TOOLS` (list of ``(name, doc,
params)``) and dispatch table :data:`_HANDLERS` by one decorator — FastMCP
registration and the description table are therefore the same source.
:func:`build_server` imports ``mcp`` lazily, so tests (and the sim
self-check) run without the ``mcp`` package installed; the optional
dependency is pinned ``mcp>=2,<3`` (``MCPServer`` — FastMCP was renamed
there).
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import struct
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    get_type_hints,
)

from .bridge import CMD, EVT, BridgeHost
from .daemon import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_SCAN_DURATION,
    SUOTA_DONE,
    SUOTA_FAILED,
    SUOTA_PROGRESS,
    DaemonError,
    connect_payload,
    parse_scan_payload,
    uuid16_arg,
)
from .framing import EVENT_CMDS, StreamParser, encode_frame
from .serial_scan import list_candidates
from .suota import SuotaError, SuotaSession
from .transport import SerialTransport, Transport

__all__ = ["TOOLS", "STATE", "call_tool", "build_server", "main", "_make_sim_transport"]

_U16 = struct.Struct("<H")

# EVT.SUOTA 子类型常量（SUOTA_PROGRESS/DONE/FAILED）单一权威在 blebridge.daemon，
# 上方 import 引用——勿在此重复定义（fix round 3, task(12) D1）。

_MAX_EVENTS = 1024  # bounded like the bridge ring; drops oldest


# ---------------------------------------------------------------- registry

TOOLS: List[Tuple[str, str, Dict[str, str]]] = []
"""Pure-Python tool description table, ``(name, doc, params)`` entries.

Single source shared with FastMCP registration (built by :func:`_tool` from
the very handler functions that :func:`call_tool` dispatches); testable
without the ``mcp`` package.
"""

_HANDLERS: Dict[str, Callable[..., dict[str, Any]]] = {}

_TYPE_NAMES: Dict[type, str] = {str: "str", int: "int", float: "float", bool: "bool"}


def _tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Register a handler into :data:`TOOLS` + :data:`_HANDLERS`.

    The MCP tool name is the function name without the ``tool_`` prefix; the
    doc is the (one-line, Chinese) docstring; params are read off the real
    signature so the table cannot drift from the FastMCP schema.
    """
    name = fn.__name__[len("tool_"):]
    hints = get_type_hints(fn)  # resolve PEP 563 string annotations
    params: Dict[str, str] = {}
    for pname, param in inspect.signature(fn).parameters.items():
        hint = hints.get(pname)
        spec = _TYPE_NAMES.get(hint, "any") if hint is not None else "any"
        if param.default is not inspect.Parameter.empty:
            spec = f"{spec}={param.default!r}"
        params[pname] = spec
    doc = (inspect.getdoc(fn) or "").splitlines()[0]
    TOOLS.append((name, doc, params))
    _HANDLERS[name] = fn
    return fn


# ------------------------------------------------------------------- state

class _ServerState:
    """The single current connection plus the connection-spanning logs.

    The event log (bounded, global monotonic ``seq``) and the scan device
    table survive ``close``/``open`` cycles — daemon parity, so client
    cursors stay valid across a reconnect.  ``suota_ack_timeout`` /
    ``request_timeout`` / ``event_window`` are constructor-injectable like
    :class:`~blebridge.daemon.BridgeDaemon` (tests shrink them to keep the
    suite free of real waiting).
    """

    def __init__(
        self,
        request_timeout: float = 2.0,
        suota_ack_timeout: float = 30.0,
        event_window: float = 0.2,
    ) -> None:
        self.backend: str = ""
        self.port: str = ""
        self.bridge: Optional[BridgeHost] = None
        self.sim: Optional[Any] = None  # sim transport when backend="sim"
        self.request_timeout = request_timeout
        self.suota_ack_timeout = suota_ack_timeout
        self.event_window = event_window
        self.events: Deque[Dict[str, Any]] = deque()
        self.next_seq = 0
        self.dropped = 0
        self.bridge_cursor = -1  # bridge-local app_seq high-water mark
        self.scan_devices: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------ helpers

    def require_bridge(self) -> BridgeHost:
        """The open bridge, or a clean error for the ``ok=false`` wrapper."""
        bridge = self.bridge
        if bridge is None or not bridge.is_open:
            raise DaemonError("not_open: call open(backend=...) first")
        return bridge

    def reset_connection(self) -> None:
        self.backend = ""
        self.port = ""
        self.bridge = None
        self.sim = None
        self.bridge_cursor = -1
        self.scan_devices = {}

    def sync_events(self) -> None:
        """Mirror new bridge-ring events into the log (global seq)."""
        bridge = self.bridge
        if bridge is None:
            return
        for event in bridge.events.drain(after=self.bridge_cursor):
            self.bridge_cursor = event.app_seq
            if event.evt == int(EVT.SCAN):
                record = parse_scan_payload(event)
                if record is not None:
                    self.scan_devices[record["addr"]] = record
            self._append(
                {
                    "seq": self.next_seq,
                    "evt": int(event.evt),
                    "subtype": event.subtype,
                    "hex": event.payload.hex(),
                }
            )

    def _append(self, record: Dict[str, Any]) -> None:
        if len(self.events) >= _MAX_EVENTS:
            self.events.popleft()
            self.dropped += 1
        self.next_seq += 1
        self.events.append(record)

    def inject_local(self, evt: int, subtype: int, payload: bytes) -> None:
        """Publish a host-local event (SUOTA progress/done/failed)."""
        bridge = self.bridge
        if bridge is None:
            return
        bridge.inject_local_event(evt, subtype, payload)
        self.sync_events()

    def settle(self, window: float) -> None:
        """Pump events for ``window`` seconds — but never on the sim backend.

        The fake transport answers/pushes everything instantly inside the
        synchronous pump (its reads never block), so real waiting there is
        pure test time; real serial links still get the window.
        """
        if self.sim is not None:
            self.sync_events()
            return
        deadline = time.monotonic() + window
        while True:
            self.sync_events()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))

    def events_since(self, after: int) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["seq"] > after]


STATE = _ServerState()

_LOCK = threading.Lock()  # one handler at a time, however mcp dispatches


# ------------------------------------------------------------- sim backend

_SIM_ADDR = bytes.fromhex("021a7fbabe01")  # displayed order
_SIM_NAME = b"blebridge-sim"
# Wire payload of the scripted EVT.SCAN frame: subtype byte (stripped by the
# ring, daemon parity) + addr_type + 6B addr + rssi (i8) + advertisement TLVs.
_SIM_ADVERT = (
    bytes([0x00])  # EVT subtype
    + bytes([0x00])  # addr_type: public
    + _SIM_ADDR
    + bytes([0xCC])  # rssi -52 (i8)
    + b"\x02\x01\x06"  # flags TLV
    + bytes([len(_SIM_NAME) + 1, 0x09])  # complete-name TLV
    + _SIM_NAME
)


def _load_suota_fake() -> Tuple[type, Callable[..., bytes]]:
    """Import the tests-side SUOTA fake, repo-relative as a fallback.

    ``suota_fake`` lives in ``host/tests`` on purpose (it is a test-behaviour
    model); under pytest it is importable directly, standalone (``blebridge-mcp``
    run from a repo checkout) the explicit path loader kicks in.  Loaded
    lazily so importing this module never requires the file.
    """
    try:
        # suota_fake 是 host/tests 侧的仿真行为模型（有意不随包发布、不在
        # mypy_path 内）——运行时由 pytest 注入 sys.path，导入失败有 fallback。
        import suota_fake  # type: ignore[import-not-found]

        return suota_fake.SuotaFakeTransport, suota_fake.synthetic_image
    except ImportError:
        pass
    path = Path(__file__).resolve().parents[2] / "tests" / "suota_fake.py"
    if not path.is_file():
        raise DaemonError(
            f"sim backend needs host/tests/suota_fake.py (not found at {path})"
        )
    spec = importlib.util.spec_from_file_location("blebridge_suota_fake", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("blebridge_suota_fake", module)
    spec.loader.exec_module(module)
    return module.SuotaFakeTransport, module.synthetic_image


def _make_sim_transport(**kwargs: Any) -> Any:
    """The sim device: SuotaFakeTransport + canned bridge answers + an advert.

    The base SUOTA behaviour model answers the whole 0xFEF5 update flow; the
    ``responses`` additions make the plain bridge-level commands
    (ping/info/scan/connect/...) succeed too, and every ``scan_start``
    request makes the simulated device push one advertisement, so the full tool
    surface runs hardware-free.  ``kwargs`` pass through to
    ``SuotaFakeTransport`` (tests script failure modes, e.g.
    ``block_ack=None`` for the ACK-timeout path).
    """
    # A per-call subclass of SuotaFakeTransport: SuotaFakeTransport.write
    # dispatches _on_request virtually, so the override below is what turns
    # every scan_start into an advertisement push.
    fake_cls: Type[Any] = _load_suota_fake()[0]

    # 基类 suota_fake.SuotaFakeTransport 是 tests/ 侧动态加载模块（无桩，对类型
    # 系统是 Any）；动态子类化是本函数的既定行为 — 显式豁免 disallow_subclassing_any。
    class _SimDeviceTransport(fake_cls):  # type: ignore[misc]
        def __init__(self) -> None:
            self._wire_seq = 0  # contiguous event-frame SEQ ring (see below)
            super().__init__(**kwargs)
            self.responses.update(
                {
                    int(CMD.ping): b"\x00",
                    int(CMD.info): b"\x00blebridge-sim",
                    int(CMD.scan_start): b"\x00",
                    int(CMD.scan_stop): b"\x00",
                    int(CMD.connect): b"\x00",
                    int(CMD.disconnect): b"\x00",
                    int(CMD.mtu): b"\x00" + _U16.pack(247),
                    int(CMD.unsubscribe): b"\x00",
                    int(CMD.log_level): b"\x00",
                    int(CMD.reset): b"\x00",
                    int(CMD.suota_status): b"\x00",
                }
            )

        def inject_raw(self, data: bytes) -> None:
            # Re-encode every injected *event* frame with our own contiguous
            # wire SEQ: the base model hardcodes SEQ 0 per frame, which the
            # host's wire-gap detector would read as a 255-event loss per
            # event (wire_gaps noise + an EVT.DEBUG record per event).
            # Response frames pass through with their request SEQ intact —
            # the ring rewrites nothing else.
            out = bytearray()
            for frame in StreamParser().feed(bytes(data)):
                if frame.cmd in EVENT_CMDS:
                    out += encode_frame(frame.cmd, self._wire_seq, frame.payload)
                    self._wire_seq = (self._wire_seq + 1) & 0xFF
                else:
                    out += encode_frame(frame.cmd, frame.seq, frame.payload)
            super().inject_raw(bytes(out))

        def _on_request(self, frame: Any) -> None:
            super()._on_request(frame)
            if frame.cmd == int(CMD.scan_start):
                # The request itself is answered by the table above; queue
                # the scripted advert behind it so the next pump finds both.
                self.inject_raw(encode_frame(int(EVT.SCAN), 0, _SIM_ADVERT))

    return _SimDeviceTransport()


# ------------------------------------------------------------ tool handlers
# Every docstring is the tool description the MCP client sees (one line,
# Chinese).  Handlers return plain dicts; call_tool wraps them into JSON.


@_tool
def tool_open(backend: str = "sim", port: str = "") -> dict[str, Any]:
    """打开桥连接（backend="serial" 真实串口 / "sim" 无硬件模拟；单连接，如需多客户端并发请外接 blebridge daemon 的 TCP 端口）。"""
    with _LOCK:
        _close_locked(report=False)
        if backend == "serial":
            if not port:
                candidates = list_candidates()
                if not candidates:
                    raise DaemonError(
                        "no_serial_port: pass port explicitly or use backend='sim'"
                    )
                port = candidates[0]
            transport: Transport = SerialTransport(port)
            transport.open()  # TransportClosed -> ok=false at the boundary
            STATE.bridge = BridgeHost(transport)
        elif backend == "sim":
            transport = _make_sim_transport()
            transport.open()
            STATE.bridge = BridgeHost(transport)
            STATE.sim = transport
        else:
            raise DaemonError("bad_arg:backend (serial|sim)")
        STATE.backend = backend
        STATE.port = port
    return {"backend": STATE.backend, "port": STATE.port}


def _close_locked(report: bool) -> dict[str, Any]:
    if STATE.bridge is not None:
        try:
            STATE.bridge.close()
        except Exception:  # noqa: BLE001 — close must never mask the result
            pass
    STATE.reset_connection()
    return {"closed": True} if report else {}


@_tool
def tool_close() -> dict[str, Any]:
    """关闭当前桥连接（释放串口；幂等，未打开时也返回成功）。"""
    with _LOCK:
        return _close_locked(report=True)


@_tool
def tool_ping() -> dict[str, Any]:
    """PING 桥固件，确认串口链路与桥任务存活。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, body = bridge.request(int(CMD.ping), timeout=STATE.request_timeout)
    return {"pong": status == 0, "status": status, "hex": body.hex()}


@_tool
def tool_info() -> dict[str, Any]:
    """读取桥固件信息（版本等，响应体为不透明字节）。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, body = bridge.request(int(CMD.info), timeout=STATE.request_timeout)
    return {"status": status, "hex": body.hex()}


@_tool
def tool_list_ports() -> dict[str, Any]:
    """列出本机候选串口设备节点（无需打开连接）。"""
    return {"ports": list_candidates()}


@_tool
def tool_scan(duration_s: float = DEFAULT_SCAN_DURATION) -> dict[str, Any]:
    """扫描 BLE 广播一个时间窗并返回去重后的设备列表（按地址排序）。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        duration = max(0.0, float(duration_s))
        duration_ms = max(0, min(int(duration * 1000), 0xFFFF))
        status, _ = bridge.request(
            int(CMD.scan_start), _U16.pack(duration_ms), timeout=STATE.request_timeout
        )
        STATE.settle(duration)  # collect adverts for the window
        try:
            bridge.request(int(CMD.scan_stop), timeout=STATE.request_timeout)
        except Exception:  # noqa: BLE001 — scan_stop is best effort
            pass
        STATE.sync_events()
    return {
        "status": status,
        "devices": sorted(STATE.scan_devices.values(), key=lambda d: d["addr"]),
    }


@_tool
def tool_connect(addr_or_name: str, timeout_s: float = DEFAULT_CONNECT_TIMEOUT) -> dict[str, Any]:
    """连接设备（12 位十六进制按地址 AA:BB:CC:DD:EE:FF 处理，否则按广播名）。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        digits = str(addr_or_name).replace(":", "").lower()
        if len(digits) == 12:
            try:
                bytes.fromhex(digits)
            except ValueError as exc:
                raise DaemonError(f"bad_arg:addr_or_name ({exc})") from exc
            payload = connect_payload(addr=digits, random=False)
        else:
            payload = connect_payload(name=str(addr_or_name))
        status, body = bridge.request(
            int(CMD.connect), payload, timeout=max(0.1, float(timeout_s))
        )
        STATE.sync_events()
    return {"status": status, "hex": body.hex()}


@_tool
def tool_disconnect() -> dict[str, Any]:
    """断开当前 BLE 链路。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, body = bridge.request(
            int(CMD.disconnect), timeout=STATE.request_timeout
        )
    return {"status": status, "hex": body.hex()}


@_tool
def tool_mtu() -> dict[str, Any]:
    """发起 ATT MTU 协商，返回状态与窗口内收到的 EVT.MTU 事件。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        before = STATE.next_seq - 1
        status, body = bridge.request(int(CMD.mtu), timeout=STATE.request_timeout)
        STATE.settle(STATE.event_window)
        mtu_events = [
            e for e in STATE.events_since(before) if e["evt"] == int(EVT.MTU)
        ]
    return {"status": status, "hex": body.hex(), "events": mtu_events}


@_tool
def tool_read(uuid: str) -> dict[str, Any]:
    """GATT 读取一个特征值（uuid16 前缀或 128 位 UUID），返回十六进制。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, body = bridge.request(
            int(CMD.gatt_read),
            _U16.pack(uuid16_arg(uuid)),
            timeout=STATE.request_timeout,
        )
    return {"status": status, "hex": body.hex()}


@_tool
def tool_write(uuid: str, data_hex: str = "", with_response: bool = True) -> dict[str, Any]:
    """GATT 写一个特征值（data_hex 十六进制；默认带响应写）。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        write_type = 0x00 if with_response else 0x01
        payload = (
            bytes([write_type]) + _U16.pack(uuid16_arg(uuid)) + _hex_arg(data_hex, "data_hex")
        )
        status, _ = bridge.request(
            int(CMD.gatt_write), payload, timeout=STATE.request_timeout
        )
    return {"status": status}


@_tool
def tool_subscribe(uuid: str) -> dict[str, Any]:
    """订阅通知（固件使能该特征的 0x2902 CCCD）。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, _ = bridge.request(
            int(CMD.subscribe),
            _U16.pack(uuid16_arg(uuid)),
            timeout=STATE.request_timeout,
        )
    return {"status": status}


@_tool
def tool_unsubscribe(uuid: str) -> dict[str, Any]:
    """取消订阅通知。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, _ = bridge.request(
            int(CMD.unsubscribe),
            _U16.pack(uuid16_arg(uuid)),
            timeout=STATE.request_timeout,
        )
    return {"status": status}


def _drain_locked(after: int) -> dict[str, Any]:
    bridge = STATE.require_bridge()
    bridge.drain()  # one synchronous pump; no extra thread ever
    STATE.sync_events()
    return {
        "events": STATE.events_since(after),
        "last_seq": STATE.next_seq - 1,
        "dropped": STATE.dropped,
        "wire_gaps": bridge.events.wire_gaps,
    }


@_tool
def tool_events(after_seq: int = -1) -> dict[str, Any]:
    """增量拉取事件（seq > after_seq；游标由调用方保存，重连后仍有效）。"""
    with _LOCK:
        return _drain_locked(int(after_seq))


@_tool
def tool_drain(after_seq: int = -1) -> dict[str, Any]:
    """增量拉取事件并附带 last_seq/dropped/wire_gaps 统计（events 的别名，daemon 对等）。"""
    with _LOCK:
        return _drain_locked(int(after_seq))


@_tool
def tool_suota_start(image_path: str, chunk_size: int = 0) -> dict[str, Any]:
    """同步执行一次完整 SUOTA 升级（真硬件大镜像会同步阻塞数十秒且持锁期间进度事件不可见，请放宽 MCP 客户端超时，完成后经 events 轮询 EVT.SUOTA；chunk_size=0 自动协商）。

    长阻塞警示：本工具持模块锁直至整次传输结束（成功或失败），期间同一
    连接上的其他工具调用不会被执行；进度/完成/失败以 EVT.SUOTA 本地事件
    写入事件日志，只能在 run 返回之后用 events/drain 取到（与 daemon 的
    同步模型一致）。
    """
    with _LOCK:
        bridge = STATE.require_bridge()
        session = SuotaSession(bridge)

        def progress_cb(progress: Any) -> None:
            STATE.inject_local(
                int(EVT.SUOTA),
                SUOTA_PROGRESS,
                struct.pack("<HHI", progress.block, progress.total_blocks, progress.bytes),
            )

        try:
            result = session.run(
                image_path,
                progress_cb=progress_cb,
                chunk_size=int(chunk_size) if chunk_size else None,
                ack_timeout=STATE.suota_ack_timeout,
            )
        except SuotaError as exc:
            STATE.inject_local(
                int(EVT.SUOTA),
                SUOTA_FAILED,
                f"{exc.status_code}:{exc.phase}".encode("ascii"),
            )
            raise
        STATE.inject_local(
            int(EVT.SUOTA),
            SUOTA_DONE,
            struct.pack("<If", result.bytes_written, result.elapsed),
        )
        return {
            "bytes_written": result.bytes_written,
            "blocks": result.blocks,
            "chunks": result.chunks,
            "elapsed": result.elapsed,
            "throughput": result.throughput,
        }


@_tool
def tool_suota_abort() -> dict[str, Any]:
    """强制写入 MEM_DEV ABORT，清除设备端卡住的 SUOTA 会话。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        SuotaSession(bridge).abort()
    return {"aborted": True}


@_tool
def tool_suota_status() -> dict[str, Any]:
    """查询桥固件缓存的 SUOTA 状态（响应体为不透明字节）。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, body = bridge.request(
            int(CMD.suota_status), timeout=STATE.request_timeout
        )
    return {"status": status, "hex": body.hex()}


@_tool
def tool_log_level(level: int) -> dict[str, Any]:
    """设置桥固件日志级别（0..255；注意 0x0E 是协议保留值，日志命令位于 0x0F）。"""
    try:
        value = int(level)
    except (TypeError, ValueError) as exc:
        raise DaemonError(f"bad_arg:level ({exc})") from exc
    if not 0 <= value <= 0xFF:
        raise DaemonError("bad_arg:level (int 0..255)")
    with _LOCK:
        bridge = STATE.require_bridge()
        status, _ = bridge.request(
            int(CMD.log_level), bytes([value]), timeout=STATE.request_timeout
        )
    return {"status": status}


@_tool
def tool_reset() -> dict[str, Any]:
    """软复位桥固件。"""
    with _LOCK:
        bridge = STATE.require_bridge()
        status, body = bridge.request(int(CMD.reset), timeout=STATE.request_timeout)
    return {"status": status, "hex": body.hex()}


@_tool
def tool_raw(raw_cmd: str, payload_hex: str = "") -> dict[str, Any]:
    """逃生口：发送任意桥命令帧（危险：0x50 credits 会破坏事件流控、0x0E 为协议保留禁用勿发、SUOTA 进行中勿用；raw_cmd 十进制或 "0x" 十六进制，0..255）。"""
    try:
        cmd_value = int(str(raw_cmd), 0)
    except ValueError as exc:
        raise DaemonError(f"bad_arg:raw_cmd ({exc})") from exc
    if not 0 <= cmd_value <= 0xFF:
        raise DaemonError("bad_arg:raw_cmd (0..255)")
    with _LOCK:
        bridge = STATE.require_bridge()
        status, body = bridge.request(
            cmd_value, _hex_arg(payload_hex, "payload_hex"), timeout=STATE.request_timeout
        )
    return {"status": status, "hex": body.hex()}


def _hex_arg(value: str, key: str) -> bytes:
    """Hex string -> bytes (tool-boundary convention; empty string allowed)."""
    if value is None:
        return b""
    try:
        return bytes.fromhex(str(value))
    except ValueError as exc:
        raise DaemonError(f"bad_arg:{key} (hex)") from exc


# ------------------------------------------------------------------ facade


def call_tool(name: str, args: Optional[Dict[str, Any]] = None) -> str:
    """mcp-independent entry point: run one tool, return a JSON string.

    Never raises: every failure becomes ``{"ok": false, "error": "..."}``
    (no traceback reaches the MCP client).  Tests drive the whole surface
    through this function without the ``mcp`` package installed.
    """
    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            raise DaemonError(
                f"unknown_cmd: {name!r} (available: {', '.join(sorted(_HANDLERS))})"
            )
        data = handler(**dict(args or {}))
        return json.dumps({"ok": True, "data": data}, default=str)
    except Exception as exc:  # noqa: BLE001 — one bad tool call kills nothing
        message = str(exc) or type(exc).__name__
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {message}"})


def build_server() -> Any:
    """Create the FastMCP server with every tool registered from TOOLS.

    Each registered function is a thin wrapper over :func:`call_tool` (so the
    MCP client always receives the ``{"ok": ...}`` JSON string and never a
    raised traceback) carrying the real handler's signature via
    ``functools.wraps`` — FastMCP reads the schema off that signature, which
    keeps registration and :data:`TOOLS` one-source.

    ``mcp`` is imported lazily (optional dependency, pinned ``mcp>=2,<3``:
    ``MCPServer`` — FastMCP was renamed in 2.0).
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("blebridge")

    def register(name: str, doc: str, handler: Callable[..., dict[str, Any]]) -> None:
        # Factory scope pins ``name``/``handler`` per tool (no late binding).
        @functools.wraps(handler)  # signature/annotations -> MCP schema
        def entry(**kwargs: Any) -> str:
            return call_tool(name, kwargs)

        # 结构化输出显式关断：本 facade 的工具契约是返回 JSON *字符串*（unstructured）。
        # 裸 dict 返回注解时代靠 _create_output_model Case 4 的巧合落在这条路径；
        # handler 注解参数化为 dict[str, Any] 后会被误判为结构化输出并对 JSON
        # 字符串做 dict 校验。structured_output=False 把既有行为显式化，
        # 输入 schema 仍来自 handler 签名（inspect.signature 沿 __wrapped__）。
        server.tool(name=name, description=doc, structured_output=False)(entry)

    for name, doc, _params in TOOLS:
        register(name, doc, _HANDLERS[name])
    return server


def main() -> None:
    """stdio entry point (the ``blebridge-mcp`` console script)."""
    build_server().run()  # stdio transport is the FastMCP default


if __name__ == "__main__":  # pragma: no cover
    main()
