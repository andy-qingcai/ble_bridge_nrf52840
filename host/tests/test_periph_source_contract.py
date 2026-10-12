"""Source-contract regression tests for the FR-H peripheral firmware module.

The peripheral GATT behaviour itself only runs on the nRF (Zephyr BT stack),
so the fix-round-1/2 real-device defects are guarded here at the source
level: each test pins the exact construction whose absence or wrong shape
produced the defect.  The pure decision logic they depend on is tested for
real in test_ble_pure_map.py (compiled C).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PERIPH_SRC = _REPO_ROOT / "firmware" / "app" / "src" / "bridge_periph.c"
_KCONFIG = _REPO_ROOT / "firmware" / "app" / "Kconfig"


def _periph_source() -> str:
    assert _PERIPH_SRC.is_file()
    return _PERIPH_SRC.read_text(encoding="utf-8")


def test_dynamic_service_decl_uses_primary_type_uuid() -> None:
    """Defect 1 (real device 2026-09-25: 0xAAAA service invisible on air).

    The primary service declaration attribute must carry the DECLARATION
    TYPE uuid (BT_UUID_GATT_PRIMARY, 0x2800) and keep the served UUID in
    user_data -- byte-equivalent to BT_GATT_PRIMARY_SERVICE.  attr->uuid =
    the service UUID itself (the pre-fix shape) makes "read by group type
    0x2800" never match, hiding the whole service from any central.
    """
    src = _periph_source()
    m = re.search(
        r"/\* \[0\] primary service declaration.*?"
        r"attr->uuid = (?P<uuid>[^;]+);.*?"
        r"attr->user_data = (?P<ud>[^;]+);",
        src,
        re.DOTALL,
    )
    assert m is not None, "primary service declaration block not found"
    assert m.group("uuid").strip() == "BT_UUID_GATT_PRIMARY"
    assert "svc_uuid" in m.group("ud")  # served UUID rides in user_data
    assert "attr->uuid = &slot->svc_uuid.uuid;" not in src


def test_gatt_define_checks_register_result() -> None:
    """Defect 1, direction (3): bt_gatt_service_register() failures must
    reset the slot and surface as a status byte, never be swallowed."""
    src = _periph_source()
    m = re.search(
        r"int err = bt_gatt_service_register\(&slot->svc\);.*?"
        r"if \(err != 0\) \{(?P<body>.*?)\}",
        src,
        re.DOTALL,
    )
    assert m is not None, "register result is not checked"
    body = m.group("body")
    assert "dyn_slot_reset(slot)" in body  # rollback, no half-built slot
    assert "BRIDGE_STATUS_ERR_NOT_READY" in body  # verdict reaches the host


def test_preset_cccd_standard_wiring_no_shadow() -> None:
    """Defect 2, round 1 (2026-09-25: subscribing 0xFFE2 -> ATT error 10)
    and round 2 (2026-09-26: CCCD write accepted but notify still 0x74 --
    the "shadow CCCD").

    The preset notify characteristic must carry a CCCD whose state lives
    in a NAMED STATIC (RAM) bt_gatt_ccc_managed_user_data wired via
    BT_GATT_CCC_MANAGED: BT_GATT_SERVICE_DEFINE's attrs array is const, so
    the bare BT_GATT_CCC() compound literal landed in rodata --
    bt_gatt_attr_write_ccc "succeeded" at the ATT layer while the cfg/value
    writes were silently dropped (no subscriber registered, cfg_changed
    never fired).  The bare compound-literal form is banned here.
    """
    src = _periph_source()
    m = re.search(
        r"static struct bt_gatt_ccc_managed_user_data (?P<name>\w+) = \{"
        r"(?P<init>.*?)\};",
        src,
        re.DOTALL,
    )
    assert m is not None, "named static CCC instance not found"
    assert m.group("name") == "chr_b_ccc"
    assert ".cfg_changed = chr_b_ccc_changed_cb" in m.group("init")
    m = re.search(
        r"BT_GATT_SERVICE_DEFINE\(preset_svc,(?P<svc>.*?)\n\);", src, re.DOTALL
    )
    assert m is not None, "preset service definition not found"
    svc = m.group("svc")
    assert "BT_UUID_DECLARE_16(PRESET_CHR_B_UUID16)" in svc  # 0xFFE2 defined
    assert "BT_GATT_CHRC_NOTIFY" in svc
    ccc = re.search(r"BT_GATT_CCC_MANAGED\(&chr_b_ccc, ([^)]*)\)", svc)
    assert ccc is not None, "0xFFE2 CCCD not wired to the named instance"
    assert "BT_GATT_PERM_READ | BT_GATT_PERM_WRITE" in ccc.group(1)
    # The shadow shape is banned: the bare BT_GATT_CCC( compound literal
    # must not be *invoked* anywhere (mentions in comments are fine -- the
    # pinning here exists precisely to explain why).
    assert re.search(r"(?m)^\s*BT_GATT_CCC\(", src) is None


def test_dynamic_cccd_standard_wiring_and_writable_storage() -> None:
    """Round 2, decree point 2: gatt_define's notify characteristics need
    the same standard CCCD wiring -- BT_UUID_GATT_CCC + the stack's ccc
    read/write handlers + the slot's static managed instance with the
    cfg_changed callback -- and the storage must stay writable (never
    const)."""
    src = _periph_source()
    assert re.search(
        r"attr->uuid = BT_UUID_GATT_CCC;", src
    ), "dynamic CCCD attribute missing"
    assert "attr->read = bt_gatt_attr_read_ccc;" in src
    assert "attr->write = bt_gatt_attr_write_ccc;" in src
    assert "attr->user_data = &slot->ccc[i];" in src
    assert "slot->ccc[i].cfg_changed = dyn_ccc_changed_cb;" in src
    # Writable storage: dyn_slots is a plain static array, never const.
    assert re.search(r"static dyn_slot_t dyn_slots\[", src)
    assert not re.search(r"const\s+dyn_slot_t\s+dyn_slots\[", src)


def test_notify_preconditions_on_cccd_subscription_state() -> None:
    """Round 2, decree point 3 (host-testable form of the mirror-flip use
    case): the cfg_changed callback and CMD.notify must share ONE mirror
    flag per characteristic, so a peer subscription flips it (notify then
    succeeds, no 0x74) and "no subscriber" keeps answering 0x74 without
    touching the air or the pushed value."""
    src = _periph_source()
    # Preset mirror: written by the cfg_changed callback from the CCCD
    # value (notify bit), read by notify() through the resolver.
    cb = re.search(
        r"static void chr_b_ccc_changed_cb\(.*?\n\}",
        src,
        re.DOTALL,
    )
    assert cb is not None, "preset cfg_changed callback not found"
    assert "atomic_set(&chr_b_subscribed" in cb.group(0)
    assert "(value & BT_GATT_CCC_NOTIFY)" in cb.group(0)
    assert "out->sub_flag = &chr_b_subscribed;" in src
    # Dynamic mirror: same wiring per slot entry.
    assert "atomic_set(&dyn_slots[s].sub_flag[c]" in src
    assert "out->sub_flag = &slot->sub_flag[c];" in src
    # notify() checks the flag (and maps a stack -ENOTCONN to 0x74 anyway).
    m = re.search(
        r"uint8_t bridge_periph_notify\(.*?(?=\nuint8_t bridge_periph_set_char)",
        src,
        re.DOTALL,
    )
    assert m is not None
    body = m.group(0)
    check_pos = body.find("atomic_get(target.sub_flag) == 0")
    notify_pos = body.find("bt_gatt_notify(")
    assert 0 < check_pos < notify_pos, "subscription check must precede notify"
    assert "BRIDGE_STATUS_ERR_NOT_READY" in body  # 0x74 on no subscriber


def test_link_loss_auto_restart_wired_and_configurable() -> None:
    """Defect 3 (real device 2026-09-25: no auto re-advertise after any
    disconnect): the disconnected callback feeds the pure intent state
    machine and restarts via a work item (a synchronous bt_le_adv_start
    must not run on the BT RX thread); an explicit adv_stop clears the
    intent; the behaviour is Kconfig-switchable (default y)."""
    src = _periph_source()
    assert "BRIDGE_ADV_EV_LINK_LOST" in src
    assert "BRIDGE_ADV_ACT_RESTART" in src
    assert "adv_restart_work_fn" in src
    assert "k_work_submit(&adv_restart_work)" in src
    assert "IS_ENABLED(CONFIG_BRIDGE_ADV_AUTO_RESTART)" in src
    assert "adv_intent_event(BRIDGE_ADV_EV_STOP)" in src  # user stop clears
    kconf = _KCONFIG.read_text(encoding="utf-8")
    assert "config BRIDGE_ADV_AUTO_RESTART" in kconf
    assert re.search(
        r"config BRIDGE_ADV_AUTO_RESTART.*?default y", kconf, re.DOTALL
    )
