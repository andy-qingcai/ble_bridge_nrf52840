#!/usr/bin/env bash
#
# blebridge bootstrap-env.sh — 固件/主机工具链环境引导（幂等，可重复执行）。
#
# 权威安装路径（本机实测成功）
# 与 progress.md "环境"节 / Task 9 环境 Ruling）：
#   - west 路线（nrfutil pip 版 sdk-manager 在本机无该子命令，不采用）
#   - NCS v3.4.1（west init -m .../sdk-nrf --mr v3.4.1），拉取必须 git HTTP/1.1
#   - Zephyr SDK 硬性要求 1.0.x（0.17.4 被 FindHostTools 拒绝），实装 1.0.1
#   - 冒烟构建: west build -p -b nrf52840dk/nrf52840（hello_world + firmware/app）
#
# 用法:
#   tools/bootstrap-env.sh [--with-uart0]
# 注意: 兼容 macOS 系统自带 bash 3.2（env bash 的实际解析目标）——
#   变量后紧跟全角标点时必须写 ${var} 花括号，否则 bash 3.2 会把
#   全角标点首字节并入变量名导致 "unbound variable"。
# 开关:
#   SKIP_NCS=1        只执行步骤 1-2（host 侧），跳过 NCS/SDK/构建
#   NCS_DIR=<dir>     自定义 NCS workspace（默认 ~/blebridge/ncs）
#   SDK_DIR=<dir>     自定义 SDK 根目录（默认 ~/.blebridge/sdk）
#   SMOKE_DIR=<dir>   自定义冒烟构建目录（默认 $NCS_DIR/build-t7，与既有 build 隔离）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NCS_DIR="${NCS_DIR:-$HOME/blebridge/ncs}"
BRIDGE_HOME="${BRIDGE_HOME:-$HOME/.blebridge}"
NCS_VENV="$BRIDGE_HOME/ncs-venv"
SDK_DIR="${SDK_DIR:-$BRIDGE_HOME/sdk}"
SMOKE_DIR="${SMOKE_DIR:-$NCS_DIR/build-t7}"

# 实测硬编码版本。注意: 换 NCS 版本时 Zephyr SDK 最低要求可能变化（见步骤 5 复核）。
NCS_REVISION="v3.4.1"
NCS_MANIFEST_URL="https://github.com/nrfconnect/sdk-nrf"
ZEPHYR_SDK_VERSION="1.0.1"
# 实测通过的组合: west 1.5.0 + pyelftools + jsonschema
WEST_PIN="west==1.5.0"

WITH_UART0=0
for arg in "$@"; do
  case "$arg" in
    --with-uart0) WITH_UART0=1 ;;
    -h|--help)
      sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: ${arg}（支持 --with-uart0）" >&2; exit 2 ;;
  esac
done

ok()   { printf '    \033[32mOK\033[0m   %s\n' "$*"; }
skip() { printf '    \033[36mSKIP\033[0m %s\n' "$*"; }
warn() { printf '    \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '    \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n== [%s/8] %s\n' "$1" "$2"; }

STEPS_OK=0; STEPS_SKIP=0; STEPS_WARN=0
note_result() { case "$1" in ok) STEPS_OK=$((STEPS_OK+1));; skip) STEPS_SKIP=$((STEPS_SKIP+1));; warn) STEPS_WARN=$((STEPS_WARN+1));; esac; }

# ---------------------------------------------------------------------------
# 步骤 1: brew python@3.12
# ---------------------------------------------------------------------------
step 1 "brew python@3.12"
if command -v brew >/dev/null 2>&1; then
  if command -v python3.12 >/dev/null 2>&1 &&
     python3.12 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
    skip "python3.12 已安装: $(command -v python3.12) ($(python3.12 --version))"
    note_result skip
  else
    ok "brew install python@3.12 ..."
    brew install python@3.12
    command -v python3.12 >/dev/null 2>&1 || die "安装后仍找不到 python3.12（PATH 检查: $(brew --prefix)/bin）"
    ok "python3.12 就绪: $(python3.12 --version)"
    note_result ok
  fi
else
  die "未找到 brew（https://brew.sh 先装 Homebrew），无法引导 python@3.12"
fi

# ---------------------------------------------------------------------------
# 步骤 2: host venv（仓库 host/.venv）
#   契约: 已存在则【不重建】（有并行任务在用）；检测 import blebridge + pytest。
# ---------------------------------------------------------------------------
step 2 "host venv (host/.venv)"
HOST_VENV="$REPO_ROOT/host/.venv"
if [ -x "$HOST_VENV/bin/python" ]; then
  if "$HOST_VENV/bin/python" -c 'import blebridge' >/dev/null 2>&1 &&
     "$HOST_VENV/bin/python" -m pytest --version >/dev/null 2>&1; then
    HOST_PY="$("$HOST_VENV/bin/python" --version 2>&1)"
    if "$HOST_VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
      skip "host venv 自检通过: import blebridge + pytest（${HOST_PY}）"
      note_result skip
    else
      warn "host venv 自检通过（import blebridge + pytest），但为 $HOST_PY — 未在支持矩阵（3.12）。按契约已存在则不重建；如需对齐请人工重建。"
      note_result warn
    fi
  else
    warn "host venv 存在但自检失败（import blebridge / pytest 不可用）。按契约不重建（可能有并行任务在用），请人工排查。"
    note_result warn
  fi
else
  ok "host venv 缺失，用 python3.12 创建并安装 host[dev] ..."
  python3.12 -m venv "$HOST_VENV"
  "$HOST_VENV/bin/python" -m pip install --quiet --upgrade pip
  "$HOST_VENV/bin/python" -m pip install --quiet -e "$REPO_ROOT/host[dev]"
  "$HOST_VENV/bin/python" -c 'import blebridge' >/dev/null 2>&1 || die "host venv 建好后 import blebridge 失败"
  ok "host venv 创建完成: $("$HOST_VENV/bin/python" --version), $("$HOST_VENV/bin/python" -m pytest --version 2>&1 | head -1)"
  note_result ok
fi

if [ "${SKIP_NCS:-0}" = "1" ]; then
  printf '\n== SKIP_NCS=1: 按开关跳过步骤 3-8（NCS/SDK/构建）\n'
  printf '\n== 总结: OK=%d SKIP=%d WARN=%d（部分执行: 仅步骤 1-2）\n' "$STEPS_OK" "$STEPS_SKIP" "$STEPS_WARN"
  exit 0
fi

# ---------------------------------------------------------------------------
# 步骤 3: NCS venv（python3.12 + west + pyelftools + jsonschema）
# ---------------------------------------------------------------------------
step 3 "NCS venv ($NCS_VENV)"
ncs_venv_ok() {
  [ -x "$NCS_VENV/bin/west" ] || return 1
  "$NCS_VENV/bin/python" -c 'import elftools, jsonschema' >/dev/null 2>&1
}
if ncs_venv_ok; then
  skip "west $("$NCS_VENV/bin/west" --version | sed 's/West version: //') + pyelftools + jsonschema 就绪（python $("$NCS_VENV/bin/python" --version 2>&1)）"
  note_result skip
else
  ok "创建 venv 并安装 $WEST_PIN pyelftools jsonschema ..."
  rm -rf "$NCS_VENV"
  python3.12 -m venv "$NCS_VENV"
  "$NCS_VENV/bin/python" -m pip install --quiet --upgrade pip
  # $WEST_PIN: west 1.5.0 为本机实测版本，钉住保证可复现
  "$NCS_VENV/bin/python" -m pip install --quiet "$WEST_PIN" pyelftools jsonschema
  ncs_venv_ok || die "NCS venv 安装后自检失败"
  ok "NCS venv 就绪: $("$NCS_VENV/bin/west" --version)"
  note_result ok
fi
WEST="$NCS_VENV/bin/west"

# ---------------------------------------------------------------------------
# 步骤 4: NCS 源码（${NCS_DIR}，west workspace）
#   契约: 无 .west 才 init/update；update 失败重试 x3；
#   仅当检出 HTTP2 framing 错误时才设 git 全局 http.version（唯一全局副作用）。
# ---------------------------------------------------------------------------
step 4 "NCS 源码 ($NCS_DIR @ $NCS_REVISION)"
if [ -d "$NCS_DIR/.west" ]; then
  skip "west workspace 已存在（$(git -C "$NCS_DIR" -C nrf describe --tags 2>/dev/null || echo 'nrf 未知版本')）"
  note_result skip
else
  ok "west init -m $NCS_MANIFEST_URL --mr $NCS_REVISION"
  mkdir -p "$(dirname "$NCS_DIR")"
  "$WEST" init -m "$NCS_MANIFEST_URL" --mr "$NCS_REVISION" "$NCS_DIR"
  UPDATE_LOG="$(mktemp)"
  trap 'rm -f "$UPDATE_LOG"' EXIT
  updated=0
  for attempt in 1 2 3; do
    ok "west update（第 $attempt/3 次）..."
    if "$WEST" update >"$UPDATE_LOG" 2>&1; then updated=1; break; fi
    # 本机实测: NCS 拉取在 git 默认 HTTP/2 下报 "Error in the HTTP2 framing layer"
    # （curl/libcurl 与 GitHub 的组合问题）。这是本脚本唯一允许的全局副作用:
    # 仅在确实检出该错误时，把 git 全局 http.version 固定为 HTTP/1.1。
    if grep -q "HTTP2 framing layer" "$UPDATE_LOG"; then
      warn "检出 HTTP2 framing 错误 → git config --global http.version HTTP/1.1（本机实测必需，唯一全局副作用）"
      git config --global http.version HTTP/1.1
    else
      tail -5 "$UPDATE_LOG" | sed 's/^/      | /' || true
    fi
  done
  [ "$updated" = "1" ] || die "west update 3 次重试后仍失败（详见 ${UPDATE_LOG}）"
  "$WEST" zephyr-export
  ok "NCS 源码就绪: $(git -C "$NCS_DIR" -C nrf describe --tags 2>/dev/null || echo nrf), zephyr $(git -C "$NCS_DIR" -C zephyr describe --tags 2>/dev/null || echo '?')"
  note_result ok
fi

# 解析 ZEPHYR_BASE（west config zephyr.base，失败回退 zephyr/）
ZEPHYR_BASE_REL="$(cd "$NCS_DIR" && "$WEST" config zephyr.base 2>/dev/null || echo zephyr)"
ZEPHYR_BASE="$NCS_DIR/$ZEPHYR_BASE_REL"
[ -d "$ZEPHYR_BASE" ] || ZEPHYR_BASE="$NCS_DIR/zephyr"
[ -d "$ZEPHYR_BASE" ] || die "ZEPHYR_BASE 不存在: $ZEPHYR_BASE"

# ---------------------------------------------------------------------------
# 步骤 5: Zephyr SDK 1.0.1（~/.blebridge/sdk 或 ${SDK_DIR}）
#   注: NCS v3.4.1 硬性要求 SDK 1.0.x（0.17.4 被 FindHostTools 拒绝，Task 9 Ruling）。
# ---------------------------------------------------------------------------
step 5 "Zephyr SDK $ZEPHYR_SDK_VERSION ($SDK_DIR)"
SDK_INSTALL_DIR="$SDK_DIR/zephyr-sdk-$ZEPHYR_SDK_VERSION"
SDK_TOOLCHAIN_GCC="$SDK_INSTALL_DIR/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc"
sdk_installed() {
  [ -f "$SDK_INSTALL_DIR/sdk_version" ] &&
    grep -qx "$ZEPHYR_SDK_VERSION" "$SDK_INSTALL_DIR/sdk_version" &&
    [ -x "$SDK_TOOLCHAIN_GCC" ]
}
if sdk_installed; then
  skip "SDK $ZEPHYR_SDK_VERSION 已安装（toolchain: gnu/arm-zephyr-eabi, $($SDK_TOOLCHAIN_GCC --version 2>/dev/null | head -n 1 || true)）"
  note_result skip
else
  # 资产平台后缀: 仅 macos-aarch64 为本机实测组合，其余给出 WARN
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  SDK_PLATFORM="macos-aarch64" ;;
    Darwin-x86_64) SDK_PLATFORM="macos-x86_64";  warn "macos-x86_64 未在本机实测过（实测组合: macos-aarch64）" ;;
    Linux-x86_64)  SDK_PLATFORM="linux-x86_64";  warn "linux-x86_64 未在本机实测过（实测组合: macos-aarch64）" ;;
    Linux-aarch64) SDK_PLATFORM="linux-aarch64"; warn "linux-aarch64 未在本机实测过（实测组合: macos-aarch64）" ;;
    *) die "未识别的平台: $(uname -s)-$(uname -m)" ;;
  esac
  SDK_ASSET="zephyr-sdk-${ZEPHYR_SDK_VERSION}_${SDK_PLATFORM}_minimal.tar.xz"
  SDK_URL="https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/${SDK_ASSET}"
  mkdir -p "$SDK_DIR"
  ok "下载 $SDK_URL"
  curl -fL --retry 3 -o "$SDK_DIR/$SDK_ASSET" "$SDK_URL"
  ok "校验 sha256 ..."
  if curl -fL --retry 3 -o "$SDK_DIR/sha256.sum" \
      "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/sha256.sum" &&
     grep -q "$SDK_ASSET" "$SDK_DIR/sha256.sum"; then
    (cd "$SDK_DIR" && grep "$SDK_ASSET" sha256.sum | shasum -a 256 -c -)
  else
    warn "无法获取 sha256.sum，跳过校验（请人工核对资产完整性）"
  fi
  ok "解压 + setup.sh -c -t arm-zephyr-eabi -h ..."
  tar -xJf "$SDK_DIR/$SDK_ASSET" -C "$SDK_DIR"
  (cd "$SDK_INSTALL_DIR" && ./setup.sh -c -t arm-zephyr-eabi -h >/dev/null)
  sdk_installed || die "SDK 安装后自检失败（缺 sdk_version 或 arm-zephyr-eabi-gcc）"
  rm -f "$SDK_DIR/$SDK_ASSET" "$SDK_DIR/sha256.sum"
  ok "SDK $ZEPHYR_SDK_VERSION 就绪: $SDK_INSTALL_DIR"
  note_result ok
fi

# 复核 Zephyr 对 SDK 的最低版本要求（不同 NCS 版本可能要求不同;
# 本脚本按 1.0.1 硬编码 —— 换 NCS 版本时必须回来核对这个数字）。
SDK_REQ_LINE="$(grep -rh -m1 "find_package(Zephyr-sdk [0-9]" "$ZEPHYR_BASE/cmake/modules/FindHostTools.cmake" 2>/dev/null || true)"
SDK_REQ_VER="$(printf '%s' "$SDK_REQ_LINE" | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
if [ -n "$SDK_REQ_VER" ] && [ "$SDK_REQ_VER" != "1.0" ]; then
  warn "Zephyr($ZEPHYR_BASE) FindHostTools 要求 SDK ${SDK_REQ_VER}，而本脚本装的是 1.0.1 — 换 NCS 版本后请复核 ZEPHYR_SDK_VERSION!"
else
  ok "复核通过: FindHostTools 最低要求 SDK ${SDK_REQ_VER:-?}.x（与 1.0.1 匹配，不计入步骤统计）"
fi

# ---------------------------------------------------------------------------
# 步骤 6: 生成 tools/ncs-env.sh（source 后即可 west build）
# ---------------------------------------------------------------------------
step 6 "生成 tools/ncs-env.sh"
NCS_ENV="$REPO_ROOT/tools/ncs-env.sh"
cat > "$NCS_ENV" <<EOF
# ncs-env.sh — 由 tools/bootstrap-env.sh 生成（本机生成日期: $(date +%F)），勿手改。
# 用法: source tools/ncs-env.sh   之后即可 west build / west flash。
export BRIDGE_NCS_DIR="$NCS_DIR"
export ZEPHYR_BASE="$ZEPHYR_BASE"
export ZEPHYR_SDK_INSTALL_DIR="$SDK_INSTALL_DIR"
# west 在 NCS venv 里; SDK 工具链 bin 一并入 PATH。
# 注: Zephyr SDK 1.0.1 不自带 cmake 二进制，构建使用 host cmake（本机实测 brew cmake 4.4.2）。
export PATH="$NCS_VENV/bin:$SDK_INSTALL_DIR/gnu/arm-zephyr-eabi/bin:\$PATH"
EOF
ok "写入 ${NCS_ENV}（ZEPHYR_BASE=${ZEPHYR_BASE}）"
note_result ok

# ---------------------------------------------------------------------------
# 步骤 7: 冒烟构建（hello_world + firmware/app @ BRIDGE_PHY=cdc; uart0 可选）
#   用独立 scratch 目录 build-t7，绝不触碰既有 build / build-uart0（Task 9 产物）。
#   目录已存在则增量构建（复用缓存），首建才 -p pristine。
#   firmware/app 从 HEAD 快照构建（git archive 到 scratch，只读仓库）:
#   工作树常有并行任务未提交的固件改动，冒烟必须钉在已提交状态，不受其影响。
# ---------------------------------------------------------------------------
step 7 "冒烟构建 ($SMOKE_DIR)"
# 生成的环境文件（步骤 6 刚写出），lint 时不存在属正常
# shellcheck disable=SC1090,SC1091
source "$NCS_ENV"
BOARD="nrf52840dk/nrf52840"
build_smoke() { # $1=build_dir $2=source_dir $3...=extra cmake args
  local bdir="$1"; local src="$2"; shift 2
  if [ -f "$bdir/CMakeCache.txt" ]; then
    # --pristine auto: 源/板一致时纯增量(复用缓存)，仅不一致时才自动 pristine
    ok "增量构建（--pristine=auto）: west build -d $(basename "$bdir")"
    "$WEST" build --pristine auto -b "$BOARD" -d "$bdir" "$src" "$@"
  else
    ok "首建（pristine）: west build -p -b $BOARD -d $(basename "$bdir")"
    "$WEST" build -p -b "$BOARD" -d "$bdir" "$src" "$@"
  fi
}
build_smoke "$SMOKE_DIR/hello_world" "$ZEPHYR_BASE/samples/hello_world"
ok "hello_world 构建成功"
APP_SNAPSHOT="$SMOKE_DIR/src-snapshot"
rm -rf "$APP_SNAPSHOT"
mkdir -p "$APP_SNAPSHOT"
git -C "$REPO_ROOT" archive HEAD firmware | tar -x -C "$APP_SNAPSHOT"
APP_SRC="$APP_SNAPSHOT/firmware/app"
ok "firmware/app HEAD 快照: $(git -C "$REPO_ROOT" rev-parse --short HEAD) -> $APP_SRC"
build_smoke "$SMOKE_DIR/app" "$APP_SRC"
# BRIDGE_PHY 默认即 "cdc"（firmware/app/Kconfig），cdc 主后端无需额外参数;
# uart0 备后端用 -DCONFIG_BRIDGE_PHY="uart0"（同 Task 9 实测命令形态）。
APP_HEX="$(find "$SMOKE_DIR/app" -name zephyr.hex -print -quit)"
ok "firmware/app 构建成功（BRIDGE_PHY=cdc, ${APP_HEX}）"
if [ "$WITH_UART0" = "1" ]; then
  # 注意: BRIDGE_PHY 是 string Kconfig，-D 值必须带内嵌引号（"uart0"），
  # 否则 extra_kconfig_options.conf 写出裸值导致 Kconfig "malformed string literal"。
  build_smoke "$SMOKE_DIR/app-uart0" "$APP_SRC" -- '-DCONFIG_BRIDGE_PHY="uart0"'
  ok "firmware/app 构建成功（BRIDGE_PHY=uart0）"
fi
note_result ok

# ---------------------------------------------------------------------------
# 步骤 8: 总结 + 下一步指引
# ---------------------------------------------------------------------------
step 8 "总结"
cat <<EOF

=====================================================================
 blebridge 工具链环境就绪
   NCS:        $NCS_DIR ($NCS_REVISION)
   ZEPHYR_BASE:                 $ZEPHYR_BASE
   ZEPHYR_SDK_INSTALL_DIR:      $SDK_INSTALL_DIR
   NCS venv:   $NCS_VENV   host venv: $HOST_VENV
   冒烟产物:   $SMOKE_DIR/{hello_world,app}$([ "$WITH_UART0" = "1" ] && echo ",app-uart0")
 下一步:
   source tools/ncs-env.sh          # 任何构建/烧录前先 source
   tools/env-report.sh              # 环境版本报告（缺项 MISSING 不报错）
   west build -d $SMOKE_DIR/app      # 增量复用缓存
   west flash -d $SMOKE_DIR/app      # 烧录（需 JLink + DK 上电）
 步骤统计: OK=$STEPS_OK SKIP=$STEPS_SKIP WARN=$STEPS_WARN
=====================================================================
EOF
