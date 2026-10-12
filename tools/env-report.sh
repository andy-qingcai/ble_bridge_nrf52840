#!/usr/bin/env bash
#
# blebridge env-report.sh — 本机环境版本报告（只读；缺项显示 MISSING，永不以错误退出）。
#
# 报告项: OS / brew / python3.12 / host venv+pytest / west / cmake /
#         NCS 版本 / ZEPHYR_BASE / Zephyr SDK / JLink / 串口(/dev/cu.usbmodem*)
# 支持与 bootstrap-env.sh 相同的 NCS_DIR / SDK_DIR 环境覆盖。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NCS_DIR="${NCS_DIR:-$HOME/blebridge/ncs}"
BRIDGE_HOME="${BRIDGE_HOME:-$HOME/.blebridge}"
NCS_VENV="$BRIDGE_HOME/ncs-venv"
SDK_DIR="${SDK_DIR:-$BRIDGE_HOME/sdk}"

MISSING="\033[33mMISSING\033[0m"
row() { printf '%-16b %b\n' "$1" "$2"; }

# --- OS -------------------------------------------------------------------
OS_STR="$(uname -s) $(uname -m)"
if [ "$(uname -s)" = "Darwin" ]; then
  OS_STR="$OS_STR (macOS $(sw_vers -productVersion 2>/dev/null || echo '?'))"
fi
row "OS" "$OS_STR"

# --- brew ------------------------------------------------------------------
if command -v brew >/dev/null 2>&1; then
  row "brew" "$(command -v brew) ($(brew --version 2>/dev/null | head -n 1))"
else
  row "brew" "$MISSING"
fi

# --- python3.12 ------------------------------------------------------------
if command -v python3.12 >/dev/null 2>&1; then
  row "python3.12" "$(command -v python3.12) ($(python3.12 --version 2>&1))"
else
  row "python3.12" "$MISSING"
fi

# --- host venv + pytest ----------------------------------------------------
HOST_VENV="$REPO_ROOT/host/.venv"
if [ -x "$HOST_VENV/bin/python" ]; then
  HOST_PY="$("$HOST_VENV/bin/python" --version 2>&1)"
  PYTEST_STR="自检失败: import blebridge/pytest"
  if "$HOST_VENV/bin/python" -c 'import blebridge' >/dev/null 2>&1 &&
     "$HOST_VENV/bin/python" -m pytest --version >/dev/null 2>&1; then
    PYTEST_STR="import blebridge OK, $("$HOST_VENV/bin/python" -m pytest --version 2>&1 | head -n 1)"
  fi
  row "host venv" "$HOST_VENV ($HOST_PY, $PYTEST_STR)"
else
  row "host venv" "$MISSING (期望 $HOST_VENV)"
fi

# --- west ------------------------------------------------------------------
WEST_BIN="$(command -v west 2>/dev/null || true)"
if [ -z "$WEST_BIN" ] && [ -x "$NCS_VENV/bin/west" ]; then
  WEST_BIN="$NCS_VENV/bin/west"
fi
if [ -n "$WEST_BIN" ]; then
  row "west" "$WEST_BIN ($("$WEST_BIN" --version 2>&1))"
else
  row "west" "$MISSING (期望 $NCS_VENV/bin/west)"
fi

# --- cmake -----------------------------------------------------------------
if command -v cmake >/dev/null 2>&1; then
  row "cmake" "$(command -v cmake) ($(cmake --version 2>/dev/null | head -n 1))"
else
  row "cmake" "$MISSING"
fi

# --- NCS 版本 ---------------------------------------------------------------
if [ -d "$NCS_DIR/.west" ]; then
  NCS_VER="$(git -C "$NCS_DIR" -C nrf describe --tags 2>/dev/null || echo '未知(无 tag)')"
  ZEP_VER="$(git -C "$NCS_DIR" -C zephyr describe --tags 2>/dev/null || echo '?')"
  row "NCS" "$NCS_DIR (nrf $NCS_VER, zephyr $ZEP_VER)"
else
  row "NCS" "$MISSING (期望 west workspace 于 $NCS_DIR)"
fi

# --- ZEPHYR_BASE ------------------------------------------------------------
ZB="${ZEPHYR_BASE:-}"
if [ -z "$ZB" ] && [ -d "$NCS_DIR/.west" ]; then
  ZB_REL="zephyr"
  if [ -x "$NCS_VENV/bin/west" ]; then
    ZB_REL="$(cd "$NCS_DIR" && "$NCS_VENV/bin/west" config zephyr.base 2>/dev/null || echo zephyr)"
  fi
  ZB="$NCS_DIR/$ZB_REL"
fi
if [ -n "$ZB" ] && [ -d "$ZB" ]; then
  row "ZEPHYR_BASE" "$ZB"
else
  row "ZEPHYR_BASE" "$MISSING (${ZB:-未解析})"
fi

# --- Zephyr SDK -------------------------------------------------------------
SDK_FOUND=""
for sdk_dir in "$BRIDGE_HOME"/sdk/zephyr-sdk-*/ "$SDK_DIR"/zephyr-sdk-*/; do
  if [ -f "${sdk_dir}sdk_version" ]; then
    SDK_FOUND="$(cat "${sdk_dir}sdk_version") @ ${sdk_dir%/}"
  fi
done
if [ -n "$SDK_FOUND" ]; then
  row "Zephyr SDK" "$SDK_FOUND"
else
  row "Zephyr SDK" "$MISSING (期望 $SDK_DIR/zephyr-sdk-1.0.x)"
fi

# --- JLink ------------------------------------------------------------------
JLINK_BIN="$(command -v JLinkExe 2>/dev/null || true)"
if [ -z "$JLINK_BIN" ] && [ -x /usr/local/bin/JLinkExe ]; then
  JLINK_BIN="/usr/local/bin/JLinkExe"
fi
if [ -z "$JLINK_BIN" ] && [ -x /opt/SEGGER/JLink/JLinkExe ]; then
  JLINK_BIN="/opt/SEGGER/JLink/JLinkExe"
fi
if [ -n "$JLINK_BIN" ]; then
  row "JLink" "$JLINK_BIN"
elif [ -d /Applications/SEGGER ]; then
  row "JLink" "/Applications/SEGGER (JLinkExe 不在 PATH)"
else
  row "JLink" "$MISSING"
fi

# --- 串口 --------------------------------------------------------------------
SERIALS="$(ls /dev/cu.usbmodem* 2>/dev/null || true)"
if [ -n "$SERIALS" ]; then
  row "串口" "$(printf '%s' "$SERIALS" | tr '\n' ' ')"
else
  row "串口" "$MISSING (无 /dev/cu.usbmodem*)"
fi

exit 0
