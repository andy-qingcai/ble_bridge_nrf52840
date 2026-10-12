"""Tests for blebridge.config — 持久化配置与串口自动解析（UX 痛点 1）。

配置文件经 conftest 的 ``BLEBRIDGE_CONFIG`` 重定向到每测试独立的临时文件（零
真实 home 读写）；自动探测通过 monkeypatch ``config.list_usb_candidates``
注入候选，不触碰真实串口枚举。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from blebridge import config  # noqa: E402


# ---------------------------------------------------------------- 文件读写


def test_load_config_missing_returns_empty() -> None:
    assert config.load_config() == {}


def test_save_and_load_roundtrip_and_merge() -> None:
    merged = config.save_config(port="/dev/cu.usbmodemA")
    assert merged == {"port": "/dev/cu.usbmodemA"}
    merged = config.save_config(baud=115200, rtscts=False)
    assert merged == {"port": "/dev/cu.usbmodemA", "baud": 115200, "rtscts": False}
    assert config.load_config() == merged
    # 落盘为合法 JSON（可手工查看/编辑）
    on_disk = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert on_disk == merged


def test_load_config_corrupt_json_warns_and_returns_empty(capsys) -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert config.load_config() == {}
    assert "警告" in capsys.readouterr().err  # WARN 不崩


def test_load_config_non_object_warns_and_returns_empty(capsys) -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert config.load_config() == {}
    assert "警告" in capsys.readouterr().err


def test_save_config_rejects_unknown_key_and_bad_types() -> None:
    with pytest.raises(ValueError, match="未知配置键"):
        config.save_config(speed=115200)
    with pytest.raises(ValueError, match="baud"):
        config.save_config(baud="fast")
    with pytest.raises(ValueError, match="baud"):
        config.save_config(baud=True)  # bool 不是合法整数
    with pytest.raises(ValueError, match="port"):
        config.save_config(port=123)
    with pytest.raises(ValueError, match="rtscts"):
        config.save_config(rtscts="yes")
    assert config.load_config() == {}  # 校验失败不写半截配置


def test_clear_config_removes_file_once() -> None:
    config.save_config(port="/dev/x")
    assert config.clear_config() is True
    assert config.load_config() == {}
    assert config.clear_config() is False


# ---------------------------------------------------------------- 优先级解析


def test_resolve_explicit_args_win_over_config(monkeypatch) -> None:
    config.save_config(port="/dev/cu.fromConfig", baud=115200, rtscts=False)
    called = []

    def boom():  # 显式参数齐全时绝不探测
        called.append(1)
        return []

    monkeypatch.setattr(config, "list_usb_candidates", boom)
    assert config.resolve_serial(port="/dev/cu.explicit", baud=921600, rtscts=True) == (
        "/dev/cu.explicit",
        921600,
        True,
    )
    # 部分显式：port 显式、其余取配置
    assert config.resolve_serial(port="/dev/cu.explicit") == (
        "/dev/cu.explicit",
        115200,
        False,
    )
    assert called == []


def test_resolve_falls_back_to_config_values() -> None:
    config.save_config(port="/dev/cu.usbmodemSaved", baud=115200, rtscts=True)
    assert config.resolve_serial() == ("/dev/cu.usbmodemSaved", 115200, True)


def test_resolve_defaults_without_config_single_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        config, "list_usb_candidates", lambda: ["/dev/cu.usbmodem11301"]
    )
    # usbmodem（CDC）→ rtscts 自动关；baud 缺省 FR-A5 921600
    assert config.resolve_serial() == ("/dev/cu.usbmodem11301", 921600, False)
    monkeypatch.setattr(
        config, "list_usb_candidates", lambda: ["/dev/cu.usbserialX"]
    )
    # usbserial（UART 转接板）→ 保留硬件流控
    assert config.resolve_serial() == ("/dev/cu.usbserialX", 921600, True)


def test_resolve_multiple_candidates_lists_them(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "list_usb_candidates",
        lambda: ["/dev/cu.usbmodem1", "/dev/cu.usbserial2"],
    )
    with pytest.raises(ValueError) as excinfo:
        config.resolve_serial()
    message = str(excinfo.value)
    assert "/dev/cu.usbmodem1" in message and "/dev/cu.usbserial2" in message
    assert "--port" in message  # 人话指引


def test_resolve_zero_candidates_prompts_to_plug_in(monkeypatch) -> None:
    monkeypatch.setattr(config, "list_usb_candidates", lambda: [])
    with pytest.raises(ValueError, match="未找到 USB 串口"):
        config.resolve_serial()


def test_resolve_rtscts_from_config_beats_portname_rule(monkeypatch) -> None:
    config.save_config(rtscts=True)
    monkeypatch.setattr(
        config, "list_usb_candidates", lambda: ["/dev/cu.usbmodemZ"]
    )
    # 配置值优先于"usbmodem→False"的端口名自动规则
    assert config.resolve_serial()[2] is True


def test_resolve_bad_config_baud_raises() -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"baud": "fast"}), encoding="utf-8")
    with pytest.raises(ValueError, match="baud"):
        config.resolve_serial(port="/dev/x")


def test_serial_resolve_error_is_valueerror() -> None:
    # CLI main() 按 ValueError 归一为一行报错 rc 2
    assert issubclass(config.SerialResolveError, ValueError)


# ------------------------------------------------------- 配置路径解析


@pytest.fixture
def fake_home(monkeypatch, tmp_path) -> Path:
    """把 HOME 指向临时目录并清除配置路径覆盖，让 config_path() 走默认 ~/。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BLEBRIDGE_CONFIG", raising=False)
    monkeypatch.delenv("BRIDGE_CONFIG", raising=False)
    return tmp_path


def test_config_path_defaults_to_home(fake_home) -> None:
    assert config.config_path() == fake_home / ".blebridge" / "config.json"


def test_save_and_load_roundtrip(fake_home) -> None:
    config.save_config(port="/dev/cu.test", baud=115200, rtscts=False)
    assert config.load_config() == {"port": "/dev/cu.test", "baud": 115200,
                                    "rtscts": False}


def test_legacy_env_var_bridge_config_still_respected(fake_home, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGE_CONFIG", str(fake_home / "via-old-env.json"))
    assert config.config_path() == fake_home / "via-old-env.json"
