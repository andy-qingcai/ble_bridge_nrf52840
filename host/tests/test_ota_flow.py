"""Tests for blebridge.ota_flow — 一键 OTA 流程（UX 痛点 2）。

四条路径（dm 未就绪重试→就绪 / 就绪直通 / SameImage / 成功全流程含设备存活
验证）+ CLI 透传，全部由 FakeTransport 系行为模型驱动：零硬件、零真实等待
（``ota_flow.sleep`` monkeypatch 成 no-op，时序参数取小值）。
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

from blebridge import cli, ota_flow  # noqa: E402
from blebridge.bridge import CMD, EVT, BridgeHost  # noqa: E402
from blebridge.framing import StreamParser, encode_frame  # noqa: E402
from blebridge.suota import STATUS_SAME_IMAGE, SameImageError  # noqa: E402
from suota_fake import SuotaFakeTransport, synthetic_image  # noqa: E402

TEST_ADDR = "AA:BB:CC:DD:EE:FF"
TEST_MAC = bytes.fromhex("AABBCCDDEEFF")
_U16_VERSION = 0x64B4  # uuid16 prefix of SUOTA_UUIDS["suota_version"]


def _advert_frame(wire_seq: int = 0) -> bytes:
    """目标地址的 EVT.SCAN 广播帧（subtype + addr_type + addr + rssi + adv）。"""
    payload = bytes([0x00, 0x00]) + TEST_MAC + bytes([0xC4]) + b"\x02\x01\x06"
    return encode_frame(int(EVT.SCAN), wire_seq, payload)


class OtaFake(SuotaFakeTransport):
    """SuotaFake 设备模型 + run_ota 需要的桥级步骤（断连/连接/扫描/配对）。

    Args:
        advertises_on_scan: 收到 scan_start 时是否注入目标广播（模拟设备重启
            后重广播；False = 存活验证扫不到 → DeviceNotBackError 路径）。
        fail_version_reads: 前 N 次 suota_version 读应答 0x74（设备 dm 未就绪
            形态；I-1 瞬窗重试路径）。
    """

    def __init__(
        self,
        *,
        advertises_on_scan: bool = True,
        fail_version_reads: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.responses.update(
            {
                int(CMD.connect): b"\x00\x00" + TEST_MAC,  # status 0 + addr_type/addr
                int(CMD.disconnect): b"\x00",
                int(CMD.scan_start): b"\x00",
                int(CMD.scan_stop): b"\x00",
                int(CMD.pair): b"\x00\x01\x02",  # encrypted=1, sec_level=2
            }
        )
        self.advertises_on_scan = advertises_on_scan
        self.fail_version_reads = fail_version_reads
        self.version_reads = 0  # suota_version 被读总次数（dm 轮询 + verify）
        self.version_failures = 0
        self.connect_count = 0
        self.scan_starts = 0
        self._flow_parser = StreamParser()

    def write(self, data: bytes) -> None:
        super().write(data)
        for frame in self._flow_parser.feed(bytes(data)):
            if frame.cmd == int(CMD.scan_start):
                self.scan_starts += 1
                if self.advertises_on_scan:
                    self.inject_raw(_advert_frame())
            elif frame.cmd == int(CMD.connect):
                self.connect_count += 1

    def _on_request(self, frame) -> None:
        if frame.cmd == int(CMD.gatt_read) and len(frame.payload) >= 2:
            (u16,) = struct.unpack_from("<H", frame.payload)
            if u16 == _U16_VERSION:
                self.version_reads += 1
                if self.version_reads <= self.fail_version_reads:
                    self.version_failures += 1
                    # 覆盖父类"读到即成功应答"：本次回 0x74（dm 未就绪）。
                    self.inject_raw(
                        encode_frame(int(CMD.gatt_read) | 0x80, frame.seq, b"\x74")
                    )
                    return
        super()._on_request(frame)


# 实战时序参数的测试化（零真实等待）；sleep 一律由 ota_flow.sleep 注入 no-op。
FAST = dict(
    addr=TEST_ADDR,
    settle_s=0.0,
    post_disconnect_settle_s=0.0,
    connect_timeout=0.05,
    dm_poll_timeout_s=0.3,
    dm_read_timeout_s=0.05,
    dm_poll_interval_s=0.0,
    verify_wait_s=0.0,
    verify_scan_window_s=0.02,
    verify_timeout_s=0.5,
    verify_settle_s=0.0,
    verify_connect_timeout=0.05,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(ota_flow, "sleep", lambda _s: None)


def written_frames(fake) -> list:
    return StreamParser().feed(bytes(fake.written))


# ---------------------------------------------------------------- run_ota


def test_run_ota_success_full_flow(tmp_path) -> None:
    fake = OtaFake(read_values={_U16_VERSION: b"\x06"})
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    progresses = []
    result = ota_flow.run_ota(
        fake, str(image), bank=1, pair=True, progress_cb=progresses.append, **FAST
    )
    assert result.blocks == 2 and result.bytes_written == 2049  # +1 XOR 字节
    assert progresses[-1].block == 2  # progress_cb 逐块透传给 SuotaSession
    assert fake.rebooted is True and fake.aborted is False
    assert 0x13000000 | 1 in fake.mem_dev_values  # bank=1 进 MEM_DEV 启动值
    frames = written_frames(fake)
    # 实战顺序：第一只请求就是断旧链（随后是首泵的 credits 补发帧），
    # 之后才连接（public 地址载荷）。
    assert frames[0].cmd == int(CMD.disconnect)
    connects = [f for f in frames if f.cmd == int(CMD.connect)]
    assert connects[0].payload == b"\x00" + TEST_MAC
    assert len(connects) == 2  # 初次连接 + 存活验证重连
    assert any(f.cmd == int(CMD.scan_start) for f in frames)  # 验证阶段扫过广播
    assert fake.version_reads == 2  # dm 轮询 1 次 + 存活验证 1 次
    assert fake.is_open is False  # 裸 Transport 由 run_ota 代管生命周期


def test_run_ota_requires_addr() -> None:
    fake = OtaFake(read_values={})
    with pytest.raises(TypeError):
        ota_flow.run_ota(fake, "fw.bin")  # type: ignore[call-arg]


def test_run_ota_accepts_open_bridge_without_closing(tmp_path) -> None:
    fake = OtaFake(read_values={_U16_VERSION: b"\x06"})
    fake.open()
    bridge = BridgeHost(fake)
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    result = ota_flow.run_ota(bridge, str(image), **FAST)
    assert result.blocks == 2
    assert bridge.is_open is True  # BridgeHost 传入时生命周期归调用方


def test_run_ota_dm_not_ready_retries_then_ready(tmp_path) -> None:
    fake = OtaFake(fail_version_reads=2, read_values={_U16_VERSION: b"\x06"})
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    result = ota_flow.run_ota(fake, str(image), **FAST)
    assert result.blocks == 2
    assert fake.version_reads == 4  # 2 次未就绪 + dm 就绪 1 次 + 存活验证 1 次
    assert fake.version_failures == 2


def test_run_ota_dm_never_ready_raises_human_error(tmp_path) -> None:
    fake = OtaFake(fail_version_reads=10**9)
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    with pytest.raises(ota_flow.OtaFlowError, match="未就绪"):
        ota_flow.run_ota(fake, str(image), **{**FAST, "dm_poll_timeout_s": 0.05})
    assert fake.rebooted is False


def test_run_ota_same_image_disconnects_without_verify(tmp_path) -> None:
    fake = OtaFake(advertises_on_scan=False, image_started_status=STATUS_SAME_IMAGE)
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    with pytest.raises(SameImageError):
        ota_flow.run_ota(fake, str(image), **FAST)
    assert fake.rebooted is False
    assert fake.aborted is False  # 设备已给结论：无 ABORT（会话已终结不宜再写）
    frames = written_frames(fake)
    assert not any(f.cmd == int(CMD.scan_start) for f in frames)  # 不做存活验证


def test_run_ota_verify_device_not_back_raises(tmp_path) -> None:
    fake = OtaFake(advertises_on_scan=False, read_values={_U16_VERSION: b"\x06"})
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    with pytest.raises(ota_flow.DeviceNotBackError, match="未恢复"):
        ota_flow.run_ota(fake, str(image), **FAST)
    assert fake.rebooted is True  # 上传本身成功，设备没回来
    assert fake.scan_starts >= 1  # 确实等过重广播


def test_run_ota_connect_failure_is_human_error(tmp_path) -> None:
    fake = OtaFake()
    fake.responses[int(CMD.connect)] = b"\x02"  # 实测失败码 2
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    with pytest.raises(ota_flow.OtaFlowError, match="连接 .* 失败"):
        ota_flow.run_ota(fake, str(image), **FAST)


# ---------------------------------------------------------------- CLI 透传


def test_cli_ota_parser_defaults() -> None:
    args = cli.build_parser().parse_args(["ota", "fw.bin", "--addr", TEST_ADDR])
    assert callable(args.func)
    assert args.bank == 1 and args.pair is True and args.verify is True
    assert args.ack_timeout == 30.0 and args.chunk_size is None
    assert args.addr == TEST_ADDR
    args = cli.build_parser().parse_args(
        ["ota", "fw.bin", "--addr", TEST_ADDR, "--no-pair", "--bank", "0",
         "--no-verify", "--chunk", "100", "--ack-timeout", "5"]
    )
    assert args.pair is False and args.bank == 0 and args.verify is False
    assert args.chunk_size == 100 and args.ack_timeout == 5.0
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["ota", "fw.bin"])  # --addr 必填


def test_cli_ota_end_to_end(monkeypatch, capsys, tmp_path) -> None:
    fake = OtaFake(read_values={_U16_VERSION: b"\x06"})
    fake.open()
    monkeypatch.setattr(cli, "open_bridge", lambda port, **kw: BridgeHost(fake))
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    rc = cli.main([
        "--port", "FAKE", "ota", str(image), "--addr", TEST_ADDR,
        "--ack-timeout", "0.05",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "block 2/2" in out  # 进度实时打印（每 10 块一行 + 末块必打）
    assert "RESULT:" in out and "throughput=" in out
    assert fake.rebooted is True and fake.connect_count == 2  # 含存活验证重连


def test_cli_ota_same_image_is_friendly_zero(monkeypatch, capsys, tmp_path) -> None:
    fake = OtaFake(image_started_status=STATUS_SAME_IMAGE)
    fake.open()
    monkeypatch.setattr(cli, "open_bridge", lambda port, **kw: BridgeHost(fake))
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    rc = cli.main([
        "--port", "FAKE", "ota", str(image), "--addr", TEST_ADDR,
        "--ack-timeout", "0.05",
    ])
    assert rc == 0
    assert "设备已是同版本，无需升级" in capsys.readouterr().out


def test_cli_ota_device_not_back_is_rc1(monkeypatch, capsys, tmp_path) -> None:
    fake = OtaFake()
    fake.open()
    monkeypatch.setattr(cli, "open_bridge", lambda port, **kw: BridgeHost(fake))
    monkeypatch.setattr(
        ota_flow, "verify_device_alive", lambda *a, **k: (False, "注入: 未重广播")
    )
    image = tmp_path / "fw.bin"
    image.write_bytes(synthetic_image(full_blocks=1, tail=0))
    rc = cli.main([
        "--port", "FAKE", "ota", str(image), "--addr", TEST_ADDR,
        "--ack-timeout", "0.05",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "上传成功" in err and "未恢复" in err
