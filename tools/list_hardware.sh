#!/usr/bin/env bash
#
# blebridge list_hardware.sh — 本机 HIL 硬件汇总（只读；缺项显示 NONE，永不以错误退出，
# 对齐 env-report.sh 约定）。
#
# 汇报项:
#   1. J-Link 调试器: 优先 nrfjprog --ids；不可用则 JLinkExe ShowEmulatorList 探测
#   2. CMSIS-DAP(DAPLink) 调试器: pyocd list 有界探测（列 Unique ID/产品描述）
#   3. 串口枚举: macOS /dev/cu.usbmodem* /dev/cu.usbserial*；Linux /dev/ttyUSB* /dev/ttyACM*
#   4. 每块板的 CDC 串口映射提示（DK 的 J-Link OB CDC 串口名通常内嵌 J-Link 序号
#      或其去前导零数字，按数字子串自动配对；配不上的端口给出人工对照提示）
#
# 用法: tools/list_hardware.sh
# 退出码: 恒为 0（纯报告；"无硬件"是有效答案，不是错误）
#
# 注: 兼容 macOS bash 3.2 —— ${var} 花括号、无关联数组/mapfile。

set -euo pipefail

ok()   { printf '    \033[32mOK\033[0m   %s\n' "$*"; }
none() { printf '    \033[33mNONE\033[0m %s\n' "$*"; }
info() { printf '    INFO %s\n' "$*"; }

# 去前导零（避免数字比较歧义，仅用于子串匹配）
stripz() { printf '%s' "${1#"${1%%[!0]*}"}"; }

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

# ---------------------------------------------------------------------------
# 1) J-Link 设备（nrfjprog 优先，回退 JLinkExe ShowEmulatorList）
#    ⚠ 实测（JLinkExe V9.70）: 无板卡时 "Connecting to J-Link ..." 无限等待，
#    -AutoConnect 0 也拦不住 —— 探测必须带看门狗强杀，绝不卡死。
#    超时秒数可用 JLINK_PROBE_TIMEOUT 覆盖（默认 6s）。
# ---------------------------------------------------------------------------
JLINK_PROBE_TIMED_OUT=0
PROBE_NOTE=""
JLINK_PROBE_OUT="$(mktemp /tmp/bridge-jlink-probe.XXXXXX)"
jlink_probe() {
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

IDS=""
IDS_VIA=""
if command -v nrfjprog >/dev/null 2>&1; then
  IDS_VIA="nrfjprog --ids"
  IDS="$(nrfjprog --ids 2>/dev/null | grep -E '^[0-9]+$' | sort -u || true)"
fi
if [ -z "${IDS}" ] && command -v JLinkExe >/dev/null 2>&1; then
  IDS_VIA="JLinkExe ShowEmulatorList"
  jlink_probe
  IDS="$(grep -i 'serial number' "${JLINK_PROBE_OUT}" |
    sed -E 's/.*[Ss]erial [Nn]umber[: ]*([0-9]+).*/\1/' |
    sort -u || true)"
  if [ "${JLINK_PROBE_TIMED_OUT}" = "1" ]; then
    PROBE_NOTE="（探测超时被看门狗强杀，无板卡时的已知行为）"
  fi
fi
rm -f "${JLINK_PROBE_OUT}"

printf '\n== J-Link 调试器（via %s）%s\n' "${IDS_VIA:-<无可用探测工具>}" "${PROBE_NOTE:-}"
if [ -z "${IDS}" ]; then
  if ! command -v JLinkExe >/dev/null 2>&1 && ! command -v nrfjprog >/dev/null 2>&1; then
    none "未安装 nrfjprog / JLinkExe —— 无法探测（SEGGER J-Link: https://www.segger.com/downloads/jlink/）"
  else
    none "无可见 J-Link 设备（检查 DK USB 线与上电；刷机诊断见 tools/flash.sh）"
  fi
else
  for sn in ${IDS}; do
    ok "J-Link S/N ${sn}"
  done
fi

# ---------------------------------------------------------------------------
# 2) CMSIS-DAP(DAPLink) 调试器（pyocd list）
#    实测（pyocd 0.45.1）: 无 probe 时秒回 "No available debug probes are
#    connected"（rc 0），不会挂起; 仍按看门狗惯例限时（默认 10s,
#    PYOCD_PROBE_TIMEOUT 可覆盖）防御异常环境，绝不卡死。
#    注意: J-Link OB 固件的 DK 板对 pyocd 不可见 —— pyocd 只认 CMSIS-DAP/
#    DAPLink 固件的 probe，J-Link 与 CMSIS-DAP 两段互不替代。
# ---------------------------------------------------------------------------
PYOCD_PROBE_TIMED_OUT=0
PYOCD_NOTE=""
PYOCD_ROWS=""
PYOCD_BIN=""
if command -v pyocd >/dev/null 2>&1; then
  PYOCD_BIN="$(command -v pyocd)"
elif [ -x "${HOME}/.blebridge/ncs-venv/bin/pyocd" ]; then
  PYOCD_BIN="${HOME}/.blebridge/ncs-venv/bin/pyocd"
fi
if [ -n "${PYOCD_BIN}" ]; then
  PYOCD_PROBE_OUT="$(mktemp /tmp/bridge-pyocd-probe.XXXXXX)"
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
  PYOCD_ROWS="$(pyocd_rows "${PYOCD_PROBE_OUT}" || true)"
  rm -f "${PYOCD_PROBE_OUT}"
  if [ "${PYOCD_PROBE_TIMED_OUT}" = "1" ]; then
    PYOCD_NOTE="（探测超时被看门狗强杀，pyocd list 预期秒回，属异常环境）"
  fi
fi

printf '\n== CMSIS-DAP 调试器（pyocd list）%s\n' "${PYOCD_NOTE}"
if [ -z "${PYOCD_BIN}" ]; then
  none "未安装 pyocd —— 无法探测 CMSIS-DAP/DAPLink（NCS venv 应自带: ~/.blebridge/ncs-venv/bin/pyocd）"
elif [ -z "${PYOCD_ROWS}" ]; then
  none "无可见 CMSIS-DAP probe（无 probe 时 pyocd list 秒回属预期; J-Link OB 固件的板卡 pyocd 不可见）"
else
  printf '%s\n' "${PYOCD_ROWS}" | while IFS=$'\t' read -r uid desc; do
    [ -n "${uid}" ] || continue
    ok "CMSIS-DAP Unique ID ${uid}（${desc:-产品名未知}）"
  done
fi

# ---------------------------------------------------------------------------
# 3) 串口枚举（macOS 用 cu.* callout 节点；tty.* 打开会等 DCD，不列）
# ---------------------------------------------------------------------------
PORTS=""
case "$(uname -s)" in
  Darwin) PORT_GLOBS="/dev/cu.usbmodem* /dev/cu.usbserial*" ;;
  *)      PORT_GLOBS="/dev/ttyUSB* /dev/ttyACM*" ;;
esac
for glob in ${PORT_GLOBS}; do
  for p in ${glob}; do
    [ -e "${p}" ] || continue
    PORTS="${PORTS}${p}
"
  done
done

printf '\n== 串口（%s）\n' "${PORT_GLOBS}"
if [ -z "${PORTS}" ]; then
  none "无 USB 串口（桥固件未刷/未上电时属预期；刷机后应出现 USB 串口节点）"
else
  printf '%s\n' "${PORTS}" | while IFS= read -r p; do
    [ -n "${p}" ] || continue
    ok "${p}"
  done
fi

# ---------------------------------------------------------------------------
# 4) 板卡 <-> CDC 串口映射提示
# ---------------------------------------------------------------------------
printf '\n== 板卡/CDC 映射提示\n'
if [ -z "${IDS}" ] || [ -z "${PORTS}" ]; then
  none "无配对可做（需同时有 J-Link 设备与 USB 串口）"
  info "规则: DK 的 J-Link OB CDC 节点名通常内嵌 J-Link 序号；桥固件枚举为 USB CDC（BRIDGE_PHY=cdc 默认后端）"
else
  printf '%s\n' "${PORTS}" | while IFS= read -r p; do
    [ -n "${p}" ] || continue
    base="$(basename "${p}")"
    digits="$(printf '%s' "${base}" | tr -cd '0-9')"
    digits_nz="$(stripz "${digits}")"
    hit=""
    for sn in ${IDS}; do
      sn_nz="$(stripz "${sn}")"
      if [ -n "${digits_nz}" ] &&
         { case "${digits_nz}" in *"${sn_nz}"*) true ;; *) false ;; esac; }; then
        hit="${sn}"
        break
      fi
    done
    if [ -n "${hit}" ]; then
      ok "S/N ${hit} <-> ${p}"
    else
      info "${p}: 未匹配到 J-Link 序号（非 DK CDC 或命名规则不同，请人工确认）"
    fi
  done
fi

N_SN=0
[ -n "${IDS}" ] && N_SN="$(printf '%s\n' "${IDS}" | grep -c .)"
N_PORT=0
[ -n "${PORTS}" ] && N_PORT="$(printf '%s\n' "${PORTS}" | grep -c .)"
N_PYCD=0
[ -n "${PYOCD_ROWS}" ] && N_PYCD="$(printf '%s\n' "${PYOCD_ROWS}" | grep -c .)"
printf '\n== 总结: J-Link %s 台, CMSIS-DAP %s 台, USB 串口 %s 个\n' "${N_SN}" "${N_PYCD}" "${N_PORT}"
exit 0
