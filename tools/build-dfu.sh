#!/usr/bin/env bash
#
# blebridge build-dfu.sh — 自 OTA 双槽镜像一键构建（task-dfu-a，⑥）。
#
# 封装 sysbuild + MCUboot 双槽构建的完整参数，保证仓库内任何人构建出的
# 镜像与 bootloader 公钥一致：
#   west build -p --sysbuild -b nrf52840dk/nrf52840 firmware/app -d build-dfu
#     -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE=<repo>/ci/keys/dfu-signing-ec-p256.pem
#
# 签名密钥自行生成（不入库）：
#   openssl ecparam -name prime256v1 -genkey -noout -out ci/keys/dfu-signing-ec-p256.pem
# 或经 BRIDGE_DFU_SIGNING_KEY=<path> 指向任意 PEM EC P-256 私钥。
#
# 为什么必须经过本脚本：签名密钥路径需绝对路径（zephyr/cmake/mcuboot.cmake
# 的解析顺序是 绝对路径 → 应用配置目录 → west topdir），且 sysbuild.conf 里
# 不便写死机器相关路径。裸 `west build` 会回退到 MCUboot 树的 root-ec-p256.pem
# 测试钥——构建成功但与板上 bootloader 公钥不匹配（镜像会被拒收）。
#
# 用法:
#   tools/build-dfu.sh                # 构建（pristine），产物在 build-dfu/
#   tools/build-dfu.sh --no-pristine  # 增量构建
#   tools/build-dfu.sh --flash        # 构建后经 WCH-Link(pyocd) 烧录整镜像
#   tools/build-dfu.sh --flash-only   # 跳过构建，只烧录现有产物
#
# 烧录: pyocd -u <uid> -t nrf52840_xxaa（WCH-Link；uid 经 BRIDGE_PYOCD_UID 给出）写入
#   merged hex（mcuboot + slot0 + slot1 + settings 整镜像，含签名）。整片覆盖
#   写入，单槽时代的旧固件被整体替换（回退 = 重刷
#   sessions/dfu-backup-*/zephyr-fallback-single-slot.hex）。
#   --target 必带（D1 防假成功，task-dfu-a §9.5）：不带时 pyocd 0.45.1 落到
#   generic cortex_m，报 "Erased 0 bytes (0 sectors)" 却 exit 0——什么都没写。
#   烧录后断言：输出须含 "programmed" 且页数非 0，否则 exit 1（假成功防御）。
#
# 已知问题（task-dfu-a 真机验证 §9.5，记录不修）：
#   D2 WCH-Link SWD 偶发 wedge（SWD WAIT ACK，多见于板卡复位/USB 重枚举窗口、
#      连续快速 pyocd 会话）——降频重连即恢复: BRIDGE_PYOCD_FREQ=300000 tools/build-dfu.sh --flash-only
#   D4 mcumgr 对与活动镜像 hash 相同的槽执行 image test 被拒（IMG_MGMT_ERR 33
#      IMAGE_SETTING_TEST_TO_ACTIVE_DENIED）——重传同版本需先回退或换镜像。
#
# 产物:
#   build-dfu/merged.hex                整镜像 hex（烧录用）
#   build-dfu/dfu_application.zip       SMP DFU 标准载荷（smpclient/mcumgr）
#   build-dfu/mcuboot/zephyr/zephyr.elf bootloader（尺寸裁定 48K/80K 分区）
#   build-dfu/app/zephyr/zephyr.elf     应用（slot0 链接，已签名）
#
# 退出码: 0 成功 / 1 构建·烧录失败 / 2 用法或环境错误
#
# 注: 兼容 macOS 系统 bash 3.2 —— 不用关联数组/mapfile；不用空数组 @ 展开。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NCS_ENV="$REPO_ROOT/tools/ncs-env.sh"
BOARD="nrf52840dk/nrf52840"
BUILD_DIR="$REPO_ROOT/build-dfu"
KEY_FILE="${BRIDGE_DFU_SIGNING_KEY:-$REPO_ROOT/ci/keys/dfu-signing-ec-p256.pem}"
PYOCD_UID="${BRIDGE_PYOCD_UID:-auto}"   # "auto" = 单探针时省略 -u 由 pyocd 自动选择
PYOCD_FREQ="${BRIDGE_PYOCD_FREQ:-1000000}"
# D1 防假成功（task-dfu-a §9.1）: 不带 --target 时 pyocd 选中 generic cortex_m，
# "Erased 0 bytes (0 sectors), programmed N bytes" 且 exit 0——flash 实际未写。
PYOCD_TARGET="${BRIDGE_PYOCD_TARGET:-nrf52840_xxaa}"

FLASH=no
FLASH_ONLY=no
PRISTINE=yes
for arg in "$@"; do
    case "$arg" in
        --flash)      FLASH=yes ;;
        --flash-only) FLASH_ONLY=yes; FLASH=yes ;;
        --no-pristine) PRISTINE=no ;;
        -h|--help) sed -n '2,47p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build-dfu: unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ -f "$NCS_ENV" ]; then
    # shellcheck disable=SC1090
    source "$NCS_ENV"
else
    # 无 ncs-env.sh 的环境（如公开检出）：要求 PATH 已含 west（bootstrap-env.sh
    # 装好的 ncs-venv），不强制脚本存在。
    if ! command -v west >/dev/null 2>&1; then
        echo "build-dfu: 找不到 west —— 先跑 tools/bootstrap-env.sh 或自行激活 NCS 环境" >&2
        exit 2
    fi
fi

if [ ! -f "$KEY_FILE" ]; then
    echo "build-dfu: 签名密钥缺失: $KEY_FILE" >&2
    echo "build-dfu: 自行生成（勿提交入库）:" >&2
    echo "  openssl ecparam -name prime256v1 -genkey -noout -out $KEY_FILE" >&2
    echo "或经 BRIDGE_DFU_SIGNING_KEY=<path> 指定已有 EC P-256 私钥" >&2
    exit 2
fi

if [ "$FLASH_ONLY" != yes ]; then
    PRISTINE_FLAG=""
    if [ "$PRISTINE" = yes ]; then
        PRISTINE_FLAG="-p"
    fi
    echo "== west build $PRISTINE_FLAG --sysbuild -b $BOARD firmware/app -d build-dfu"
    # 注意1: -D 的字符串值必须自带 Kconfig 引号（kconfig.cmake 会把缓存值
    #   原样写进 extra_kconfig_options.conf，不带引号 = "malformed string
    #   literal"）。
    # 80K 双槽分区图在板级 overlay（app）与 sysbuild/mcuboot.overlay
    #   （bootloader）中各声明一份，必须一致；sysbuild.conf 存在使任何
    #   west build 自动走 sysbuild，无单槽消费者。
    west build $PRISTINE_FLAG --sysbuild -b "$BOARD" "$REPO_ROOT/firmware/app" \
        -d "$BUILD_DIR" -- \
        "-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE=\"$KEY_FILE\""
fi

MERGED="$(find "$BUILD_DIR" -maxdepth 1 -name "merged*.hex" 2>/dev/null | head -1)"
if [ -z "$MERGED" ]; then
    # 命名兜底：更老版本产出 merged.hex / zephyr.hex
    if [ -f "$BUILD_DIR/merged.hex" ]; then
        MERGED="$BUILD_DIR/merged.hex"
    elif [ -f "$BUILD_DIR/zephyr.hex" ]; then
        MERGED="$BUILD_DIR/zephyr.hex"
    else
        echo "build-dfu: 找不到合并镜像 ($BUILD_DIR/merged*.hex)" >&2
        exit 1
    fi
fi
echo "== 产物"
ls -l "$MERGED" "$BUILD_DIR/dfu_application.zip" 2>/dev/null || true

if [ "$FLASH" = yes ]; then
    FLASH_LOG="$(mktemp -t build-dfu-pyocd)"
    echo "== pyocd flash -u $PYOCD_UID -f $PYOCD_FREQ -t $PYOCD_TARGET ($MERGED)"
    if ! pyocd flash -u "$PYOCD_UID" -f "$PYOCD_FREQ" -t "$PYOCD_TARGET" \
            --format hex "$MERGED" >"$FLASH_LOG" 2>&1; then
        cat "$FLASH_LOG" >&2
        rm -f "$FLASH_LOG"
        echo "build-dfu: pyocd flash 失败" >&2
        exit 1
    fi
    cat "$FLASH_LOG"
    # 烧录后断言（D1 防假成功，task-dfu-a §9.1 实锤场景）: pyocd 在 generic
    # cortex_m 目标上报成功却不写一字节，必须校验输出里的实际写入页数。
    if ! grep -q "programmed" "$FLASH_LOG"; then
        echo "build-dfu: 假成功防御触发: pyocd 输出缺 'programmed'（未确认写入）" >&2
        rm -f "$FLASH_LOG"
        exit 1
    fi
    # 页数形如 "programmed 327680 bytes (80 pages)"；BRE 提取。
    PAGES="$(sed -n 's/.*programmed [0-9][0-9]* bytes (\([0-9][0-9]*\) pages).*/\1/p' \
        "$FLASH_LOG" | tail -1)"
    if [ -z "$PAGES" ] || [ "$PAGES" -eq 0 ]; then
        echo "build-dfu: 假成功防御触发: pyocd 写入页数为 0 或缺失（generic 目标假成功?）" >&2
        rm -f "$FLASH_LOG"
        exit 1
    fi
    rm -f "$FLASH_LOG"
    echo "== pyocd reset -u $PYOCD_UID -t $PYOCD_TARGET"
    pyocd reset -u "$PYOCD_UID" -f "$PYOCD_FREQ" -t "$PYOCD_TARGET"
fi
