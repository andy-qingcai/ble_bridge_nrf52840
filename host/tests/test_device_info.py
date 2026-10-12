"""Tests for blebridge.device_info — 一键设备信息（UX 痛点 3）。

SuotaFakeTransport 复用为"任意 GATT 读都按 read_values 应答"的设备模型（另加
0x2A00/0x2A19），缺字符场景以 0x74 应答 fake 覆盖；CLI 薄壳透传同样零硬件、
零真实等待。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from blebridge import cli, device_info  # noqa: E402
from blebridge.bridge import CMD, BridgeHost, BridgeTimeout  # noqa: E402
from blebridge.device_info import (  # noqa: E402
    UUID_BATTERY_LEVEL,
    UUID_DEVICE_NAME,
    collect_device_info,
    format_device_info,
)
from blebridge.framing import StreamParser, encode_frame  # noqa: E402
from blebridge.suota import SUOTA_UUIDS, uuid16_prefix  # noqa: E402
from suota_fake import SuotaFakeTransport  # noqa: E402

TEST_ADDR = "AA:BB:CC:DD:EE:FF"
TEST_MAC = bytes.fromhex("AABBCCDDEEFF")
_U16_VERSION = uuid16_prefix(SUOTA_UUIDS["suota_version"])


def _full_read_values() -> dict:
    return {
        UUID_DEVICE_NAME: "BLE Test Device".encode("utf-8"),
        UUID_BATTERY_LEVEL: b"\x57",  # 87%
        _U16_VERSION: b"\x06",
        uuid16_prefix(SUOTA_UUIDS["patch_data_char_size"]): struct.pack("<H", 244),
        uuid16_prefix(SUOTA_UUIDS["suota_mtu"]): struct.pack("<H", 251),
        uuid16_prefix(SUOTA_UUIDS["l2cap_psm"]): struct.pack("<H", 0x0005),
    }


class DeviceFake(SuotaFakeTransport):
    """SuotaFake + 连接/断开应答；``read_status_0x74`` 里的 uuid 读回 0x74。"""

    def __init__(self, read_values=None, read_status_0x74=()) -> None:
        super().__init__(read_values=read_values)
        self.responses[int(CMD.connect)] = b"\x00\x00" + TEST_MAC  # addr_type 0
        self.responses[int(CMD.disconnect)] = b"\x00"
        self._read_0x74 = set(read_status_0x74)

    def _on_request(self, frame) -> None:
        if frame.cmd == int(CMD.gatt_read) and len(frame.payload) >= 2:
            (u16,) = struct.unpack_from("<H", frame.payload)
            if u16 in self._read_0x74:
                self.inject_raw(
                    encode_frame(int(CMD.gatt_read) | 0x80, frame.seq, b"\x74")
                )
                return
        super()._on_request(frame)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(device_info, "sleep", lambda _s: None)


def _bridge(fake: DeviceFake) -> BridgeHost:
    fake.open()
    return BridgeHost(fake)


# ------------------------------------------------------------ collect


def test_collect_full_success_with_suota() -> None:
    fake = DeviceFake(read_values=_full_read_values())
    info = collect_device_info(_bridge(fake), addr=TEST_ADDR, suota=True)
    assert info["connected"] is True
    assert info["connect_status"] == 0
    assert info["addr"] == TEST_ADDR  # 从 connect 响应体解析
    assert info["addr_type"] == 0
    assert info["device_name"] == "BLE Test Device"
    assert info["battery"] == 87
    suota = info["suota"]
    assert suota["suota_version"] == {"status": 0, "hex": "06"}
    assert suota["patch_data_char_size"]["hex"] == struct.pack("<H", 244).hex()
    assert suota["suota_mtu"]["hex"] == struct.pack("<H", 251).hex()
    assert suota["l2cap_psm"]["hex"] == struct.pack("<H", 5).hex()
    assert info["errors"] == []
    frames = StreamParser().feed(bytes(fake.written))
    # 两次 disc_svc：GAP(0x1800) + SUOTA(0xFEF5)；结尾断开链路
    discs = [f.payload for f in frames if f.cmd == int(CMD.disc_svc)]
    assert discs == [b"\x00\x18", b"\xf5\xfe"]
    assert any(f.cmd == int(CMD.disconnect) for f in frames)


def test_collect_without_suota_flag_skips_fef5() -> None:
    fake = DeviceFake(read_values=_full_read_values())
    info = collect_device_info(_bridge(fake), addr=TEST_ADDR)
    assert info["connected"] is True and info["suota"] is None
    discs = [f.payload for f in StreamParser().feed(bytes(fake.written))
             if f.cmd == int(CMD.disc_svc)]
    assert discs == [b"\x00\x18"]  # 只发现 GAP


def test_collect_connect_failure() -> None:
    fake = DeviceFake(read_values=_full_read_values())
    fake.responses[int(CMD.connect)] = b"\x02"  # 真机观测失败码 2
    info = collect_device_info(_bridge(fake), addr=TEST_ADDR)
    assert info["connected"] is False
    assert info["connect_status"] == 2
    assert any("连接失败" in e for e in info["errors"])


def test_collect_connect_timeout() -> None:
    fake = DeviceFake(read_values=_full_read_values())
    del fake.responses[int(CMD.connect)]  # 永不应答 → BridgeTimeout
    info = collect_device_info(_bridge(fake), addr=TEST_ADDR, connect_timeout=0.02)
    assert info["connected"] is False
    assert any("连接超时" in e for e in info["errors"])


def test_collect_tolerates_missing_name_and_battery() -> None:
    fake = DeviceFake(
        read_values={_U16_VERSION: b"\x06"},
        read_status_0x74=(UUID_DEVICE_NAME, UUID_BATTERY_LEVEL),
    )
    info = collect_device_info(_bridge(fake), addr=TEST_ADDR, suota=True)
    assert info["connected"] is True
    assert info["device_name"] is None  # "如存在"语义：缺席不算致命
    assert info["battery"] is None
    assert any("0x2A00" in e for e in info["errors"])
    assert any("0x2A19" in e for e in info["errors"])
    assert info["suota"]["suota_version"]["hex"] == "06"


def test_collect_requires_addr_or_name() -> None:
    """addr/name 都缺省时不再有隐式默认目标，直接报错。"""
    fake = DeviceFake(read_values={})
    with pytest.raises(ValueError):
        collect_device_info(_bridge(fake))


def test_collect_by_name_mode() -> None:
    fake = DeviceFake(read_values={})
    collect_device_info(_bridge(fake), name="test-device")
    (connect,) = [
        f for f in StreamParser().feed(bytes(fake.written))
        if f.cmd == int(CMD.connect)
    ]
    assert connect.payload == b"\x02test-device"  # mode 2 = 广播名


def test_collect_by_random_addr_type() -> None:
    fake = DeviceFake(read_values={})
    collect_device_info(_bridge(fake), addr="E0:F1:00:02:03:04", random=True)
    (connect,) = [
        f for f in StreamParser().feed(bytes(fake.written))
        if f.cmd == int(CMD.connect)
    ]
    assert connect.payload == b"\x01" + bytes.fromhex("e0f100020304")


# ------------------------------------------------------------ format


def test_format_full() -> None:
    fake = DeviceFake(read_values=_full_read_values())
    text = format_device_info(collect_device_info(_bridge(fake), addr=TEST_ADDR, suota=True))
    assert "地址:   AA:BB:CC:DD:EE:FF (public)" in text
    assert "设备名: BLE Test Device" in text
    assert "电量:   87%" in text
    assert "SUOTA:" in text
    assert "suota_version: 06" in text
    assert "patch_data_char_size: 244" in text
    assert "suota_mtu: 251" in text


def test_format_connect_failure_and_notes() -> None:
    fake = DeviceFake()
    fake.responses[int(CMD.connect)] = b"\x02"
    text = format_device_info(collect_device_info(_bridge(fake), addr=TEST_ADDR))
    assert "连接失败: AA:BB:CC:DD:EE:FF" in text
    assert "设备名: 不可用" in text
    assert "电量:   不可用" in text
    assert "备注:" in text and "连接失败 status=2" in text


# ------------------------------------------------------------ CLI 透传


def test_cli_device_info_parser_routes() -> None:
    args = cli.build_parser().parse_args(["device-info", "--suota"])
    assert callable(args.func) and args.suota is True and args.settle == 2.0
    args = cli.build_parser().parse_args(
        ["device-info", "E0:F1:00:02:03:04", "--random", "--name", "x"]
    )
    assert args.addr == "E0:F1:00:02:03:04" and args.random and args.name == "x"


def test_cli_device_info_end_to_end(monkeypatch, capsys) -> None:
    fake = DeviceFake(read_values=_full_read_values())
    fake.open()
    monkeypatch.setattr(cli, "open_bridge", lambda port, **kw: BridgeHost(fake))
    rc = cli.main(["--port", "FAKE", "device-info", TEST_ADDR, "--suota"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "设备名: BLE Test Device" in out
    assert "电量:   87%" in out
    assert "suota_version: 06" in out


def test_cli_device_info_connect_failure_is_rc1(monkeypatch, capsys) -> None:
    fake = DeviceFake()
    fake.responses[int(CMD.connect)] = b"\x02"
    fake.open()
    monkeypatch.setattr(cli, "open_bridge", lambda port, **kw: BridgeHost(fake))
    rc = cli.main(["--port", "FAKE", "device-info", TEST_ADDR])
    assert rc == 1
    assert "连接失败" in capsys.readouterr().out


def test_bridge_timeout_in_read_maps_to_unavailable(monkeypatch) -> None:
    """读超时归一化 status -1：不中断收集，也不抛出。"""

    class TimeoutReadBridge(BridgeHost):
        def gatt_read(self, uuid16=None, **kwargs):
            raise BridgeTimeout(int(CMD.gatt_read))

    fake = DeviceFake(read_values={})
    fake.open()
    info = collect_device_info(TimeoutReadBridge(fake), addr=TEST_ADDR)
    assert info["connected"] is True
    assert info["device_name"] is None and info["battery"] is None
    assert any("status=-1" in e for e in info["errors"])
