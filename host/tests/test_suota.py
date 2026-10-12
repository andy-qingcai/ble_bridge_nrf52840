"""Tests for blebridge.suota: the SUOTA update state machine (FR-C).

Every scenario runs the scripted peripheral from ``suota_fake`` on
FakeTransport (synchronous pump): instant ACKs, injected notifications,
no real waiting except the deliberately short timeout paths (0.05 s).
The full-success flow is asserted to finish well under 5 s.
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from blebridge.bridge import CMD, BridgeHost  # noqa: E402
from blebridge.framing import StreamParser  # noqa: E402
from blebridge.image import build_payload, xor_byte  # noqa: E402
from blebridge.suota import (  # noqa: E402
    ATT_ERR_INSUFFICIENT_AUTHORIZATION,
    ATT_ERR_INSUFFICIENT_ENCRYPTION,
    STATUS_ABORTED,
    STATUS_CMD_SUCCESS,
    STATUS_IMAGE_STARTED,
    STATUS_SAME_IMAGE,
    STATUS_TIMEOUT,
    SUOTA_STATUS,
    SUOTA_UUIDS,
    EncryptionRequired,
    SameImageError,
    SuotaError,
    SuotaProgress,
    SuotaSession,
    SuotaResult,
    uuid16_le,
    uuid16_prefix,
)
from blebridge.transport import FakeTransport  # noqa: E402
from suota_fake import (  # noqa: E402
    _U16_CHAR_SIZE,
    _U16_MEM_DEV,
    _U16_MTU,
    _U16_PATCH_DATA,
    _U16_SERV_STATUS,
    SuotaFakeTransport,
    make_session,
    synthetic_image,
)

_U16_GPIO_MAP = uuid16_prefix(SUOTA_UUIDS["gpio_map"])
_U16_PATCH_LEN = uuid16_prefix(SUOTA_UUIDS["patch_len"])


# ----------------------------------------------------------------- helpers


def written_frames(t: FakeTransport) -> list:
    return StreamParser().feed(bytes(t.written))


def gatt_write_frames(t: FakeTransport) -> List[Tuple[int, int, bytes]]:
    """(write_type, uuid16, value) for every CMD.gatt_write request."""
    out = []
    for frame in written_frames(t):
        if frame.cmd == int(CMD.gatt_write) and len(frame.payload) >= 3:
            out.append(
                (
                    frame.payload[0],
                    struct.unpack_from("<H", frame.payload, 1)[0],
                    frame.payload[3:],
                )
            )
    return out


def gatt_read_uuids(t: FakeTransport) -> List[int]:
    """uuid16 addressed by every CMD.gatt_read request."""
    return [
        struct.unpack("<H", frame.payload)[0]
        for frame in written_frames(t)
        if frame.cmd == int(CMD.gatt_read) and len(frame.payload) == 2
    ]


# ------------------------------------------------------------ constants


def test_uuid16_prefixes_are_distinct() -> None:
    """The 2-byte prefix addressing is only unambiguous if prefixes differ."""
    prefixes = [uuid16_prefix(u) for u in SUOTA_UUIDS.values()]
    assert len(prefixes) == len(set(prefixes))


def test_suota_status_dict_matches_spec() -> None:
    expected = {
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
    assert SUOTA_STATUS == expected


def test_suota_uuids_match_spec() -> None:
    assert SUOTA_UUIDS["svc"] == "0000fef5-0000-1000-8000-00805f9b34fb"
    assert SUOTA_UUIDS["mem_dev"] == "8082caa8-41a6-4021-91c6-56f9b954cc34"
    assert SUOTA_UUIDS["gpio_map"] == "724249f0-5ec3-4b5f-8804-42345af08651"
    assert SUOTA_UUIDS["patch_len"] == "9d84b9a3-000c-49d8-9183-855b673fda31"
    assert SUOTA_UUIDS["patch_data"] == "457871e8-d516-4ca1-9116-57d0b17b9cb2"
    assert SUOTA_UUIDS["serv_status"] == "5f78df94-798c-46f5-990a-b3eb6a065c88"


# ------------------------------------------------------------ full success


def test_full_run_success(tmp_path: Path) -> None:
    image = synthetic_image(full_blocks=3, tail=100)
    path = tmp_path / "synth.img"
    path.write_bytes(image)
    payload = image + bytes([xor_byte(image)])  # 6245 B

    fake = SuotaFakeTransport()
    session = make_session(fake)
    progress: List[SuotaProgress] = []
    t0 = time.monotonic()
    result = session.run(path, progress_cb=progress.append, ack_timeout=0.5)
    wall = time.monotonic() - t0

    # Result contract
    assert isinstance(result, SuotaResult)
    assert result.bytes_written == 6245
    assert result.blocks == 4  # 3 x 2048 + 101-byte remainder
    assert result.chunks == 28  # 3 x ceil(2048/244)=9 + ceil(101/244)=1
    assert result.throughput > 0
    assert result.elapsed < 5.0
    assert wall < 5.0  # brief: fake full flow must stay under 5 s

    # Device model got the exact upload body (image + XOR byte)
    assert bytes(fake.received) == payload

    # State sequence: mem_dev(start) ... mem_dev(END), mem_dev(REBOOT)
    assert fake.mem_dev_values == [0x13000000, 0xFE000000, 0xFD000000]
    assert fake.rebooted is True
    assert fake.gpio_values == [0x05060300]

    # PATCH_LEN: full block size, then rewritten with the remainder
    assert fake.patch_len_values == [2048, 101]

    # Notifications: IMAGE_STARTED + 4 block ACKs + END ACK
    assert fake.notified == [0x10, 0x02, 0x02, 0x02, 0x02, 0x02]

    # Subscribe addressed SERV_STATUS by uuid16 prefix
    assert fake.subscribes == [uuid16_le(SUOTA_UUIDS["serv_status"])]

    # Info reads negotiated chunk = min(244, 251-3, 244) = 244
    assert gatt_read_uuids(fake)[:2] == [_U16_CHAR_SIZE, _U16_MTU]
    chunk_writes = [v for _, u16, v in gatt_write_frames(fake)
                    if u16 == _U16_PATCH_DATA]
    assert all(len(c) <= 244 for c in chunk_writes)

    # Progress: exactly one callback per block
    assert [p.block for p in progress] == [1, 2, 3, 4]
    assert all(p.total_blocks == 4 and p.phase == "block" for p in progress)
    assert [p.bytes for p in progress] == [2048, 4096, 6144, 6245]


def test_write_types_strict(tmp_path: Path) -> None:
    """PATCH_DATA without response; every control characteristic with."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=2, tail=7))
    fake = SuotaFakeTransport()
    session = make_session(fake)
    session.run(path, ack_timeout=0.5)

    types_by_uuid = {}
    for write_type, u16, _ in gatt_write_frames(fake):
        types_by_uuid.setdefault(u16, set()).add(write_type)

    assert types_by_uuid[_U16_PATCH_DATA] == {1}  # without response
    assert types_by_uuid[_U16_MEM_DEV] == {0}  # with response
    assert types_by_uuid[_U16_GPIO_MAP] == {0}
    assert types_by_uuid[_U16_PATCH_LEN] == {0}


def test_patch_data_chunks_are_byte_exact_concatenation(tmp_path: Path) -> None:
    """Payload exactly one full block: image 2047 B + XOR byte = 2048 B."""
    image = synthetic_image(full_blocks=0, tail=2047)  # payload = 2048 B
    path = tmp_path / "synth.img"
    path.write_bytes(image)
    fake = SuotaFakeTransport()
    session = make_session(fake)
    result = session.run(path, ack_timeout=0.5)

    chunks = [v for _, u16, v in gatt_write_frames(fake) if u16 == _U16_PATCH_DATA]
    assert [len(c) for c in chunks] == [244] * 8 + [96]  # 2048 = 8*244 + 96
    assert b"".join(chunks) == build_payload(path)
    assert result.chunks == 9 and result.blocks == 1
    assert fake.patch_len_values == [2048]  # no remainder -> single write


def test_chunk_size_override(tmp_path: Path) -> None:
    image = synthetic_image(full_blocks=3, tail=100)
    path = tmp_path / "synth.img"
    path.write_bytes(image)
    fake = SuotaFakeTransport()
    session = make_session(fake)
    result = session.run(path, chunk_size=100, ack_timeout=0.5)

    chunks = [v for _, u16, v in gatt_write_frames(fake) if u16 == _U16_PATCH_DATA]
    assert all(len(c) <= 100 for c in chunks)
    assert result.chunks == 3 * 21 + 2  # ceil(2048/100)=21; ceil(101/100)=2
    assert result.blocks == 4
    assert bytes(fake.received) == build_payload(path)


def test_single_short_block(tmp_path: Path) -> None:
    """Image smaller than one block: PATCH_LEN holds the whole size."""
    path = tmp_path / "tiny.img"
    path.write_bytes(b"\x01" * 300)
    fake = SuotaFakeTransport()
    session = make_session(fake)
    result = session.run(path, ack_timeout=0.5)

    assert result.blocks == 1
    assert result.chunks == 2  # ceil(301/244)
    assert fake.patch_len_values == [301]
    assert bytes(fake.received) == build_payload(path)


# ------------------------------------------------------- SERV_STATUS read fallback


def test_image_started_lost_read_fallback(tmp_path: Path) -> None:
    """Notification eaten (e.g. SMP handshake): SERV_STATUS read continues."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport(
        image_started_status=None,
        read_values={_U16_SERV_STATUS: bytes([STATUS_IMAGE_STARTED])},
    )
    session = make_session(fake, image_started_timeout=0.05)

    progress: List[SuotaProgress] = []
    result = session.run(path, progress_cb=progress.append, ack_timeout=0.5)

    assert result.blocks == 4  # flow completed after the fallback
    assert STATUS_IMAGE_STARTED not in fake.notified  # notify never arrived
    assert _U16_SERV_STATUS in gatt_read_uuids(fake)  # read fallback happened
    assert len(progress) == 4
    assert fake.rebooted is True


def test_image_started_lost_and_read_reports_error(tmp_path: Path) -> None:
    """Fallback read also fails to show IMAGE_STARTED -> timeout error + ABORT."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport(image_started_status=None)  # read answers empty
    session = make_session(fake, image_started_timeout=0.05)

    with pytest.raises(SuotaError) as excinfo:
        session.run(path, ack_timeout=0.5)
    assert excinfo.value.status_code == STATUS_TIMEOUT
    assert excinfo.value.phase == "image_started"
    assert fake.aborted is True
    assert fake.rebooted is False


# ---------------------------------------------------------------- timeouts


def test_block_ack_timeout_sends_abort(tmp_path: Path) -> None:
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport(block_ack=None)  # device never ACKs a block
    session = make_session(fake)

    t0 = time.monotonic()
    with pytest.raises(SuotaError) as excinfo:
        session.run(path, ack_timeout=0.05)
    assert time.monotonic() - t0 < 2.0  # 0.05 s-level timeout, no long sleep

    assert excinfo.value.status_code == STATUS_TIMEOUT
    assert excinfo.value.phase == "block"
    assert fake.aborted is True  # auto-ABORT on the timeout path
    assert 0xFF000000 in fake.mem_dev_values
    assert fake.rebooted is False  # never reached END/REBOOT


def test_end_ack_timeout_sends_abort(tmp_path: Path) -> None:
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=1, tail=0))
    fake = SuotaFakeTransport(end_ack=None)
    session = make_session(fake)

    with pytest.raises(SuotaError) as excinfo:
        session.run(path, ack_timeout=0.05)
    assert excinfo.value.phase == "end"
    assert excinfo.value.status_code == STATUS_TIMEOUT
    assert fake.aborted is True
    assert fake.rebooted is False


# ------------------------------------------------------ device error paths


def test_same_image_error(tmp_path: Path) -> None:
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport(image_started_status=STATUS_SAME_IMAGE)
    session = make_session(fake)

    with pytest.raises(SameImageError) as excinfo:
        session.run(path)
    assert excinfo.value.status_code == STATUS_SAME_IMAGE
    assert excinfo.value.phase == "image_started"
    assert isinstance(excinfo.value, SuotaError)
    # Refused before the transfer: no ABORT, no reboot (session already ended)
    assert fake.aborted is False
    assert fake.rebooted is False


def test_same_image_error_in_write_response(tmp_path: Path) -> None:
    """Reference device (measured): SAME_IMAGE rides the MEM_DEV write response
    (bridge surfaces the raw ATT error 0x15 as the response status) instead
    of the SERV_STATUS notification — still a SameImageError."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport(image_started_status=None)  # notify path silent
    fake.responses[int(CMD.gatt_write)] = bytes([STATUS_SAME_IMAGE])
    session = make_session(fake)

    with pytest.raises(SameImageError) as excinfo:
        session.run(path)
    assert excinfo.value.status_code == STATUS_SAME_IMAGE
    assert excinfo.value.phase == "mem_dev"  # refused at the MEM_DEV write
    assert "bridge status" in excinfo.value.detail
    assert fake.notified == []  # notification path not involved
    # Same device verdict: no ABORT, no reboot (session already ended)
    assert fake.aborted is False
    assert fake.rebooted is False


def test_device_error_status_mid_block(tmp_path: Path) -> None:
    """Device CRC_MISMATCH on the first block ACK: exact status surfaces."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport(block_ack=0x04)  # CRC_MISMATCH as block ACK
    session = make_session(fake)

    with pytest.raises(SuotaError) as excinfo:
        session.run(path, ack_timeout=0.5)
    assert excinfo.value.status_code == 0x04
    assert excinfo.value.phase == "block"
    assert fake.aborted is False  # device verdict, no auto-ABORT
    assert fake.rebooted is False


def test_bridge_gatt_write_failure_surfaces(tmp_path: Path) -> None:
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport()
    fake.responses[int(CMD.gatt_write)] = b"\x03gatt error"  # bridge status 3
    session = make_session(fake)

    with pytest.raises(SuotaError) as excinfo:
        session.run(path)
    assert excinfo.value.status_code == 3
    assert "bridge status" in excinfo.value.detail


# ------------------------------------------------------------------ abort


def test_abort_from_progress_callback(tmp_path: Path) -> None:
    """abort() is callable from progress_cb (same-thread contract)."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport()
    session = make_session(fake)
    progress: List[SuotaProgress] = []

    def stop_after_two(p: SuotaProgress) -> None:
        progress.append(p)
        if p.block == 2:
            session.abort()

    with pytest.raises(SuotaError) as excinfo:
        session.run(path, progress_cb=stop_after_two, ack_timeout=0.5)

    assert excinfo.value.status_code == STATUS_ABORTED
    assert fake.aborted is True
    assert 0xFF000000 in fake.mem_dev_values
    assert [p.block for p in progress] == [1, 2]  # stopped right after block 2
    assert fake.rebooted is False


def test_standalone_abort_then_run_starts_fresh(tmp_path: Path) -> None:
    """abort() outside run(): writes ABORT + flags; a later run() starts fresh."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=2047))  # 1 block
    fake = SuotaFakeTransport()
    session = make_session(fake)
    session.abort()

    assert fake.aborted is True
    assert fake.mem_dev_values == [0xFF000000]
    result = session.run(path, ack_timeout=0.5)  # run() resets the flag
    assert result.blocks == 1
    assert fake.mem_dev_values == [0xFF000000, 0x13000000, 0xFE000000, 0xFD000000]


# --------------------------------------------------------- session config


def test_block_size_configurable(tmp_path: Path) -> None:
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=600))
    fake = SuotaFakeTransport()
    session = make_session(fake, block_size=512)
    result = session.run(path, ack_timeout=0.5)

    assert result.blocks == 2  # 601 B payload -> 512 + 89
    assert result.bytes_written == 601
    assert fake.patch_len_values == [512, 89]


def test_invalid_block_size_rejected() -> None:
    fake = SuotaFakeTransport()
    fake.open()
    with pytest.raises(ValueError):
        SuotaSession(BridgeHost(fake), block_size=65536)
    with pytest.raises(ValueError):
        SuotaSession(BridgeHost(fake), block_size=0)


def test_bank_selects_mem_dev_start_value(tmp_path: Path) -> None:
    """bank=1 encodes into the MEM_DEV start write (0x13000001); END and
    REBOOT carry no bank (the device needs the other bank to re-flash the
    same image after a SAME_IMAGE refusal)."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    # The fake only notifies IMAGE_STARTED for the bank-0 value, so continue
    # via the SERV_STATUS read fallback with the bank-1 start value.
    fake = SuotaFakeTransport(
        image_started_status=None,
        read_values={_U16_SERV_STATUS: bytes([STATUS_IMAGE_STARTED])},
    )
    session = make_session(fake, image_started_timeout=0.05)
    result = session.run(path, bank=1, ack_timeout=0.5)

    assert fake.mem_dev_values == [0x13000001, 0xFE000000, 0xFD000000]
    assert result.blocks == 4
    assert bytes(fake.received) == build_payload(path)
    assert fake.rebooted is True


def test_invalid_bank_rejected_before_io(tmp_path: Path) -> None:
    """bank must fit the MEM_DEV low byte; rejected before any bridge I/O."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport()
    session = make_session(fake)

    for bad_bank in (-1, 256):
        with pytest.raises(ValueError):
            session.run(path, bank=bad_bank)

    assert written_frames(fake) == []
    assert fake.mem_dev_values == []


def test_session_reusable_for_second_run(tmp_path: Path) -> None:
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=2047))  # 1 block
    fake = SuotaFakeTransport()
    session = make_session(fake)
    first = session.run(path, ack_timeout=0.5)
    second = session.run(path, ack_timeout=0.5)
    assert first.blocks == second.blocks == 1
    assert fake.notified.count(STATUS_IMAGE_STARTED) == 2


def test_info_read_defaults_when_device_reports_nothing(tmp_path: Path) -> None:
    """Short info values degrade to the protocol minimum (chunk 20 @ MTU 23)."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=50))
    fake = SuotaFakeTransport(read_values={_U16_CHAR_SIZE: b"\x00", _U16_MTU: b""})
    session = make_session(fake)
    result = session.run(path, ack_timeout=0.5)

    chunks = [v for _, u16, v in gatt_write_frames(fake) if u16 == _U16_PATCH_DATA]
    assert all(0 < len(c) <= 20 for c in chunks)
    assert result.chunks == ceil_div(51, 20)


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


# --------------------------------------------- PATCH_DATA 流水化


class RequestSpy:
    """Instance-level call recorder for BridgeHost.request/request_noreply.

    Records ``(cmd, payload)`` per call, then delegates to the original
    bound method — used to prove the pipelined path issues zero blocking
    per-chunk requests without changing any behaviour.
    """

    def __init__(self, bridge: BridgeHost) -> None:
        self.blocking: List[Tuple[int, bytes]] = []
        self.noreply: List[Tuple[int, bytes]] = []
        self._bridge = bridge
        self._orig_request = bridge.request
        self._orig_noreply = bridge.request_noreply
        bridge.request = self._request  # type: ignore[method-assign]
        bridge.request_noreply = self._noreply  # type: ignore[method-assign]

    def _request(self, cmd, payload: bytes = b"", timeout: float = 2.0):
        self.blocking.append((int(cmd), bytes(payload)))
        return self._orig_request(cmd, payload, timeout=timeout)

    def _noreply(self, cmd, payload: bytes = b""):
        self.noreply.append((int(cmd), bytes(payload)))
        return self._orig_noreply(cmd, payload)


def _patch_data_chunks(calls: List[Tuple[int, bytes]]) -> List[bytes]:
    """Value bytes of the gatt_write calls addressing PATCH_DATA."""
    u16 = uuid16_prefix(SUOTA_UUIDS["patch_data"])
    return [
        payload[3:]
        for cmd, payload in calls
        if cmd == int(CMD.gatt_write)
        and len(payload) >= 3
        and struct.unpack_from("<H", payload, 1)[0] == u16
    ]


def test_pipelined_run_chunks_fire_and_forget(tmp_path: Path) -> None:
    """pipelined=True（默认）：2048 B -> 9 片产生 9 个免响应请求帧，
    全程零逐片阻塞请求；固件的 9 个即时响应在块 ACK 等待的 pump 中被消费为
    孤儿计数；块 ACK（notify）路径、数据完整性与 result 契约不变。"""
    image = synthetic_image(full_blocks=0, tail=2047)  # payload 2048 B, 1 block
    path = tmp_path / "synth.img"
    path.write_bytes(image)
    fake = SuotaFakeTransport()
    session = make_session(fake)
    spy = RequestSpy(session.bridge)

    result = session.run(path, ack_timeout=0.5)

    assert [len(c) for c in _patch_data_chunks(spy.noreply)] == [244] * 8 + [96]
    assert _patch_data_chunks(spy.blocking) == []  # zero per-chunk blocking
    assert session.bridge.orphan_responses == 9  # drained at the block-ACK pump
    assert session.bridge._responses == {}  # nothing left in the match table
    assert result.blocks == 1 and result.chunks == 9
    assert bytes(fake.received) == build_payload(path)  # byte-exact upload
    # IMAGE_STARTED + block ACK + END ACK
    assert fake.notified == [STATUS_IMAGE_STARTED, STATUS_CMD_SUCCESS, STATUS_CMD_SUCCESS]
    assert fake.patch_len_values == [2048]
    assert fake.rebooted is True


class ChunkFaultFake(SuotaFakeTransport):
    """Fault injection on PATCH_DATA chunk writes only (control writes keep
    their OK responses).  ``chunk_response=None`` models chunk frames lost
    between host and bridge (firmware CDC RX ring overflow): no verdict ever
    arrives.  Bytes (e.g. ``b"\\x69"`` = ENOBUFS) model the bridge answering a
    chunk-write error per ``bridge_ble.c:1000`` (``(uint8_t)(-err)``).
    """

    def __init__(self, chunk_response: Optional[bytes] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.chunk_response = chunk_response
        self._watch = StreamParser()  # pre-response inspection parse

    def write(self, data: bytes) -> None:
        frames = self._watch.feed(bytes(data))
        is_chunk_write = any(
            f.cmd == int(CMD.gatt_write)
            and len(f.payload) >= 3
            and struct.unpack_from("<H", f.payload, 1)[0] == _U16_PATCH_DATA
            for f in frames
        )
        if not is_chunk_write:
            super().write(data)
            return
        saved = self._responses.get(int(CMD.gatt_write))
        if self.chunk_response is None:
            self._responses.pop(int(CMD.gatt_write), None)  # stay silent
        else:
            self._responses[int(CMD.gatt_write)] = self.chunk_response
        try:
            super().write(data)
        finally:
            if saved is not None:
                self._responses[int(CMD.gatt_write)] = saved


def test_pipelined_missing_chunk_responses_window_scenarios(tmp_path: Path) -> None:
    """片写无响应（桥 RX ring 溢出丢帧的模型）三场景（fix round 1）：
    window=None 纯 fire-and-forget 照发不误；默认窗口在窗口末 join 快速失败
    （不再 30s 死等一个永远不会来的块 ACK）并给设备 ABORT 退出；阻塞路径
    第一片即超时。"""
    image = synthetic_image(full_blocks=0, tail=2047)
    path = tmp_path / "synth.img"
    path.write_bytes(image)

    # (a) unbounded fire-and-forget (legacy semantics, explicit opt-in)
    fake_a = ChunkFaultFake()
    piped = make_session(fake_a)
    result = piped.run(path, ack_timeout=0.5, pipeline_window=None)
    assert result.chunks == 9
    assert bytes(fake_a.received) == build_payload(path)
    assert piped.bridge.orphan_responses == 0  # no responses -> no orphans

    # (b) default windowed pipeline: fail fast on the window-end join
    fake_b = ChunkFaultFake()
    failing = make_session(fake_b, request_timeout=0.05)
    t0 = time.monotonic()
    with pytest.raises(SuotaError) as excinfo:
        failing.run(path, ack_timeout=0.5)
    assert time.monotonic() - t0 < 2.0  # join timeout, not the 30 s ACK hang
    assert excinfo.value.status_code == STATUS_TIMEOUT
    assert excinfo.value.phase == "block"
    assert "chunk" in excinfo.value.detail
    assert fake_b.aborted is True  # device mid-block: got its ABORT exit

    # (c) legacy blocking path: first chunk times out as before
    blocking = make_session(ChunkFaultFake(), request_timeout=0.05)
    with pytest.raises(SuotaError) as block_exc:
        blocking.run(path, ack_timeout=0.5, pipelined=False)
    assert block_exc.value.status_code == STATUS_TIMEOUT
    assert block_exc.value.phase == "block"


def test_pipelined_chunk_bridge_error_fails_fast(tmp_path: Path) -> None:
    """根因回归（fix round 1）：桥对片写回非零 status（BLE TX 背压等，
    bridge_ble.c:1000 语义）时，窗口末 join 立刻把它变成带桥 status 的
    SuotaError——不再是"孤儿吞掉 + 30s TIMEOUT"的不可归因失败。"""
    image = synthetic_image(full_blocks=0, tail=2047)
    path = tmp_path / "synth.img"
    path.write_bytes(image)
    fake = ChunkFaultFake(chunk_response=b"\x69")
    session = make_session(fake, request_timeout=0.05)

    t0 = time.monotonic()
    with pytest.raises(SuotaError) as excinfo:
        session.run(path, ack_timeout=0.5)
    wall = time.monotonic() - t0

    assert wall < 2.0  # surfaced at the first window-end join
    assert excinfo.value.status_code == 0x69
    assert excinfo.value.phase == "block"
    assert "bridge status 105" in excinfo.value.detail
    # 窗口内 3 片全部回 0x69：首片 join 抛出前，同窗响应都已被 pump 记账。
    assert session.bridge.orphan_errors == 3
    assert fake.aborted is False  # bridge verdict, no auto-ABORT (as legacy)


def test_pipelined_window_joins_bound_inflight(tmp_path: Path) -> None:
    """窗口语义（fix round 1 的核心保护）：在飞片数峰值永不超过
    pipeline_window（9 片 W=2 → 8 次窗口 join + 1 次块尾排空 = 9 次）；
    window=None → 零 join（修复前行为）；join 不走 request（全程仍零逐片
    阻塞请求），数据逐字节完整。"""
    image = synthetic_image(full_blocks=0, tail=2047)
    path = tmp_path / "synth.img"
    path.write_bytes(image)

    for window, expected_joins in ((2, 9), (None, 0)):
        fake = SuotaFakeTransport()
        session = make_session(fake)
        spy = RequestSpy(session.bridge)
        timeline: List[str] = []  # "send" / "join" in issue order
        orig_noreply = session.bridge.request_noreply  # spy's wrapper
        orig_join = session.bridge.join_noreply

        def counting_noreply(cmd, payload=b"", _orig=orig_noreply, _tl=timeline):
            _tl.append("send")
            return _orig(cmd, payload)

        def counting_join(seq, timeout=2.0, _orig=orig_join, _tl=timeline):
            _tl.append("join")
            return _orig(seq, timeout=timeout)

        session.bridge.request_noreply = counting_noreply  # type: ignore[method-assign]
        session.bridge.join_noreply = counting_join  # type: ignore[method-assign]

        result = session.run(path, ack_timeout=0.5, pipeline_window=window)

        inflight = 0
        max_inflight = 0
        for kind in timeline:
            if kind == "send":
                inflight += 1
                max_inflight = max(max_inflight, inflight)
            else:
                inflight -= 1
        assert max_inflight <= (window or 99)  # the ring-protection property
        assert len([e for e in timeline if e == "join"]) == expected_joins
        assert len(_patch_data_chunks(spy.noreply)) == 9
        assert _patch_data_chunks(spy.blocking) == []
        assert session.bridge.orphan_responses == 9
        assert result.chunks == 9
        assert bytes(fake.received) == build_payload(path)


def test_join_chunk_does_not_translate_host_bookkeeping_errors(tmp_path: Path) -> None:
    """M-2：join_noreply 的"SEQ 不在飞"RuntimeError 不被 _join_chunk 翻译成
    ack timeout，也不触发 ABORT——host 簿记 bug 原样上抛，不牵连设备。"""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=2047))
    fake = SuotaFakeTransport()
    session = make_session(fake)

    def not_in_flight(seq, timeout=2.0):
        raise RuntimeError(f"join_noreply: SEQ {seq} is not in flight")

    session.bridge.join_noreply = not_in_flight  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="not in flight"):
        session.run(path, ack_timeout=0.5)
    assert fake.aborted is False  # no MEM_DEV ABORT for a host-side bug
    assert fake.rebooted is False


def test_pipelined_window_validation_rejected_before_io(tmp_path: Path) -> None:
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=50))
    fake = SuotaFakeTransport()
    session = make_session(fake)

    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            session.run(path, ack_timeout=0.5, pipeline_window=bad)
    assert written_frames(fake) == []  # rejected before any bridge I/O


def test_pipelined_false_restores_per_chunk_blocking(tmp_path: Path) -> None:
    """pipelined=False（A/B 对照基线）：每片一个阻塞 request，逐片匹配响应，
    零孤儿；其余行为与流水化路径完全一致。"""
    image = synthetic_image(full_blocks=0, tail=2047)
    path = tmp_path / "synth.img"
    path.write_bytes(image)
    fake = SuotaFakeTransport()
    session = make_session(fake)
    spy = RequestSpy(session.bridge)

    result = session.run(path, ack_timeout=0.5, pipelined=False)

    assert [len(c) for c in _patch_data_chunks(spy.blocking)] == [244] * 8 + [96]
    assert _patch_data_chunks(spy.noreply) == []
    assert session.bridge.orphan_responses == 0  # every response was matched
    assert result.blocks == 1 and result.chunks == 9
    assert bytes(fake.received) == build_payload(path)
    # IMAGE_STARTED + block ACK + END ACK
    assert fake.notified == [STATUS_IMAGE_STARTED, STATUS_CMD_SUCCESS, STATUS_CMD_SUCCESS]


def test_pipelined_block_ack_remains_integrity_boundary(tmp_path: Path) -> None:
    """流水化不改变完整性边界：片全部发出后块 ACK 缺失仍走超时 + 自动
    ABORT，不会把已发片当成成功。"""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=0, tail=2047))
    fake = SuotaFakeTransport(block_ack=None)
    session = make_session(fake)

    with pytest.raises(SuotaError) as excinfo:
        session.run(path, ack_timeout=0.05)

    assert excinfo.value.status_code == STATUS_TIMEOUT
    assert excinfo.value.phase == "block"
    assert len(fake.received) == 2048  # all chunks went out (fire-and-forget)
    assert fake.aborted is True
    assert fake.rebooted is False


def test_pipelined_progress_and_multi_block_unchanged(tmp_path: Path) -> None:
    """流水化下每片仍推进本地字节计数：progress 每块一次、bytes 累计语义
    与阻塞路径逐位相同（4 块默认图）。"""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image(full_blocks=3, tail=100))
    fake = SuotaFakeTransport()
    session = make_session(fake)
    progress: List[SuotaProgress] = []
    result = session.run(path, progress_cb=progress.append, ack_timeout=0.5)

    assert result.blocks == 4 and result.chunks == 28
    assert [p.block for p in progress] == [1, 2, 3, 4]
    assert [p.bytes for p in progress] == [2048, 4096, 6144, 6245]
    assert bytes(fake.received) == build_payload(path)
    assert fake.notified == [0x10, 0x02, 0x02, 0x02, 0x02, 0x02]


# --------------------------------------- FR-B5: encryption guard (0x0F)


class EncryptionGuardFake(SuotaFakeTransport):
    """Reference-device MEM_DEV encryption guard: control writes answer ATT 0x0F
    (Insufficient Encryption, decimal 15 — distinct from SAME_IMAGE 0x15 = 21)
    until a CMD.pair request arrives and succeeds.

    Args:
        clear_on_pair: when True (default) a *successful* pairing lifts the
            guard; when False the retried write is rejected 0x0F again.
        pair_status: status byte answered to CMD.pair (0 = paired).
        retry_status: when set, post-pairing writes answer this status
            instead (e.g. 0x15: SAME_IMAGE once the link is encrypted).
    """

    def __init__(
        self,
        clear_on_pair: bool = True,
        pair_status: int = 0,
        retry_status: Optional[int] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.clear_on_pair = clear_on_pair
        self.pair_status = pair_status
        self.retry_status = retry_status
        self.paired = False
        self.mem_dev_refused = []  # MEM_DEV values rejected with 0x0F
        self._guard_on = True  # control writes currently answer 0x0F
        self._reject_model = True  # refused writes never reach the device model
        self._responses[int(CMD.gatt_write)] = bytes([0x0F])
        self._watch = StreamParser()  # pre-response inspection parse

    def write(self, data: bytes) -> None:
        # Inspect BEFORE super().write(): the guard decides this write's
        # table answer, and CMD.pair must be answered by the same write.
        for frame in self._watch.feed(bytes(data)):
            if frame.cmd == int(CMD.pair):
                self.paired = True
                self._responses[int(CMD.pair)] = (
                    b"\x00\x01\x02" if self.pair_status == 0 else bytes([self.pair_status])
                )
                if self.pair_status != 0:
                    pass  # pairing failed at the bridge: the guard stays up
                elif self.retry_status is not None:
                    self._guard_on = False  # 0x15 now, not 0x0F
                    self._responses[int(CMD.gatt_write)] = bytes([self.retry_status])
                elif self.clear_on_pair:
                    self._guard_on = False
                    self._reject_model = False
                    self._responses[int(CMD.gatt_write)] = b"\x00"
                # clear_on_pair=False: keep answering 0x0F
            elif (
                frame.cmd == int(CMD.gatt_write)
                and len(frame.payload) >= 3
                and self._guard_on
                and struct.unpack_from("<H", frame.payload, 1)[0] == _U16_MEM_DEV
            ):
                (value,) = struct.unpack("<I", frame.payload[3:])
                self.mem_dev_refused.append(value)
        super().write(data)

    def _on_gatt_write(self, write_type: int, u16: int, data: bytes) -> None:
        if self._reject_model and u16 == _U16_MEM_DEV:
            # Refused write (0x0F / post-pair 0x15): the ATT layer rejected
            # it, so the device-side SUOTA model never sees the value and never
            # notifies IMAGE_STARTED for it.
            return
        super()._on_gatt_write(write_type, u16, data)


def test_mem_dev_0x0f_pairs_and_retry_succeeds(tmp_path: Path) -> None:
    """Reference device (measured): MEM_DEV write -> ATT 0x0F; the session pairs
    (CMD.pair) once and the retried write succeeds; the update completes."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = EncryptionGuardFake()
    session = make_session(fake)

    result = session.run(path, ack_timeout=0.5)

    assert result.blocks == 4
    assert fake.paired is True
    assert fake.mem_dev_refused == [0x13000000]  # first start write refused
    assert fake.mem_dev_values == [0x13000000, 0xFE000000, 0xFD000000]
    assert fake.notified[0] == STATUS_IMAGE_STARTED  # encrypted link: notify lands
    assert fake.rebooted is True
    pair_frames = [f for f in written_frames(fake) if f.cmd == int(CMD.pair)]
    assert len(pair_frames) == 1  # exactly one pairing attempt


def test_mem_dev_0x0f_retry_still_0x0f_raises_encryption_required(tmp_path: Path) -> None:
    """Guard survives pairing (retry answers 0x0F again) -> EncryptionRequired,
    no ABORT and no data upload (the device never entered image mode)."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = EncryptionGuardFake(clear_on_pair=False)
    session = make_session(fake)

    with pytest.raises(EncryptionRequired) as excinfo:
        session.run(path)

    assert isinstance(excinfo.value, SuotaError)
    assert excinfo.value.status_code == ATT_ERR_INSUFFICIENT_ENCRYPTION
    assert excinfo.value.phase == "mem_dev"
    assert "still rejected 0x0F" in excinfo.value.detail
    assert fake.paired is True
    assert fake.mem_dev_refused == [0x13000000, 0x13000000]  # refused twice
    assert fake.received == bytearray()  # never reached the upload
    assert fake.aborted is False
    assert fake.rebooted is False


def test_mem_dev_0x0f_pair_failure_raises_encryption_required(tmp_path: Path) -> None:
    """Bridge answers CMD.pair with 0x75 (ERR_PAIR_FAILED) -> EncryptionRequired
    carrying the bridge status, surfaced verbatim from the guard path."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = EncryptionGuardFake(pair_status=0x75)
    session = make_session(fake)

    with pytest.raises(EncryptionRequired) as excinfo:
        session.run(path)

    assert excinfo.value.status_code == ATT_ERR_INSUFFICIENT_ENCRYPTION
    assert excinfo.value.phase == "mem_dev"
    assert "bridge status 117" in excinfo.value.detail  # 0x75 = 117
    assert fake.rebooted is False


def test_run_always_discovers_service_first(tmp_path: Path) -> None:
    """Host-triggered discovery (generic bridge firmware): every run opens
    with disc_svc(0xFEF5) — payload f5 fe — before any GATT access."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = SuotaFakeTransport()
    session = make_session(fake)

    session.run(path, ack_timeout=0.5)

    frames = written_frames(fake)
    assert frames[0].cmd == int(CMD.disc_svc)
    assert frames[0].payload == b"\xf5\xfe"  # 0xFEF5, u16 LE
    gatt_cmds = (int(CMD.gatt_read), int(CMD.gatt_write), int(CMD.subscribe))
    assert all(f.cmd not in gatt_cmds for f in frames[:1])
    assert any(f.cmd == gatt_cmds[0] for f in frames[1:])  # flow continued


def test_discover_retries_once_on_legacy_0x74_transition_window(
    monkeypatch, tmp_path: Path
) -> None:
    """评审 I-1：旧自动发现固件在 connect 期 dm 完成前对 disc_svc 回 0x74
    （NOT_READY）。_discover 延迟一次后重发，第二发成功，整个流程照常完成；
    恰好两次 disc_svc，无第三次尝试。"""
    monkeypatch.setattr("blebridge.suota._DISCOVER_RETRY_DELAY_S", 0.0)

    class Legacy74Fake(SuotaFakeTransport):
        """disc_svc 第一发回答 0x74，第二发起变为 0x00 + nchars=10。"""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.disc_svc_attempts = 0
            self._responses[int(CMD.disc_svc)] = bytes([0x74])

        def write(self, data: bytes) -> None:
            for frame in StreamParser().feed(bytes(data)):
                if frame.cmd == int(CMD.disc_svc):
                    self.disc_svc_attempts += 1
                    if self.disc_svc_attempts >= 2:
                        self._responses[int(CMD.disc_svc)] = b"\x00\x0a\x00"
            super().write(data)

    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = Legacy74Fake()
    session = make_session(fake)

    result = session.run(path, ack_timeout=0.5)

    assert result.blocks == 4
    assert fake.disc_svc_attempts == 2  # exactly one retry, no third
    assert fake.rebooted is True  # the flow continued past discovery


def test_run_pair_flag_pairs_after_discovery_before_any_gatt(tmp_path: Path) -> None:
    """run(..., pair=True) is a pairing PREREQUISITE (stress R1/R2): the wire
    order is disc_svc -> CMD.pair -> any GATT access; the whole flow then
    completes normally with the guard pre-emptively lifted."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = EncryptionGuardFake()
    session = make_session(fake)

    result = session.run(path, pair=True, ack_timeout=0.5)

    frames = written_frames(fake)
    cmds = [f.cmd for f in frames]
    assert cmds[0] == int(CMD.disc_svc)  # discovery first (generic fw)
    assert int(CMD.pair) in cmds[1:3]  # then the pairing prerequisite
    gatt_cmds = (int(CMD.gatt_read), int(CMD.gatt_write), int(CMD.subscribe))
    pair_idx = cmds.index(int(CMD.pair))
    assert all(f.cmd not in gatt_cmds for f in frames[:pair_idx])
    assert fake.paired is True
    assert fake.mem_dev_refused == []  # guard already lifted pre-emptively
    assert result.blocks == 4
    assert fake.rebooted is True


def test_run_pair_prerequisite_failure_raises_before_any_gatt(tmp_path: Path) -> None:
    """run(..., pair=True) with the bridge answering 0x75: EncryptionRequired
    in phase "pair", before a single GATT access leaves the bridge."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = EncryptionGuardFake(pair_status=0x75)
    session = make_session(fake)

    with pytest.raises(EncryptionRequired) as excinfo:
        session.run(path, pair=True, ack_timeout=0.5)

    assert excinfo.value.phase == "pair"
    assert "bridge status 117" in excinfo.value.detail  # 0x75 = 117
    frames = written_frames(fake)
    cmds = [f.cmd for f in frames]
    assert cmds[0] == int(CMD.disc_svc)
    # CMD.pair follows (the session-start credits grant may interleave);
    # no GATT access happens before or instead of it.
    assert int(CMD.pair) in cmds[1:3]
    gatt_cmds = (int(CMD.gatt_read), int(CMD.gatt_write), int(CMD.subscribe))
    assert all(f.cmd not in gatt_cmds for f in frames[: cmds.index(int(CMD.pair))])
    assert fake.received == bytearray()
    assert fake.rebooted is False


# --------------------------------------- authorization guard (0x78, CCCD)


class AuthorizationGuardFake(SuotaFakeTransport):
    """Rebooted-device CCCD guard (measured bisection): CMD.subscribe
    answers ATT 0x78 (Insufficient Authorization) until ONE MEM_DEV
    transaction completes on this connection — the unlock probe's start
    write (0x13000000|bank) is that transaction.  Pairing does NOT lift
    the guard, and while locked the device never enters image mode (no 0x10
    notification, MEM_DEV writes swallowed).

    Args:
        clear_on_unlock: when True (default) the probe's start write lifts
            the guard; when False the retried subscribe is rejected 0x78
            again.
    """

    def __init__(self, clear_on_unlock: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.clear_on_unlock = clear_on_unlock
        self.subscribe_refused = 0  # subscribes rejected with 0x78
        self.unlock_start_values = []  # MEM_DEV start writes seen while locked
        self._guard_on = True
        self._responses[int(CMD.subscribe)] = bytes([ATT_ERR_INSUFFICIENT_AUTHORIZATION])
        self._watch = StreamParser()  # pre-response inspection parse

    def write(self, data: bytes) -> None:
        for frame in self._watch.feed(bytes(data)):
            if frame.cmd == int(CMD.subscribe) and self._guard_on:
                self.subscribe_refused += 1
        super().write(data)

    def _on_gatt_write(self, write_type: int, u16: int, data: bytes) -> None:
        if u16 == _U16_MEM_DEV and len(data) == 4 and self._guard_on:
            (value,) = struct.unpack("<I", data)
            self.unlock_start_values.append(value)
            if self.clear_on_unlock:
                self._guard_on = False  # one completed transaction unlocks
                self._responses[int(CMD.subscribe)] = b"\x00"
            # Locked state: swallow MEM_DEV writes entirely (no image mode,
            # no IMAGE_STARTED notification, no abort bookkeeping).
            return
        super()._on_gatt_write(write_type, u16, data)


def test_subscribe_0x78_mem_dev_unlock_then_subscribe_succeeds(tmp_path: Path) -> None:
    """Rebooted device (measured bisection): CCCD subscribe -> ATT 0x78; the
    session runs one MEM_DEV-transaction unlock (start write + ABORT roll-
    back), the retried subscribe succeeds, the update completes."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = AuthorizationGuardFake()
    session = make_session(fake)

    result = session.run(path, ack_timeout=0.5)

    assert result.blocks == 4
    assert fake.subscribe_refused == 1  # exactly one refused attempt...
    subscribes = [f for f in written_frames(fake) if f.cmd == int(CMD.subscribe)]
    assert len(subscribes) == 2  # ...and exactly one retry
    assert fake.unlock_start_values == [0x13000000]  # probe start write
    assert fake.aborted is True  # the probe's ABORT rollback went out
    assert fake.notified[0] == STATUS_IMAGE_STARTED  # real start notify
    assert fake.rebooted is True


def test_subscribe_0x78_unlock_retry_still_0x78_raises_encryption_required(tmp_path: Path) -> None:
    """Guard survives the MEM_DEV unlock (retry answers 0x78 again) ->
    EncryptionRequired carrying the 0x78 verdict, no data upload and no
    third subscribe attempt."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = AuthorizationGuardFake(clear_on_unlock=False)
    session = make_session(fake)

    with pytest.raises(EncryptionRequired) as excinfo:
        session.run(path, ack_timeout=0.5)

    assert isinstance(excinfo.value, SuotaError)
    assert excinfo.value.status_code == ATT_ERR_INSUFFICIENT_AUTHORIZATION
    assert excinfo.value.phase == "subscribe"
    assert "still rejected 0x78 after MEM_DEV unlock" in excinfo.value.detail
    assert fake.subscribe_refused == 2  # refused twice, no third attempt
    # exactly one probe start write (the ABORT rollback also shows up
    # here because the guard was still on and swallowing MEM_DEV writes)
    assert fake.unlock_start_values.count(0x13000000) == 1
    assert fake.received == bytearray()
    assert fake.rebooted is False


def test_same_image_still_raised_when_encrypted_write_refuses_0x15(tmp_path: Path) -> None:
    """0x0F pairing then a 0x15 retry (running image re-requested): the SAME
    IMAGE verdict wins — SameImageError, never EncryptionRequired (0x15 = 21
    vs 0x0F = 15 stay distinct on both paths)."""
    path = tmp_path / "synth.img"
    path.write_bytes(synthetic_image())
    fake = EncryptionGuardFake(retry_status=0x15)
    session = make_session(fake)

    with pytest.raises(SameImageError) as excinfo:
        session.run(path, ack_timeout=0.5)

    assert excinfo.value.status_code == STATUS_SAME_IMAGE
    assert excinfo.value.phase == "mem_dev"
    assert fake.paired is True
    assert fake.mem_dev_refused == [0x13000000]  # exactly one 0x0F refusal
