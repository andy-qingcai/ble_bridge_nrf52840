"""Scripted SUOTA peripheral on FakeTransport — shared test helper (Task 3).

A behavioural model of a SUOTA device server (0xFEF5), layered on
:class:`~blebridge.transport.FakeTransport` exactly like the legacy
``tests_simulated.py`` FakeDevice, but over the bridge frame protocol.
Everything runs inside the synchronous pump, so a whole update is
deterministic with zero real waiting except deliberately short timeout
paths (tests use 0.05 s).

How the script drives a full session:

- Every ``CMD.gatt_write`` / ``CMD.subscribe`` request is answered
  immediately (base-class responses table, status 0).  Requests are also
  fed to the device model below.
- Every ``CMD.gatt_read`` request is answered per-characteristic from
  ``read_values`` (uuid16 prefix -> raw value bytes), because the base
  table is keyed by command only and the two info reads need different
  values.  An unlisted uuid16 answers with an empty value.
- Protocol notifications are injected as ``EVT.GATTX`` event frames when
  the model decides the device would send them:

    * ``mem_dev = 0x13000000``  -> notify ``image_started_status`` (0x10,
      or ``None`` to simulate an SMP handshake eating the notification)
    * PATCH_DATA reaching the current PATCH_LEN -> notify
      ``block_ack`` (True -> 0x02, an int -> that status, None/False ->
      stay silent, exercising the ACK timeout)
    * ``mem_dev = 0xFE000000`` (END) -> notify 0x02 when ``end_ack``
    * ``mem_dev = 0xFD000000`` (REBOOT) -> recorded, no notification
    * ``mem_dev = 0xFF000000`` (ABORT) -> recorded in ``aborted``

Because the base class queues the write response before the model queues
the notification, the session's ``request`` returns first and the waiting
loop finds the notification already in the event ring — no sleeps.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Dict, Optional, Union

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from blebridge.bridge import CMD, EVT, BridgeHost  # noqa: E402
from blebridge.framing import StreamParser, encode_frame  # noqa: E402
from blebridge.suota import (  # noqa: E402
    GATTX_NOTIFY,
    MEM_DEV_EXTERNAL_SPI_BANK0,
    STATUS_CMD_SUCCESS,
    STATUS_IMAGE_STARTED,
    STATUS_SAME_IMAGE,
    SUOTA_UUIDS,
    SuotaSession,
    uuid16_le,
    uuid16_prefix,
)
from blebridge.transport import FakeTransport  # noqa: E402

_U16_MEM_DEV = uuid16_prefix(SUOTA_UUIDS["mem_dev"])
_U16_GPIO_MAP = uuid16_prefix(SUOTA_UUIDS["gpio_map"])
_U16_PATCH_LEN = uuid16_prefix(SUOTA_UUIDS["patch_len"])
_U16_PATCH_DATA = uuid16_prefix(SUOTA_UUIDS["patch_data"])
_U16_SERV_STATUS = uuid16_prefix(SUOTA_UUIDS["serv_status"])
_U16_CHAR_SIZE = uuid16_prefix(SUOTA_UUIDS["patch_data_char_size"])
_U16_MTU = uuid16_prefix(SUOTA_UUIDS["suota_mtu"])

# Reference-device info values (AN-B-022): char size 244, MTU 251.
DEFAULT_INFO_READS: Dict[int, bytes] = {
    _U16_CHAR_SIZE: struct.pack("<H", 244),
    _U16_MTU: struct.pack("<H", 251),
}


class SuotaFakeTransport(FakeTransport):
    """FakeTransport + SUOTA server behaviour model (see module docstring).

    Args:
        image_started_status: notification sent after the MEM_DEV
            0x13000000 write; ``None`` suppresses it (read-fallback path).
        block_ack: per-block ACK: ``True`` -> 0x02, an ``int`` -> that
            status code, ``None``/``False`` -> never ACK (timeout path).
        end_ack: END-phase ACK: same semantics as ``block_ack``.
        read_values: uuid16 prefix -> raw value bytes answered to
            ``CMD.gatt_read``; defaults to the reference-device info reads.
    """

    def __init__(
        self,
        image_started_status: Optional[int] = STATUS_IMAGE_STARTED,
        block_ack: Union[bool, int, None] = True,
        end_ack: Union[bool, int, None] = True,
        read_values: Optional[Dict[int, bytes]] = None,
    ) -> None:
        super().__init__(
            responses={
                # Host-triggered discovery (generic firmware): status 0 +
                # discovered characteristic count u16 LE (10 SUOTA chars).
                int(CMD.disc_svc): b"\x00\x0a\x00",
                int(CMD.gatt_write): b"\x00",
                int(CMD.subscribe): b"\x00",
            }
        )
        # Public alias of the base-class response table (tests tweak e.g.
        # ``fake.responses[int(CMD.gatt_write)]`` to force a bridge error).
        self.responses = self._responses
        self._observer = StreamParser()  # private 2nd parse of written bytes
        self.read_values: Dict[int, bytes] = dict(DEFAULT_INFO_READS)
        if read_values:
            self.read_values.update(read_values)
        self.image_started_status = image_started_status
        self.block_ack = block_ack
        self.end_ack = end_ack

        # Device state / observation points for assertions.
        self.received = bytearray()  # all PATCH_DATA bytes (upload body)
        self.mem_dev_values = []  # every MEM_DEV u32 written
        self.gpio_values = []  # every GPIO_MAP u32 written
        self.patch_len_values = []  # every PATCH_LEN u16 written
        self.notified = []  # SERV_STATUS values pushed by the model
        self.subscribes = []  # subscribe request payloads
        self.rebooted = False
        self.aborted = False
        self._patch_len = 0
        self._block_received = 0

    # ------------------------------------------------------------ Fake API

    def write(self, data: bytes) -> None:
        super().write(data)  # responses table + written log (base behaviour)
        for frame in self._observer.feed(bytes(data)):
            self._on_request(frame)

    # ------------------------------------------------------------ internals

    def _on_request(self, frame) -> None:
        if frame.cmd == int(CMD.gatt_read) and len(frame.payload) >= 2:
            (u16,) = struct.unpack_from("<H", frame.payload)
            value = self.read_values.get(u16, b"")
            self.inject_raw(
                encode_frame(
                    int(CMD.gatt_read) | 0x80, frame.seq, b"\x00" + value
                )
            )
        elif frame.cmd == int(CMD.gatt_write) and len(frame.payload) >= 3:
            write_type = frame.payload[0]
            (u16,) = struct.unpack_from("<H", frame.payload, 1)
            self._on_gatt_write(write_type, u16, bytes(frame.payload[3:]))
        elif frame.cmd == int(CMD.subscribe):
            self.subscribes.append(bytes(frame.payload))

    def _on_gatt_write(self, write_type: int, u16: int, data: bytes) -> None:
        if u16 == _U16_MEM_DEV and len(data) == 4:
            (value,) = struct.unpack("<I", data)
            self.mem_dev_values.append(value)
            if value & 0xFFFFFF00 == MEM_DEV_EXTERNAL_SPI_BANK0:
                # MEM_DEV start（0x13000000 | bank，bank 任意）→ IMAGE_STARTED
                if self.image_started_status is not None:
                    self._notify(self.image_started_status)
            elif value == 0xFE000000:
                self._notify_option(self.end_ack)
            elif value == 0xFD000000:
                self.rebooted = True
            elif value == 0xFF000000:
                self.aborted = True
        elif u16 == _U16_GPIO_MAP and len(data) == 4:
            (value,) = struct.unpack("<I", data)
            self.gpio_values.append(value)
        elif u16 == _U16_PATCH_LEN and len(data) == 2:
            self._patch_len = struct.unpack("<H", data)[0]
            self.patch_len_values.append(self._patch_len)
            self._block_received = 0
        elif u16 == _U16_PATCH_DATA:
            self.received += data
            # Receiver counts block bytes against PATCH_LEN, then ACKs.
            self._block_received += len(data)
            if self._patch_len and self._block_received >= self._patch_len:
                self._block_received = 0
                self._notify_option(self.block_ack)

    def _notify_option(self, option: Union[bool, int, None]) -> None:
        """ACK-style notification: True -> 0x02, int -> that code, else none."""
        if option is True:
            self._notify(STATUS_CMD_SUCCESS)
        elif isinstance(option, int) and option is not False:
            self._notify(option)
        # None / False: stay silent -> session-side timeout path

    def _notify(self, status: int) -> None:
        self.notified.append(status)
        self.inject_raw(
            encode_frame(
                int(EVT.GATTX),
                0,  # wire SEQ unused by the host ring (Task 2 decision)
                bytes([GATTX_NOTIFY]) + uuid16_le(SUOTA_UUIDS["serv_status"]) + bytes([status]),
            )
        )


def make_session(
    fake: SuotaFakeTransport, **session_kwargs
) -> SuotaSession:
    """Open ``fake`` and wrap it in a BridgeHost + SuotaSession."""
    fake.open()
    return SuotaSession(BridgeHost(fake), **session_kwargs)


def synthetic_image(full_blocks: int = 3, tail: int = 100) -> bytes:
    """Deterministic image: ``full_blocks`` * 2048 bytes plus ``tail`` bytes.

    With the defaults the upload body is 3*2048 + 100 + 1 = 6245 bytes ->
    4 blocks (3 full + one 101-byte remainder), 9 chunks per full block at
    the negotiated chunk size of 244.
    """
    length = full_blocks * 2048 + tail
    return bytes((i * 7 + 3) % 251 for i in range(length))
