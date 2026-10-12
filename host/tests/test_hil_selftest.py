"""L1 集成: tools/hil_test.py 步骤函数在 FakeTransport 世界上的无硬件演练（Task 8）。

同一套 ``step_*`` / ``run_quick`` / ``run_full`` 既被 ``hil_test.py --selftest``
使用，也被这里的 pytest 用例直接驱动（brief: "用 FakeTransport 跑 quick 步骤
函数，入 pytest 可选集成"）。所有用例零硬件、零真实等待（除刻意 0.05s 超时路径）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "host" / "src"), str(_REPO / "host" / "tests"),
           str(_REPO / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

import hil_test  # noqa: E402
from blebridge.bridge import BridgeHost  # noqa: E402
from blebridge.framing import encode_frame  # noqa: E402
from blebridge.image import build_payload  # noqa: E402
from blebridge.transport import FakeTransport  # noqa: E402


# ----------------------------------------------------------------- helpers


def _sim_bridge(**kwargs):
    transport, bridge = hil_test.make_sim_bridge(**kwargs)
    return transport, bridge


def _tmp_image(tmp_path: Path, data: bytes) -> Path:
    return hil_test._write_image(tmp_path, data)


# ------------------------------------------------------------------- tests


def test_hil_quick_steps_pass_on_fake():
    """brief 主验收条: FakeTransport 跑 quick 步骤函数（G1 全链，无硬件全绿）。"""
    sim, bridge = _sim_bridge()
    rows = []
    try:
        summary = hil_test.run_quick(
            bridge, record=lambda *a: rows.append(a), scan_duration=0.1
        )
    finally:
        bridge.close()

    assert [name for name, _, _ in rows] == [
        "ping", "info", "scan", "connect", "mtu", "disconnect",
    ]
    assert all(ok for _, ok, _ in rows), rows
    assert summary["mtu"] >= hil_test.MIN_MTU_DEFAULT
    assert summary["addr"] == "C0:FF:EE:12:34:56"
    # quick 不应发起 SUOTA 上传; 桥/链路各动作恰好一次
    assert bytes(sim.received) == b""
    assert sim.connect_count == 1 and sim.disconnect_count == 1
    assert sim.scan_count == 1
    assert not sim.rebooted


def test_hil_full_steps_pass_on_fake(tmp_path):
    """full（G2）在仿真世界全绿: 零损坏字节比对 + REBOOT 后重新广播可见。"""
    sim, bridge = _sim_bridge()
    image = _tmp_image(tmp_path, hil_test.synthetic_bytes(3 * 2048 + 100))
    payload = build_payload(image)
    rows = []
    try:
        result = hil_test.run_full(
            bridge, record=lambda *a: rows.append(a), image_path=image,
            sink=sim.received, scan_duration=0.1,
        )
    finally:
        bridge.close()

    assert [name for name, _, _ in rows] == [
        "ping", "info", "scan", "connect", "mtu", "suota", "readvertise",
        "disconnect",
    ]
    assert all(ok for _, ok, _ in rows), rows
    assert result.bytes_written == len(payload)
    assert bytes(sim.received) == payload  # 零损坏: 字节级全等
    assert sim.rebooted  # MEM_DEV 0xFD 已写 -> suota_sim 会重新广播
    assert sim.aborted is False


def test_hil_step_ping_rejects_wrong_body():
    """桥应答了错误 body -> StepFailure（断言失败，不是静默通过）。"""
    fake = FakeTransport(responses={0x01: b"\x00something-else"})
    fake.open()
    bridge = BridgeHost(fake)
    try:
        with pytest.raises(hil_test.StepFailure):
            hil_test.step_ping(bridge)
    finally:
        bridge.close()


def test_hil_step_ping_timeout_is_bounded():
    """无应答 -> BridgeTimeout 且有界（0.05s），绝不卡死（FR: 无硬件不假死）。"""
    import time as _time

    fake = FakeTransport()  # 空响应表: ping 永无应答
    fake.open()
    bridge = BridgeHost(fake)
    try:
        start = _time.monotonic()
        with pytest.raises(hil_test.BridgeTimeout):
            hil_test.step_ping(bridge, timeout=0.05)
        assert _time.monotonic() - start < 2.0
    finally:
        bridge.close()


def test_hil_exit_code_constants_documented():
    """退出码契约常量（0/1/2/3）与 CLI --help 自述一致。"""
    assert (hil_test.EXIT_OK, hil_test.EXIT_ASSERT,
            hil_test.EXIT_ENV, hil_test.EXIT_NOHW) == (0, 1, 2, 3)
    assert hil_test.MIN_THROUGHPUT_DEFAULT == 10.0
    assert hil_test.THROUGHPUT_BASELINE_KIB == 14.8  # 参考链路实测基线


# ----------------------------- main() 退出码行为路径（fix round1 I-1）-------


def test_main_exit_3_for_nonexistent_bridge_port():
    """显式端口不存在 -> 即刻 exit 3 + 指引（零硬件、零真实等待）。"""
    rc = hil_test.main(["--bridge", "/dev/cu.blebridge-nonexistent-port"])
    assert rc == hil_test.EXIT_NOHW == 3


def test_main_exit_3_when_auto_has_no_candidate(monkeypatch):
    """--auto 探测不到候选 -> exit 3。"""
    monkeypatch.setattr(hil_test, "list_candidates", lambda: [])
    rc = hil_test.main(["--auto"])
    assert rc == hil_test.EXIT_NOHW == 3


def test_main_exit_2_when_blebridge_import_broken(monkeypatch):
    """blebridge 依赖缺失（模块 import 自检失败）-> exit 2。"""
    monkeypatch.setattr(
        hil_test, "BLEBRIDGE_IMPORT_ERROR", "simulated: pyserial missing"
    )
    rc = hil_test.main(["--selftest"])
    assert rc == hil_test.EXIT_ENV == 2


def test_main_exit_2_for_missing_image(monkeypatch, tmp_path, capsys):
    """full 且 --image 不存在 -> exit 2（环境错）。

    SerialTransport 换成"开即成功"的替身，让 main 走到镜像检查分支——
    该分支在 run_full 之前返回，替身全程不承载数据，零硬件。
    """
    opened = []

    class _DummySerial:
        def __init__(self, port, **kwargs):
            self.port = port

        def open(self):
            opened.append(self.port)

        def close(self):
            pass

        @property
        def is_open(self):
            return True

    monkeypatch.setattr(hil_test, "SerialTransport", _DummySerial)
    rc = hil_test.main([
        "--level", "full", "--bridge", "dummy0",
        "--image", str(tmp_path / "no-such.img"),
    ])
    assert rc == hil_test.EXIT_ENV == 2
    assert opened == ["dummy0"]  # 替身被用过（证明走到了桥之后、run_full 之前）
    assert "环境错" in capsys.readouterr().err
