"""Source-contract regression test for the BLE central-role probe (FR-H).

The "board GATT server 100% unresponsive to service discovery" investigation
(2026-09-26) ended with a TEST-HARNESS root cause: on macOS 26 CoreBluetooth
invokes the CLASSIC ``peripheral:didDiscoverServices:`` selector; a delegate
implementing only the ``...error:`` variant never hears about discovery and
its watchdog times out, which is indistinguishable from a dead GATT server
(connection stays up, notify commands still answer).  This test pins that
both selector variants exist for every discovery/notification callback in
tools/ble_central_probe.py, so the probe cannot regress into producing
false "GATT server dead" evidence again.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_SRC = _REPO_ROOT / "tools" / "ble_central_probe.py"

# (classic selector, error-selector base name) pairs whose classic form is
# mandatory: macOS 26 fires the classic variant for these callbacks.
_CALLBACKS = (
    "peripheral_didDiscoverServices_",
    "peripheral_didDiscoverCharacteristicsForService_",
    "peripheral_didUpdateNotificationStateForCharacteristic_",
    "peripheral_didUpdateValueForCharacteristic_",
)


def _probe_source() -> str:
    assert _PROBE_SRC.is_file(), f"missing {_PROBE_SRC}"
    return _PROBE_SRC.read_text(encoding="utf-8")


def test_probe_file_present_and_documented() -> None:
    src = _probe_source()
    assert "FR-H 根因备忘" in src, "selector-trap memo must stay in the probe"
    assert "classic" in src.lower()


def test_every_delegate_callback_has_classic_selector() -> None:
    """Each guarded callback must define BOTH the classic 2-arg variant and
    the 3-arg error variant, with the classic delegating to the error one.
    A classic-only or error-only definition reintroduces the FR-H false
    "GATT server unresponsive" failure mode."""
    src = _probe_source()
    for cb in _CALLBACKS:
        classic = re.search(rf"def {cb}\(self", src)
        assert classic is not None, f"{cb} (classic selector) missing"
        base = cb.rstrip("_")
        err = re.search(rf"def {base}_error_\(self", src)
        assert err is not None, f"{base}_error_ variant missing"
        # classic body delegates to the error variant (same class scope,
        # appears before the next def after the classic one)
        tail = src[classic.start():err.start() if err.start() > classic.start() else classic.end()]
        assert "self." + base + "_error_(" in tail or "_error_(per" in tail, (
            f"{cb} must delegate to {base}_error_ so both macOS selector "
            "spellings share one code path"
        )


def test_error_variants_are_never_the_only_definition() -> None:
    """Guard the actual 2026-09-26 regression shape: a delegate class that
    defines only ``..._error_`` for a guarded callback."""
    src = _probe_source()
    for cb in _CALLBACKS:
        base = cb.rstrip("_")
        classic_defs = len(re.findall(rf"def {cb}\(self", src))
        assert classic_defs >= 1, (
            f"{cb}: classic selector missing — macOS 26 never calls "
            f"{base}_error_ alone; discovery results would be silently dropped"
        )
