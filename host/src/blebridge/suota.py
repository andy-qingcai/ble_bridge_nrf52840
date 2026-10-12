"""SUOTA update session state machine over a BridgeHost (FR-C, protocol core).

Authority: Renesas/Dialog ``AN-B-022`` (SUOTA profile: UUIDs, state machine,
block/chunk protocol, command sequence) plus live captures against reference
SUOTA devices.
The whole SUOTA protocol lives here on the host; the nRF52840 firmware only
relays GATT operations to the target device's 0xFEF5 service.

GATT addressing contract (defined here; the bridge firmware (Task 6) and
the daemon (Task 4) must agree byte-for-byte):

::

    CMD.gatt_write (0x09) request payload
        offset  size  field
        0       1     write type: 0 = with response, 1 = without response
        1       2     characteristic select: uuid16 prefix, little-endian
        3       n     value bytes

    CMD.gatt_read (0x08) request payload
        uuid16 prefix (2B LE); the response body is the characteristic value

    CMD.subscribe (0x0A) request payload
        uuid16 prefix (2B LE); firmware enables that characteristic's
        0x2902 CCCD (SERV_STATUS is the only notification source)

    EVT.GATTX (0x23) event payload (notification from the device)
        offset  size  field
        0       1     subtype: GATTX_NOTIFY (0x00) = characteristic notify
        1       2     source characteristic: uuid16 prefix, little-endian
        3       n     notification value

The *uuid16 prefix* is the first 16 bits of the characteristic's 128-bit
UUID (``int(uuid[:4], 16)``).  Every SUOTA characteristic has a distinct
prefix (asserted by the tests), so two bytes address them unambiguously.
Write types are strict (FR-C2): control characteristics with response,
PATCH_DATA without response.

State machine per :meth:`SuotaSession.run` (connect is the caller's job):
host-triggered discovery (``disc_svc 0xFEF5`` — the generic bridge firmware
discovers nothing on its own) -> optional pairing prerequisite
(``pair=True`` pairs immediately after discovery, before any GATT access) ->
read info (patch_data_char_size, mtu) -> chunk = min(244, mtu-3, max_chunk);
subscribe SERV_STATUS (an ATT 0x78 verdict runs one MEM_DEV-transaction
unlock probe and retries — measured against a reference device);
write MEM_DEV ``0x13000000 | bank`` (external SPI,
``bank=0`` by default) and wait notify 0x10 IMAGE_STARTED — on timeout, read
SERV_STATUS as a fallback (an SMP handshake on a fresh link can eat
the notification); write
GPIO_MAP 0x05060300; per block write PATCH_LEN (again with the remainder
before a short last block), stream PATCH_DATA write-without-response chunks
(pipelined by default: chunks go out fire-and-forget via
``BridgeHost.request_noreply`` — the firmware answers the bridge frame
synchronously with no BLE delivery information, so the per-chunk wait is
pure protocol-mode overhead — with at most ``DEFAULT_PIPELINE_WINDOW``
chunks in flight and a window-end join as flow-control sync point; the
window also keeps the burst inside the firmware's CDC RX ring so chunk
frames are not dropped; the block-tail notify 0x02 stays the only integrity
boundary; ``pipelined=False`` restores the legacy per-chunk
request/response round trip as the A/B measurement baseline), wait notify
0x02 (block ACK); write MEM_DEV 0xFE000000 (END) and wait 0x02;
write MEM_DEV 0xFD000000 (REBOOT) — the device then disconnects and re-advertises
after ~10 s.

Failure policy:
- notification timeout -> best-effort ABORT (MEM_DEV 0xFF000000) then
  :class:`SuotaError` with ``status_code == STATUS_TIMEOUT`` (the block-ACK
  abort is brief-mandated);
- unexpected device status -> :class:`SuotaError` (``0x15`` raises
  :class:`SameImageError` on both observed paths: the SERV_STATUS
  notification *and* the ATT write response of the MEM_DEV start write —
  reference devices reject it in the write response, suota_sim historically
  notifies); no ABORT is sent — the device already produced a verdict, and
  ABORT on a finished session is best avoided;
- MEM_DEV write rejected with ATT ``0x0F`` (Insufficient Encryption,
  measured against a reference device): the session pairs the bridge link
  once (CMD.pair, FR-B5) and retries the write; if the retry is still
  rejected (or pairing itself fails) :class:`EncryptionRequired` is raised;
- subscribe rejected with ATT ``0x78`` (Insufficient Authorization,
  observed on a reference device after OTA reboot; bisection showed it is
  independent of pairing — the only unlock is ONE completed MEM_DEV
  transaction on the same connection): the session writes the MEM_DEV
  start value (rolling back with ABORT 0xFF000000 when accepted) and
  retries the subscribe once; a surviving 0x78 raises
  :class:`EncryptionRequired`;
- ``pair=True`` keeps pairing as an explicit *prerequisite*: it runs right
  after discovery, before any GATT access, and a failure raises
  :class:`EncryptionRequired` with ``phase == "pair"``;
- :meth:`SuotaSession.abort` (also callable from ``progress_cb``) writes
  MEM_DEV 0xFF000000, and the running session raises with
  ``status_code == STATUS_ABORTED``.

Threading: a :class:`SuotaSession` borrows the single-threaded synchronous
pump of :class:`~blebridge.bridge.BridgeHost` — call it from one thread only.
``abort()`` is meant for same-thread use (e.g. from ``progress_cb``).
"""

from __future__ import annotations

import struct
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Optional, Tuple, Union

from .bridge import CMD, EVT, BridgeHost, BridgeTimeout, Event
from .image import build_payload
from .transport import TransportClosed

__all__ = [
    "SUOTA_UUIDS",
    "SUOTA_STATUS",
    "STATUS_IMAGE_STARTED",
    "STATUS_CMD_SUCCESS",
    "STATUS_SAME_IMAGE",
    "STATUS_TIMEOUT",
    "STATUS_ABORTED",
    "MEM_DEV_EXTERNAL_SPI_BANK0",
    "MEM_DEV_END",
    "MEM_DEV_REBOOT",
    "MEM_DEV_ABORT",
    "GPIO_MAP_SPI_DEFAULT",
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_PIPELINE_WINDOW",
    "MAX_PATCH_CHUNK",
    "MIN_ATT_MTU",
    "WRITE_WITH_RESPONSE",
    "WRITE_WITHOUT_RESPONSE",
    "GATTX_NOTIFY",
    "SUOTA_SVC_UUID16",
    "uuid16_prefix",
    "uuid16_le",
    "ATT_ERR_INSUFFICIENT_ENCRYPTION",
    "ATT_ERR_INSUFFICIENT_AUTHORIZATION",
    "SuotaError",
    "SameImageError",
    "EncryptionRequired",
    "SuotaProgress",
    "SuotaResult",
    "SuotaSession",
]

# --------------------------------------------------------------- constants

SUOTA_UUIDS: Dict[str, str] = {
    # Service (AN-B-022; legacy 0xFEF5, new devices may expose 0xFE9E)
    "svc": "0000fef5-0000-1000-8000-00805f9b34fb",
    # Control + data characteristics of 0xFEF5
    "mem_dev": "8082caa8-41a6-4021-91c6-56f9b954cc34",
    "gpio_map": "724249f0-5ec3-4b5f-8804-42345af08651",
    "mem_info": "6c53db25-47a1-45fe-a022-7c92fb334fd4",
    "patch_len": "9d84b9a3-000c-49d8-9183-855b673fda31",
    "patch_data": "457871e8-d516-4ca1-9116-57d0b17b9cb2",
    "serv_status": "5f78df94-798c-46f5-990a-b3eb6a065c88",
    # Info characteristics (read to negotiate the chunk size)
    "suota_version": "64b4e8b5-0de5-401b-a21d-acc8db3b913a",
    "patch_data_char_size": "42c3dfdd-77be-4d9c-8454-8f875267fb3b",
    "suota_mtu": "b7de1eea-823d-43bb-a3af-c4903dfce23c",
    "l2cap_psm": "61c8849c-f639-4765-946e-5c3419bebb2a",
}

SUOTA_STATUS: Dict[int, str] = {
    0x01: "SPOTA_SRV_STARTED",
    0x02: "CMD_SUCCESS",
    0x03: "SRV_EXIT",
    0x04: "CRC_MISMATCH",
    0x05: "PATCH_LENGTH_ERROR",
    0x06: "EXTERNAL_MEMORY_ERROR",
    0x07: "INTERNAL_MEMORY_ERROR",
    0x08: "INVALID_MEMORY_TYPE",
    0x09: "APPLICATION_ERROR",
    0x10: "IMAGE_STARTED",
    0x11: "INVALID_IMAGE_BANK",
    0x12: "INVALID_IMAGE_HEADER",
    0x13: "INVALID_IMAGE_SIZE",
    0x14: "INVALID_PRODUCT_HEADER",
    0x15: "SAME_IMAGE",
    0x16: "EXTERNAL_MEMORY_READ_ERROR",
    0x17: "INVALID_IMAGE_VERSION",
    0x18: "ANTI_ROLLBACK",
    0x19: "SUOTA_LOCKED",
}

STATUS_IMAGE_STARTED = 0x10
STATUS_CMD_SUCCESS = 0x02
STATUS_SAME_IMAGE = 0x15
# Host-side synthetic status codes (never on the wire; SUOTA codes are 0x01..0x19)
STATUS_TIMEOUT = -1  # a waited-for notification never arrived
STATUS_ABORTED = -2  # abort() was requested

# ATT error codes (bridge surfaces them verbatim as the response status of
# CMD.gatt_write; Zephyr include/zephyr/bluetooth/att.h spelling).
ATT_ERR_INSUFFICIENT_ENCRYPTION = 0x0F  # MEM_DEV encryption guard (FR-B5)

# MEM_DEV control values (written as 4-byte little-endian)
MEM_DEV_EXTERNAL_SPI_BANK0 = 0x13000000  # memory type 0x13 = external SPI, bank 0
MEM_DEV_END = 0xFE000000
MEM_DEV_REBOOT = 0xFD000000
MEM_DEV_ABORT = 0xFF000000

GPIO_MAP_SPI_DEFAULT = 0x05060300  # MISO=5, MOSI=6, CS=3, SCK=0 (Dialog EVB)

DEFAULT_BLOCK_SIZE = 2048  # host-chosen; PATCH_LEN is a u16 (<= 65535)
MAX_PATCH_CHUNK = 244  # reference-reported PATCH_DATA single-write maximum
MIN_ATT_MTU = 23

# Windowed-pipelining cap (measured against a reference device): an
# unbounded 9-chunk burst (9 x 254 B = 2286 B) outruns the bridge firmware's
# 1024 B CDC RX ring (bridge_phy.c RX_RING_SIZE) while its RX thread blocks
# inside bt_gatt_write_without_response — chunk frames were silently dropped
# (rx_overflows counter), the device never reached PATCH_LEN and the block ACK
# never came (host 30 s TIMEOUT in phase 'block').  With at most 3 chunks in
# flight the host writes <= 762 B between sync points, which the ring absorbs
# even with zero concurrent drain, and the bridge holds <= 3 unconfirmed ACL
# TX buffers against a pool of 10 (CONFIG_BT_BUF_ACL_TX_COUNT).  The
# window-end join is a hard sync: the bridge answers a chunk only AFTER its
# BLE write completed.  None restores the pre-fix unbounded burst (only safe
# once the firmware ring grows — plan C-6 option A).
DEFAULT_PIPELINE_WINDOW = 3

WRITE_WITH_RESPONSE = 0x00
WRITE_WITHOUT_RESPONSE = 0x01

GATTX_NOTIFY = 0x00  # EVT.GATTX subtype: characteristic notification

SUOTA_SVC_UUID16 = 0xFEF5  # Dialog SUOTA service (SIG 16-bit form on the wire)

# Bridge status space (protocol contract section 1) + the I-1 transition-
# window retry: the previous auto-discovery firmware answers ERR_NOT_READY
# when disc_svc lands before its connect-time dm finished.
_BRIDGE_STATUS_NOT_READY = 0x74
_DISCOVER_RETRY_DELAY_S = 1.5  # patched to 0 in tests

# ATT error codes (bridge surfaces them verbatim as the response status of
# CMD.gatt_write; Zephyr include/zephyr/bluetooth/att.h spelling).
ATT_ERR_INSUFFICIENT_ENCRYPTION = 0x0F  # MEM_DEV encryption guard (FR-B5)
# Reference device after OTA reboot (measured): CCCD writes demand
# authorization until the link is paired.
ATT_ERR_INSUFFICIENT_AUTHORIZATION = 0x78


def uuid16_prefix(uuid: str) -> int:
    """First 16 bits of a 128-bit UUID string as an int (``"8082caa8-…"`` -> 0x8082)."""
    return int(uuid[:4], 16)


def uuid16_le(uuid: str) -> bytes:
    """uuid16 prefix as 2 little-endian bytes (the gatt_* address form)."""
    return struct.pack("<H", uuid16_prefix(uuid))


_U32 = struct.Struct("<I")
_U16 = struct.Struct("<H")


# --------------------------------------------------------------- exceptions


class SuotaError(Exception):
    """SUOTA session failure.

    ``status_code`` is a SERV_STATUS code (``SUOTA_STATUS``) or one of the
    host-side synthetic codes ``STATUS_TIMEOUT`` / ``STATUS_ABORTED``; a
    non-zero bridge-level GATT status is passed through verbatim (flagged in
    ``detail``).  ``phase`` names the state-machine step that failed.
    """

    def __init__(self, status_code: int, phase: str, detail: str = "") -> None:
        self.status_code = status_code
        self.phase = phase
        self.detail = detail
        name = SUOTA_STATUS.get(status_code)
        if name is None:
            synthetic = {STATUS_TIMEOUT: "TIMEOUT", STATUS_ABORTED: "ABORTED"}
            name = synthetic.get(status_code, f"0x{status_code:02X}")
        message = f"suota failed in phase {phase!r}: {name} (status {status_code})"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class SameImageError(SuotaError):
    """Device refused the update: the image equals the running one (0x15).

    Raised on both real-world delivery paths: the SERV_STATUS notification
    (suota_sim model) and the ATT write response of the MEM_DEV start write
    (reference device, measured).
    """

    def __init__(self, phase: str, detail: str = "") -> None:
        super().__init__(STATUS_SAME_IMAGE, phase, detail)


class EncryptionRequired(SuotaError):
    """A GATT access was rejected for lack of link security and pairing
    could not clear it.

    Raised when a device guard survives the session's automatic CMD.pair
    + single retry: the MEM_DEV encryption guard (ATT 0x0F Insufficient
    Encryption) and the post-reboot CCCD authorization
    guard (ATT 0x78 Insufficient Authorization).
    ``status_code`` carries the surviving ATT verdict (0x0F = decimal 15 vs
    0x78 = decimal 120; never confuse either with SAME_IMAGE 0x15 = 21).
    """

    def __init__(
        self,
        phase: str,
        detail: str = "",
        att_err: int = ATT_ERR_INSUFFICIENT_ENCRYPTION,
    ) -> None:
        super().__init__(att_err, phase, detail)


# ------------------------------------------------------------------ results


@dataclass
class SuotaProgress:
    """Per-block progress snapshot (one callback per acknowledged block)."""

    block: int  # 1-based index of the block just acknowledged
    total_blocks: int
    bytes: int  # cumulative payload bytes acknowledged so far
    phase: str  # "block"


@dataclass
class SuotaResult:
    """Outcome of a successful :meth:`SuotaSession.run`."""

    bytes_written: int  # image size + 1 XOR byte (the actual upload body)
    blocks: int
    chunks: int
    elapsed: float  # seconds, whole run() including negotiation
    throughput: float  # bytes_written / elapsed


# ------------------------------------------------------------------ session


class SuotaSession:
    """Drives one SUOTA update against a connected device via a BridgeHost.

    Args:
        bridge: an open :class:`~blebridge.bridge.BridgeHost`; the BLE link to
            the device must already be established by the caller.
        block_size: host-chosen block size written to PATCH_LEN (2048 is the
            field-proven value; must be >= 1 and <= 65535).
        image_started_timeout: seconds to wait for the IMAGE_STARTED
            notification before the SERV_STATUS read fallback.
        request_timeout: per-bridge-request timeout for GATT operations.
        pair_timeout: host timeout for the automatic CMD.pair (FR-B5); must
            exceed the firmware pairing timeout (Kconfig
            ``BRIDGE_PAIR_TIMEOUT_MS``, default 8 s).
    """

    def __init__(
        self,
        bridge: BridgeHost,
        block_size: int = DEFAULT_BLOCK_SIZE,
        image_started_timeout: float = 2.0,
        request_timeout: float = 2.0,
        pair_timeout: float = 10.0,
    ) -> None:
        if not 1 <= block_size <= 0xFFFF:
            raise ValueError(f"block_size must fit PATCH_LEN u16, got {block_size}")
        self.bridge = bridge
        self.block_size = block_size
        self.image_started_timeout = image_started_timeout
        self.request_timeout = request_timeout
        self.pair_timeout = pair_timeout
        self.aborted = False
        self._paired = False  # CMD.pair attempted for the current run()
        self._cursor = -1  # EventRing app_seq up to which we consumed events

    # --------------------------------------------------------------- public

    def run(
        self,
        image_path: Union[str, Path],
        progress_cb: Optional[Callable[[SuotaProgress], None]] = None,
        chunk_size: Optional[int] = None,
        ack_timeout: float = 30.0,
        bank: int = 0,
        pair: bool = False,
        pipelined: bool = True,
        pipeline_window: Optional[int] = DEFAULT_PIPELINE_WINDOW,
    ) -> SuotaResult:
        """Upload ``image_path`` (any supported image family) to the device.

        ``chunk_size=None`` negotiates ``min(244, mtu-3, max_chunk)``; an
        explicit value is still clamped to ``min(value, mtu-3, max_chunk)``.
        ``ack_timeout`` bounds every block/END ACK wait (device field value:
        30 s).  ``progress_cb`` fires exactly once per acknowledged block.
        ``bank`` (0..255) is OR-ed into the MEM_DEV start value
        (``0x13000000 | bank``): the device answers SAME_IMAGE when the
        requested bank equals the running one, so re-flashing the same image
        needs the other bank (END/REBOOT/ABORT values carry no bank).
        ``pair=True`` makes pairing a *prerequisite*: discovery runs first,
        then CMD.pair, and only then any GATT access.  Regardless of the
        flag, a MEM_DEV write rejected with ATT 0x0F triggers one automatic
        pair + retry, and a subscribe rejected with ATT 0x78 triggers one
        MEM_DEV-transaction unlock probe + retry (see
        :class:`EncryptionRequired`).
        ``pipelined=True`` (default) sends PATCH_DATA chunks
        fire-and-forget (:meth:`BridgeHost.request_noreply`): the per-chunk
        bridge response carries no BLE delivery information (the firmware
        answers synchronously), so blocking on every chunk wastes ~9 RTTs per
        block; the block-tail notify ACK stays the integrity boundary.
        ``pipeline_window`` (default :data:`DEFAULT_PIPELINE_WINDOW`) caps
        how many chunks may be in flight: every window-end chunk is followed
        by :meth:`BridgeHost.join_noreply` on the oldest outstanding chunk —
        the bridge answers only after its BLE write completed, so the join is
        a sync point that keeps the firmware's 1024 B CDC RX ring from
        overflowing (the unbounded burst dropped
        chunk frames and stalled the block ACK).  A non-zero join verdict
        (bridge chunk-write error) raises immediately instead of hanging for
        the ACK timeout, and a join timeout (chunk frame lost between host
        and bridge) aborts the session fast.  ``pipeline_window=None``
        restores the unbounded pre-fix burst (for A/B and after a firmware
        ring fix).  ``pipelined=False`` restores the legacy per-chunk
        request/response round trip (A/B measurement baseline);
        ``pipeline_window`` is ignored then.

        Raises:
            SameImageError: the device already runs this image (0x15) — via the
                MEM_DEV write response or the SERV_STATUS notification.
            EncryptionRequired: the link stayed unencrypted after the
                automatic pairing (ATT 0x0F survived the retry).
            SuotaError: any other protocol/transport failure.
        """
        if not 0 <= bank <= 0xFF:
            raise ValueError(f"bank must fit the MEM_DEV low byte (0..255), got {bank}")
        if pipeline_window is not None and (
            isinstance(pipeline_window, bool)
            or not isinstance(pipeline_window, int)
            or pipeline_window < 1
        ):
            raise ValueError(
                f"pipeline_window must be an int >= 1 or None, got {pipeline_window!r}"
            )
        payload = build_payload(image_path)
        self.aborted = False
        self._paired = False
        self._cursor = self.bridge.events.latest_seq()  # ignore stale events
        started = time.monotonic()

        # Host-triggered service discovery FIRST: the generic bridge
        # firmware discovers nothing on its own, so disc_svc(0xFEF5) must
        # populate its GATT map before any uuid16-addressed operation.
        # On the previous firmware (auto-discovery at connect) this
        # re-runs the same 0xFEF5 discovery — idempotent, harmless.
        self._discover()

        if pair:
            # Pairing prerequisite (kept per controller ruling; the R3
            # bisection showed 0x78 is pair-independent, but pairing here
            # is cheap, always succeeds per evidence, and pre-clears the
            # MEM_DEV 0x0F encryption guard).
            self._ensure_paired("pair")

        max_chunk, link_mtu = self._read_info()
        if chunk_size is None:
            chunk = min(MAX_PATCH_CHUNK, link_mtu - 3, max_chunk)
        else:
            chunk = min(chunk_size, link_mtu - 3, max_chunk)
        if chunk < 1:
            raise ValueError(f"negotiated chunk size {chunk} < 1")

        self._subscribe(bank)
        self._enter_image_mode(bank)
        self._write_control("gpio_map", _U32.pack(GPIO_MAP_SPI_DEFAULT), "gpio_map")

        total = len(payload)
        blocks = (total + self.block_size - 1) // self.block_size
        chunks_sent = 0
        offset = 0
        outstanding: Deque[Tuple[int, int]] = deque()  # (seq, 1-based chunk no)
        for block in range(blocks):
            if self.aborted:
                raise SuotaError(STATUS_ABORTED, "block", "abort requested")
            block_len = min(self.block_size, total - offset)
            if block == 0 or block_len != self.block_size:
                # First block always, and again before a short last block.
                self._write_control("patch_len", _U16.pack(block_len), "patch_len")
            block_end = offset + block_len
            while offset < block_end:
                # A chunk must never cross the block boundary: the receiver
                # counts the block's bytes against PATCH_LEN (AN-B-022).
                piece = payload[offset : min(offset + chunk, block_end)]
                seq = self._write_patch_data(piece, pipelined=pipelined)
                offset += len(piece)
                chunks_sent += 1
                if seq is not None and pipeline_window is not None:
                    outstanding.append((seq, chunks_sent))
                    if len(outstanding) >= pipeline_window:
                        self._join_chunk(*outstanding.popleft())
            # Block boundary: drain every in-flight verdict before waiting
            # for the notify ACK (also pumps the ACK itself into the ring).
            # window=None keeps the pre-fix behaviour: no joins at all, the
            # orphan verdicts are consumed uninspected at the ACK pump.
            if pipeline_window is not None:
                while outstanding:
                    self._join_chunk(*outstanding.popleft())
            self._expect_status(STATUS_CMD_SUCCESS, ack_timeout, "block")
            if progress_cb is not None:
                progress_cb(
                    SuotaProgress(
                        block=block + 1,
                        total_blocks=blocks,
                        bytes=offset,
                        phase="block",
                    )
                )

        self._write_mem_dev(MEM_DEV_END, "end")
        self._expect_status(STATUS_CMD_SUCCESS, ack_timeout, "end")
        self._write_mem_dev(MEM_DEV_REBOOT, "reboot")

        elapsed = time.monotonic() - started
        return SuotaResult(
            bytes_written=total,
            blocks=blocks,
            chunks=chunks_sent,
            elapsed=elapsed,
            throughput=total / max(elapsed, 1e-6),
        )

    def abort(self) -> None:
        """Abort the session: flags the run loop and writes MEM_DEV 0xFF000000.

        Safe to call from ``progress_cb`` (same thread); the running
        :meth:`run` then raises :class:`SuotaError` (``STATUS_ABORTED``).
        """
        self.aborted = True
        self._write_mem_dev(MEM_DEV_ABORT, "abort")

    # ------------------------------------------------------ protocol steps

    def _discover(self) -> None:
        """disc_svc(0xFEF5): host-triggered GATT discovery on the bridge.

        The response body carries the discovered characteristic count
        (u16 LE) on the generic firmware; the legacy firmware answers an
        empty body from its connect-time map.  Only the status matters
        here — a discovery failure is fatal (the uuid16-addressed steps
        below cannot resolve without the map).  An ERR_NOT_READY (0x74,
        review I-1: the legacy firmware's answer inside the connect-time
        auto-discovery window) retries once after a short delay.
        """
        payload = struct.pack("<H", SUOTA_SVC_UUID16)
        try:
            self._request(
                CMD.disc_svc,
                payload,
                "discover",
                timeout=5.0,  # firmware blocks in discovery (dm wait 3 s + margin)
            )
            return
        except SuotaError as exc:
            if exc.status_code != _BRIDGE_STATUS_NOT_READY:
                raise
        time.sleep(_DISCOVER_RETRY_DELAY_S)
        self._request(
            CMD.disc_svc,
            payload,
            "discover",
            timeout=5.0,  # firmware blocks in discovery (dm wait 3 s + margin)
        )

    def _read_info(self) -> Tuple[int, int]:
        """Read (patch_data_char_size, mtu); fall back to protocol minimums.

        The info reads are recommended but not structurally required
        (AN-B-022 command sequence), so a short/failed value
        degrades to the always-workable minimum (chunk 20 @ MTU 23).
        """
        max_chunk = self._read_u16(
            "patch_data_char_size", default=MIN_ATT_MTU - 3, phase="info"
        )
        link_mtu = self._read_u16("suota_mtu", default=MIN_ATT_MTU, phase="info")
        return max_chunk, link_mtu

    def _read_u16(self, name: str, default: int, phase: str) -> int:
        body = self._gatt_read(name, phase)
        if len(body) < 2:
            return default
        return int.from_bytes(body[:2], "little")

    def _subscribe(self, bank: int = 0) -> None:
        payload = uuid16_le(SUOTA_UUIDS["serv_status"])
        try:
            self._request(CMD.subscribe, payload, "subscribe")
            return
        except SuotaError as exc:
            verdict = exc
        if verdict.status_code != ATT_ERR_INSUFFICIENT_AUTHORIZATION:
            raise verdict
        # Rebooted device (measured bisection): 0x78 is independent of
        # pairing -- the ONLY unlock is one completed MEM_DEV transaction
        # on this connection.  Run the probe, then retry once.
        self._mem_dev_unlock(bank)
        try:
            self._request(CMD.subscribe, payload, "subscribe")
        except SuotaError as exc:
            if exc.status_code == ATT_ERR_INSUFFICIENT_AUTHORIZATION:
                raise EncryptionRequired(
                    "subscribe",
                    "subscribe still rejected 0x78 after MEM_DEV unlock",
                    att_err=ATT_ERR_INSUFFICIENT_AUTHORIZATION,
                ) from exc
            raise

    def _mem_dev_unlock(self, bank: int) -> None:
        """One MEM_DEV transaction: write the start value, then ABORT back.

        The post-reboot 0x78 authorization state unlocks on the completion
        of a MEM_DEV transaction (controlled bisection; the
        unlock does not survive a reconnect).  The probe ignores the
        device's verdicts: a refused write opened no transaction (nothing
        to roll back), an accepted one enters image mode and MUST be
        closed with MEM_DEV 0xFF000000.  This deliberately bypasses
        :meth:`abort` — the session-level abort flag must stay untouched.
        Any probe error is swallowed: the retried subscribe carries the
        authoritative verdict.
        """
        try:
            self._write_mem_dev(MEM_DEV_EXTERNAL_SPI_BANK0 | bank, "unlock")
        except SuotaError:
            return
        try:
            self._write_mem_dev(MEM_DEV_ABORT, "unlock")
        except SuotaError:
            pass  # best effort: the retry decides the outcome

    def _enter_image_mode(self, bank: int = 0) -> None:
        """MEM_DEV start (external SPI) + IMAGE_STARTED wait.

        ``bank`` selects the image bank and is OR-ed into the MEM_DEV value
        (``0x13000000 | bank``); the device refuses the bank
        it is currently running from, so only the other bank starts a
        transfer for an identical image.
        """
        self._write_mem_dev(MEM_DEV_EXTERNAL_SPI_BANK0 | bank, "mem_dev")
        self._expect_status(
            STATUS_IMAGE_STARTED,
            self.image_started_timeout,
            "image_started",
            fallback_read=True,
        )

    # ------------------------------------------------------------- waiting

    def _expect_status(
        self,
        expected: int,
        timeout: float,
        phase: str,
        fallback_read: bool = False,
    ) -> int:
        """Wait for a SERV_STATUS notification and map it to outcome/error.

        With ``fallback_read`` (the IMAGE_STARTED case) a timeout is
        followed by a direct SERV_STATUS read before giving up.
        """
        status = self._wait_status(timeout, phase)
        if status is None and fallback_read:
            body = self._gatt_read("serv_status", phase)
            status = body[0] if body else None
        if status == expected:
            return status
        if status is None:
            self._send_abort()  # device still waiting; give it the exit (brief)
            raise SuotaError(STATUS_TIMEOUT, phase, "notification timeout")
        if status == STATUS_SAME_IMAGE:
            # Refused before the transfer started: no ABORT.
            raise SameImageError(phase)
        raise SuotaError(status, phase)  # device-reported error: no ABORT

    def _wait_status(self, timeout: float, phase: str) -> Optional[int]:
        """Next SERV_STATUS notification value, or None on timeout.

        Consumes the event ring behind a cursor; non-SERV_STATUS events are
        skipped.  Raises immediately when ``abort()`` was requested.
        """
        bridge = self.bridge
        deadline = time.monotonic() + timeout
        while True:
            if self.aborted:
                raise SuotaError(STATUS_ABORTED, phase, "abort requested")
            for event in bridge.drain(after=self._cursor):
                self._cursor = event.app_seq
                status = self._serv_status_of(event)
                if status is not None:
                    return status
            if time.monotonic() >= deadline:
                return None
            time.sleep(bridge.poll_interval)

    @staticmethod
    def _serv_status_of(event: Event) -> Optional[int]:
        """SERV_STATUS value if ``event`` is a SERV_STATUS notification."""
        if event.evt != int(EVT.GATTX) or event.subtype != GATTX_NOTIFY:
            return None
        if len(event.payload) < 3:
            return None
        (source,) = struct.unpack_from("<H", event.payload)
        if source != uuid16_prefix(SUOTA_UUIDS["serv_status"]):
            return None
        return event.payload[2]

    # -------------------------------------------------------------- GATT IO

    def _request(
        self, cmd: int, payload: bytes, phase: str, timeout: Optional[float] = None
    ) -> bytes:
        """One bridge request; raise SuotaError on timeout/status failure."""
        try:
            status, body = self.bridge.request(
                cmd, payload, timeout=timeout or self.request_timeout
            )
        except BridgeTimeout as exc:
            raise SuotaError(STATUS_TIMEOUT, phase, "bridge request timeout") from exc
        if status != 0:
            # Bridge-level GATT failure (firmware status space, not SUOTA).
            raise SuotaError(status, phase, f"bridge status {status}")
        return body

    def _gatt_write(
        self,
        name: str,
        value: bytes,
        with_response: bool,
        phase: str,
        *,
        wait: bool = True,
    ) -> Optional[int]:
        """One characteristic write; ``wait=False`` sends fire-and-forget.

        ``wait=False`` (PATCH_DATA pipelining) hands the request frame
        to :meth:`BridgeHost.request_noreply` and returns the in-flight SEQ
        (for :meth:`BridgeHost.join_noreply` accounting); the response that
        arrives later is consumed as an orphan whose status byte is recorded.
        Only meaningful for write-without-response chunks — control writes
        keep ``wait=True`` (FR-C2).  The blocking path returns ``None``.
        """
        write_type = WRITE_WITH_RESPONSE if with_response else WRITE_WITHOUT_RESPONSE
        payload = (
            bytes([write_type]) + uuid16_le(SUOTA_UUIDS[name]) + value
        )
        if wait:
            self._request(CMD.gatt_write, payload, phase)
            return None
        return self.bridge.request_noreply(int(CMD.gatt_write), payload)

    def _gatt_read(self, name: str, phase: str) -> bytes:
        return self._request(CMD.gatt_read, uuid16_le(SUOTA_UUIDS[name]), phase)

    def _write_control(self, name: str, value: bytes, phase: str) -> None:
        """Control-characteristic write — always with response (FR-C2)."""
        self._gatt_write(name, value, with_response=True, phase=phase)

    def _join_chunk(self, seq: int, chunk_no: int) -> None:
        """Wait for one in-flight pipelined chunk's bridge verdict (fix r1).

        The bridge answers a chunk only after ``bt_gatt_write_without_response``
        completed, so the join is both the flow-control sync point (keeps the
        1024 B CDC RX ring from overflowing) and the chunk-error surfacing:
        a non-zero verdict raises immediately instead of hanging for a block
        ACK that can never come.  A join timeout (BridgeTimeout) means the
        chunk frame was lost between host and bridge — best-effort ABORT (the
        device sits mid-block) and fail fast.  A RuntimeError from
        :meth:`BridgeHost.join_noreply` ("SEQ not in flight") is a host-side
        bookkeeping violation, NOT a device situation: it is deliberately not
        caught here and propagates untranslated, without any ABORT.
        """
        try:
            status = self.bridge.join_noreply(seq, timeout=self.request_timeout)
        except BridgeTimeout as exc:
            self._send_abort()
            raise SuotaError(
                STATUS_TIMEOUT,
                "block",
                f"pipelined chunk {chunk_no} ack timeout (bridge never confirmed)",
            ) from exc
        if status != 0:
            raise SuotaError(
                status, "block", f"pipelined chunk {chunk_no} bridge status {status}"
            )

    def _write_patch_data(self, piece: bytes, pipelined: bool = False) -> Optional[int]:
        """PATCH_DATA write — always without response (FR-C2).

        ``pipelined=True`` fires the chunk without waiting for the
        bridge response here: the firmware answers the bridge frame
        synchronously and that answer says nothing about BLE delivery (the
        write-without-response ATT procedure has no ACK), so per-chunk
        waiting is pure round-trip overhead — measured ~3.9 ms p50 per chunk,
        ~35 ms per 9-chunk block.  The chunk is still counted into the local
        byte cursor immediately, so progress semantics are unchanged; the
        block's integrity boundary remains the tail SERV_STATUS 0x02
        notification, and the in-flight verdicts are joined at the pipeline
        window boundary (see :data:`DEFAULT_PIPELINE_WINDOW`).

        Returns the in-flight SEQ when pipelined, ``None`` on the blocking
        path.
        """
        return self._gatt_write(
            "patch_data",
            piece,
            with_response=False,
            phase="block",
            wait=not pipelined,
        )

    def _write_mem_dev(self, value: int, phase: str) -> None:
        """MEM_DEV write with response, SAME_IMAGE / encryption-guard aware.

        Dual-path 0x15 mapping (reference-device measurement): the device
        refuses the MEM_DEV start write *in the ATT write response*
        (the bridge surfaces the raw ATT error as the response status,
        0x15), while suota_sim notifies SERV_STATUS 0x15 instead.  Both are
        the same device verdict, so both raise :class:`SameImageError`.

        Encryption guard (same session's other finding, FR-B5): the real
        device answers MEM_DEV writes with ATT 0x0F (Insufficient Encryption,
        decimal 15 — never confuse it with 0x15 = 21) until the link is
        encrypted.  On 0x0F the session pairs once (CMD.pair) and retries
        the write a single time; a surviving 0x0F — or a failed pairing —
        raises :class:`EncryptionRequired`.
        """
        try:
            self._write_control("mem_dev", _U32.pack(value), phase)
            return
        except SuotaError as exc:
            verdict = exc
        if verdict.status_code == ATT_ERR_INSUFFICIENT_ENCRYPTION:
            self._ensure_paired(phase, cause=verdict)
            try:
                self._write_control("mem_dev", _U32.pack(value), phase)
                return
            except SuotaError as exc:
                if exc.status_code == ATT_ERR_INSUFFICIENT_ENCRYPTION:
                    raise EncryptionRequired(
                        phase, "MEM_DEV write still rejected 0x0F after pairing"
                    ) from exc
                verdict = exc  # e.g. 0x15 once encrypted: SAME_IMAGE below
        if verdict.status_code == STATUS_SAME_IMAGE:
            raise SameImageError(phase, verdict.detail) from verdict
        raise verdict

    def _ensure_paired(self, phase: str, cause: Optional[SuotaError] = None) -> None:
        """CMD.pair once per :meth:`run`; raise EncryptionRequired on failure.

        Exactly one attempt per session (``run(..., pair=True)`` calls this
        right after discovery, before any GATT access; a MEM_DEV 0x0F or a
        subscribe 0x78 calls it lazily): a
        loop would stall the state machine against a device whose guard never
        clears, and a second attempt cannot succeed where the first failed
        at the bridge.
        """
        if self._paired:
            return
        self._paired = True
        try:
            status, _body = self.bridge.pair(timeout=self.pair_timeout)
        except BridgeTimeout as exc:
            raise EncryptionRequired(phase, "pair request timeout") from exc
        if status != 0:
            raise EncryptionRequired(
                phase, f"pair failed (bridge status {status})"
            ) from cause

    def _send_abort(self) -> None:
        """Best-effort ABORT; never masks the error that triggered it."""
        try:
            self.abort()
        except (SuotaError, BridgeTimeout, TransportClosed):
            pass
