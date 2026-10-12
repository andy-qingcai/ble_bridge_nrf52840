"""Tests for blebridge.cli — the ``blebridge`` console script (Task 4).

Subcommands are driven against FakeTransport-backed BridgeHosts by
monkeypatching :func:`blebridge.cli.open_bridge`; output is asserted via
``capsys``.  No serial hardware, no real waiting beyond sub-0.1 s windows.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Dict

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from blebridge import cli  # noqa: E402
from blebridge.bridge import CMD, EVT, BridgeHost  # noqa: E402
from blebridge.framing import StreamParser, encode_frame  # noqa: E402
from blebridge.suota import SUOTA_UUIDS  # noqa: E402
from blebridge.transport import FakeTransport  # noqa: E402

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS))

from suota_fake import SuotaFakeTransport, synthetic_image  # noqa: E402

GOOD_RESPONSES: Dict[int, bytes] = {
    int(CMD.ping): b"\x00\x01",
    int(CMD.info): b"\x00" + b"blebridge-v2",
    int(CMD.scan_start): b"\x00",
    int(CMD.scan_stop): b"\x00",
    int(CMD.connect): b"\x00\x01",
    int(CMD.disconnect): b"\x00",
    int(CMD.gatt_read): b"\x00\xde\xad",
    int(CMD.gatt_write): b"\x00",
    int(CMD.subscribe): b"\x00",
    int(CMD.unsubscribe): b"\x00",
    int(CMD.disc_svc): b"\x00\x0a\x00",
    int(CMD.conn_update): b"\x00",
    int(CMD.phy): b"\x00",
    int(CMD.adv_start): b"\x00",
    int(CMD.adv_stop): b"\x00",
    int(CMD.notify): b"\x00",
    int(CMD.set_char): b"\x00",
    int(CMD.gatt_define): b"\x00\x40\x00",
}


class CliFake(FakeTransport):
    def __init__(self) -> None:
        super().__init__(responses=dict(GOOD_RESPONSES))


@pytest.fixture
def fake(monkeypatch) -> CliFake:
    inst = CliFake()
    monkeypatch.setattr(
        cli, "open_bridge", lambda port, **kwargs: BridgeHost(_already_open(inst))
    )
    return inst


def _already_open(fake: FakeTransport) -> FakeTransport:
    fake.open()
    return fake


def written_frames(fake: FakeTransport) -> list:
    return StreamParser().feed(bytes(fake.written))


def test_console_script_entry_declared_in_pyproject() -> None:
    text = (_TESTS.parent / "pyproject.toml").read_text()
    assert 'blebridge = "blebridge.cli:main"' in text


def test_parser_routes_every_subcommand() -> None:
    parser = cli.build_parser()
    for argv in (
        ["info"],
        ["scan", "--duration", "1"],
        ["connect", "E0:F1:00:02:03:04"],
        ["connect", "--name", "dev"],
        ["disconnect"],
        ["write", "--uuid", "f5fe", "--data", "aa"],
        ["write", "--handle", "0x12", "--data", "aa"],
        ["read", "--uuid", "f5fe"],
        ["read", "--handle", "18"],
        ["subscribe", "--uuid", "df94"],
        ["subscribe", "--handle", "18", "--cccd", "19"],
        ["unsubscribe", "--uuid", "df94"],
        ["unsubscribe", "--handle", "18"],
        ["disc-svc"],
        ["disc-svc", "--uuid", "fef5"],
        ["conn-update"],
        ["conn-update", "--interval-min", "16", "--interval-max", "32",
         "--latency", "1", "--timeout", "500"],
        ["phy", "2"],
        ["adv-start"],
        ["adv-start", "--interval", "250", "--no-name"],
        ["adv-stop"],
        ["notify", "--data", "aabb"],
        ["notify", "--data", "aa", "--uuid", "ffe2"],
        ["set-char", "--data", "0102"],
        ["set-char", "--data", "01", "--uuid", "0xFFA2"],
        ["gatt-define", "--uuid", "ffa0", "--chars",
         '[{"uuid16": "ffa1", "read": true, "notify": true, "max_len": 64}]'],
        ["drain"],
        ["events"],
        ["suota", "fw.bin"],
        ["monitor", "--duration", "0.05"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func)


def test_info_command(fake, capsys) -> None:
    assert cli.main(["--port", "FAKE", "info"]) == 0
    out = capsys.readouterr().out
    assert "status=0" in out
    assert b"blebridge-v2".hex() in out


def test_read_and_write_dispatch_wire_payloads(fake, capsys) -> None:
    assert cli.main(["--port", "FAKE", "read", "--uuid", "f5fe"]) == 0
    assert cli.main(
        ["--port", "FAKE", "write", "--uuid", "f5fe", "--data", "0a0b"]
    ) == 0
    assert cli.main(
        [
            "--port", "FAKE", "write", "--uuid", "f5fe", "--data", "0a",
            "--without-response",
        ]
    ) == 0
    frames = written_frames(fake)
    reads = [f for f in frames if f.cmd == int(CMD.gatt_read)]
    writes = [f for f in frames if f.cmd == int(CMD.gatt_write)]
    assert reads[0].payload == b"\xfe\xf5"
    assert writes[0].payload == b"\x00\xfe\xf5\x0a\x0b"
    assert writes[1].payload == b"\x01\xfe\xf5\x0a"
    assert "dead" in capsys.readouterr().out  # read value printed as hex


def test_subscribe_uses_cccd_uuid(fake, capsys) -> None:
    assert cli.main(["--port", "FAKE", "subscribe", "--uuid", SUOTA_UUIDS["serv_status"]]) == 0
    (sub,) = [f for f in written_frames(fake) if f.cmd == int(CMD.subscribe)]
    assert sub.payload == b"\x78\x5f"  # uuid16 prefix 0x5F78 LE of 5f78df94-...
    assert "subscribe status=0" in capsys.readouterr().out


def test_peripheral_commands_wire_payloads(fake, capsys) -> None:
    """FR-H: adv-start/adv-stop/notify/set-char/gatt-define subcommands put
    the contract payload bytes on the wire."""
    assert cli.main(["--port", "FAKE", "adv-start"]) == 0
    assert cli.main(["--port", "FAKE", "adv-start", "--interval", "250", "--no-name"]) == 0
    assert cli.main(["--port", "FAKE", "adv-stop"]) == 0
    assert cli.main(["--port", "FAKE", "notify", "--data", "aabb"]) == 0
    assert cli.main(["--port", "FAKE", "notify", "--data", "aa", "--uuid", "ffe2"]) == 0
    assert cli.main(["--port", "FAKE", "set-char", "--data", "0102"]) == 0
    assert cli.main(["--port", "FAKE", "set-char", "--data", "01", "--uuid", "0xFFA2"]) == 0
    assert cli.main(
        [
            "--port", "FAKE", "gatt-define", "--uuid", "ffa0", "--chars",
            '[{"uuid16": "ffa1", "read": true, "notify": true, "max_len": 64}]',
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "gatt_define status=0 base_handle=64" in out  # response 0x40
    starts = [f for f in written_frames(fake) if f.cmd == int(CMD.adv_start)]
    assert [f.payload for f in starts] == [
        b"\xff\xff\x01",
        struct.pack("<H", 250) + b"\x00",
    ]
    (stop,) = [f for f in written_frames(fake) if f.cmd == int(CMD.adv_stop)]
    assert stop.payload == b""
    notifies = [f for f in written_frames(fake) if f.cmd == int(CMD.notify)]
    assert [f.payload for f in notifies] == [b"\x00\xaa\xbb", b"\x01\xe2\xff\xaa"]
    sets = [f for f in written_frames(fake) if f.cmd == int(CMD.set_char)]
    assert [f.payload for f in sets] == [b"\x00\x01\x02", b"\x01\xa2\xff\x01"]
    (define,) = [f for f in written_frames(fake) if f.cmd == int(CMD.gatt_define)]
    assert define.payload == (
        b"\xa0\xff\x01" + b"\xa1\xff\x09\x40\x00"
    )


def test_handle_addressing_wire_payloads(fake, capsys) -> None:
    """read/write/subscribe --handle switch to the 0x0000 handle encoding."""
    assert cli.main(["--port", "FAKE", "read", "--handle", "0x1234"]) == 0
    assert cli.main(["--port", "FAKE", "write", "--handle", "0x11", "--data", "0a0b"]) == 0
    assert cli.main(
        ["--port", "FAKE", "subscribe", "--handle", "42", "--cccd", "43"]
    ) == 0
    assert cli.main(["--port", "FAKE", "unsubscribe", "--handle", "42"]) == 0
    reads = [f for f in written_frames(fake) if f.cmd == int(CMD.gatt_read)]
    assert reads[0].payload == b"\x00\x00\x34\x12"
    writes = [f for f in written_frames(fake) if f.cmd == int(CMD.gatt_write)]
    assert writes[0].payload == b"\x00\x00\x00\x00\x11\x00\x0a\x0b"
    subs = [f for f in written_frames(fake) if f.cmd == int(CMD.subscribe)]
    assert subs[0].payload == b"\x00\x00\x2a\x00\x2b\x00"
    uns = [f for f in written_frames(fake) if f.cmd == int(CMD.unsubscribe)]
    assert uns[0].payload == b"\x00\x00\x2a\x00"


def test_disc_svc_conn_update_phy_commands(fake, capsys) -> None:
    assert cli.main(["--port", "FAKE", "disc-svc", "--uuid", "fef5"]) == 0
    assert "nchars=10" in capsys.readouterr().out
    assert cli.main(
        ["--port", "FAKE", "conn-update", "--interval-min", "16",
         "--interval-max", "32", "--latency", "1", "--timeout", "500"]
    ) == 0
    assert cli.main(["--port", "FAKE", "phy", "2"]) == 0
    disc = [f for f in written_frames(fake) if f.cmd == int(CMD.disc_svc)]
    assert disc[0].payload == b"\xf5\xfe"
    upd = [f for f in written_frames(fake) if f.cmd == int(CMD.conn_update)]
    assert upd[0].payload == struct.pack("<HHHH", 16, 32, 1, 500)
    phy = [f for f in written_frames(fake) if f.cmd == int(CMD.phy)]
    assert phy[0].payload == b"\x02"


def test_read_without_uuid_or_handle_exits_gracefully(fake, capsys) -> None:
    assert cli.main(["--port", "FAKE", "read"]) == 2
    err = capsys.readouterr().err
    assert "error" in err and "--uuid" in err


def test_connect_by_address_and_name(fake, capsys) -> None:
    assert cli.main(["--port", "FAKE", "connect", "E0:F1:00:02:03:04"]) == 0
    assert cli.main(["--port", "FAKE", "connect", "--name", "suotasim"]) == 0
    payloads = [
        f.payload for f in written_frames(fake) if f.cmd == int(CMD.connect)
    ]
    assert payloads[0] == b"\x00" + bytes.fromhex("e0f100020304")
    assert payloads[1] == b"\x02suotasim"
    assert "connect status=0" in capsys.readouterr().out


def test_scan_prints_device_table(fake, capsys) -> None:
    fake.inject_raw(
        encode_frame(
            int(EVT.SCAN),
            0,
            bytes([0x00, 0x01])
            + bytes.fromhex("e0f100020304")
            + bytes([0xBA])  # rssi -70
            + b"\x02\x01\x06",
        )
    )
    assert cli.main(["--port", "FAKE", "scan", "--duration", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "E0:F1:00:02:03:04" in out
    assert "1 device(s)" in out
    assert any(f.cmd == int(CMD.scan_stop) for f in written_frames(fake))


def test_drain_and_events_print_event_lines(fake, capsys) -> None:
    fake.inject_raw(encode_frame(int(EVT.SCAN), 0, b"\x00\x01\xab"))
    assert cli.main(["--port", "FAKE", "drain"]) == 0
    out = capsys.readouterr().out
    assert "evt=0x20" in out
    fake.inject_raw(encode_frame(int(EVT.MTU), 1, b"\x00\xf7\x00"))
    # Each CLI invocation owns a fresh BridgeHost (and ring), so the default
    # cursor -1 replays the whole retained window of this invocation.
    assert cli.main(["--port", "FAKE", "events"]) == 0
    out = capsys.readouterr().out
    assert "evt=0x24" in out


def test_monitor_streams_until_duration(fake, capsys) -> None:
    fake.inject_raw(encode_frame(int(EVT.DEBUG), 0, b"\x02hi"))
    assert cli.main(["--port", "FAKE", "monitor", "--duration", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "evt=0x8E" in out
    assert "6869" in out  # payload hex of b"hi"


def test_suota_runs_full_update(monkeypatch, capsys, tmp_path) -> None:
    suota_fake = SuotaFakeTransport()
    suota_fake.open()
    monkeypatch.setattr(
        cli, "open_bridge", lambda port, **kwargs: BridgeHost(suota_fake)
    )
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    assert cli.main(["--port", "FAKE", "suota", str(image)]) == 0
    out = capsys.readouterr().out
    assert "block 1/2" in out  # 2049 bytes -> 2 blocks
    assert "OK bytes=2049 blocks=2" in out
    assert suota_fake.rebooted is True


def test_open_bridge_auto_resolve_failure_is_rc2(monkeypatch, capsys) -> None:
    """UX 波: 无候选串口不再 SystemExit，走 resolve_serial → 一行报错 rc 2。"""
    monkeypatch.setattr("blebridge.config.list_usb_candidates", lambda: [])
    assert cli.main(["info"]) == 2
    err = capsys.readouterr().err
    assert "blebridge: error:" in err and "USB 串口" in err


def test_open_bridge_resolves_serial_via_config_module(monkeypatch) -> None:
    """open_bridge(None, None, None) 交给 resolve_serial：单候选自动选用 +
    usbmodem 端口 rtscts 自动关（CDC 无流控线）。"""
    monkeypatch.setattr(
        "blebridge.config.list_usb_candidates", lambda: ["/dev/cu.usbmodemX"]
    )
    opened = {}

    class DummyTransport(FakeTransport):
        def __init__(self, port, baudrate=921600, rtscts=True, timeout=0.1):
            super().__init__()
            self.port = port
            self.baudrate = baudrate
            self.rtscts = rtscts

        def open(self):
            opened["kwargs"] = (self.port, self.baudrate, self.rtscts)
            super().open()

    monkeypatch.setattr(cli, "SerialTransport", DummyTransport)
    bridge = cli.open_bridge(None)
    bridge.close()
    assert opened["kwargs"] == ("/dev/cu.usbmodemX", 921600, False)


def test_bad_input_exits_gracefully_no_traceback(fake, capsys) -> None:
    """Fix round 2: malformed user input prints one error line, rc != 0."""
    # cmd_write: bad hex (ValueError at the entry point the review flagged)
    rc = cli.main(["--port", "FAKE", "write", "--uuid", "f5fe", "--data", "zz"])
    assert rc == 2
    # cmd_connect: malformed address (ValueError from connect_payload)
    rc = cli.main(["--port", "FAKE", "connect", "NOPE"])
    assert rc == 2
    # uuid16_arg: DaemonError surfaces the same way
    rc = cli.main(["--port", "FAKE", "read", "--uuid", "zzzz"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.count("blebridge: error:") == 3
    assert "Traceback" not in err


def test_open_bridge_picks_first_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        "blebridge.config.list_usb_candidates", lambda: ["/dev/cu.usbmodemX"]
    )
    opened = {}

    class DummyTransport(FakeTransport):
        def __init__(self, port, **kwargs):
            super().__init__()
            self.port = port

        def open(self):
            opened["port"] = self.port
            super().open()

    monkeypatch.setattr(cli, "SerialTransport", DummyTransport)
    bridge = cli.open_bridge(None, baud=115200, rtscts=False)
    bridge.close()
    assert opened["port"] == "/dev/cu.usbmodemX"


# ------------------- global --baud / --rtscts (bring-up 链路欠账) -------------


def test_global_serial_args_tri_state_defaults() -> None:
    """UX 波: 全局 --baud/--rtscts 三态化（不给 = None = 走 resolve_serial）。"""
    parser = cli.build_parser()
    args = parser.parse_args(["info"])
    assert args.port is None
    assert args.baud is None  # None = 解析（配置 > 自动），不再是硬编码默认
    assert args.rtscts is None
    args = parser.parse_args(["--baud", "115200", "--no-rtscts", "info"])
    assert args.baud == 115200
    assert args.rtscts is False
    args = parser.parse_args(["--rtscts", "info"])
    assert args.rtscts is True


def test_open_bridge_passes_baud_and_rtscts_to_serial_transport(monkeypatch) -> None:
    opened = {}

    class DummyTransport(FakeTransport):
        def __init__(self, port, baudrate=921600, rtscts=True, timeout=0.1):
            super().__init__()
            self.port = port
            self.baudrate = baudrate
            self.rtscts = rtscts

        def open(self):
            opened["kwargs"] = (self.port, self.baudrate, self.rtscts)
            super().open()

    monkeypatch.setattr(cli, "SerialTransport", DummyTransport)
    bridge = cli.open_bridge("/dev/cu.X", baud=115200, rtscts=False)
    assert opened["kwargs"] == ("/dev/cu.X", 115200, False)
    bridge.close()
    # Defaults keep the FR-A5 line settings.
    bridge = cli.open_bridge("/dev/cu.X")
    assert opened["kwargs"] == ("/dev/cu.X", 921600, True)
    bridge.close()


def test_main_forwards_baud_and_rtscts(monkeypatch) -> None:
    captured = {}
    inst = CliFake()
    inst.open()

    def fake_open(port, baud=921600, rtscts=True):
        captured.update(port=port, baud=baud, rtscts=rtscts)
        return BridgeHost(inst)

    monkeypatch.setattr(cli, "open_bridge", fake_open)
    assert cli.main(["--port", "FAKE", "--baud", "115200", "--no-rtscts", "info"]) == 0
    assert captured == {"port": "FAKE", "baud": 115200, "rtscts": False}
