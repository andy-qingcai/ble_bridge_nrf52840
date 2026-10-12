"""Bridge client: command request/response, event ring, credit flow control.

Sits on a :class:`~blebridge.transport.Transport` and speaks the frame protocol
of ``blebridge.framing`` (ADR-002).  Three responsibilities, nothing more —
GATT/SUOTA semantics live in later layers:

- **Commands**: :meth:`BridgeHost.request` writes a request frame with an
  auto-incrementing SEQ (1..255 ring) and waits for the ``CMD | 0x80``
  response frame carrying the same SEQ.  Response payloads carry the status
  code as their first byte; the remaining bytes are the response body.
  ``status != 0`` is returned as-is, never raised.
  :meth:`BridgeHost.request_noreply` (request_noreply) writes the same kind of frame and
  returns immediately — its late response is recognised by SEQ as an *orphan*
  at dispatch time: the status byte is recorded for
  :meth:`BridgeHost.join_noreply` (non-zero statuses also count in
  :attr:`BridgeHost.orphan_errors`), the frame is dropped — never stored — so
  it can never be matched by a future request on a recycled SEQ, and
  :attr:`BridgeHost.orphan_responses` counts every orphan consumed.
- **Events**: event frames (``EVENT_CMDS``) are numbered by arrival order
  with a monotonically increasing host-side ``app_seq`` and stored in an
  :class:`EventRing` (bounded, drops oldest).  The wire SEQ of event frames
  (firmware-side ring number) is *not* used for numbering, but it is
  continuity-checked: the firmware increments it by one per event (u8
  wraparound), and a jump means device-side loss (or a firmware restart).
  Every jump increments :attr:`EventRing.wire_gaps` by the number of missing
  events and injects a local ``EVT.DEBUG`` (subtype ``0xFF``) record — no
  exception is raised, a firmware restart is tolerated.
- **Credit flow control** (FR-A3, host is the grantor): every received event
  consumes one credit; when the remaining balance drops below the threshold
  (default 32) the host automatically writes a ``CMD.credits`` frame whose
  payload is the grant size (u16 LE, default 128).  Because the firmware
  keeps its credit balance across serial reconnects while every
  :class:`BridgeHost` session starts from the full-window assumption, the
  host re-grants the full window on **first use** (before the first event
  is consumed — P0 cross-session resync); the firmware saturates the
  balance at ``0xFFFF``, so the "full window in effect" invariant holds for
  a fresh session whether the leftover balance was low or high.

Concurrency model: **synchronous pump**.  There is no receive thread; the
internal :meth:`BridgeHost._pump` drains the transport into the frame parser
and is called on entry to :meth:`~BridgeHost.request`,
:meth:`~BridgeHost.wait_for` and :meth:`~BridgeHost.drain`.  This makes every
interaction deterministic under :class:`~blebridge.transport.FakeTransport`
(no background thread, no real waiting except explicit timeout polling).

Threading: ``BridgeHost`` is **not thread-safe** — it supports exactly one
calling thread (or access serialised externally).  Before introducing a
background pump or multi-threaded callers, every entry point
(``request`` / ``wait_for`` / ``drain`` / ``grant_credits`` / ``close``)
must be serialised with a lock.  One residual risk is accepted for v1: a
late response of a timed-out request can be mis-matched to a future request
only if the same SEQ recurs after the 1..255 wraparound *while the stale
response is still queued in the transport* — under the synchronous pump
model responses are consumed within the same ``request`` call, so the
window is practically unreachable; revisit together with the lock.
"""

from __future__ import annotations

import struct
import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from types import TracebackType
from typing import Any, Deque, Dict, Iterator, List, Optional

from .framing import EVENT_CMDS, Frame, StreamParser, encode_frame
from .transport import Transport, TransportClosed

__all__ = [
    "CMD",
    "EVT",
    "Event",
    "BridgeTimeout",
    "EventRing",
    "BridgeHost",
    "gatt_read_payload",
    "gatt_write_payload",
    "subscribe_payload",
    "unsubscribe_payload",
    "disc_svc_payload",
    "conn_update_payload",
    "phy_payload",
    "adv_start_payload",
    "notify_payload",
    "set_char_payload",
    "gatt_define_payload",
    "ROLE_CENTRAL",
    "ROLE_PERIPHERAL",
    "GATTX_NOTIFY",
    "GATTX_PERIPH_WRITE",
]

_CREDITS_PAYLOAD = struct.Struct("<H")  # grant size, u16 LE
_WIRE_GAP_SUBTYPE = 0xFF  # local EVT.DEBUG subtype: wire event SEQ jump

_U16 = struct.Struct("<H")

# uuid16-field sentinel selecting raw-handle addressing (protocol contract
# section 4): the request's uuid16 field is 0x0000 and the bytes that follow
# carry attribute handles instead.
HANDLE_MODE_KEY = 0x0000


def _check_u16(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must be an int 0..0xFFFF, got {value!r}")
    return value


def gatt_read_payload(uuid16: Optional[int] = None, *, handle: Optional[int] = None) -> bytes:
    """CMD.gatt_read (0x08) payload.

    ``uuid16`` addressing: ``u16 LE uuid16`` (map lookup on the firmware).
    ``handle`` addressing: ``0x0000 + u16 LE handle`` — a raw attribute
    handle read, no discovery map required.
    """
    if handle is not None:
        if uuid16 is not None:
            raise ValueError("pass uuid16 or handle, not both")
        return _U16.pack(HANDLE_MODE_KEY) + _U16.pack(_check_u16(handle, "handle"))
    if uuid16 is None:
        raise ValueError("gatt_read needs uuid16 or handle")
    return _U16.pack(_check_u16(uuid16, "uuid16"))


def gatt_write_payload(
    data: bytes = b"",
    uuid16: Optional[int] = None,
    *,
    handle: Optional[int] = None,
    handle_type: int = 0,
    with_response: bool = True,
) -> bytes:
    """CMD.gatt_write (0x09) payload.

    ``uuid16`` addressing: ``1B write_type + u16 LE uuid16 + value``.
    ``handle`` addressing: ``1B write_type + 0x0000 + 1B handle_type +
    u16 LE handle + value`` with ``handle_type`` 0 = characteristic value
    handle, 1 = descriptor handle (both a raw ATT write today; the byte
    documents intent).
    """
    if handle_type not in (0, 1):
        raise ValueError(f"handle_type must be 0 or 1, got {handle_type!r}")
    write_type = 0x01 if not with_response else 0x00
    head = _U16.pack(HANDLE_MODE_KEY) + bytes([handle_type])
    if handle is not None:
        if uuid16 is not None:
            raise ValueError("pass uuid16 or handle, not both")
        return bytes([write_type]) + head + _U16.pack(_check_u16(handle, "handle")) + bytes(data)
    if uuid16 is None:
        raise ValueError("gatt_write needs uuid16 or handle")
    return bytes([write_type]) + _U16.pack(_check_u16(uuid16, "uuid16")) + bytes(data)


def subscribe_payload(
    uuid16: Optional[int] = None,
    *,
    value_handle: Optional[int] = None,
    cccd_handle: Optional[int] = None,
) -> bytes:
    """CMD.subscribe (0x0A) payload.

    ``uuid16`` addressing: ``u16 LE uuid16`` (firmware writes the mapped
    CCCD).  ``value_handle``+``cccd_handle`` addressing: ``0x0000 + u16 LE
    value handle + u16 LE cccd handle`` — pins the notification CCCD value
    (0x0001); no discovery map required.
    """
    if value_handle is not None or cccd_handle is not None:
        if uuid16 is not None:
            raise ValueError("pass uuid16 or value_handle/cccd_handle, not both")
        if value_handle is None or cccd_handle is None:
            raise ValueError("handle addressing needs value_handle AND cccd_handle")
        return (
            _U16.pack(HANDLE_MODE_KEY)
            + _U16.pack(_check_u16(value_handle, "value_handle"))
            + _U16.pack(_check_u16(cccd_handle, "cccd_handle"))
        )
    if uuid16 is None:
        raise ValueError("subscribe needs uuid16 or value_handle/cccd_handle")
    return _U16.pack(_check_u16(uuid16, "uuid16"))


def unsubscribe_payload(uuid16: Optional[int] = None, *, handle: Optional[int] = None) -> bytes:
    """CMD.unsubscribe (0x0B) payload (handle form undoes a handle-mode
    subscription: ``0x0000 + u16 LE value handle``)."""
    if handle is not None:
        if uuid16 is not None:
            raise ValueError("pass uuid16 or handle, not both")
        return _U16.pack(HANDLE_MODE_KEY) + _U16.pack(_check_u16(handle, "handle"))
    if uuid16 is None:
        raise ValueError("unsubscribe needs uuid16 or handle")
    return _U16.pack(_check_u16(uuid16, "uuid16"))


def disc_svc_payload(svc_uuid16: int = 0) -> bytes:
    """CMD.disc_svc (0x0C) payload: the 16-bit service UUID to discover now.

    ``0`` re-discovers the last successfully discovered service (also the
    meaning of the legacy empty payload).
    """
    return _U16.pack(_check_u16(svc_uuid16, "svc_uuid16"))


def conn_update_payload(
    interval_min: int = 24,
    interval_max: int = 40,
    latency: int = 0,
    supervision_timeout: int = 400,
) -> bytes:
    """CMD.conn_update (0x44) payload: 4 x u16 LE.

    Defaults mirror the firmware connect parameters (30-50 ms interval,
    latency 0, 4 s supervision timeout); units are 1.25 ms (intervals) and
    10 ms (supervision timeout) per the LE spec.
    """
    return _U16.pack(_check_u16(interval_min, "interval_min")) + _U16.pack(
        _check_u16(interval_max, "interval_max")
    ) + _U16.pack(_check_u16(latency, "latency")) + _U16.pack(
        _check_u16(supervision_timeout, "supervision_timeout")
    )


def phy_payload(phy: int) -> bytes:
    """CMD.phy (0x45) payload: 1B phy preference, 1 = 1M, 2 = 2M."""
    if phy not in (1, 2):
        raise ValueError(f"phy must be 1 (1M) or 2 (2M), got {phy!r}")
    return bytes([phy])


ADV_INTERVAL_DEFAULT = 0xFFFF
"""CMD.adv_start sentinel interval: the firmware applies its own default."""

BRIDGE_CHAR_ADDR_DEFAULT = 0x00
"""CMD.notify/set_char addressing selector: the preset characteristic."""

BRIDGE_CHAR_ADDR_UUID16 = 0x01
"""CMD.notify/set_char addressing selector: address by characteristic
uuid16 within the locally served (preset + host-defined) services."""

# Characteristic property bits of CMD.gatt_define (wire byte, FR-H5).
GATT_DEFINE_PROP_READ = 0x01
GATT_DEFINE_PROP_WRITE_WO_RESP = 0x02
GATT_DEFINE_PROP_WRITE = 0x04
GATT_DEFINE_PROP_NOTIFY = 0x08


def adv_start_payload(interval_ms: Optional[int] = None, name: bool = True) -> bytes:
    """CMD.adv_start (0x46) payload.

    ``u16 LE interval_ms`` (``None``/``0xFFFF`` = the firmware default) +
    ``1B name switch`` (complete local name in the scan response).
    """
    if interval_ms is None:
        interval_ms = ADV_INTERVAL_DEFAULT
    if not isinstance(interval_ms, int) or isinstance(interval_ms, bool):
        raise ValueError(f"interval_ms must be an int, got {interval_ms!r}")
    if not 0 <= interval_ms <= 0xFFFF:
        raise ValueError(f"interval_ms out of range 0..0xFFFF: {interval_ms}")
    return _U16.pack(interval_ms) + bytes([1 if name else 0])


def _char_addr_head(mode: int, uuid16: Optional[int], name: str) -> bytes:
    if mode == BRIDGE_CHAR_ADDR_DEFAULT:
        if uuid16 is not None:
            raise ValueError(f"{name} default mode takes no uuid16")
        return bytes([BRIDGE_CHAR_ADDR_DEFAULT])
    if mode == BRIDGE_CHAR_ADDR_UUID16:
        if uuid16 is None:
            raise ValueError(f"{name} uuid16 mode needs uuid16")
        return bytes([BRIDGE_CHAR_ADDR_UUID16]) + _U16.pack(
            _check_u16(uuid16, "uuid16")
        )
    raise ValueError(f"{name} mode must be 0 (default) or 1 (uuid16)")


def notify_payload(data: bytes = b"", uuid16: Optional[int] = None) -> bytes:
    """CMD.notify (0x48) payload.

    Default characteristic (preset notify char): ``0x00 + data``; uuid16
    addressing within the locally served services: ``0x01 + u16 LE uuid16 +
    data``.
    """
    mode = BRIDGE_CHAR_ADDR_UUID16 if uuid16 is not None else BRIDGE_CHAR_ADDR_DEFAULT
    return _char_addr_head(mode, uuid16, "notify") + bytes(data)


def set_char_payload(data: bytes = b"", uuid16: Optional[int] = None) -> bytes:
    """CMD.set_char (0x49) payload (addressing as in :func:`notify_payload`,
    default target = the preset write characteristic)."""
    mode = BRIDGE_CHAR_ADDR_UUID16 if uuid16 is not None else BRIDGE_CHAR_ADDR_DEFAULT
    return _char_addr_head(mode, uuid16, "set_char") + bytes(data)


def gatt_define_payload(svc_uuid16: int, chars: List[dict[str, Any]]) -> bytes:
    """CMD.gatt_define (0x4A) payload.

    ``u16 LE svc_uuid16 + 1B n_chars`` then per characteristic
    ``u16 LE char_uuid16 + 1B props + u16 LE max_len`` where the props bits
    are ``GATT_DEFINE_PROP_*``.  Each ``chars`` entry is a dict with
    ``uuid16`` (required; int or hex string like ``"ffa1"``), the boolean
    properties ``read``, ``write_wo_resp``, ``write``, ``notify`` and
    ``max_len`` (1..244).
    """
    head = _U16.pack(_check_u16(svc_uuid16, "svc_uuid16"))
    if svc_uuid16 == 0:
        raise ValueError("svc_uuid16 must be nonzero")
    if not chars:
        raise ValueError("gatt_define needs at least one characteristic")
    if len(chars) > 8:
        raise ValueError("at most 8 characteristics per service (firmware)")
    body = bytearray()
    seen = set()
    for ch in chars:
        raw_uuid = ch.get("uuid16")
        if raw_uuid is None:
            raise ValueError(f"characteristic entry missing uuid16: {ch!r}")
        if isinstance(raw_uuid, str):
            try:
                uuid16 = int(raw_uuid.lower().replace("0x", ""), 16)
            except ValueError as exc:
                raise ValueError(f"bad characteristic uuid16 {raw_uuid!r}") from exc
        else:
            uuid16 = int(raw_uuid)
        uuid16 = _check_u16(uuid16, "char uuid16")
        if uuid16 in seen:
            raise ValueError(f"duplicate characteristic uuid16 {uuid16:#06x}")
        seen.add(uuid16)
        props = 0
        for flag, bit in (
            ("read", GATT_DEFINE_PROP_READ),
            ("write_wo_resp", GATT_DEFINE_PROP_WRITE_WO_RESP),
            ("write", GATT_DEFINE_PROP_WRITE),
            ("notify", GATT_DEFINE_PROP_NOTIFY),
        ):
            value = ch.get(flag, False)
            if not isinstance(value, bool):
                raise ValueError(f"prop {flag} must be bool, got {value!r}")
            if value:
                props |= bit
        if props == 0:
            raise ValueError(f"characteristic {uuid16:#06x} has no properties")
        max_len = ch.get("max_len", 244)
        if not isinstance(max_len, int) or isinstance(max_len, bool):
            raise ValueError(f"max_len must be an int, got {max_len!r}")
        if not 1 <= max_len <= 244:
            raise ValueError(f"max_len out of range 1..244: {max_len}")
        body += _U16.pack(uuid16) + bytes([props]) + _U16.pack(max_len)
    return head + bytes([len(chars)]) + bytes(body)


class CMD(IntEnum):
    """Bridge request commands (ADR-002 command space 0x01..0x3F, 0x40..0x5F).

    Invariant (Task 4 fix round 1, controller ruling): every ``CMD`` value
    must satisfy ``(cmd | 0x80) not in EVENT_CMDS`` — a response frame is
    always ``cmd | 0x80`` and must never be confused with an event frame.
    ``0x0E`` is RESERVED for exactly that reason: its response byte ``0x8E``
    used to collide with ``EVT.DEBUG`` (``log_level`` sat there before
    moving to ``0x0F``); do not assign it.
    """

    ping = 0x01
    info = 0x02
    scan_start = 0x03
    scan_stop = 0x04
    connect = 0x05
    disconnect = 0x06
    mtu = 0x07
    gatt_read = 0x08
    gatt_write = 0x09
    subscribe = 0x0A
    unsubscribe = 0x0B
    disc_svc = 0x0C
    reset = 0x0D
    # 0x0E RESERVED (response 0x8E would collide with EVT.DEBUG; former
    # log_level slot) — disabled, never assign.
    log_level = 0x0F
    suota_start = 0x40
    suota_abort = 0x41
    suota_status = 0x42
    # FR-B5: SMP pairing on the current BLE link (L2, Just Works).  Empty
    # payload; success body = [encrypted u8, sec_level u8]; failure answers
    # status 0x75 (BRIDGE_STATUS_ERR_PAIR_FAILED).  The firmware blocks up
    # to its pairing timeout (default 8 s), so pair() defaults to a larger
    # host timeout than request()'s 2 s.
    pair = 0x43
    # Generic bridge: LE connection parameter update.  Payload = 4 x u16 LE
    # (interval_min, interval_max in 1.25 ms units; latency; supervision
    # timeout in 10 ms units).  Async: the response confirms submission,
    # the settled parameters arrive as an EVT.DEBUG record.
    conn_update = 0x44
    # Generic bridge: LE PHY preference.  Payload = 1B (1 = 1M, 2 = 2M,
    # both directions).  Async like conn_update.
    phy = 0x45
    # FR-H peripheral mode: start connectable advertising.  Payload =
    # u16 LE interval_ms (0xFFFF = firmware default) + 1B name switch.
    # Idempotent when already advertising; state rides EVT.DEBUG.
    adv_start = 0x46
    # FR-H peripheral mode: stop advertising (idempotent, empty payload).
    adv_stop = 0x47
    # FR-H peripheral mode: push notification content of a locally served
    # characteristic and notify subscribed peers.  Payload = 0x00 + data
    # (preset notify characteristic) or 0x01 + u16 LE uuid16 + data.
    # status 0x74 when no peer is subscribed.
    notify = 0x48
    # FR-H peripheral mode: update a locally served characteristic's value
    # (what peers read back).  Same addressing byte as notify.
    set_char = 0x49
    # FR-H5 peripheral mode: register/replace one host-defined GATT
    # service (dynamic GATT DB).  Payload = svc_uuid16 + per-char
    # {uuid16, props, max_len}; success body = base handle (u16 LE).
    # BUSY (0x73) while a peripheral-role link is up.
    gatt_define = 0x4A
    # task-dfu-a: soft-enter the MCUboot serial recovery console.  Empty
    # payload; the firmware ACKs, tears down BLE, latches the bootloader
    # boot mode (GPREGRET via the boot-mode retention node) and reboots.
    # The USB port then re-enumerates as the MCUboot serial recovery
    # device (default PID 0x8523) and the bridge protocol is OFFLINE
    # until the next normal reset.  See docs/protocol-contract.md §1.
    enter_dfu = 0x4B
    credits = 0x50


class EVT(IntEnum):
    """Event frame commands (subset of EVENT_CMDS currently assigned)."""

    SCAN = 0x20
    CONN = 0x21
    DISC = 0x22
    GATTX = 0x23
    MTU = 0x24
    SUOTA = 0x25
    DEBUG = 0x8E


# EVT.CONN / EVT.DISC role byte (FR-H): which side the link belongs to.
ROLE_CENTRAL = 0x00  # host-initiated link (CMD.connect)
ROLE_PERIPHERAL = 0x01  # link accepted while advertising

# EVT.GATTX subtypes: 0x00 = notification from a central-role peer
# (brief-pinned, payload [u16 LE uuid16][data]); 0x01 = FR-H peripheral-role
# write to a locally served characteristic ([u16 LE uuid16][u16 LE value
# handle][data]).
GATTX_NOTIFY = 0x00
GATTX_PERIPH_WRITE = 0x01


@dataclass
class Event:
    """One decoded event frame.  ``payload`` excludes the subtype byte."""

    app_seq: int  # host-side monotonic arrival number (see EventRing)
    evt: int  # event command, one of EVT
    subtype: int  # first payload byte of the wire frame (0 when absent)
    payload: bytes  # remainder of the wire payload


class BridgeTimeout(Exception):
    """A response (or matching event for wait_for) did not arrive in time."""

    def __init__(self, cmd: int) -> None:
        self.cmd = cmd
        super().__init__(f"timeout waiting for cmd {int(cmd):#04x}")


class EventRing:
    """Bounded FIFO of :class:`Event` with host-side monotonic numbering.

    ``app_seq`` starts at 0 and never resets or reuses a value, even across
    evictions, so ``drain(after)`` cursors stay valid.  When the ring is full
    the oldest event is evicted and ``dropped`` is incremented — the retained
    window is always contiguous in ``app_seq``.

    ``wire_gaps`` counts device-side event-frame losses detected by
    :class:`BridgeHost` (jumps of the wire SEQ ring); it is a pure counter
    and never affects ``app_seq`` numbering.
    """

    CAPACITY = 1024

    def __init__(self, capacity: int = CAPACITY) -> None:
        self.capacity = capacity
        self.dropped = 0
        self.wire_gaps = 0
        self._events: Deque[Event] = deque()
        self._next_seq = 0

    def append(self, evt: int, subtype: int, payload: bytes) -> Event:
        """Record one event, assigning it the next ``app_seq``."""
        if len(self._events) >= self.capacity:
            self._events.popleft()
            self.dropped += 1
        event = Event(
            app_seq=self._next_seq, evt=int(evt), subtype=subtype, payload=bytes(payload)
        )
        self._next_seq += 1
        self._events.append(event)
        return event

    def drain(self, after: int = -1) -> List[Event]:
        """Events with ``app_seq > after``, oldest first (numbering contiguous).

        The default ``after=-1`` returns the whole retained window.  Events
        are *not* removed from the ring; callers keep their own cursor.
        """
        return [e for e in self._events if e.app_seq > after]

    def latest_seq(self) -> int:
        """Highest ``app_seq`` handed out so far (-1 when empty)."""
        return self._next_seq - 1

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)


class BridgeHost:
    """Command/event client over a transport (synchronous pump model).

    Args:
        transport: any :class:`~blebridge.transport.Transport`; must be open.
        credit_threshold: when the credit balance falls below this after
            consuming an event, a top-up grant is written automatically.
        credit_grant: number of credits per grant (auto and the initial
            balance; the host assumes the firmware starts out with this
            grant in effect).  On first use the host also writes one
            resync grant of this size to re-align a balance left over from
            a previous serial session (firmware persists it across
            reconnects; see the module docstring).
        poll_interval: sleep between pump iterations while waiting inside
            :meth:`request` / :meth:`wait_for`.
    """

    _PUMP_READ_SIZE = 4096
    _PUMP_MAX_READS = 64  # bound per pump so one pump cannot starve forever

    def __init__(
        self,
        transport: Transport,
        credit_threshold: int = 32,
        credit_grant: int = 128,
        poll_interval: float = 0.001,
    ) -> None:
        self.transport = transport
        self.events = EventRing()
        self.credit_threshold = credit_threshold
        self.credit_grant = credit_grant
        self.poll_interval = poll_interval
        self._parser = StreamParser()
        self._responses: Dict[int, Frame] = {}  # seq -> response frame
        # Fire-and-forget bookkeeping: pending maps seq -> cmd for
        # in-flight noreply requests; status records each orphan's status
        # byte until join_noreply() or a SEQ recycle consumes it (bounded:
        # <= 255 entries each, keyed by the SEQ ring).
        self._noreply_pending: Dict[int, int] = {}
        self._noreply_status: Dict[int, int] = {}
        self.orphan_responses = 0  # noreply responses consumed at dispatch
        self.orphan_errors = 0  # orphan responses with a non-zero status
        self._seq = 0
        self._credits = credit_grant
        self._credit_resync_pending = True  # session-start grant not sent yet
        self._closed = False
        self._last_wire_seq: Optional[int] = None  # event-frame SEQ tracker

    # ------------------------------------------------------------ commands

    def _next_seq(self) -> int:
        """SEQ ring 1..255 (never 0, so responses never alias event frames)."""
        self._seq = self._seq % 255 + 1
        return self._seq

    def request(
        self,
        cmd: int,
        payload: bytes = b"",
        timeout: float = 2.0,
    ) -> tuple[int, bytes]:
        """Send a command and wait for its SEQ-matched response.

        Returns ``(status, body)`` where ``status`` is the first byte of the
        response payload and ``body`` the remainder.  ``status != 0`` is
        returned, not raised.  Raises :class:`BridgeTimeout` on timeout and
        :class:`~blebridge.transport.TransportClosed` when the transport is down.
        """
        self._require_open()
        cmd = int(cmd)
        seq = self._next_seq()
        # Drop any stale response for this SEQ from a previous cycle, and a
        # pending fire-and-forget marking: a recycled SEQ must answer
        # normally again (noreply guard).
        self._responses.pop(seq, None)
        self._noreply_pending.pop(seq, None)
        self._noreply_status.pop(seq, None)
        self.transport.write(encode_frame(cmd, seq, payload))
        deadline = time.monotonic() + timeout
        while True:
            self._pump()
            frame = self._responses.pop(seq, None)
            if frame is not None:
                body = frame.payload
                status = body[0] if body else 0
                return status, bytes(body[1:])
            if time.monotonic() >= deadline:
                raise BridgeTimeout(cmd)
            time.sleep(self.poll_interval)

    def request_noreply(self, cmd: int, payload: bytes = b"") -> int:
        """Send a command frame and return immediately (fire-and-forget).

        The request frame is written with a normally allocated SEQ from the
        shared ring, but no response is awaited — control returns as soon as
        the bytes are handed to the transport (the CDC/bridge queue is the
        backpressure boundary).  Made for PATCH_DATA write-without-response
        chunks: the firmware answers the bridge frame synchronously and that
        answer carries no BLE delivery information, so the per-chunk wait is
        pure overhead — the integrity boundary lives one layer up (the
        block-tail SERV_STATUS ACK).

        The late response is *orphaned*: :meth:`_dispatch` recognises its SEQ
        as noreply-pending, records the response's status byte for
        :meth:`join_noreply` (non-zero ones also increment
        :attr:`orphan_errors`) and drops the frame — nothing stale survives
        to be matched by a future :meth:`request` (which additionally
        pops/discards its SEQ before writing — belt and braces).  A SEQ whose
        response never arrives keeps its pending marking until either method
        recycles the SEQ, and the discard in :meth:`request` guarantees a
        legitimate response to a recycled SEQ is stored and matched normally.
        The residual mis-match window is the one already accepted for
        timed-out requests (same SEQ after a 1..255 wraparound while the
        stale response is still queued in the transport — practically
        unreachable under the synchronous pump).

        Returns the allocated SEQ (1..255).  Raises
        :class:`~blebridge.transport.TransportClosed` when the transport is down.
        """
        self._require_open()
        cmd = int(cmd)
        seq = self._next_seq()
        # A recycled SEQ must not inherit stale state from an earlier cycle.
        self._responses.pop(seq, None)
        self._noreply_pending.pop(seq, None)
        self._noreply_status.pop(seq, None)
        self._noreply_pending[seq] = cmd
        self.transport.write(encode_frame(cmd, seq, payload))
        return seq

    def join_noreply(self, seq: int, timeout: float = 2.0) -> int:
        """Wait until the fire-and-forget request ``seq`` has been answered.

        Pumps the transport until the response of the given
        :meth:`request_noreply` SEQ has been consumed as an orphan and
        returns its status byte (0 = OK; non-zero = the bridge's verdict,
        e.g. the ``(uint8_t)(-err)`` of a failed write-without-response).
        This is the bounded-lookahead primitive behind SuotaSession's
        windowed pipelining: the pipeline stays full while
        the host may only run a fixed number of chunks ahead of the bridge's
        verdicts — the bridge answers a chunk only *after* its BLE write
        completed, so a join at the window end is a hard sync point that
        keeps the firmware's CDC RX ring from overflowing.

        Raises :class:`BridgeTimeout` when no response arrives in time (the
        chunk frame was most likely lost between host and bridge — callers
        fail fast instead of waiting for a block ACK that can never come).
        A SEQ that is *not in flight* (already joined, or its SEQ was
        recycled) is a host bookkeeping violation and raises
        :class:`RuntimeError` carrying the SEQ — deliberately not a
        :class:`BridgeTimeout`, so callers cannot mistake it for a device
        answer-never-came situation and must not translate it into an
        ack-timeout/ABORT.
        """
        self._require_open()
        deadline = time.monotonic() + timeout
        while True:
            self._pump()
            if seq in self._noreply_status:
                return self._noreply_status.pop(seq)
            if seq not in self._noreply_pending:
                # Host bookkeeping violation, not a timeout: the caller
                # joined a SEQ that was already joined, or whose SEQ was
                # recycled.  RuntimeError on purpose (message carries the
                # SEQ, not a cmd) — _join_chunk must not translate this into
                # an ack timeout, which would ABORT the device for a
                # host-side bug.
                raise RuntimeError(
                    f"join_noreply: SEQ {seq} is not in flight "
                    "(already joined or recycled)"
                )
            if time.monotonic() >= deadline:
                raise BridgeTimeout(self._noreply_pending[seq])
            time.sleep(self.poll_interval)

    # -------------------------------------------------------------- events

    def drain(self, after: int = -1) -> List[Event]:
        """Pump the transport once, then return ring events after ``after``."""
        self._require_open()
        self._pump()
        return self.events.drain(after)

    def inject_local_event(self, evt: int, subtype: int, payload: bytes = b"") -> Event:
        """Append a *host-local* event to the ring (pure addition, Task 4).

        Local events never travelled the wire: they carry no wire SEQ, do not
        consume event credits, and do not disturb the wire-SEQ gap tracker.
        Used e.g. by the daemon to publish SUOTA progress on the ``EVT.SUOTA``
        channel and by the wire-gap detector itself (``EVT.DEBUG``/0xFF).
        """
        return self.events.append(evt, subtype, bytes(payload))

    def wait_for(
        self,
        evt: int,
        subtype: Optional[int] = None,
        timeout: float = 2.0,
    ) -> Event:
        """Poll until an event of type ``evt`` (and ``subtype`` if given).

        Scans the whole ring each iteration and returns the *oldest* match,
        so events already in the ring are returned immediately; use
        :meth:`drain` with a cursor for incremental consumption.  Raises
        :class:`BridgeTimeout` (``cmd`` carries the EVT value) on timeout.
        """
        self._require_open()
        evt = int(evt)
        deadline = time.monotonic() + timeout
        while True:
            self._pump()
            for event in self.events:
                if event.evt == evt and (subtype is None or event.subtype == subtype):
                    return event
            if time.monotonic() >= deadline:
                raise BridgeTimeout(evt)
            time.sleep(self.poll_interval)

    def grant_credits(self, n: int) -> None:
        """Manually grant ``n`` event credits (writes a CMD.credits frame)."""
        self._require_open()
        self._credits += n
        self._write_credits(n)

    def pair(self, timeout: float = 10.0) -> tuple[int, bytes]:
        """Run SMP pairing on the current BLE link (CMD.pair 0x43, FR-B5).

        The firmware starts LE pairing (L2, Just Works) on the connected device
        link and answers once the procedure settles: ``(0, [encrypted,
        sec_level])`` on success, ``(0x75, ...)`` on pairing failure/timeout.
        Raises :class:`BridgeTimeout` if the response itself never arrives
        (the firmware's own pairing timeout defaults to 8 s, hence the 10 s
        host default).
        """
        return self.request(int(CMD.pair), b"", timeout=timeout)

    # -------------------------------------------------- generic BLE surface

    def disc_svc(self, svc_uuid16: int = 0, timeout: float = 5.0) -> tuple[int, int]:
        """Host-triggered GATT discovery (CMD.disc_svc 0x0C, generic bridge).

        Runs service discovery for the 16-bit ``svc_uuid16`` NOW and merges
        every found characteristic into the firmware's GATT map; ``0``
        re-discovers the last service.  Returns ``(status, nchars)`` with
        the characteristic count of this run (0 on failure).  The default
        5 s host timeout brackets the firmware's in-discovery block.
        """
        status, body = self.request(
            int(CMD.disc_svc), disc_svc_payload(svc_uuid16), timeout=timeout
        )
        nchars = int.from_bytes(body[:2], "little") if status == 0 and len(body) >= 2 else 0
        return status, nchars

    def gatt_read(
        self, uuid16: Optional[int] = None, *, handle: Optional[int] = None, timeout: float = 2.0
    ) -> tuple[int, bytes]:
        """Read a characteristic by uuid16 (needs discovery) or raw handle."""
        payload = gatt_read_payload(uuid16, handle=handle)
        return self.request(int(CMD.gatt_read), payload, timeout=timeout)

    def gatt_write(
        self,
        data: bytes = b"",
        uuid16: Optional[int] = None,
        *,
        handle: Optional[int] = None,
        handle_type: int = 0,
        with_response: bool = True,
        timeout: float = 2.0,
    ) -> tuple[int, bytes]:
        """Write a characteristic by uuid16 or raw handle (see the payload
        encoder for the addressing encodings)."""
        payload = gatt_write_payload(
            data,
            uuid16,
            handle=handle,
            handle_type=handle_type,
            with_response=with_response,
        )
        return self.request(int(CMD.gatt_write), payload, timeout=timeout)

    def subscribe(
        self,
        uuid16: Optional[int] = None,
        *,
        value_handle: Optional[int] = None,
        cccd_handle: Optional[int] = None,
        timeout: float = 2.0,
    ) -> tuple[int, bytes]:
        """Enable notifications by uuid16 (needs discovery) or by pinned
        value/CCCD handle pair (notification value 0x0001)."""
        payload = subscribe_payload(uuid16, value_handle=value_handle, cccd_handle=cccd_handle)
        return self.request(int(CMD.subscribe), payload, timeout=timeout)

    def unsubscribe(
        self, uuid16: Optional[int] = None, *, handle: Optional[int] = None, timeout: float = 2.0
    ) -> tuple[int, bytes]:
        """Disable notifications by uuid16, or by value handle for a
        handle-mode subscription."""
        return self.request(
            int(CMD.unsubscribe), unsubscribe_payload(uuid16, handle=handle), timeout=timeout
        )

    def conn_update(
        self,
        interval_min: int = 24,
        interval_max: int = 40,
        latency: int = 0,
        supervision_timeout: int = 400,
        timeout: float = 5.0,
    ) -> tuple[int, bytes]:
        """Request an LE connection parameter update (CMD.conn_update 0x44).

        Units: 1.25 ms (intervals) / 10 ms (supervision timeout); the
        defaults mirror the firmware connect parameters.  The response
        confirms submission only — the settled parameters arrive as an
        EVT.DEBUG record.
        """
        payload = conn_update_payload(interval_min, interval_max, latency, supervision_timeout)
        return self.request(int(CMD.conn_update), payload, timeout=timeout)

    def phy_update(self, phy: int, timeout: float = 5.0) -> tuple[int, bytes]:
        """Request an LE PHY change (CMD.phy 0x45): 1 = 1M, 2 = 2M.  The
        result arrives as an EVT.DEBUG record."""
        return self.request(int(CMD.phy), phy_payload(phy), timeout=timeout)

    # -------------------------------------------------- peripheral surface

    def adv_start(
        self,
        interval_ms: Optional[int] = None,
        name: bool = True,
        timeout: float = 2.0,
    ) -> tuple[int, bytes]:
        """Start connectable peripheral advertising (CMD.adv_start 0x46).

        Advertises the preset test service (0xFFE0); ``name`` includes the
        complete local name ("blebridge") in the scan response.
        ``interval_ms=None`` uses the firmware default.  Idempotent: the
        firmware answers OK when advertising is already on (parameter
        changes need an adv_stop first); the state rides EVT.DEBUG.
        """
        return self.request(
            int(CMD.adv_start), adv_start_payload(interval_ms, name), timeout=timeout
        )

    def adv_stop(self, timeout: float = 2.0) -> tuple[int, bytes]:
        """Stop peripheral advertising (CMD.adv_stop 0x47, idempotent)."""
        return self.request(int(CMD.adv_stop), timeout=timeout)

    def notify(
        self, data: bytes = b"", uuid16: Optional[int] = None, timeout: float = 2.0
    ) -> tuple[int, bytes]:
        """Push notification content (CMD.notify 0x48).

        Default (``uuid16=None``) targets the preset notify characteristic
        (0xFFE2); otherwise the characteristic is addressed by uuid16
        within the locally served services.  The pushed content also becomes
        the characteristic's readable value.  Status 0x74 when no peer is
        subscribed; 0x72 for unknown characteristic / length problems.
        """
        return self.request(int(CMD.notify), notify_payload(data, uuid16), timeout=timeout)

    def set_char(
        self, data: bytes = b"", uuid16: Optional[int] = None, timeout: float = 2.0
    ) -> tuple[int, bytes]:
        """Set a locally served characteristic's value (CMD.set_char 0x49).

        Default (``uuid16=None``) targets the preset write characteristic
        (0xFFE1); peers read the new value back; never touches the air.
        """
        return self.request(
            int(CMD.set_char), set_char_payload(data, uuid16), timeout=timeout
        )

    def gatt_define(
        self, svc_uuid16: int, chars: List[dict[str, Any]], timeout: float = 2.0
    ) -> tuple[int, int]:
        """Register or replace a host-defined GATT service (CMD.gatt_define
        0x4A, FR-H5).

        ``chars`` entries are dicts accepted by
        :func:`gatt_define_payload` (``uuid16``, the boolean properties
        ``read``/``write_wo_resp``/``write``/``notify`` and ``max_len``).
        Values start zeroed in firmware RAM; peer writes are echoed as
        EVT.GATTX peripheral-role events (subtype 0x01).  Returns
        ``(status, base_handle)`` — the service declaration handle the
        host computes attribute offsets from (0 on failure).  Status 0x73
        while a peripheral-role link is up or no slot/pool is free.
        """
        status, body = self.request(
            int(CMD.gatt_define), gatt_define_payload(svc_uuid16, chars), timeout=timeout
        )
        base = int.from_bytes(body[:2], "little") if status == 0 and len(body) >= 2 else 0
        return status, base

    # ------------------------------------------------------------ internals

    def _require_open(self) -> None:
        if self._closed or not self.transport.is_open:
            raise TransportClosed("bridge host or transport is closed")

    def _pump(self) -> None:
        """Read available transport bytes into the parser, dispatch frames."""
        if self._credit_resync_pending:
            # Cross-session credit resync (P0): the firmware keeps its credit
            # balance across serial reconnects (e.g. leftover 81 from the
            # last session) while a fresh BridgeHost assumes a full window —
            # re-grant before any event is consumed so the first event burst
            # cannot drive the stale balance to 0 and drop.  The firmware
            # saturates at 0xFFFF, so the full-window invariant holds either
            # way.  Fired once per BridgeHost on the first pump (request /
            # wait_for / drain all pump on entry).
            self._credit_resync_pending = False
            self._write_credits(self.credit_grant)
        for _ in range(self._PUMP_MAX_READS):
            data = self.transport.read(self._PUMP_READ_SIZE, 0.0)
            if not data:
                return
            for frame in self._parser.feed(data):
                self._dispatch(frame)

    def _dispatch(self, frame: Frame) -> None:
        cmd = frame.cmd
        if cmd in EVENT_CMDS:
            subtype = frame.payload[0] if frame.payload else 0
            self.events.append(cmd, subtype, frame.payload[1:])
            self._check_wire_seq(frame.seq)
            self._consume_credit()
        elif cmd & 0x80:
            if frame.seq in self._noreply_pending:
                # Orphan: response of a fire-and-forget request.
                # Record the status byte for join_noreply(), keep counters,
                # and drop — storing it in _responses could only pollute a
                # future request that recycles this SEQ.  Event frames (bit
                # 7 clear, e.g. the block-ACK EVT.GATTX) never reach this
                # branch, no matter what wire SEQ they carry.
                del self._noreply_pending[frame.seq]
                status = frame.payload[0] if frame.payload else 0
                self._noreply_status[frame.seq] = status
                self.orphan_responses += 1
                if status != 0:
                    self.orphan_errors += 1
            else:
                self._responses[frame.seq] = frame
        # else: unsolicited request-direction frame from device — ignore.

    def _check_wire_seq(self, seq: int) -> None:
        """Track the event-frame wire SEQ ring; record jumps (device loss).

        The firmware numbers event frames 0..255 with strict +1 wraparound.
        A jump of ``n`` means ``n - 1`` events were lost (or the firmware
        restarted — tolerated, never raised).  Each jump increments
        ``EventRing.wire_gaps`` by the missing count and appends one local
        ``EVT.DEBUG`` (subtype 0xFF) record describing the gap.
        """
        prev = self._last_wire_seq
        self._last_wire_seq = seq
        if prev is None:
            return
        jump = (seq - prev) & 0xFF
        if jump == 1:
            return  # contiguous
        missing = (jump - 1) & 0xFF  # 1..255 events lost between prev and seq
        self.events.wire_gaps += missing
        self.events.append(
            int(EVT.DEBUG),
            _WIRE_GAP_SUBTYPE,
            f"wire_seq_gap prev={prev} got={seq} missing={missing}".encode("ascii"),
        )

    def _consume_credit(self) -> None:
        self._credits -= 1
        if self._credits < self.credit_threshold:
            self._credits += self.credit_grant
            self._write_credits(self.credit_grant)

    def _write_credits(self, n: int) -> None:
        if self._closed:
            return  # nothing to grant once closed
        self.transport.write(
            encode_frame(int(CMD.credits), self._next_seq(), _CREDITS_PAYLOAD.pack(n))
        )

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Close the underlying transport.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.transport.close()

    @property
    def is_open(self) -> bool:
        return not self._closed and self.transport.is_open

    def __enter__(self) -> "BridgeHost":
        self.transport.open()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()
