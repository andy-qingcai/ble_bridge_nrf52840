#!/usr/bin/env bash
#
# blebridge flash.sh — 一条命令构建+烧录（Task 8; DAPLink(pyocd) 后端 task-daplink）。
#
# 用法:
#   tools/flash.sh <app|suota_sim> [--hil] [serial_number] [--backend jlink|daplink|auto]
#
# 目标与 build 目录映射（既有现成构建，缺失则先构建）:
#   app      -> ${BRIDGE_NCS_DIR:-~/blebridge/ncs}/build-t10   源: firmware/app
#   suota_sim  -> ${BRIDGE_NCS_DIR:-~/blebridge/ncs}/build-t11   源: firmware/suota_sim
#   suota_sim --hil -> .../build-t11-hil 附加 boards/nrf52840dk_nrf52840_hil.conf
#               （HIL 严格校验: CHECK_XOR=y + EXPECTED_SIZE；task(12) 终审 I-2）
#   app --uart-custom -> .../build-t10-uart-custom 附加 BRIDGE_PHY="uart0" +
#               boards/nrf52840dk_nrf52840_uart_custom.overlay（用户板 UART 直连
#               形态: 921600 8N1 + 流控, TX=P1.04 RX=P1.13 RTS=P1.15 CTS=P1.00）
#
# 烧录后端（--backend，默认 auto）:
#   jlink   JLinkExe ShowEmulatorList 有界探测 -> west flash -d <dir> [--dev-id <sn>]
#   daplink pyocd list 有界探测（默认 10s，PYOCD_PROBE_TIMEOUT 可覆盖）->
#           有 probe 才放行 west flash -d <dir> --runner pyocd [--dev-id <uid>]；
#           无 probe -> 诊断 + exit 1（绝不假成功）。
#           ⚠ pyocd/west 在无 probe 时会无限等待插入（"Waiting for a debug probe
#             to be connected..."，插上即刷）—— 本脚本烧录前的前置有界探测就是
#             为了不让 flash.sh 本身卡死；"插上即刷"的等待只保留在实刷阶段。
#   auto    先 JLink 有界探测 -> 有 J-Link 走 jlink；无则 pyocd 有界探测 ->
#           有 CMSIS-DAP probe 走 daplink；两者皆无 -> 双后端诊断 + exit 1。
#
# serial_number: jlink = J-Link S/N；daplink = DAPLink Unique ID（校验: 须在
#           pyocd list 可见列表中，否则 exit 1 绝不盲刷）。
# 多板守卫: 可见 >=2 块时必须传 serial_number（--dev-id）；仅 1 块时告警
#          "将刷到唯一可见板卡"后继续。
#
# 并发安全: 探测+实刷全程持有互斥锁 /tmp/bridge_flash.lock —— 多 agent 并发
#           烧录时同一时刻仅一个进程可占用调试探针（SWD 同时只能被一个进程
#           打开）; 抢锁最多等 5 秒, 超时 exit 2（flock 优先, 无则 mkdir 原子
#           锁+陈旧锁自动清理; 脚本退出自动释放）。
#
# 流程: source tools/ncs-env.sh（缺失则报错指引 bootstrap）->
#       build 目录无 CMakeCache.txt 时先首建（west build -p -b nrf52840dk/nrf52840）->
#       west build -d <dir>（增量）-> 后端探测 -> west flash。
#
# 退出码: 0 成功 / 1 构建·烧录·硬件失败 / 2 用法或环境错误
#
# 注: 兼容 macOS 系统 bash 3.2 —— 变量后紧跟全角标点必须写 ${var} 花括号
#     （见 bootstrap-env.sh 头部立规）；不用关联数组/mapfile；不用空数组 @ 展开。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NCS_ENV="$REPO_ROOT/tools/ncs-env.sh"
BOARD="nrf52840dk/nrf52840"

ok()   { printf '    \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mWARN\033[0m %s\n' "$*"; }
# die 真退出（fix round1 C-1）: 第 1 参为消息, 第 2 参可选退出码（默认 1 =
# 构建/烧录/硬件失败; 传 2 = 用法或环境错误）。调用点语义:
#   exit 2: ncs-env 缺失 / west 不可用 / 固件源目录缺失 / --backend 值非法 /
#           --backend daplink 但找不到 pyocd / 抢并发锁超时: /tmp/bridge_flash.lock
#           被另一烧录进程持有（环境错）
#   exit 1: 指定 S/N 不可见 / 多板未指定 S/N / 无可见调试器（板卡选择错,
#           绝不落回默认板卡, 绝不假成功）
die()  { printf '    \033[31mFAIL\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

usage() {
  sed -n '2,44p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# 参数: [--hil] [serial_number] [--backend jlink|daplink|auto]
# ---------------------------------------------------------------------------
TARGET="${1:-}"
if [ -z "${TARGET}" ] || [ "${TARGET}" = "-h" ] || [ "${TARGET}" = "--help" ]; then
  usage
  [ -z "${TARGET}" ] && { printf '\n缺少目标参数（app|suota_sim [--hil] [serial_number] [--backend ...]）\n' >&2; exit 2; }
  exit 0
fi

shift
HIL=0
UART_CUSTOM=0
SN=""
BACKEND="auto"
while [ $# -gt 0 ]; do
  case "${1}" in
    --hil) HIL=1 ;;
    --uart-custom) UART_CUSTOM=1 ;;
    --backend)
      if [ $# -lt 2 ]; then
        printf "flash.sh: --backend 需要值（jlink|daplink|auto）\n" >&2
        exit 2
      fi
      BACKEND="${2}"; shift ;;
    --backend=*) BACKEND="${1#--backend=}" ;;
    -*) printf "flash.sh: 无法识别的选项 '%s'（见 --help）\n" "${1}" >&2; exit 2 ;;
    *) if [ -n "${SN}" ]; then
         printf "flash.sh: 多余的位置参数 '%s'\n" "${1}" >&2
         exit 2
       fi
       SN="${1}" ;;
  esac
  shift
done

case "${BACKEND}" in
  jlink|daplink|auto) ;;
  *) printf "flash.sh: 无效 --backend '%s'（jlink|daplink|auto）\n" "${BACKEND}" >&2; exit 2 ;;
esac

BUILD_ARGS=""
case "${TARGET}" in
  app)     BUILD_NAME="build-t10"; SRC_REL="firmware/app" ;;
  suota_sim) BUILD_NAME="build-t11"; SRC_REL="firmware/suota_sim" ;;
  *) printf "flash.sh: 未知目标 '%s'（app|suota_sim）\n" "${TARGET}" >&2; exit 2 ;;
esac
if [ "${HIL}" = "1" ]; then
  if [ "${TARGET}" != "suota_sim" ]; then
    printf "flash.sh: --hil 仅支持 suota_sim 目标\n" >&2
    exit 2
  fi
  BUILD_NAME="build-t11-hil"
  BUILD_ARGS="-DEXTRA_CONF_FILE=boards/nrf52840dk_nrf52840_hil.conf"
fi
# --uart-custom: 用户板 UART 直连形态 = uart0 PHY + 自定义引脚 overlay 一键附加。
# 独立 build 目录（同 --hil 先例），避免这两个 -D 进 build-t10 的 CMake 缓存后
# 污染后续默认构建。
if [ "${UART_CUSTOM}" = "1" ]; then
  if [ "${TARGET}" != "app" ]; then
    printf "flash.sh: --uart-custom 仅支持 app 目标\n" >&2
    exit 2
  fi
  BUILD_NAME="build-t10-uart-custom"
  # Kconfig 字符串符号须带字面双引号（Zephyr 惯例 -DCONFIG_X='"str"'），故含引号
  # 的 BUILD_ARGS 走既定的故意分词展开（下方 SC2086 点）: SC2089/SC2090 属预期。
  # shellcheck disable=SC2089
  BUILD_ARGS='-DCONFIG_BRIDGE_PHY="uart0" -DEXTRA_DTC_OVERLAY_FILE=boards/nrf52840dk_nrf52840_uart_custom.overlay'
fi

# ---------------------------------------------------------------------------
# 1) 环境: ncs-env.sh（Task 7 bootstrap 生成）
# ---------------------------------------------------------------------------
if [ ! -f "${NCS_ENV}" ]; then
  die "tools/ncs-env.sh 不存在。先运行 tools/bootstrap-env.sh 生成（用法: source tools/ncs-env.sh 后再 flash）" 2
fi
# shellcheck disable=SC1090,SC1091
source "${NCS_ENV}"
NCS_DIR="${BRIDGE_NCS_DIR:-$HOME/blebridge/ncs}"
BUILD_DIR="${NCS_DIR}/${BUILD_NAME}"
SRC_DIR="${REPO_ROOT}/${SRC_REL}"

# 源目录检查放在 OK 行之前（fix round1: 避免环境错时先误打印 OK）
[ -d "${SRC_DIR}" ] || die "固件源目录不存在: ${SRC_DIR}" 2

if ! command -v west >/dev/null 2>&1; then
  die "source ncs-env.sh 后 PATH 中仍无 west（期望 ${HOME}/.blebridge/ncs-venv/bin/west）—— 重跑 tools/bootstrap-env.sh" 2
fi
ok "west: $(command -v west)"
ok "构建目录: ${BUILD_DIR}（源: ${SRC_DIR}）"

# ---------------------------------------------------------------------------
# 2) 构建: 缺失则首建，再增量（--hil 形态附加 HIL conf；两种形态独立 build 目录）
# ---------------------------------------------------------------------------
if [ ! -f "${BUILD_DIR}/CMakeCache.txt" ]; then
  echo "== 首建（pristine）: west build -p -b ${BOARD} -d ${BUILD_DIR} ${BUILD_ARGS}"
  # BUILD_ARGS 为空或 "-DVAR=val" 对（已写死无空格），故意分词展开；
  # --uart-custom 的值含字面引号（Kconfig 字符串语法），SC2090 属预期
  # shellcheck disable=SC2086,SC2090
  west build -p -b "${BOARD}" -d "${BUILD_DIR}" "${SRC_DIR}" ${BUILD_ARGS}
else
  echo "== 增量构建: west build -d ${BUILD_DIR} -- ${BUILD_ARGS}"
  # "--" 必须有: 无 source_dir 时 west 的余参解析 (build.py _parse_remainder)
  # 把首个 token 无条件当 source_dir, "-D..." 会被误判为源目录而 exit 1
  # (复审复现的 --hil 第二次运行必失败路径). 分词展开同上, SC2090 属预期.
  # shellcheck disable=SC2086,SC2090
  west build -d "${BUILD_DIR}" -- ${BUILD_ARGS}
fi

# ---------------------------------------------------------------------------
# 2a) 并发互斥锁: JLink/pyocd 探测与 west flash 实刷都会独占打开 SWD 探针
#     （同时只能被一个进程打开）, 多 agent 并发烧录必须在探测前就串行 ——
#     锁在探测之前获取, EXIT 时释放。实现: flock(1) 优先（macOS 自身不带,
#     brew install flock 后可用; 同机所有 flash.sh 探测结果一致, 不会混用
#     两种锁语义）; 无 flock 则 mkdir 原子锁 + PID 文件 + 陈旧锁（持有者
#     已死）自动清理。抢锁最多等 5 秒（实做 4.5s, 留进程启动余量）,
#     超时属环境错 exit 2, 消息指明锁路径与持有者 PID 及处置办法。
# ---------------------------------------------------------------------------
FLASH_LOCK="/tmp/bridge_flash.lock"
FLASH_LOCK_WAIT_S=5
LOCK_MODE="none"

lock_pid_alive() {
  # $1=pid -> 0=存活 / 1=陈旧。空串/非数字/PID 1（launchd/init, 绝非本脚本
  # 持有者）/kill -0 失败（进程已死）均判陈旧
  [ -n "${1}" ] || return 1
  case "${1}" in *[!0-9]*) return 1 ;; esac
  [ "${1}" -gt 1 ] || return 1
  kill -0 "${1}" 2>/dev/null
}

# 仅经 EXIT trap 间接调用; shellcheck 0.11 在脚本以显式 exit 结尾时对
# trap 处理函数误报 SC2329（最小复现已确认）, 故此处 disable
# shellcheck disable=SC2329
release_flash_lock() {
  # EXIT trap。flock 模式: 关 fd 即释放（进程退出本也会自动关, 显式关保平安）;
  # mkdir 模式: 仅在锁仍归我所有时删除 —— 防"mkdir 成功 -> 写 PID 前"的极小
  # 竞态窗口里锁被等待者误判陈旧改名后, 本进程退出又误删现任持有者的锁。
  if [ "${LOCK_MODE}" = "flock" ]; then
    exec 9>&- || true
  elif [ "${LOCK_MODE}" = "mkdir" ]; then
    if [ -f "${FLASH_LOCK}/pid" ] \
       && [ "$(cat "${FLASH_LOCK}/pid" 2>/dev/null || true)" = "$$" ]; then
      rm -rf "${FLASH_LOCK}"
    fi
  fi
}

lock_busy_fail() {
  # $1=持有者描述（mkdir 模式为 "PID xxx"; flock 模式无 PID, 给核查提示）
  printf '      - 等待 %ss 仍抢不到锁: 等对方烧录完成后再跑; 若确认对方进程已死,\n' "${FLASH_LOCK_WAIT_S}" >&2
  printf '        删除陈旧锁后重试: rm -rf %s\n' "${FLASH_LOCK}" >&2
  die "另一烧录进程持有锁 ${FLASH_LOCK}（${1}）" 2
}

acquire_flash_lock() {
  if command -v flock >/dev/null 2>&1; then
    # 先探锁文件可写（exec 重定向失败在部分 bash 下直接退出, 先用 :> 保证
    # 失败走 die exit 2 而非裸退出码）
    if ! : >"${FLASH_LOCK}" 2>/dev/null; then
      die "无法创建锁文件 ${FLASH_LOCK}（检查 /tmp 权限）" 2
    fi
    exec 9>"${FLASH_LOCK}"
    if flock -w 4 9; then
      LOCK_MODE="flock"
      return 0
    fi
    lock_busy_fail "持有者 PID 未知（flock 模式）, 可 lsof ${FLASH_LOCK} 核查"
  fi
  # mkdir 原子锁回退（macOS 默认无 flock）: mkdir 成功即持锁, 随后写 PID;
  # 等待者读 PID 判持有者死活, 死锁先 mv 原子改名（并发清理者仅一个成功,
  # 不误删新锁）再删。持有者若在"mkdir 后写 PID 前"被改名, 写 PID 失败即
  # 自知失锁重试 —— 任何一边退出都不会误删他人持有的锁。
  local i holder
  i=0
  while [ "${i}" -lt 18 ]; do
    if mkdir "${FLASH_LOCK}" 2>/dev/null; then
      if printf '%s\n' "$$" >"${FLASH_LOCK}/pid" 2>/dev/null; then
        LOCK_MODE="mkdir"
        return 0
      fi
      i=$((i + 1))   # 写 PID 失败 = 锁刚被并发清理者改名, 重试
      continue
    fi
    holder="$(cat "${FLASH_LOCK}/pid" 2>/dev/null || true)"
    if ! lock_pid_alive "${holder}"; then
      if mv "${FLASH_LOCK}" "${FLASH_LOCK}.stale.$$" 2>/dev/null; then
        warn "清理陈旧锁 ${FLASH_LOCK}（原持有者 PID ${holder:-未知} 已死）"
        rm -rf "${FLASH_LOCK}.stale.$$"
      fi
      continue
    fi
    sleep 0.25
    i=$((i + 1))
  done
  holder="$(cat "${FLASH_LOCK}/pid" 2>/dev/null || true)"
  lock_busy_fail "PID ${holder:-未知}"
}
trap release_flash_lock EXIT

echo "== 获取烧录互斥锁: ${FLASH_LOCK}（多 agent 并发防 DAPLink/SWD 争抢）"
acquire_flash_lock
ok "烧录锁已持有（本进程 PID $$，探测+烧录期间独占探针）"

# ---------------------------------------------------------------------------
# 3) 后端探测
#    JLink（⚠ 实测 V9.70: 无板卡时 "Connecting to J-Link ..." 无限等待，
#    -AutoConnect 0 也拦不住 —— 探测必须带看门狗强杀，绝不卡死。
#    超时秒数可用 JLINK_PROBE_TIMEOUT 覆盖（默认 6s））
#    pyocd（实测 0.45.1: `pyocd list` 无 probe 时秒回
#    "No available debug probes are connected"; 但 west flash --runner pyocd
#    无 probe 时无限等待插入 —— daplink 放行前必须先有界探测,
#    PYOCD_PROBE_TIMEOUT 可覆盖（默认 10s））
# ---------------------------------------------------------------------------
JLINK_PROBE_TIMED_OUT=0
JLINK_PROBE_OUT="$(mktemp /tmp/bridge-jlink-probe.XXXXXX)"
jlink_probe() {
  local pid
  # { } 2>/dev/null: 吞掉 bash 对被杀后台作业的 "Terminated" 噪声
  {
    printf 'ShowEmulatorList\nexit\n' | JLinkExe -NoGui 1 -AutoConnect 0 \
      >"${JLINK_PROBE_OUT}" 2>&1 &
    pid=$!
    for _ in $(seq 1 "${JLINK_PROBE_TIMEOUT:-6}"); do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      sleep 0.3
      kill -9 "${pid}" 2>/dev/null || true
      JLINK_PROBE_TIMED_OUT=1
    fi
    wait "${pid}" 2>/dev/null || true
  } 2>/dev/null
}

sn_list() {
  # IDS/PIDS 为换行分隔的 S/N -> 单行空格并排（供消息展示）
  printf '%s' "$1" | tr '\n' ' '
}

jlink_ids() {
  grep -i 'serial number' "${JLINK_PROBE_OUT}" |
    sed -E 's/.*[Ss]erial [Nn]umber[: ]*([0-9]+).*/\1/' |
    sort -u || true
}

PYOCD_PROBE_TIMED_OUT=0
PYOCD_PROBE_OUT="$(mktemp /tmp/bridge-pyocd-probe.XXXXXX)"
PYOCD_BIN=""
resolve_pyocd() {
  # ncs-env.sh 已把 venv/bin 放进 PATH; PATH 仍不可达时回退探测该路径（保险）
  if command -v pyocd >/dev/null 2>&1; then
    PYOCD_BIN="$(command -v pyocd)"
  elif [ -x "${HOME}/.blebridge/ncs-venv/bin/pyocd" ]; then
    PYOCD_BIN="${HOME}/.blebridge/ncs-venv/bin/pyocd"
    warn "PATH 中无 pyocd，回退使用 ${PYOCD_BIN}"
  fi
}

pyocd_probe() {
  local pid
  {
    "${PYOCD_BIN}" list >"${PYOCD_PROBE_OUT}" 2>&1 &
    pid=$!
    for _ in $(seq 1 "${PYOCD_PROBE_TIMEOUT:-10}"); do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      sleep 0.3
      kill -9 "${pid}" 2>/dev/null || true
      PYOCD_PROBE_TIMED_OUT=1
    fi
    wait "${pid}" 2>/dev/null || true
  } 2>/dev/null
}

# pyocd list 表格（prettytable: 列等宽左对齐）-> "Unique ID<TAB>产品描述" 行。
# 以表头行 Probe/Board / Unique ID / Target 的列偏移切列（prettytable 各列
# 等宽对齐，偏移在数据行同样成立; 序号列非数字的 board 信息附行被跳过）。
# 先剥 ANSI 色码兜底（旧版 pyocd 非 tty 可能带色）; 无 probe 时输出仅
# "No available debug probes are connected" -> 解析结果为空。
pyocd_rows() {
  esc="$(printf '\033')"
  sed "s/${esc}\[[0-9;]*m//g" "${1}" | awk '
    /Unique ID/ {
      hdr = $0
      c2 = index(hdr, "Probe/Board"); c3 = index(hdr, "Unique ID")
      c4 = index(hdr, "Target")
      if (c2 > 0 && c3 > c2 && c4 > c3) { have = 1 }
      next
    }
    have && $0 ~ /^[ \t]*[0-9]+[ \t]/ {
      desc = substr($0, c2, c3 - c2)
      uid  = substr($0, c3, c4 - c3)
      gsub(/[ \t]+$/, "", desc); gsub(/^[ \t]+/, "", desc)
      gsub(/[ \t]+$/, "", uid);  gsub(/^[ \t]+/, "", uid)
      if (uid != "" && uid != "n/a") { printf "%s\t%s\n", uid, desc }
    }
  ' | sort -u
}

# --- 无硬件诊断（FAIL 头行 + 正文; auto 双后端诊断复用正文两个小节） ---
diag_jlink_lines() {
  if command -v JLinkExe >/dev/null 2>&1; then
    printf '      - JLinkExe: %s\n' "$(command -v JLinkExe)" >&2
    if [ "${JLINK_PROBE_TIMED_OUT}" = "1" ]; then
      printf '      - JLinkExe 探测超时被强杀（无板卡时的已知行为，非卡死缺陷）\n' >&2
    fi
    printf '      - ShowEmulatorList 无可见设备 -> 检查 USB 线/上电（DK 左侧电源开关、J3 供电选择）\n' >&2
  else
    printf '      - 未安装 JLinkExe（SEGGER J-Link 工具链）; west flash 无法进行\n' >&2
    printf '        安装: https://www.segger.com/downloads/jlink/（macOS pkg 后重开终端）\n' >&2
  fi
}

diag_daplink_lines() {
  if [ -n "${PYOCD_BIN}" ]; then
    printf '      - pyocd: %s\n' "${PYOCD_BIN}" >&2
    if [ "${PYOCD_PROBE_TIMED_OUT}" = "1" ]; then
      printf '      - pyocd list 探测超时被强杀（pyocd list 预期秒回，属异常，可手动重跑核查）\n' >&2
    fi
  else
    printf '      - 未找到 pyocd（期望 ncs-venv/bin/pyocd）—— 重跑 tools/bootstrap-env.sh\n' >&2
  fi
  printf '      - pyocd list 无可见 CMSIS-DAP probe -> 检查 USB 线/上电; 确认调试器固件为\n' >&2
  printf '        CMSIS-DAP/DAPLink（J-Link OB 固件的 DK 板 pyocd 不可见: 换 DAPLink 固件或 --backend jlink）\n' >&2
  printf '      - 手动验证: pyocd list（无 probe 时秒回 "No available debug probes are connected"）\n' >&2
}

usb_lines() {
  if command -v lsusb >/dev/null 2>&1; then
    printf '      - lsusb USB 设备:\n' >&2
    lsusb 2>/dev/null | sed 's/^/          /' >&2 || true
  else
    printf '      - macOS USB 检查: system_profiler SPUSBDataType | grep -A4 -i segger\n' >&2
  fi
}

diagnose_no_hardware() {
  printf '    \033[31mFAIL\033[0m 未检测到任何 J-Link 调试器/板卡，诊断:\n' >&2
  diag_jlink_lines
  usb_lines
  printf '      - 板卡确认在位后重试: tools/flash.sh %s [serial_number]\n' "${TARGET}" >&2
  printf '      - 刷机前可先 tools/list_hardware.sh 查看当前硬件汇总\n' >&2
}

diagnose_no_daplink() {
  printf '    \033[31mFAIL\033[0m 未检测到任何 CMSIS-DAP(DAPLink) 调试器，诊断:\n' >&2
  diag_daplink_lines
  usb_lines
  printf '      - probe 确认在位后重试: tools/flash.sh %s [unique_id] --backend daplink\n' "${TARGET}" >&2
  printf '      - 刷机前可先 tools/list_hardware.sh 查看当前硬件汇总\n' >&2
}

diagnose_no_backend_both() {
  printf '    \033[31mFAIL\033[0m 未检测到任何调试器（J-Link 与 CMSIS-DAP 均无），诊断:\n' >&2
  diag_jlink_lines
  diag_daplink_lines
  usb_lines
  printf '      - 任一调试器在位后重试: tools/flash.sh %s [serial_number] [--backend jlink|daplink]\n' "${TARGET}" >&2
  printf '      - 刷机前可先 tools/list_hardware.sh 查看当前硬件汇总\n' >&2
}

# ---------------------------------------------------------------------------
# 3a) JLink 探测（--backend jlink|auto）
# ---------------------------------------------------------------------------
RESOLVED=""
IDS=""
N_IDS=0
if [ "${BACKEND}" != "daplink" ]; then
  jlink_probe
  IDS="$(jlink_ids || true)"
  rm -f "${JLINK_PROBE_OUT}"
  if [ -n "${IDS}" ]; then
    N_IDS="$(printf '%s\n' "${IDS}" | grep -c .)"
  fi

  if [ "${N_IDS}" -gt 0 ]; then
    RESOLVED="jlink"
    ok "可见 J-Link（${N_IDS} 块）: $(sn_list "${IDS}")"
  elif [ "${BACKEND}" = "jlink" ]; then
    # 构建已成功、烧录目标缺失: 给出完整诊断后失败（绝不假成功）。
    ok "构建成功（产物已就绪，硬件就位后可直接重跑本脚本烧录）"
    diagnose_no_hardware
    exit 1
  else
    echo "== 无 J-Link（auto）: 回落 DAPLink(pyocd) 探测"
  fi
fi

# ---------------------------------------------------------------------------
# 3b/4) 板卡区分 -> 烧录参数（J-Link: --dev-id <sn>; DAPLink: --dev-id <uid>)
# ---------------------------------------------------------------------------
FLASH_ARGS=""
RUNNER_ARGS=""
PIDS=""
N_PIDS=0
if [ "${RESOLVED}" = "jlink" ]; then
  if [ -n "${SN}" ]; then
    if ! printf '%s\n' "${IDS}" | grep -qx "${SN}"; then
      # fix round1 C-1: die 真退出 —— 指定板不可见时绝不落回默认板卡
      die "指定的 J-Link 序号 ${SN} 不在可见列表: $(sn_list "${IDS}")" 1
    fi
    FLASH_ARGS="--dev-id ${SN}"
    ok "目标板（--dev-id）: ${SN}"
  elif [ "${N_IDS}" -gt 1 ]; then
    # fix round1 C-1: die 真退出 —— 多板未指定 S/N 时绝不无 --dev-id 盲刷
    die "检测到 ${N_IDS} 块可见板卡，必须显式指定 serial_number: $(sn_list "${IDS}")" 1
  else
    warn "仅一块可见板卡（S/N $(sn_list "${IDS}")）: 将刷到唯一可见板卡"
  fi
else
  # DAPLink(pyocd) 分支（--backend daplink 或 auto 回落）:
  # 前置有界探测通过才放行 west flash --runner pyocd（pyocd 无 probe 时会
  # 无限等待插入，绝不能无探测直接进 west flash）。
  resolve_pyocd
  if [ -z "${PYOCD_BIN}" ]; then
    if [ "${BACKEND}" = "daplink" ]; then
      die "未找到 pyocd（--backend daplink 需要）; source tools/ncs-env.sh 后应可用 —— 重跑 tools/bootstrap-env.sh" 2
    fi
    ok "构建成功（产物已就绪，硬件就位后可直接重跑本脚本烧录）"
    diagnose_no_backend_both
    exit 1
  fi
  pyocd_probe
  ROWS="$(pyocd_rows "${PYOCD_PROBE_OUT}" || true)"
  rm -f "${PYOCD_PROBE_OUT}"
  if [ -n "${ROWS}" ]; then
    PIDS="$(printf '%s\n' "${ROWS}" | cut -f1 | sort -u)"
    N_PIDS="$(printf '%s\n' "${PIDS}" | grep -c .)"
  fi

  if [ "${N_PIDS}" -eq 0 ]; then
    ok "构建成功（产物已就绪，硬件就位后可直接重跑本脚本烧录）"
    if [ "${BACKEND}" = "auto" ]; then
      diagnose_no_backend_both
    else
      diagnose_no_daplink
    fi
    exit 1
  fi
  ok "可见 CMSIS-DAP probe（${N_PIDS} 个）: $(sn_list "${PIDS}")"

  if [ -n "${SN}" ]; then
    if ! printf '%s\n' "${PIDS}" | grep -qx "${SN}"; then
      die "指定的 DAPLink Unique ID ${SN} 不在 pyocd list 可见列表: $(sn_list "${PIDS}")" 1
    fi
    FLASH_ARGS="--dev-id ${SN}"
    ok "目标 probe（--dev-id）: ${SN}"
  elif [ "${N_PIDS}" -gt 1 ]; then
    die "检测到 ${N_PIDS} 个 CMSIS-DAP probe，必须显式指定 unique_id: $(sn_list "${PIDS}")" 1
  else
    warn "仅一个可见 probe（Unique ID $(sn_list "${PIDS}")）: 将刷到唯一可见 probe"
  fi
  RUNNER_ARGS="--runner pyocd"
fi

# ---------------------------------------------------------------------------
# 5) 烧录（捕获退出码，失败给诊断）
# ---------------------------------------------------------------------------
echo "== 烧录: west flash -d ${BUILD_DIR} ${RUNNER_ARGS} ${FLASH_ARGS}"
set +e
# RUNNER_ARGS 为 "" 或 "--runner pyocd"; FLASH_ARGS 为空或 "--dev-id <已校验
# 纯数字/十六进制 ID>"，故意分词展开
# shellcheck disable=SC2086
west flash -d "${BUILD_DIR}" ${RUNNER_ARGS} ${FLASH_ARGS}
RC=$?
set -e

if [ "${RC}" -ne 0 ]; then
  printf '    \033[31mFAIL\033[0m west flash 退出码 %s，诊断:\n' "${RC}" >&2
  if [ "${RESOLVED}" = "jlink" ]; then
    printf '      - J-Link 可见性: %s\n' "$(sn_list "${IDS}")" >&2
    printf '      - 常见原因: 板卡被其它进程占用（JLinkExe/nrfjprog/另一终端）、\n' >&2
    printf '        USB 掉线、--dev-id 序号不匹配、SWD 线被外设干扰\n' >&2
  else
    printf '      - pyocd: %s; probe 可见性: %s\n' "${PYOCD_BIN}" "$(sn_list "${PIDS}")" >&2
    printf '      - 常见原因: probe 被其它进程占用（另一终端 pyocd/west/IDE）、\n' >&2
    printf '        USB 掉线、--dev-id Unique ID 失配、固件非 CMSIS-DAP\n' >&2
  fi
  printf '      - 复查: tools/list_hardware.sh; 重试: tools/flash.sh %s [serial_number]\n' "${TARGET}" >&2
  exit 1
fi

BOARD_SN="${SN}"
if [ -z "${BOARD_SN}" ]; then
  if [ "${RESOLVED}" = "jlink" ]; then
    BOARD_SN="$(sn_list "${IDS}")"
  else
    BOARD_SN="$(sn_list "${PIDS}")"
  fi
fi
ok "烧录完成: ${TARGET} -> ${BUILD_DIR}（板卡 S/N ${BOARD_SN}）"
exit 0
