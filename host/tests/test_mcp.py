"""Tests for blebridge.mcp_server — the MCP tool facade (Task 5).

Two layers, both free of the optional ``mcp`` package:

- **Contract tests** over the pure-Python description table
  ``mcp_server.TOOLS`` (list of ``(name, doc, params)``): the whole required
  tool surface is present, every doc is a Chinese one-liner, and the table
  shares one source with the FastMCP registration (asserted by the
  ``mcp``-dependent smoke class at the bottom, skipped when ``mcp`` is not
  installed).
- **Sim self-check** through the mcp-independent entry point
  ``mcp_server.call_tool(name, args)``: open(sim) -> ping -> scan ->
  connect(fake address / fake name) -> GATT read/write/subscribe ->
  suota_start(synthetic image via the SuotaFake device model) -> drain -> close.
  The sim backend is instant (synchronous pump model), so the chain costs no
  real waiting except deliberately shrunken timeout windows.

Everything is single-threaded and drives the module-level ``STATE``; an
autouse fixture closes any leftover connection around every test.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from blebridge import mcp_server  # noqa: E402
from suota_fake import synthetic_image  # noqa: E402

# ----------------------------------------------------------------- helpers

REQUIRED_TOOLS = [
    "open",
    "close",
    "ping",
    "info",
    "list_ports",
    "scan",
    "connect",
    "disconnect",
    "mtu",
    "read",
    "write",
    "subscribe",
    "unsubscribe",
    "events",
    "drain",
    "suota_start",
    "suota_abort",
    "suota_status",
    "log_level",
    "reset",
    "raw",
]

_SIM_ADDR = "02:1A:7F:BA:BE:01"


def call(name: str, args: dict | None = None) -> dict:
    """One call_tool round trip; the envelope must always be parseable JSON."""
    raw = mcp_server.call_tool(name, args or {})
    obj = json.loads(raw)
    assert isinstance(obj, dict) and "ok" in obj, raw
    return obj


def ok(name: str, args: dict | None = None) -> dict:
    obj = call(name, args)
    assert obj["ok"] is True, obj
    return obj["data"]


def fail(name: str, args: dict | None = None) -> str:
    obj = call(name, args)
    assert obj["ok"] is False, obj
    return obj["error"]


@pytest.fixture(autouse=True)
def _clean_state():
    """No test may leak a connection or tweaked timeout into the next one."""
    mcp_server.call_tool("close")
    yield
    mcp_server.call_tool("close")


# -------------------------------------------------------- registry contract


class TestToolContract:
    def test_required_tool_names_covered(self):
        names = [name for name, _doc, _params in mcp_server.TOOLS]
        missing = [n for n in REQUIRED_TOOLS if n not in names]
        assert missing == []

    def test_registry_and_dispatch_share_one_source(self):
        """TOOLS and the call_tool dispatch table are exactly the same set."""
        names = [name for name, _doc, _params in mcp_server.TOOLS]
        assert len(names) == len(set(names)), "duplicate tool registration"
        assert set(names) == set(mcp_server._HANDLERS)

    def test_every_doc_is_a_chinese_one_liner(self):
        for name, doc, _params in mcp_server.TOOLS:
            assert doc, name
            assert "\n" not in doc, name
            assert any("\u4e00" <= ch <= "\u9fff" for ch in doc), name

    def test_params_read_off_real_signatures(self):
        table = {name: params for name, _doc, params in mcp_server.TOOLS}
        assert table["open"] == {"backend": "str='sim'", "port": "str=''"}
        assert table["connect"] == {"addr_or_name": "str", "timeout_s": "float=8.0"}
        assert set(table["write"]) == {"uuid", "data_hex", "with_response"}
        assert set(table["suota_start"]) == {"image_path", "chunk_size"}
        assert table["read"] == {"uuid": "str"}

    def test_docstrings_note_concurrency_boundary(self):
        open_doc = next(doc for name, doc, _p in mcp_server.TOOLS if name == "open")
        assert "daemon" in open_doc  # 如需并发请外接 blebridge daemon


# ------------------------------------------------------------ sim self-check


class TestSimSelfCheck:
    """The no-hardware full chain — the core value of the sim backend."""

    def test_full_chain_open_to_close(self, tmp_path):
        # open / lifecycle
        assert ok("open", {"backend": "sim"}) == {"backend": "sim", "port": ""}
        assert ok("ping")["pong"] is True
        assert ok("info")["status"] == 0

        # list_ports needs no connection semantics but must answer
        assert isinstance(ok("list_ports")["ports"], list)

        # scan finds the scripted fake device
        scan = ok("scan", {"duration_s": 0.1})
        assert scan["status"] == 0
        devices = scan["devices"]
        assert [d["addr"] for d in devices] == [_SIM_ADDR]
        assert devices[0]["rssi"] == -52
        assert b"blebridge-sim" in bytes.fromhex(devices[0]["adv_hex"])

        before_suota = ok("drain")["last_seq"]

        # connect by the scanned address, then by advertised name
        assert ok("connect", {"addr_or_name": _SIM_ADDR})["status"] == 0
        assert ok("connect", {"addr_or_name": "blebridge-sim"})["status"] == 0

        # link-level GATT surface
        assert ok("mtu")["status"] == 0
        assert ok("read", {"uuid": "1800"}) == {"status": 0, "hex": ""}
        assert ok("write", {"uuid": "2a00", "data_hex": "cafe"}) == {"status": 0}
        assert ok("write", {"uuid": "2a01", "data_hex": "01", "with_response": False}) == {
            "status": 0
        }
        serv_status = "5f78df94-798c-46f5-990a-b3eb6a065c88"
        assert ok("subscribe", {"uuid": serv_status}) == {"status": 0}
        assert ok("unsubscribe", {"uuid": "5f78"}) == {"status": 0}
        assert ok("log_level", {"level": 3}) == {"status": 0}
        assert ok("suota_status")["status"] == 0
        assert ok("reset")["status"] == 0

        # full SUOTA update of a synthetic image through the fake device model
        image = tmp_path / "synthetic.img"
        image_bytes = synthetic_image()
        image.write_bytes(image_bytes)
        result = ok("suota_start", {"image_path": str(image)})
        assert result["bytes_written"] == len(image_bytes) + 1  # + XOR byte
        assert result["blocks"] == 4  # 3 full blocks + 101-byte remainder
        assert result["chunks"] == 28  # 9 chunks per full block @244 + 1

        # drain: progress + done arrive as host-local EVT.SUOTA events
        drained = ok("drain", {"after_seq": before_suota})
        suota_events = [e for e in drained["events"] if e["evt"] == 0x25]
        assert [e["subtype"] for e in suota_events] == [0, 0, 0, 0, 1]  # 4x progress+done
        assert drained["wire_gaps"] == 0
        assert drained["dropped"] == 0
        assert drained["last_seq"] >= before_suota
        # events() is the daemon-parity alias with the same payload shape
        again = ok("events", {"after_seq": before_suota})
        assert again["events"] == drained["events"]

        assert ok("close") == {"closed": True}
        assert "not_open" in fail("ping")

    def test_suota_abort_clears_session(self):
        ok("open", {"backend": "sim"})
        assert ok("suota_abort") == {"aborted": True}
        # The ABORT write (MEM_DEV 0xFF000000) went out over the bridge.
        assert ok("raw", {"raw_cmd": "0x09", "payload_hex": ""})["status"] == 0

    def test_suota_ack_timeout_reports_failed_event(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_server.STATE, "suota_ack_timeout", 0.05)
        original = mcp_server._make_sim_transport

        def silent_pen(**kwargs):
            fake = original(**kwargs)
            fake.block_ack = None  # the device never ACKs -> timeout path
            return fake

        monkeypatch.setattr(mcp_server, "_make_sim_transport", silent_pen)
        ok("open", {"backend": "sim"})
        image = tmp_path / "synthetic.img"
        image.write_bytes(synthetic_image())
        error = fail("suota_start", {"image_path": str(image)})
        assert "suota" in error and "block" in error
        drained = ok("drain")
        failed = [e for e in drained["events"] if e["evt"] == 0x25 and e["subtype"] == 2]
        assert len(failed) == 1
        payload = bytes.fromhex(failed[0]["hex"]).decode("ascii")
        assert payload == "-1:block"  # STATUS_TIMEOUT : phase

    def test_bridge_request_timeout_is_ok_false(self, monkeypatch):
        monkeypatch.setattr(mcp_server.STATE, "request_timeout", 0.05)
        ok("open", {"backend": "sim"})
        # 0x41 (suota_abort placeholder) has no canned sim answer ->
        # BridgeTimeout (0x0C disc_svc gained one with the generic-bridge
        # host-triggered discovery)
        error = fail("raw", {"raw_cmd": "0x41"})
        assert "timeout" in error.lower()

    def test_open_switches_single_connection(self):
        ok("open", {"backend": "sim"})
        assert ok("ping")["pong"] is True
        ok("open", {"backend": "sim"})  # old connection replaced, not stacked
        assert ok("ping")["pong"] is True
        assert ok("close") == {"closed": True}
        assert ok("close") == {"closed": True}  # idempotent


# ---------------------------------------------------------------- error paths


class TestErrorPaths:
    def test_unknown_tool(self):
        error = fail("no_such_tool")
        assert "unknown_cmd" in error

    def test_tools_require_open_connection(self):
        for name, args in (
            ("ping", {}),
            ("scan", {"duration_s": 0.0}),
            ("read", {"uuid": "1800"}),
            ("drain", {}),
        ):
            assert "not_open" in fail(name, args), name

    def test_bad_backend(self):
        assert "bad_arg" in fail("open", {"backend": "bogus"})

    def test_serial_open_failure_is_ok_false(self):
        error = fail("open", {"backend": "serial", "port": "/dev/blebridge_no_such_port"})
        assert "serial open failed" in error

    def test_serial_auto_detect_without_port_error(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "list_candidates", lambda: [])
        error = fail("open", {"backend": "serial"})
        assert "no_serial_port" in error

    def test_bad_hex_and_args(self):
        ok("open", {"backend": "sim"})
        assert "bad_arg" in fail("write", {"uuid": "2a00", "data_hex": "xyz"})
        assert "bad_arg" in fail("raw", {"raw_cmd": "not_a_number"})
        assert "bad_arg" in fail("raw", {"raw_cmd": "256"})
        assert "bad_arg" in fail("log_level", {"level": 256})
        assert "bad_arg" in fail("log_level", {"level": "loud"})
        assert "bad_arg" in fail("read", {"uuid": "zzzz"})
        assert "bad_arg" in fail("connect", {"addr_or_name": "aa:bb:cc:dd:ee:fg"})

    def test_suota_start_missing_image(self):
        ok("open", {"backend": "sim"})
        error = fail("suota_start", {"image_path": "/no/such/image.img"})
        assert error  # any structured failure, never a raised exception


# ------------------------------------------------- FastMCP wiring (optional)


try:
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:  # pragma: no cover — depends on the venv
    HAS_MCP = False


@pytest.mark.skipif(not HAS_MCP, reason="optional mcp package not installed")
class TestFastMcpWiring:
    def test_build_server_registers_the_same_tool_table(self):
        server = mcp_server.build_server()
        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}
        assert set(by_name) == {name for name, _doc, _params in mcp_server.TOOLS}
        for name, doc, params in mcp_server.TOOLS:
            assert by_name[name].description == doc
            schema = by_name[name].input_schema
            assert set(schema.get("properties", {})) == set(params)
            for pname, param in params.items():
                expected_required = "=" not in param  # defaulted params optional
                assert (pname in schema.get("required", [])) == expected_required

    def test_server_tool_call_round_trip(self):
        server = mcp_server.build_server()
        result = asyncio.run(server.call_tool("list_ports", {}))
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is True
        assert isinstance(payload["data"]["ports"], list)

    @pytest.mark.slow
    def test_real_stdio_subprocess_session(self):
        """真 blebridge-mcp 子进程 + MCP stdio 客户端全握手（fix round 1, I1）.

        Spawns ``python -m blebridge.mcp_server`` (the exact code path of the
        ``blebridge-mcp`` console script), performs the MCP initialize /
        list_tools / call_tool handshake over real stdio, and asserts the
        ``{"ok": ...}`` envelope byte-for-byte — the previously
        hand-executed smoke, now in-repo.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "blebridge.mcp_server"]
        )

        async def session_round_trip():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    opened = await session.call_tool("open", {"backend": "sim"})
                    pong = await session.call_tool("ping", {})
                    closed = await session.call_tool("close", {})
                    return tools, opened, pong, closed

        tools, opened, pong, closed = asyncio.run(
            asyncio.wait_for(session_round_trip(), timeout=60)
        )
        assert {t.name for t in tools.tools} == {
            n for n, _doc, _params in mcp_server.TOOLS
        }
        assert json.loads(opened.content[0].text) == {
            "ok": True,
            "data": {"backend": "sim", "port": ""},
        }
        assert json.loads(pong.content[0].text) == {
            "ok": True,
            "data": {"pong": True, "status": 0, "hex": ""},
        }
        assert json.loads(closed.content[0].text) == {"ok": True, "data": {"closed": True}}
