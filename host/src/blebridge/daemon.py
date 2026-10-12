"""Persistent TCP-JSON daemon over the BLE bridge (FR-D2 / FR-D4, Task 4).

Semantic port of the legacy ``suota_daemon.py`` (one-shot TCP JSON, newline
delimited) onto the new layered stack: instead of touching raw opcodes, every
command maps onto :class:`~blebridge.bridge.BridgeHost` requests plus the
SUOTA session of :mod:`blebridge.suota`.

Wire protocol (unchanged from the legacy daemon)::

    request  (one per line):  {"cmd": "...", ...args}
    response (one per line):  {"ok": true|false, "data": ...|"error": "...",
                               "_ms": <elapsed milliseconds>}

Unknown commands answer ``{"ok": false, "error": "unknown_cmd"}``; malformed
JSON lines answer ``{"ok": false, "error": "bad_json: ..."}``.

Command set (legacy-15 + suota family + generic BLE): ping / info /
scan_start(duration?) / scan_stop / connect{addr|name, random?, timeout?} /
disconnect / mtu{} / read{uuid | handle} / write{uuid | handle,
handle_type?, data, with_response?} / subscribe{uuid | handle+cccd_handle} /
unsubscribe{uuid | handle} / enable_notify{uuid} (alias of subscribe) /
disc_svc{uuid?} (0x0000/absent = re-discover the last service) /
disc_all / conn_update{interval_min?, interval_max?, latency?,
supervision_timeout?} / phy{phy: 1|2} / adv_start{interval_ms?, name?} /
adv_stop / notify{data, uuid16?} / set_char{data, uuid16?} /
gatt_define{svc_uuid16, chars:[{uuid16, read?, write_wo_resp?, write?,
notify?, max_len?}...]} (FR-H peripheral mode: advertising, local test
services, host-defined GATT services) / drain{after?} / events{after?}
(alias of drain) / sec{} / suota_start{image_path, chunk_size?, pair?} /
suota_abort / suota_status /
log_level{level} / reset / raw{raw_cmd,
payload_hex?}.

Bridge-level payload formats (the firmware side, Task 9/10, must agree
byte-for-byte; GATT addressing follows ``blebridge.suota``; the encoders are
the module-level functions of :mod:`blebridge.bridge`):

::

    CMD.scan_start   u16 LE duration_ms (0 = firmware default)
    CMD.connect      1B mode (0 = public addr, 1 = random addr, 2 = name)
                     mode 0/1: 6B address, displayed order ("AA:BB:.." ->
                     AA BB CC DD EE FF); mode 2: name bytes (UTF-8)
    CMD.gatt_write   1B write type (0 with response, 1 without) + u16 LE
                     uuid16 prefix + value bytes          (see suota.py)
                     handle addressing: 1B write type + 0x0000 + 1B
                     handle_type (0 value / 1 descriptor) + u16 LE handle
                     + value bytes
    CMD.gatt_read    u16 LE uuid16 prefix; handle addressing: 0x0000 +
                     u16 LE handle
    CMD.subscribe    u16 LE uuid16 prefix (enable_notify is an alias);
                     handle addressing: 0x0000 + u16 LE value handle +
                     u16 LE cccd handle (notification CCCD value 0x0001)
    CMD.unsubscribe  u16 LE uuid16 prefix; handle addressing: 0x0000 +
                     u16 LE value handle
    CMD.disc_svc     u16 LE service uuid16 (0 = re-discover the last
                     service; the legacy empty payload means the same);
                     generic firmware runs bt_gatt_dm NOW, the response
                     body carries the discovered characteristic count
    CMD.conn_update  4 x u16 LE: interval_min, interval_max (1.25 ms),
                     latency, supervision timeout (10 ms)
    CMD.phy          1B phy preference (1 = 1M, 2 = 2M)
    CMD.adv_start    u16 LE interval_ms (0xFFFF = firmware default) + 1B
                     name switch (FR-H; idempotent when advertising)
    CMD.adv_stop     empty (FR-H; idempotent)
    CMD.notify       0x00 + data (preset notify characteristic) or 0x01 +
                     u16 LE uuid16 + data (any locally served
                     characteristic); status 0x74 when nobody subscribed
    CMD.set_char     same addressing as notify; default = preset write
                     characteristic (FR-H)
    CMD.gatt_define  u16 LE svc_uuid16 + 1B n + n x {u16 LE char uuid16 +
                     1B props (bit0 read / bit1 write-wo-resp /
                     bit2 write / bit3 notify) + u16 LE max_len}
                     (FR-H5; success body = base handle u16 LE)
    CMD.log_level    1B level (command value 0x0F; 0x0E is RESERVED — its
                     response byte 0x8E would collide with EVT.DEBUG)
    ``raw``          verbatim CMD value + hex payload (escape hatch)

    EVT.CONN/EVT.DISC payloads carry a 1B role after the subtype byte
    (0 = central-role link, 1 = peripheral-role link, FR-H); EVT.GATTX
    subtype 0x01 = peripheral-role write ([uuid16 LE][handle LE][data]).

    EVT.SCAN (0x20) payload (subtype byte 0x00 already stripped by the ring):
                     1B addr_type (0 public / 1 random) + 6B addr (displayed
                     order) + 1B rssi (i8) + adv bytes

Daemon-side event channel: every bridge event is mirrored into a daemon-level
bounded log whose ``seq`` is a *global* monotonic counter that survives
reconnects (review decision on Task 4: the bridge-local ``app_seq`` restarts
with the new :class:`BridgeHost`, but daemon ``seq`` keeps counting, so
client cursors stay valid across a serial re-enumeration).
``drain``/``events{after}`` filter that log.

Reconnect semantics (Task 2 review ruling: a :class:`BridgeHost` cannot be
revived): on ``TransportClosed``/``OSError`` the daemon destroys the old
transport/bridge pair, sleeps :attr:`reconnect_delay` seconds (2 s in
production, injectable to 0 in tests), builds a fresh pair and retries the
*original* command once.  The first successful response after a reconnect
carries ``data["reconnected"] = true`` exactly once.  A second failure
answers ``ok=false``.

Concurrency model (single-threaded, review ruling): there is one thread —
the TCP accept/serve loop.  Connections are served strictly one at a time
and lines within a connection strictly in order, so all bridge access stays
on one path; ``suota_start`` simply runs to completion inside its request
(early-arriving TCP commands from other connections wait in the socket
backlog — i.e. they are queued by the transport itself, which keeps the
semantics trivially correct).  No selectors, no worker threads.
"""

from __future__ import annotations

import json
import socket
import struct
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, TypeVar, cast

from .bridge import (
    CMD,
    EVT,
    GATTX_NOTIFY,
    GATTX_PERIPH_WRITE,
    BridgeHost,
    BridgeTimeout,
    Event,
    adv_start_payload,
    conn_update_payload,
    gatt_define_payload,
    gatt_read_payload,
    gatt_write_payload,
    notify_payload,
    phy_payload,
    set_char_payload,
    subscribe_payload,
    unsubscribe_payload,
)
from .config import resolve_serial
from .suota import SuotaError, SuotaProgress, SuotaSession, uuid16_prefix
from .transport import SerialTransport, Transport, TransportClosed

__all__ = [
    "DEFAULT_TCP_HOST",
    "DEFAULT_TCP_PORT",
    "DaemonError",
    "BridgeDaemon",
    "connect_payload",
    "parse_scan_payload",
    "parse_gattx_payload",
    "uuid16_arg",
]

DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 7878

_U16 = struct.Struct("<H")

# op return type of BridgeDaemon._call (request tuple, or the bridge itself
# for the drain op) — flows through to status/body instead of degrading to
# ``object`` (mypy strict: generic _call keeps every unpacked pair typed).
_T = TypeVar("_T")

# Daemon-local EVT.SUOTA subtypes (host-defined; firmware SUOTA events keep
# their own meaning when Task 9/10 assign them).
SUOTA_PROGRESS = 0x00  # payload: block u16, total_blocks u16, bytes u32 (LE)
SUOTA_DONE = 0x01  # payload: bytes_written u32, elapsed f32 (LE)
SUOTA_FAILED = 0x02  # payload: ascii "<status_code>:<phase>"

DEFAULT_SCAN_DURATION = 8.0
DEFAULT_CONNECT_TIMEOUT = 8.0


class DaemonError(Exception):
    """A command-level failure reported as ``{"ok": false, "error": ...}``."""


def connect_payload(
    addr: Optional[str] = None, name: Optional[str] = None, random: bool = False
) -> bytes:
    """Encode the CMD.connect request payload (see module docstring).

    ``addr`` is in **display order** (left-to-right as a phone shows it),
    v1.4.2; the firmware reverses it into Zephyr's little-endian ``val``.
    Use the address exactly as :func:`parse_scan_payload` reported it.
    """
    if addr:
        digits = addr.replace(":", "").lower()
        if len(digits) != 12:
            raise ValueError(f"bad address {addr!r}: expected AA:BB:CC:DD:EE:FF")
        return bytes([0x01 if random else 0x00]) + bytes.fromhex(digits)
    if name:
        return bytes([0x02]) + name.encode("utf-8")
    raise ValueError("connect needs addr or name")


def parse_scan_payload(event: Event) -> Optional[dict[str, Any]]:
    """Decode one ``EVT.SCAN`` ring event into an advertisement record.

    Layout v1.4.2: ``[1B addr_type][6B addr][1B rssi i8][1B adv_type]
    [adv TLV...]`` -- ``addr`` is in **display order** (left-to-right as a
    phone shows it; the firmware reverses Zephyr's little-endian ``val``
    before packing), and ``adv_type`` is the raw HCI LE adv report event
    type (0 = connectable undirected, 2 = scannable, 3 = non-connectable,
    4 = scan response); the host decides which adverts are connectable.
    The address string here feeds ``connect_payload`` unchanged.

    Returns ``None`` for non-SCAN events or payloads shorter than the
    fixed-size prefix (truncated advertisement).
    """
    if event.evt != int(EVT.SCAN) or len(event.payload) < 9:
        return None
    p = event.payload
    addr_type = p[0]
    (rssi,) = struct.unpack_from("<b", p, 7)
    return {
        "addr_type": addr_type,
        "addr": ":".join(f"{b:02X}" for b in p[1:7]),
        "rssi": rssi,
        "adv_type": p[8],
        "adv_hex": p[9:].hex(),
    }


def parse_gattx_payload(event: Event) -> Optional[dict[str, Any]]:
    """Decode one ``EVT.GATTX`` ring event (FR-H aware).

    Subtype 0x00 = notification from a central-role peer:
    ``{"role": "central", "uuid16": n, "hex": ...}``.  Subtype 0x01 =
    peripheral-role write to a locally served characteristic:
    ``{"role": "peripheral", "uuid16": n, "handle": n, "hex": ...}``.
    Returns ``None`` for other events or malformed payloads.
    """
    if event.evt != int(EVT.GATTX):
        return None
    if event.subtype == GATTX_NOTIFY:
        if len(event.payload) < 2:
            return None
        return {
            "role": "central",
            "uuid16": int.from_bytes(event.payload[0:2], "little"),
            "hex": event.payload[2:].hex(),
        }
    if event.subtype == GATTX_PERIPH_WRITE:
        if len(event.payload) < 4:
            return None
        return {
            "role": "peripheral",
            "uuid16": int.from_bytes(event.payload[0:2], "little"),
            "handle": int.from_bytes(event.payload[2:4], "little"),
            "hex": event.payload[4:].hex(),
        }
    return None


class BridgeDaemon:
    """Single-threaded TCP-JSON facade over one exclusive serial bridge.

    Args:
        port: serial device node; ``None`` resolves lazily via
            :func:`blebridge.config.resolve_serial` on first use (显式参数 >
            ``~/.blebridge/config.json`` > USB 串口自动探测).
        tcp_host / tcp_port: TCP listen address (loopback by default).
        baud / rtscts: serial line parameters passed to the default
            :class:`~blebridge.transport.SerialTransport` factory.  ``None``
            (default) = resolve via :func:`blebridge.config.resolve_serial`
            (配置值，再缺省 921600；rtscts 按端口名自动判定)。  Ignored when
            ``transport_factory`` is given.
        transport_factory: ``(port) -> Transport`` override for tests;
            defaults to :class:`~blebridge.transport.SerialTransport`.
        reconnect_delay: seconds to sleep between tearing down a dead
            transport and rebuilding (legacy daemon: 2 s; tests: 0).
        suota_ack_timeout: block/END ACK wait passed to
            :meth:`SuotaSession.run` (device field value 30 s; tests shrink
            it to keep the suite free of real waiting).
    """

    MAX_DAEMON_EVENTS = 1024  # bounded like the bridge ring; drops oldest

    def __init__(
        self,
        port: Optional[str] = None,
        tcp_host: str = DEFAULT_TCP_HOST,
        tcp_port: int = DEFAULT_TCP_PORT,
        baud: Optional[int] = None,
        rtscts: Optional[bool] = None,
        transport_factory: Optional[Callable[[str], Transport]] = None,
        reconnect_delay: float = 2.0,
        suota_ack_timeout: float = 30.0,
    ) -> None:
        self.port = port
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.baud = baud
        self.rtscts = rtscts
        self._transport_factory = transport_factory or self._default_transport
        self.reconnect_delay = reconnect_delay
        self.suota_ack_timeout = suota_ack_timeout
        self._bridge: Optional[BridgeHost] = None
        self._events: Deque[dict[str, Any]] = deque()  # daemon-level log, global seq
        self._next_seq = 0
        self._dropped = 0
        self._bridge_cursor = -1  # bridge-local app_seq high-water mark
        self._reconnect_marker = False  # arm the one-shot reconnected flag
        self._scan_devices: Dict[str, dict[str, Any]] = {}  # accumulated scan results
        self._stop = False
        self._server: Optional[socket.socket] = None

    # ------------------------------------------------------------ lifecycle

    def _default_transport(self, port: str) -> Transport:
        # baud/rtscts 已由 _ensure_bridge 在取得 transport 前解析兜底（resolve_serial
        # 恒返回非 None 三元组），Optional 只对外部早调用可见 — cast 仅类型层面。
        return SerialTransport(
            port,
            baudrate=cast(int, self.baud),
            rtscts=cast(bool, self.rtscts),
        )

    def _ensure_bridge(self) -> BridgeHost:
        """Return the live bridge, (re)creating it on first use."""
        if self._bridge is not None and self._bridge.is_open:
            return self._bridge
        if self._bridge is not None:  # half-dead: bury it before rebuilding
            try:
                self._bridge.close()
            except Exception:  # noqa: BLE001 — close must never mask the cause
                pass
            self._bridge = None
        if self.port is None or self.baud is None or self.rtscts is None:
            # 首次使用时解析（UX 痛点 1）：显式参数 > ~/.blebridge/config.json >
            # USB 串口自动探测；解析结果就地缓存，重连复用同一结论。
            self.port, self.baud, self.rtscts = resolve_serial(
                self.port, self.baud, self.rtscts
            )
        transport = self._transport_factory(self.port)
        transport.open()
        self._bridge = BridgeHost(transport)
        self._bridge_cursor = -1  # new ring: app_seq restarts at 0
        return self._bridge

    def _reconnect(self) -> None:
        """Destroy the dead pair; the next _ensure_bridge builds a fresh one."""
        self._sync_events()  # flush whatever the old ring still holds
        if self._bridge is not None:
            try:
                self._bridge.close()
            except Exception:  # noqa: BLE001
                pass
            self._bridge = None
        if self.reconnect_delay > 0:
            time.sleep(self.reconnect_delay)
        self._reconnect_marker = True

    def _sync_events(self) -> None:
        """Mirror new bridge-ring events into the daemon log (global seq)."""
        bridge = self._bridge
        if bridge is None:
            return
        for event in bridge.events.drain(after=self._bridge_cursor):
            self._bridge_cursor = event.app_seq
            if event.evt == int(EVT.SCAN):
                record = parse_scan_payload(event)
                if record is not None:
                    self._scan_devices[record["addr"]] = record
            self._append_event(
                {
                    "seq": self._next_seq,
                    "evt": int(event.evt),
                    "subtype": event.subtype,
                    "hex": event.payload.hex(),
                }
            )

    def _append_event(self, record: dict[str, Any]) -> None:
        if len(self._events) >= self.MAX_DAEMON_EVENTS:
            self._events.popleft()
            self._dropped += 1
        self._next_seq += 1
        self._events.append(record)

    def _inject_local(self, bridge: BridgeHost, evt: int, subtype: int, payload: bytes) -> None:
        bridge.inject_local_event(evt, subtype, payload)
        self._sync_events()

    # ------------------------------------------------------ command handling

    def handle_command(self, obj: dict[str, Any]) -> dict[str, Any]:
        """Execute one decoded request object; never raises.

        Independent of the TCP layer (thin shell, see ``serve_forever``) so
        tests drive commands directly.
        """
        started = time.monotonic()
        cmd = str(obj.get("cmd", ""))
        try:
            resp: Dict[str, Any] = {"ok": True, "data": self._dispatch(cmd, obj)}
        except DaemonError as exc:
            resp = {"ok": False, "error": str(exc)}
        except BridgeTimeout as exc:
            resp = {"ok": False, "error": f"timeout: {exc}"}
        except (TransportClosed, OSError) as exc:
            resp = {"ok": False, "error": f"transport_down: {exc}"}
        except SuotaError as exc:
            resp = {"ok": False, "error": f"suota_failed: {exc}"}
        except Exception as exc:  # noqa: BLE001 — one bad request kills nothing
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        data = resp.get("data")
        if resp.get("ok") and self._reconnect_marker and isinstance(data, dict):
            # The first successful bridge-backed command after a reconnect.
            data["reconnected"] = True
            self._reconnect_marker = False
        resp["_ms"] = (time.monotonic() - started) * 1000.0
        return resp

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, cmd: str, req: dict[str, Any]) -> dict[str, Any]:
        if cmd == "ping":

            def op(bridge: BridgeHost) -> Tuple[int, bytes]:
                return bridge.request(int(CMD.ping))

            status, body = self._call(op)
            return {"pong": status == 0, "status": status, "hex": body.hex()}

        if cmd == "info":
            return self._simple(CMD.info)

        if cmd == "scan_start":
            return self._scan_start(req)

        if cmd == "scan_stop":
            status, _ = self._call(lambda b: b.request(int(CMD.scan_stop)))
            return {"status": status, "devices": self._scan_list()}

        if cmd == "connect":
            return self._connect(req)

        if cmd == "disconnect":
            return self._simple(CMD.disconnect)

        if cmd == "mtu":
            return self._mtu(req)

        if cmd == "read":
            payload = _gatt_addr_arg(req)
            status, body = self._call(
                lambda b: b.request(int(CMD.gatt_read), payload)
            )
            return {"status": status, "hex": body.hex()}

        if cmd == "write":
            return self._write(req)

        if cmd in ("subscribe", "enable_notify"):
            # enable_notify: legacy name kept for old clients; the new
            # firmware model enables notifications via the CCCD subscribe.
            payload = _subscribe_addr_arg(req)
            status, _ = self._call(lambda b: b.request(int(CMD.subscribe), payload))
            return {"status": status}

        if cmd == "unsubscribe":
            payload = _unsubscribe_addr_arg(req)
            status, _ = self._call(lambda b: b.request(int(CMD.unsubscribe), payload))
            return {"status": status}

        if cmd == "disc_svc":
            payload = _U16.pack(uuid16_arg(req["uuid"])) if "uuid" in req else b""
            status, body = self._call(
                lambda b: b.request(int(CMD.disc_svc), payload)
            )
            return {"status": status, "hex": body.hex()}

        if cmd == "disc_all":
            # Empty uuid = all services (same CMD value, legacy semantics).
            status, body = self._call(lambda b: b.request(int(CMD.disc_svc)))
            return {"status": status, "hex": body.hex()}

        if cmd == "conn_update":
            return self._conn_update(req)

        if cmd == "phy":
            phy = _require(req, "phy")
            try:
                payload = phy_payload(int(phy))
            except (TypeError, ValueError) as exc:
                raise DaemonError(f"bad_arg:phy (1 or 2): {exc}") from exc
            status, body = self._call(lambda b: b.request(int(CMD.phy), payload))
            return {"status": status, "hex": body.hex()}

        if cmd == "adv_start":
            return self._adv_start(req)

        if cmd == "adv_stop":
            status, _ = self._call(lambda b: b.request(int(CMD.adv_stop)))
            return {"status": status}

        if cmd == "notify":
            return self._notify(req)

        if cmd == "set_char":
            return self._set_char(req)

        if cmd == "gatt_define":
            return self._gatt_define(req)

        if cmd in ("drain", "events"):
            return self._drain(req)

        if cmd == "sec":
            # Legacy command 0x56 reported link encryption.  Under the nRF
            # stack that is an SMP/LL operation; placeholder until firmware
            # defines a bridge command for it.
            raise DaemonError("smp_not_implemented")

        if cmd == "suota_start":
            return self._suota_start(req)

        if cmd == "suota_abort":
            return self._suota_abort()

        if cmd == "suota_status":
            return self._simple(CMD.suota_status)

        if cmd == "log_level":
            level = _require(req, "level")
            if not isinstance(level, int) or not 0 <= level <= 0xFF:
                raise DaemonError("bad_arg:level (int 0..255)")
            status, _ = self._call(
                lambda b: b.request(int(CMD.log_level), bytes([level]))
            )
            return {"status": status}

        if cmd == "reset":
            return self._simple(CMD.reset)

        if cmd == "raw":
            return self._raw(req)

        raise DaemonError("unknown_cmd")

    # ---------------------------------------------------------- command impls

    def _simple(self, bridge_cmd: CMD) -> dict[str, Any]:
        """Empty-payload request; return status and raw body."""

        def op(bridge: BridgeHost) -> Tuple[int, bytes]:
            return bridge.request(int(bridge_cmd))

        status, body = self._call(op)
        return {"status": status, "hex": body.hex()}

    def _call(self, op: Callable[[BridgeHost], _T]) -> _T:
        """Run ``op(bridge)``; on transport death rebuild and retry once.

        Reconnect = destroy old transport/bridge, build new instances
        (BridgeHost is not revivable — Task 2 ruling).  A second failure
        propagates to :meth:`handle_command` and surfaces as ``ok=false``.
        Generic in the op return type: request ops yield ``(status, body)``
        tuples, the drain op yields the bridge — callers unpack typed values
        instead of ``object`` (mypy strict has-type/misc cascade otherwise).
        """
        try:
            return op(self._ensure_bridge())
        except (TransportClosed, OSError):
            self._reconnect()
            return op(self._ensure_bridge())

    def _scan_start(self, req: dict[str, Any]) -> dict[str, Any]:
        duration = float(req.get("duration", DEFAULT_SCAN_DURATION))
        duration_ms = max(0, min(int(duration * 1000), 0xFFFF))
        payload = _U16.pack(duration_ms)

        def op(bridge: BridgeHost) -> Tuple[int, bytes]:
            return bridge.request(int(CMD.scan_start), payload)

        status, _ = self._call(op)
        # Synchronous simple model: wait out the window while pumping events,
        # then stop the scan and report the accumulated device list.
        deadline = time.monotonic() + duration
        while True:
            self._sync_events()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))
        bridge = self._ensure_bridge()
        try:
            bridge.request(int(CMD.scan_stop))
        except (TransportClosed, OSError, BridgeTimeout):
            pass  # scan_stop is best effort; the window already elapsed
        self._sync_events()
        return {"status": status, "devices": self._scan_list()}

    def _scan_list(self) -> List[dict[str, Any]]:
        """Accumulated scan devices (dedup by addr across scans), RSSI-sorted."""
        return sorted(self._scan_devices.values(), key=lambda d: d["addr"])

    def _connect(self, req: dict[str, Any]) -> dict[str, Any]:
        addr = req.get("addr")
        name = req.get("name")
        try:
            payload = connect_payload(addr=addr, name=name, random=bool(req.get("random")))
        except ValueError as exc:
            raise DaemonError(f"bad_arg: {exc}") from exc
        timeout = float(req.get("timeout", DEFAULT_CONNECT_TIMEOUT))

        def op(bridge: BridgeHost) -> Tuple[int, bytes]:
            return bridge.request(int(CMD.connect), payload, timeout=timeout)

        status, body = self._call(op)
        self._sync_events()
        return {"status": status, "hex": body.hex()}

    def _mtu(self, req: dict[str, Any]) -> dict[str, Any]:
        """Request an MTU exchange; report status plus EVT.MTU events seen."""

        def op(bridge: BridgeHost) -> Tuple[int, bytes]:
            return bridge.request(int(CMD.mtu))

        # Snapshot the *last handed out* seq (next_seq - 1): events recorded
        # from now on start at next_seq, and _daemon_events_since filters
        # with a strict >  (fix round 2: using next_seq itself dropped the
        # first — typically the only — EVT.MTU event).
        before = self._next_seq - 1
        status, body = self._call(op)
        self._collect_window(float(req.get("timeout", 0.2)))
        mtu_events = [e for e in self._daemon_events_since(before) if e["evt"] == int(EVT.MTU)]
        return {"status": status, "hex": body.hex(), "events": mtu_events}

    def _collect_window(self, window: float) -> None:
        """Pump events for ``window`` seconds (bounded, poll-style)."""
        deadline = time.monotonic() + window
        while True:
            self._sync_events()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))

    def _write(self, req: dict[str, Any]) -> dict[str, Any]:
        data = _hex_arg(req)
        with_response = bool(req.get("with_response", True))
        try:
            if "handle" in req:
                payload = gatt_write_payload(
                    data,
                    handle=_int_arg(req, "handle", 0, 0xFFFF),
                    handle_type=int(req.get("handle_type", 0)),
                    with_response=with_response,
                )
            else:
                uuid = _require(req, "uuid")
                payload = gatt_write_payload(
                    data, uuid16_arg(uuid), with_response=with_response
                )
        except ValueError as exc:
            raise DaemonError(f"bad_arg: {exc}") from exc
        status, _ = self._call(lambda b: b.request(int(CMD.gatt_write), payload))
        return {"status": status}

    def _conn_update(self, req: dict[str, Any]) -> dict[str, Any]:
        """LE connection parameter update (CMD.conn_update 0x44)."""
        try:
            payload = conn_update_payload(
                interval_min=(
                    _int_arg(req, "interval_min", 0, 0xFFFF)
                    if "interval_min" in req
                    else 24
                ),
                interval_max=(
                    _int_arg(req, "interval_max", 0, 0xFFFF)
                    if "interval_max" in req
                    else 40
                ),
                latency=_int_arg(req, "latency", 0, 0xFFFF) if "latency" in req else 0,
                supervision_timeout=(
                    _int_arg(req, "supervision_timeout", 0, 0xFFFF)
                    if "supervision_timeout" in req
                    else 400
                ),
            )
        except ValueError as exc:
            raise DaemonError(f"bad_arg: {exc}") from exc
        status, body = self._call(
            lambda b: b.request(int(CMD.conn_update), payload)
        )
        return {"status": status, "hex": body.hex()}

    def _adv_start(self, req: dict[str, Any]) -> dict[str, Any]:
        """FR-H: start peripheral advertising (CMD.adv_start 0x46)."""
        interval_ms = None
        if "interval_ms" in req:
            interval_ms = _int_arg(req, "interval_ms", 0, 0xFFFF)
        name = bool(req.get("name", True))
        payload = adv_start_payload(interval_ms, name)

        def op(bridge: BridgeHost) -> Tuple[int, bytes]:
            return bridge.request(int(CMD.adv_start), payload)

        status, _ = self._call(op)
        return {"status": status}

    def _notify(self, req: dict[str, Any]) -> dict[str, Any]:
        """FR-H: push notify content (CMD.notify 0x48)."""
        data = _hex_arg(req)
        uuid16 = uuid16_arg(req["uuid16"]) if "uuid16" in req else None
        payload = notify_payload(data, uuid16)
        status, _ = self._call(lambda b: b.request(int(CMD.notify), payload))
        return {"status": status}

    def _set_char(self, req: dict[str, Any]) -> dict[str, Any]:
        """FR-H: set a locally served characteristic value (0x49)."""
        data = _hex_arg(req)
        uuid16 = uuid16_arg(req["uuid16"]) if "uuid16" in req else None
        payload = set_char_payload(data, uuid16)
        status, _ = self._call(lambda b: b.request(int(CMD.set_char), payload))
        return {"status": status}

    def _gatt_define(self, req: dict[str, Any]) -> dict[str, Any]:
        """FR-H5: register/replace a host-defined GATT service (0x4A).

        ``chars`` entries: {"uuid16": int|"ffe1", "read": bool,
        "write_wo_resp": bool, "write": bool, "notify": bool,
        "max_len": int} (uuid16 required, properties default false,
        max_len default 244).  Success returns the service base handle.
        """
        svc_uuid16 = uuid16_arg(_require(req, "svc_uuid16"))
        if svc_uuid16 == 0:
            raise DaemonError("bad_arg:svc_uuid16 (nonzero)")
        chars_req = _require(req, "chars")
        if not isinstance(chars_req, list) or not chars_req:
            raise DaemonError("bad_arg:chars (nonempty list)")
        chars = []
        try:
            for entry in chars_req:
                if not isinstance(entry, dict):
                    raise ValueError(f"characteristic entry must be an object: {entry!r}")
                raw_uuid = entry["uuid16"]
                uuid_val = uuid16_arg(raw_uuid) if isinstance(raw_uuid, str) else int(raw_uuid)
                chars.append(
                    {
                        "uuid16": uuid_val,
                        "read": bool(entry.get("read", False)),
                        "write_wo_resp": bool(entry.get("write_wo_resp", False)),
                        "write": bool(entry.get("write", False)),
                        "notify": bool(entry.get("notify", False)),
                        "max_len": int(entry.get("max_len", 244)),
                    }
                )
            payload = gatt_define_payload(svc_uuid16, chars)
        except KeyError as exc:
            raise DaemonError(f"bad_arg:chars missing {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise DaemonError(f"bad_arg:chars: {exc}") from exc

        def op(bridge: BridgeHost) -> Tuple[int, bytes]:
            return bridge.request(int(CMD.gatt_define), payload)

        status, body = self._call(op)
        base = int.from_bytes(body[:2], "little") if status == 0 and len(body) >= 2 else 0
        return {"status": status, "base_handle": base}

    def _raw(self, req: dict[str, Any]) -> dict[str, Any]:
        # Legacy spelling nested a second "cmd" key; a flat JSON object cannot
        # carry both, so the escape hatch reads "raw_cmd" (int or "0x08").
        raw_cmd = _require(req, "raw_cmd")
        try:
            cmd_value = int(str(raw_cmd), 0)
        except ValueError as exc:
            raise DaemonError("bad_arg:raw_cmd") from exc
        if not 0 <= cmd_value <= 0xFF:
            raise DaemonError("bad_arg:raw_cmd (0..255)")
        payload = _hex_arg(req, key="payload_hex")
        status, body = self._call(lambda b: b.request(cmd_value, payload))
        return {"status": status, "hex": body.hex()}

    def _drain(self, req: dict[str, Any]) -> dict[str, Any]:
        after = int(req.get("after", -1))

        def op(bridge: BridgeHost) -> BridgeHost:
            bridge.drain()  # one synchronous pump; no extra thread ever
            return bridge

        bridge = self._call(op)
        self._sync_events()
        return {
            "events": self._daemon_events_since(after),
            "last_seq": self._next_seq - 1,
            "dropped": self._dropped,
            # Per-bridge counter; the durable record of past gaps lives in
            # the daemon log as the injected EVT.DEBUG/0xFF events.
            "wire_gaps": bridge.events.wire_gaps,
        }

    def _daemon_events_since(self, after: int) -> List[dict[str, Any]]:
        return [e for e in self._events if e["seq"] > after]

    # --------------------------------------------------------------- suota

    def _suota_start(self, req: dict[str, Any]) -> dict[str, Any]:
        """Synchronous full update (one request, one response).

        The bridge handle is acquired with the usual reconnect retry, but a
        failure *during* the upload is terminal: retrying a half-flashed
        image automatically would be worse than reporting the failure.
        """
        image_path = _require(req, "image_path")
        chunk_size = req.get("chunk_size")
        if chunk_size is not None:
            chunk_size = int(chunk_size)
        pair = bool(req.get("pair", False))
        bridge = self._acquire_bridge()
        session = SuotaSession(bridge)

        def progress_cb(progress: SuotaProgress) -> None:
            self._inject_local(
                bridge,
                int(EVT.SUOTA),
                SUOTA_PROGRESS,
                struct.pack("<HHI", progress.block, progress.total_blocks, progress.bytes),
            )

        try:
            result = session.run(
                image_path,
                progress_cb=progress_cb,
                chunk_size=chunk_size,
                ack_timeout=self.suota_ack_timeout,
                pair=pair,
            )
        except SuotaError as exc:
            self._inject_local(
                bridge,
                int(EVT.SUOTA),
                SUOTA_FAILED,
                f"{exc.status_code}:{exc.phase}".encode("ascii"),
            )
            raise
        except (TransportClosed, OSError):
            self._reconnect()
            raise
        self._inject_local(
            bridge,
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

    def _acquire_bridge(self) -> BridgeHost:
        """_ensure_bridge with one reconnect retry (acquisition only)."""
        try:
            return self._ensure_bridge()
        except (TransportClosed, OSError):
            self._reconnect()
            return self._ensure_bridge()

    def _suota_abort(self) -> dict[str, Any]:
        """Force-write MEM_DEV ABORT, clearing a stuck device-side session.

        In the synchronous model no session can be running concurrently with
        this command, so "abort" degenerates to this cleanup write.
        """
        bridge = self._acquire_bridge()
        try:
            SuotaSession(bridge).abort()
        except (TransportClosed, OSError):
            self._reconnect()
            raise
        return {"aborted": True}

    # ------------------------------------------------------------ TCP serve

    def serve_forever(self) -> None:
        """Accept loop; one connection at a time, lines strictly in order.

        Blocking by design; :meth:`shutdown` breaks it from another thread
        (tests) or a signal handler.
        """
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.tcp_host, self.tcp_port))
        server.listen(4)
        server.settimeout(0.2)
        self.tcp_port = server.getsockname()[1]  # resolved when tcp_port=0
        self._server = server
        try:
            while not self._stop:
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break  # socket shut down
                self._serve_connection(conn)
        finally:
            try:
                server.close()
            except OSError:
                pass
            self._server = None

    def shutdown(self) -> None:
        """Request ``serve_forever`` to stop (idempotent)."""
        self._stop = True
        server = self._server
        if server is not None:
            try:
                server.close()
            except OSError:
                pass

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn:
            reader = conn.makefile("rb")
            try:
                for raw in reader:
                    line = raw.strip()
                    if not line:
                        continue
                    response = self.handle_line(line.decode("utf-8", "replace"))
                    conn.sendall((response + "\n").encode("utf-8"))
                    if self._stop:
                        break
            except (ConnectionError, OSError):
                pass  # client vanished mid-request; keep serving
            finally:
                try:
                    reader.close()
                except OSError:
                    pass

    def handle_line(self, line: str) -> str:
        """One JSON line in, one JSON response line out (serve-layer shell)."""
        started = time.monotonic()
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            response = {"ok": False, "error": f"bad_json: {exc}"}
        else:
            response = self.handle_command(req)
        response.setdefault("_ms", (time.monotonic() - started) * 1000.0)
        return json.dumps(response, default=str)


# --------------------------------------------------------------- arg helpers


def _require(req: dict[str, Any], key: str) -> Any:
    if key not in req:
        raise DaemonError(f"missing_arg:{key}")
    return req[key]


def _hex_arg(req: dict[str, Any], key: str = "data", default: str = "") -> bytes:
    value = req.get(key, default)
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, (list, tuple)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            raise DaemonError(f"bad_arg:{key} (hex)") from exc
    raise DaemonError(f"bad_arg:{key}")


def uuid16_arg(value: object) -> int:
    """Accept "f5fe" / "0xF5FE" / full 128-bit UUID -> uuid16 prefix int."""
    text = str(value).strip()
    compact = text.lower().replace("0x", "")
    if "-" in text or len(compact) > 4:
        return uuid16_prefix(compact)
    try:
        return int(compact, 16)
    except ValueError as exc:
        raise DaemonError("bad_arg:uuid") from exc


def _int_arg(req: dict[str, Any], key: str, lo: int, hi: int) -> int:
    """Integer request field, bounds-checked (ValueError like the encoders)."""
    value = req[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an int {lo}..{hi}")
    if not lo <= value <= hi:
        raise ValueError(f"{key} out of range {lo}..{hi}: {value}")
    return value


def _gatt_addr_arg(req: dict[str, Any]) -> bytes:
    """read addressing: {"uuid": ...} or {"handle": n} (0x0000 + handle)."""
    if "handle" in req:
        try:
            return gatt_read_payload(handle=_int_arg(req, "handle", 0, 0xFFFF))
        except ValueError as exc:
            raise DaemonError(f"bad_arg: {exc}") from exc
    uuid = _require(req, "uuid")
    return gatt_read_payload(uuid16_arg(uuid))


def _subscribe_addr_arg(req: dict[str, Any]) -> bytes:
    """subscribe addressing: {"uuid": ...} or {"handle": n, "cccd_handle": m}."""
    if "handle" in req or "cccd_handle" in req:
        try:
            return subscribe_payload(
                value_handle=_int_arg(req, "handle", 0, 0xFFFF),
                cccd_handle=_int_arg(req, "cccd_handle", 0, 0xFFFF),
            )
        except ValueError as exc:
            raise DaemonError(f"bad_arg: {exc}") from exc
    uuid = _require(req, "uuid")
    return subscribe_payload(uuid16_arg(uuid))


def _unsubscribe_addr_arg(req: dict[str, Any]) -> bytes:
    """unsubscribe addressing: {"uuid": ...} or {"handle": n}."""
    if "handle" in req:
        try:
            return unsubscribe_payload(handle=_int_arg(req, "handle", 0, 0xFFFF))
        except ValueError as exc:
            raise DaemonError(f"bad_arg: {exc}") from exc
    uuid = _require(req, "uuid")
    return unsubscribe_payload(uuid16_arg(uuid))
