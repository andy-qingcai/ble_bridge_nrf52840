"""Tests for blebridge.transport: lazy serial open, context manager, FakeTransport.

No real serial port is ever touched: ``serial.Serial`` is monkeypatched with a
recording fake, and FakeTransport is exercised directly.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Dict, List

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

import blebridge.transport as transport_module  # noqa: E402
from blebridge.bridge import CMD, EVT, BridgeHost  # noqa: E402
from blebridge.framing import EVENT_CMDS, StreamParser, encode_frame  # noqa: E402
from blebridge.transport import (  # noqa: E402
    FakeTransport,
    SerialTransport,
    Transport,
    TransportClosed,
)


class FakeSerialPort:
    """Stands in for serial.Serial; records constructor kwargs and I/O."""

    instances: List["FakeSerialPort"] = []
    fail_io: bool = False  # when True, read/write raise SerialException

    def __init__(self, port: str, baudrate: int, rtscts: bool, timeout: float) -> None:
        self.port = port
        self.baudrate = baudrate
        self.rtscts = rtscts
        self.timeout = timeout
        self.is_open = True
        self.rx: bytearray = bytearray()  # bytes delivered to read()
        self.written: bytearray = bytearray()
        FakeSerialPort.instances.append(self)

    def write(self, data: bytes) -> int:
        if FakeSerialPort.fail_io:
            raise transport_module.serial.SerialException("port died")
        self.written += data
        return len(data)

    def read(self, n: int) -> bytes:
        if FakeSerialPort.fail_io:
            raise transport_module.serial.SerialException("port died")
        chunk = bytes(self.rx[:n])
        del self.rx[:n]
        return chunk

    def close(self) -> None:
        self.is_open = False


@pytest.fixture()
def fake_serial(monkeypatch: pytest.MonkeyPatch) -> List[FakeSerialPort]:
    FakeSerialPort.instances = []
    FakeSerialPort.fail_io = False
    monkeypatch.setattr(transport_module.serial, "Serial", FakeSerialPort)
    return FakeSerialPort.instances


# --------------------------------------------------------------------- ABC


def test_transport_is_abstract() -> None:
    with pytest.raises(TypeError):
        Transport()  # type: ignore[abstract]


# ---------------------------------------------------------- SerialTransport


def test_serial_transport_lazy_open(fake_serial: List[FakeSerialPort]) -> None:
    t = SerialTransport("/dev/ttyFAKE", baudrate=115200, rtscts=False, timeout=0.2)
    assert t.is_open is False
    assert fake_serial == []  # constructor must not touch serial.Serial
    t.open()
    assert t.is_open is True
    assert len(fake_serial) == 1
    port = fake_serial[0]
    assert port.port == "/dev/ttyFAKE"
    assert port.baudrate == 115200
    assert port.rtscts is False
    assert port.timeout == pytest.approx(0.2)


def test_serial_transport_open_idempotent(fake_serial: List[FakeSerialPort]) -> None:
    t = SerialTransport("/dev/ttyFAKE")
    t.open()
    t.open()
    assert len(fake_serial) == 1


def test_serial_transport_default_line_settings(
    fake_serial: List[FakeSerialPort],
) -> None:
    t = SerialTransport("/dev/ttyFAKE")
    t.open()
    port = fake_serial[0]
    assert port.baudrate == 921600  # FR-A5 default
    assert port.rtscts is True
    assert port.timeout == pytest.approx(0.1)


def test_serial_transport_close_reopen_makes_new_port(
    fake_serial: List[FakeSerialPort],
) -> None:
    t = SerialTransport("/dev/ttyFAKE")
    t.open()
    t.close()
    assert t.is_open is False
    assert fake_serial[0].is_open is False
    t.close()  # idempotent
    assert t.is_open is False
    t.open()
    assert len(fake_serial) == 2  # fresh handle for the reopen
    assert t.is_open is True


def test_serial_transport_context_manager(fake_serial: List[FakeSerialPort]) -> None:
    with SerialTransport("/dev/ttyFAKE") as t:
        assert t.is_open is True
    assert t.is_open is False


def test_serial_transport_write_and_read_delegate(
    fake_serial: List[FakeSerialPort],
) -> None:
    t = SerialTransport("/dev/ttyFAKE")
    t.open()
    port = fake_serial[0]
    t.write(b"\x01\x02")
    assert port.written == b"\x01\x02"
    port.rx += b"abc"
    assert t.read(2, timeout=0.0) == b"ab"
    assert t.read(10, timeout=0.0) == b"c"
    assert t.read(10, timeout=0.0) == b""  # timeout -> b"", not an error
    # per-call timeout overrides the constructor default on the port object
    port.rx += b"xyz"
    t.read(3, timeout=0.05)
    assert port.timeout == pytest.approx(0.05)


def test_serial_transport_io_on_closed_raises(
    fake_serial: List[FakeSerialPort],
) -> None:
    t = SerialTransport("/dev/ttyFAKE")
    with pytest.raises(TransportClosed):
        t.write(b"x")
    with pytest.raises(TransportClosed):
        t.read(1, timeout=0.0)


def test_serial_transport_dead_port_surfaces_as_transport_closed(
    fake_serial: List[FakeSerialPort],
) -> None:
    t = SerialTransport("/dev/ttyFAKE")
    t.open()
    FakeSerialPort.fail_io = True  # simulate unplug mid-session
    with pytest.raises(TransportClosed):
        t.read(1, timeout=0.0)
    with pytest.raises(TransportClosed):
        t.write(b"x")


def test_serial_transport_open_failure_surfaces_as_transport_closed(
    fake_serial: List[FakeSerialPort], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_constructor(port, baudrate, rtscts, timeout):
        raise transport_module.serial.SerialException("no such port")

    monkeypatch.setattr(transport_module.serial, "Serial", broken_constructor)
    t = SerialTransport("/dev/ttyNOPE")
    with pytest.raises(TransportClosed):
        t.open()
    assert t.is_open is False  # stays closed; retry may succeed later


# -------------------------------------------------- open settle (U-1 glitch)
#
# The suite-wide default settle window is 0 (conftest autouse fixture);
# these tests opt back in with small windows (0.1-0.2 s) via monkeypatch.


def test_open_settle_seconds_defaults_to_0_6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRIDGE_OPEN_SETTLE_S", raising=False)
    assert transport_module.open_settle_seconds() == pytest.approx(0.6)


def test_open_settle_seconds_env_override_and_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0.25")
    assert transport_module.open_settle_seconds() == pytest.approx(0.25)
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0")
    assert transport_module.open_settle_seconds() == 0.0  # 0 disables settle


def test_open_settle_seconds_invalid_or_negative_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "nonsense")
    assert transport_module.open_settle_seconds() == pytest.approx(0.6)  # fallback
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "-5")
    assert transport_module.open_settle_seconds() == 0.0  # clamped, never negative


def _patch_glitchy_port(
    monkeypatch: pytest.MonkeyPatch, glitch: bytes = b"\x55\x02\x00\xff\x00"
) -> None:
    """Make every opened serial port start with ``glitch`` already buffered."""
    original_init = FakeSerialPort.__init__

    def init_with_glitch(self, port, baudrate, rtscts, timeout):
        original_init(self, port, baudrate, rtscts, timeout)
        self.rx += glitch

    monkeypatch.setattr(FakeSerialPort, "__init__", init_with_glitch)


def test_serial_transport_open_settle_drains_glitch_bytes(
    fake_serial: List[FakeSerialPort], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0.2")
    _patch_glitchy_port(monkeypatch)
    t = SerialTransport("/dev/ttyFAKE")
    t.open()
    assert fake_serial[0].rx == b""  # glitch silently read and discarded
    assert t.read(4096, timeout=0.0) == b""  # nothing leaks to the first I/O


def test_serial_transport_settle_waits_out_window_on_quiet_port(
    fake_serial: List[FakeSerialPort], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0.15")

    class QuietPort(FakeSerialPort):
        def read(self, n: int) -> bytes:
            time.sleep(0.15)  # quiet link: read blocks out the whole window
            return b""

    monkeypatch.setattr(transport_module.serial, "Serial", QuietPort)
    t = SerialTransport("/dev/ttyFAKE")
    start = time.monotonic()
    t.open()
    elapsed = time.monotonic() - start
    assert 0.15 <= elapsed < 1.0  # window honoured, not spun through instantly


def test_serial_transport_settle_bounded_under_never_ending_stream(
    fake_serial: List[FakeSerialPort], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0.15")

    class FirehosePort(FakeSerialPort):
        def read(self, n: int) -> bytes:
            return b"\xaa" * n  # glitch bytes forever

    monkeypatch.setattr(transport_module.serial, "Serial", FirehosePort)
    t = SerialTransport("/dev/ttyFAKE")
    start = time.monotonic()
    t.open()
    elapsed = time.monotonic() - start
    assert 0.15 <= elapsed < 1.0  # deadline stops the drain, port stays open
    assert t.is_open is True


def test_serial_transport_explicit_settle_s_overrides_env(
    fake_serial: List[FakeSerialPort], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_glitchy_port(monkeypatch)
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0")  # env says: no settle
    t = SerialTransport("/dev/ttyFAKE", settle_s=0.2)  # explicit wins
    t.open()
    assert fake_serial[0].rx == b""  # settle ran despite env disable

    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0.2")  # env asks for settle
    t2 = SerialTransport("/dev/ttyFAKE", settle_s=0.0)  # explicit 0 wins
    t2.open()
    assert fake_serial[1].rx != b""  # glitch retained: settle skipped


def test_bridge_first_request_survives_open_glitch(
    fake_serial: List[FakeSerialPort], monkeypatch: pytest.MonkeyPatch
) -> None:
    """U-1 acceptance: glitch frame at open + normal response -> first
    request matches its response (glitch consumed by the settle window)."""
    monkeypatch.setenv("BRIDGE_OPEN_SETTLE_S", "0.1")
    _patch_glitchy_port(monkeypatch)  # frame-shaped glitch: 0x55 SOF + junk
    original_write = FakeSerialPort.write

    def write_with_ping_response(self, data: bytes) -> int:
        result = original_write(self, data)
        for frame in StreamParser().feed(bytes(data)):
            if frame.cmd == int(CMD.ping):  # credits resync frame gets none
                self.rx += encode_frame(
                    int(CMD.ping) | 0x80, frame.seq, b"\x00\x2a"
                )
        return result

    monkeypatch.setattr(FakeSerialPort, "write", write_with_ping_response)

    t = SerialTransport("/dev/ttyFAKE")
    t.open()
    host = BridgeHost(t)
    assert host.request(int(CMD.ping), timeout=0.5) == (0, b"\x2a")


# ------------------------------------------------------------ FakeTransport


def test_fake_transport_open_close_is_open() -> None:
    t = FakeTransport()
    assert t.is_open is False
    t.open()
    assert t.is_open is True
    t.close()
    assert t.is_open is False
    t.close()  # idempotent


def test_fake_transport_records_written_and_generates_response() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00\x01\x02"})
    t.open()
    request = encode_frame(int(CMD.ping), 7, b"")
    t.write(request)
    assert t.written == request

    raw = t.read(4096, timeout=0.0)
    frames = StreamParser().feed(raw)
    assert len(frames) == 1
    resp = frames[0]
    assert resp.cmd == int(CMD.ping) | 0x80
    assert resp.seq == 7  # SEQ echoed
    assert resp.payload == b"\x00\x01\x02"  # status byte included, verbatim
    assert not resp.is_event


def test_fake_transport_no_response_for_unknown_cmd() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    t.open()
    t.write(encode_frame(int(CMD.info), 1, b""))  # not in table -> silence
    assert t.read(4096, timeout=0.0) == b""
    assert t.pending() == 0


def test_fake_transport_no_response_for_events_or_response_cmds() -> None:
    responses: Dict[int, bytes] = {int(CMD.ping): b"\x00"}
    t = FakeTransport(responses=responses)
    t.open()
    t.write(encode_frame(int(EVT.SCAN), 3, b"\x01"))  # event cmd: no echo
    t.write(encode_frame(int(CMD.ping) | 0x80, 3, b"\x00"))  # already a response
    assert t.read(4096, timeout=0.0) == b""


def test_fake_transport_script_queued_in_order() -> None:
    f1 = encode_frame(int(EVT.SCAN), 0, b"\x01a")
    f2 = encode_frame(int(EVT.SCAN), 1, b"\x01b")
    t = FakeTransport(script=[f1, f2])
    t.open()
    assert t.read(3, timeout=0.0) == f1[:3]  # chunked reads slice the stream
    assert t.read(4096, timeout=0.0) == f1[3:] + f2


def test_fake_transport_read_empty_returns_empty_bytes() -> None:
    t = FakeTransport()
    t.open()
    assert t.read(16, timeout=0.5) == b""  # timeout accepted, never slept on
    assert t.read(0, timeout=0.0) == b""


def test_fake_transport_io_on_closed_raises() -> None:
    t = FakeTransport()
    with pytest.raises(TransportClosed):
        t.write(b"x")
    with pytest.raises(TransportClosed):
        t.read(1, timeout=0.0)


def test_fake_transport_inject_raw_supports_garbage_plus_frames() -> None:
    t = FakeTransport()
    t.open()
    good = encode_frame(int(EVT.DEBUG), 9, b"\x02hello")
    # 55 AA AA: bogus LEN > 512 -> exactly 3 bytes dropped, good frame intact.
    t.inject_raw(b"\x00\xff" + b"\x55\xaa\xaa" + good)
    raw = t.read(4096, timeout=0.0)
    parser = StreamParser()
    frames = parser.feed(raw)
    assert parser.resyncs == 1
    assert len(frames) == 1
    assert frames[0].cmd == int(EVT.DEBUG)
    assert frames[0].payload == b"\x02hello"


def test_fake_transport_context_manager() -> None:
    t = FakeTransport(responses={int(CMD.ping): b"\x00"})
    with t:
        assert t.is_open is True
        t.write(encode_frame(int(CMD.ping), 1, b""))
        assert t.read(4096, timeout=0.0) != b""
    assert t.is_open is False


def test_all_cmd_constants_inside_request_space() -> None:
    # Guard against accidentally assigning a CMD into the event window.
    for member in CMD:
        assert int(member) not in EVENT_CMDS
        assert 0x01 <= int(member) <= 0x5F
