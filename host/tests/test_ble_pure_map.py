"""L2 contract test: firmware GATT characteristic map vs host uuid16 semantics.

Fix C-1 / A3 (final review): the bridge firmware used to register every
128-bit SUOTA characteristic with key=0, so the whole 0xFEF5 service
collapsed onto one dedup entry and GATT addressing (CMD.gatt_read/write/
subscribe payloads, EVT.GATTX sources) was dead on arrival.  The firmware
now extracts the uuid16 prefix (first 16 bits of the readable UUID form,
host ``suota.py uuid16_prefix``) from the over-the-air little-endian
``bt_uuid_128.val`` array in the pure-logic layer
(``firmware/app/src/bridge_ble_pure.c``).

This module compiles that pure C file with the host C compiler (same
no-CMake scheme as tests/conftest.py, session-scoped) and asserts the
cross-language contract byte-for-byte:

1. ``bridge_ble_uuid128_prefix`` on the 10 real SUOTA 128-bit UUIDs (the
   Zephyr ``BT_UUID_128_ENCODE`` byte order) equals ``uuid16_prefix`` AND
   equals the exact 2-byte LE pairs the host puts on the wire
   (``uuid16_le``); the naive val[12]|val[13]<<8 reading is pinned WRONG.
2. Feeding all 10 characteristics through ``bridge_ble_char_map_add``
   yields a 10-entry map -- no key-0 collapse -- with handles intact.
3. Mixed 16-bit + 128-bit characteristics coexist; map-full and duplicate
   key semantics stay as documented.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Optional

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from blebridge.suota import SUOTA_UUIDS, uuid16_le, uuid16_prefix  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PURE_SRC = _REPO_ROOT / "firmware" / "app" / "src" / "bridge_ble_pure.c"

# Mirror of bridge_ble_pure.h defaults (host build: no Kconfig autoconf.h).
_CHAR_MAX = 10
_UUID128_LEN = 16

# The keys the final-review checklist pins for the 10 SUOTA characteristics.
_EXPECTED_KEYS = {
    "mem_dev": 0x8082,
    "gpio_map": 0x7242,
    "mem_info": 0x6C53,
    "patch_len": 0x9D84,
    "patch_data": 0x4578,
    "serv_status": 0x5F78,
    "suota_version": 0x64B4,
    "patch_data_char_size": 0x42C3,
    "suota_mtu": 0xB7DE,
    "l2cap_psm": 0x61C8,
}


def uuid128_le(uuid: str) -> bytes:
    """Readable UUID form -> the 16-byte over-the-air little-endian array.

    This is exactly the ``val[]`` layout Zephyr's ``bt_uuid_128`` uses and
    what ``BT_UUID_128_ENCODE(w32, w1, w2, w3, w48)`` produces (LE48(w48)
    LE16(w3) LE16(w2) LE16(w1) LE32(w32) -- the readable form reversed).
    """
    raw = bytes.fromhex(uuid.replace("-", ""))
    assert len(raw) == _UUID128_LEN
    return raw[::-1]


class CharEnt(ctypes.Structure):
    """Mirror of bridge_char_ent_t (keep in sync with bridge_ble_pure.h)."""

    _fields_ = [
        ("uuid16", ctypes.c_uint16),
        ("value_handle", ctypes.c_uint16),
        ("ccc_handle", ctypes.c_uint16),
        ("props", ctypes.c_uint8),
    ]


class CharMap(ctypes.Structure):
    """Mirror of bridge_char_map_t (keep in sync with bridge_ble_pure.h)."""

    _fields_ = [
        ("count", ctypes.c_uint8),
        ("ents", CharEnt * _CHAR_MAX),
    ]


def _find_cc() -> Optional[str]:
    for name in ("cc", "clang", "gcc"):
        cc = shutil.which(name)
        if cc:
            return cc
    configured = sysconfig.get_config_var("CC")
    if configured:
        return shutil.which(configured.split()[0])
    return None


@pytest.fixture(scope="module")
def pure_lib(tmp_path_factory) -> ctypes.CDLL:
    """Compile bridge_ble_pure.c (pure C11, no Zephyr) once per module."""
    if not _PURE_SRC.is_file():
        pytest.fail(f"pure C source not found: {_PURE_SRC}", pytrace=False)
    cc = _find_cc()
    if cc is None:
        pytest.fail(
            "no C compiler found (tried 'cc', 'clang', 'gcc'); "
            "install the Xcode command line tools: xcode-select --install",
            pytrace=False,
        )
    out = tmp_path_factory.mktemp("bridge_ble_pure_build") / "libbridge_ble_pure.so"
    cmd = [
        cc,
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-O2",
        "-fPIC",
        "-shared",
        "-o",
        str(out),
        str(_PURE_SRC),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out.exists():
        pytest.fail(
            "compiling firmware/app/src/bridge_ble_pure.c failed "
            f"(exit {proc.returncode})\ncommand: {' '.join(cmd)}\n"
            f"stderr:\n{proc.stderr}",
            pytrace=False,
        )
    lib = ctypes.CDLL(str(out))
    lib.bridge_ble_uuid128_prefix.argtypes = [ctypes.c_char_p]
    lib.bridge_ble_uuid128_prefix.restype = ctypes.c_uint16
    lib.bridge_ble_char_map_clear.argtypes = [ctypes.POINTER(CharMap)]
    lib.bridge_ble_char_map_clear.restype = None
    lib.bridge_ble_char_map_add.argtypes = [
        ctypes.POINTER(CharMap),
        ctypes.c_uint16,
        ctypes.c_uint16,
        ctypes.c_uint16,
        ctypes.c_uint8,
    ]
    lib.bridge_ble_char_map_add.restype = ctypes.c_int
    lib.bridge_ble_char_map_find.argtypes = [
        ctypes.POINTER(CharMap),
        ctypes.c_uint16,
        ctypes.POINTER(CharEnt),
    ]
    lib.bridge_ble_char_map_find.restype = ctypes.c_bool
    return lib


@pytest.fixture
def char_map(pure_lib) -> CharMap:
    m = CharMap()
    pure_lib.bridge_ble_char_map_clear(ctypes.byref(m))
    return m


def _prefix(lib: ctypes.CDLL, uuid_or_val) -> int:
    """uuid16 key of a readable-form UUID string or a LE bytes value."""
    val = uuid_or_val if isinstance(uuid_or_val, bytes) else uuid128_le(uuid_or_val)
    return int(lib.bridge_ble_uuid128_prefix(val))


# ---------------------------------------------------------------------------
# 1) uuid16 prefix extraction == host semantics, byte for byte
# ---------------------------------------------------------------------------


def test_all_ten_suota_uuids_extract_the_pinned_keys(pure_lib):
    """Every SUOTA characteristic maps to its final-review key 0x8082..0x61C8."""
    for name, uuid in SUOTA_UUIDS.items():
        if name == "svc":
            continue  # the 16-bit service itself, not a characteristic
        assert uuid16_prefix(uuid) == _EXPECTED_KEYS[name], name
        assert _prefix(pure_lib, uuid) == _EXPECTED_KEYS[name], name


def test_prefix_bytes_equal_the_host_wire_encoding(pure_lib):
    """val[14],val[15] (LSB,MSB) == uuid16_le bytes: firmware key and host
    request payload agree byte-for-byte on the wire (protocol-contract.md
    section 4)."""
    for name, uuid in SUOTA_UUIDS.items():
        if name == "svc":
            continue
        val = uuid128_le(uuid)
        assert (val[14] | (val[15] << 8)) == uuid16_prefix(uuid), name
        assert bytes((val[14], val[15])) == uuid16_le(uuid), name
        assert _prefix(pure_lib, uuid) == uuid16_prefix(uuid), name


def test_naive_val12_val13_reading_is_pinned_wrong(pure_lib):
    """The review-checklist draft val[12]|val[13]<<8 is verified NOT the
    host semantics for vendor UUIDs (it reads the readable form's bytes
    3-4, e.g. 0xCAA8 for mem_dev) -- the regression guard for the endian
    verification performed in task-12."""
    mem_dev = uuid128_le(SUOTA_UUIDS["mem_dev"])
    naive = mem_dev[12] | (mem_dev[13] << 8)
    assert naive == 0xCAA8
    assert naive != 0x8082
    assert naive != uuid16_prefix(SUOTA_UUIDS["mem_dev"])
    assert _prefix(pure_lib, mem_dev) == 0x8082


def test_sig_base_16bit_form_roundtrip(pure_lib):
    """SIG-base consistency: the readable form 0000fef5-... starts with
    0x0000, and uuid128_prefix agrees with uuid16_prefix on it (host
    never addresses the service characteristic-style anyway).  The 16-bit
    short form 0xFEF5 lives at val[12]|val[13]<<8 of the expanded form --
    that is exactly what the firmware's BT_UUID_TYPE_16 branch yields for
    a SIG attribute (bt_uuid_16.val is host-endian 0xFEF5), so both
    branches implement the same contract on their native encoding."""
    svc = uuid128_le(SUOTA_UUIDS["svc"])
    assert svc[12] | (svc[13] << 8) == 0xFEF5          # short form @12..13
    assert _prefix(pure_lib, svc) == 0x0000            # readable-form prefix
    assert uuid16_prefix(SUOTA_UUIDS["svc"]) == 0x0000  # host agrees


# ---------------------------------------------------------------------------
# 2) the 10-characteristic map does not collapse
# ---------------------------------------------------------------------------


def test_ten_suota_chars_map_without_collapse(pure_lib, char_map):
    """The G2 killer scenario: 10 distinct 128-bit UUIDs -> 10 distinct
    non-zero keys, all handles intact (was: all key=0, dedup to 1)."""
    for i, (name, uuid) in enumerate(SUOTA_UUIDS.items()):
        if name == "svc":
            continue
        key = _prefix(pure_lib, uuid)
        assert key != 0
        rc = pure_lib.bridge_ble_char_map_add(
            ctypes.byref(char_map), key,
            0x0010 + i,        # value_handle
            0x0100 + i,        # ccc_handle
            0x02 | 0x10,       # props: read + notify
        )
        assert rc == 0, name

    assert char_map.count == 10

    seen = set()
    for i, (name, uuid) in enumerate(SUOTA_UUIDS.items()):
        if name == "svc":
            continue
        key = _prefix(pure_lib, uuid)
        ent = CharEnt()
        assert pure_lib.bridge_ble_char_map_find(
            ctypes.byref(char_map), key, ctypes.byref(ent)
        ), name
        assert ent.uuid16 == key
        assert ent.value_handle == 0x0010 + i, name
        assert ent.ccc_handle == 0x0100 + i, name
        assert ent.props == 0x12, name
        seen.add(key)
    assert len(seen) == 10  # no dedup collapse happened


# ---------------------------------------------------------------------------
# 3) mixed 16-bit + 128-bit coexistence and boundary semantics
# ---------------------------------------------------------------------------


def test_mixed_16bit_and_128bit_chars_coexist(pure_lib, char_map):
    """A SIG 16-bit characteristic (key = its 16-bit value) and 128-bit
    vendor characteristics (key = readable-form prefix) share one map
    without colliding -- the dm_completed_cb dual-branch contract."""
    mixed = [
        (0x2A00, None),                       # Device Name (16-bit SIG)
        (uuid16_prefix(SUOTA_UUIDS["mem_dev"]),
         uuid128_le(SUOTA_UUIDS["mem_dev"])),
        (uuid16_prefix(SUOTA_UUIDS["patch_data"]),
         uuid128_le(SUOTA_UUIDS["patch_data"])),
        (uuid16_prefix(SUOTA_UUIDS["serv_status"]),
         uuid128_le(SUOTA_UUIDS["serv_status"])),
    ]
    for i, (key, val128) in enumerate(mixed):
        if val128 is not None:
            assert _prefix(pure_lib, val128) == key
        rc = pure_lib.bridge_ble_char_map_add(
            ctypes.byref(char_map), key, 0x20 + i, 0, 0x02
        )
        assert rc == 0

    assert char_map.count == len(mixed)
    for i, (key, _) in enumerate(mixed):
        ent = CharEnt()
        assert pure_lib.bridge_ble_char_map_find(
            ctypes.byref(char_map), key, ctypes.byref(ent)
        )
        assert ent.value_handle == 0x20 + i


def test_duplicate_key_add_is_idempotent(pure_lib, char_map):
    """Documented map semantics: re-adding a key returns 0 and keeps the
    FIRST entry (dm_completed_cb now detects this dedup hit with a
    find-before-add and raises EVT.DEBUG WARN with the key)."""
    key = uuid16_prefix(SUOTA_UUIDS["mem_dev"])
    val = uuid128_le(SUOTA_UUIDS["mem_dev"])
    assert _prefix(pure_lib, val) == key

    assert pure_lib.bridge_ble_char_map_add(
        ctypes.byref(char_map), key, 0x0042, 0x0043, 0x02
    ) == 0
    # Same key via the same 128-bit UUID again (or a colliding one): no-op.
    assert pure_lib.bridge_ble_char_map_add(
        ctypes.byref(char_map), key, 0x0099, 0x0000, 0x00
    ) == 0

    ent = CharEnt()
    assert pure_lib.bridge_ble_char_map_find(
        ctypes.byref(char_map), key, ctypes.byref(ent)
    )
    assert ent.value_handle == 0x0042  # first entry wins
    assert char_map.count == 1


def test_map_full_reports_enospc(pure_lib, char_map):
    """11th distinct characteristic -> -1 (entry dropped), matching the
    header contract; the first 10 stay intact."""
    for i in range(_CHAR_MAX):
        assert pure_lib.bridge_ble_char_map_add(
            ctypes.byref(char_map), 0x1000 + i, i, 0, 0x02
        ) == 0
    assert char_map.count == _CHAR_MAX
    assert pure_lib.bridge_ble_char_map_add(
        ctypes.byref(char_map), 0x2000, 0, 0, 0
    ) == -1
    ent = CharEnt()
    assert not pure_lib.bridge_ble_char_map_find(
        ctypes.byref(char_map), 0x2000, ctypes.byref(ent)
    )


# ---------------------------------------------------------------------------
# 3) FR-H event payload builders (cross-language wire contract)
# ---------------------------------------------------------------------------


def _bind_evt_builders(lib) -> None:
    """ctypes prototypes of the pure event builders used by these tests."""
    lib.bridge_ble_evt_conn_build.argtypes = [
        ctypes.c_uint8,
        ctypes.c_uint8,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.bridge_ble_evt_conn_build.restype = ctypes.c_int
    lib.bridge_ble_evt_disc_build.argtypes = [
        ctypes.c_uint8,
        ctypes.c_uint8,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.bridge_ble_evt_disc_build.restype = ctypes.c_int
    lib.bridge_ble_evt_gattx_write_build.argtypes = [
        ctypes.c_uint16,
        ctypes.c_uint16,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.bridge_ble_evt_gattx_write_build.restype = ctypes.c_int


@pytest.fixture
def evt_lib(pure_lib):
    _bind_evt_builders(pure_lib)
    return pure_lib


def test_evt_conn_and_disc_carry_role_byte(evt_lib):
    """FR-H: EVT.CONN/EVT.DISC payloads carry the role byte after the
    subtype (0 = central-role link, 1 = peripheral-role link).

    v1.4.2: the ``addr`` argument is Zephyr's little-endian ``val`` (the
    internal order), while the wire carries **display order** (reversed)."""
    addr = bytes.fromhex("020304050607")  # val order (Zephyr internal)
    wire = addr[::-1]  # display order on the wire
    buf = ctypes.create_string_buffer(16)

    rc = evt_lib.bridge_ble_evt_conn_build(0x00, 0x01, addr, buf, len(buf))
    assert rc == 9
    assert buf.raw[:rc] == b"\x00\x00\x01" + wire

    rc = evt_lib.bridge_ble_evt_conn_build(0x01, 0x00, addr, buf, len(buf))
    assert rc == 9
    assert buf.raw[:rc] == b"\x00\x01\x00" + wire

    rc = evt_lib.bridge_ble_evt_disc_build(0x01, 0x13, buf, len(buf))
    assert rc == 3
    assert buf.raw[:rc] == b"\x00\x01\x13"

    rc = evt_lib.bridge_ble_evt_disc_build(0x00, 0x16, buf, len(buf))
    assert rc == 3
    assert buf.raw[:rc] == b"\x00\x00\x16"


def test_evt_gattx_write_builder_layout(evt_lib):
    """EVT.GATTX subtype 0x01 (peripheral-role write):
    [0x01][uuid16 LE][value handle LE][data]."""
    buf = ctypes.create_string_buffer(64)

    rc = evt_lib.bridge_ble_evt_gattx_write_build(
        0xFFE1, 0x0012, b"\xde\xad\xbe\xef", 4, buf, len(buf)
    )
    assert rc == 9
    assert buf.raw[:rc] == b"\x01\xe1\xff\x12\x00\xde\xad\xbe\xef"

    # zero-length write still carries the fixed prefix
    rc = evt_lib.bridge_ble_evt_gattx_write_build(0xFFE1, 0x0012, b"", 0, buf, len(buf))
    assert rc == 5
    assert buf.raw[:rc] == b"\x01\xe1\xff\x12\x00"

    # capacity one short reports the negative required length
    need = 1 + 2 + 2 + 3
    rc = evt_lib.bridge_ble_evt_gattx_write_build(
        0xFFE1, 0x0012, b"\x01\x02\x03", 3, buf, need - 1
    )
    assert rc == -need


# ---------------------------------------------------------------------------
# 4) FR-H fix round 1: advertising-intent state machine (pure, host-tested)
# ---------------------------------------------------------------------------


@pytest.fixture
def intent_lib(pure_lib):
    pure_lib.bridge_ble_adv_intent_step.argtypes = [
        ctypes.POINTER(ctypes.c_bool),
        ctypes.c_int,
    ]
    pure_lib.bridge_ble_adv_intent_step.restype = ctypes.c_int
    return pure_lib


def test_adv_intent_start_sets_wanted_no_action(intent_lib):
    wanted = ctypes.c_bool(False)
    act = intent_lib.bridge_ble_adv_intent_step(
        ctypes.byref(wanted), 0
    )  # BRIDGE_ADV_EV_START
    assert act == 0  # BRIDGE_ADV_ACT_NONE
    assert wanted.value is True


def test_adv_intent_stop_clears_wanted_no_action(intent_lib):
    wanted = ctypes.c_bool(True)
    act = intent_lib.bridge_ble_adv_intent_step(
        ctypes.byref(wanted), 1
    )  # BRIDGE_ADV_EV_STOP
    assert act == 0
    assert wanted.value is False


def test_adv_intent_link_loss_restarts_only_when_wanted(intent_lib):
    """Defect 3: a lost peripheral link restarts advertising only when the
    user's adv_start intent is still set; an explicit adv_stop silenced it."""
    wanted = ctypes.c_bool(True)
    act = intent_lib.bridge_ble_adv_intent_step(
        ctypes.byref(wanted), 2
    )  # BRIDGE_ADV_EV_LINK_LOST
    assert act == 1  # BRIDGE_ADV_ACT_RESTART
    assert wanted.value is True  # state survives so later losses recover too

    wanted = ctypes.c_bool(False)
    act = intent_lib.bridge_ble_adv_intent_step(ctypes.byref(wanted), 2)
    assert act == 0
    assert wanted.value is False


def test_adv_intent_user_flow_start_stop_start_linkloss(intent_lib):
    """Full user flow: start -> (link loss restarts) -> stop silences ->
    a later start re-arms -> link loss restarts again."""
    wanted = ctypes.c_bool(False)

    def step(evt: int) -> int:
        return intent_lib.bridge_ble_adv_intent_step(ctypes.byref(wanted), evt)

    assert step(0) == 0 and wanted.value
    assert step(2) == 1  # clean/crashed disconnect -> restart
    assert step(1) == 0 and not wanted.value  # explicit adv_stop
    assert step(2) == 0  # link loss does NOT restart after explicit stop
    assert step(0) == 0 and wanted.value  # re-arm
    assert step(2) == 1  # and recovery is live again
