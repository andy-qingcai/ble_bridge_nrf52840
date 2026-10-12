"""Serial port candidate discovery for the blebridge bridge (Task 4).

Pure-stdlib glob over the OS-specific device-node naming conventions; no
pyserial port scan (keeps daemon/CLI startup dependency-light and
deterministic in tests).  Candidates are sorted and de-duplicated — the
caller picks the first one that opens.
"""

from __future__ import annotations

import glob
import sys

__all__ = ["list_candidates"]

# sys.platform -> glob patterns of plausible bridge CDC ACM / UART nodes.
_PATTERNS: dict[str, list[str]] = {
    "darwin": ["/dev/cu.usbmodem*", "/dev/cu.usbserial*"],
    # Linux and every other platform: classic USB-serial adapter nodes.
}
_FALLBACK = ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usbmodem*", "/dev/cu.usbserial*"]


def list_candidates() -> list[str]:
    """Return plausible bridge serial ports for this platform, sorted.

    macOS: ``/dev/cu.*`` callout devices only (``tty.*`` would hang on open
    while waiting for DCD).  Other platforms: ``ttyUSB*``/``ttyACM*``.
    """
    patterns = _PATTERNS.get(sys.platform, _FALLBACK)
    found: set[str] = set()
    for pattern in patterns:
        found.update(glob.glob(pattern))
    return sorted(found)
