# USB-CDC 自升级 OTA + A/B 双分区防变砖 评估报告

- 日期：2026-09-26（评估性调研，只读；未构建、未烧录、未改任何固件/代码）
- 仓库：本仓库 @ `06d54d4`（main，FR-H fix 后）
- NCS 工作区：`<NCS>`（NCS v3.4.1 / Zephyr 4.4.2 / SDK 1.0.1）
- 结论有效性声明：所有 Kconfig / 采样 / 分区数字均出自本地树文件路径 + 行号，或给出官方 URL；估算处显式标注"估算"。

---

## 0. TL;DR

**可行，且推荐"方案 A（MCUmgr SMP over 双 CDC）为主 + 方案 B（MCUboot 串口恢复作一键兜底）"的复合架构**：

1. **A/B 双槽在 1MB 内绰绰有余**：Nordic 官方 smp_svr 对 nrf52840dk 的参考分区就是 mcuboot 48K + 双槽各 456K + storage 64K（`<NCS>/nrf/samples/dfu/smp_svr/boards/nrf52840dk_nrf52840.overlay`）。我们实测应用仅 ~182KiB（见 §1.1），最坏按 330KiB 估，双槽后占用 < 1MB 的 70%。
2. **防变砖闭环由 MCUboot swap（offset/move）+ 未确认回退原语提供**：坏镜像拒绝、断电续接/回滚、启动即崩 revert，全部是 MCUboot 内建行为（§2.2/§4）；应用侧只需一处 `boot_write_img_confirmed()`（`include/zephyr/dfu/mcuboot.h:199`）。
3. **host 侧零新建协议**：Python `smpclient`/`smpmgr`（pip）原生支持 USB 串口 SMP，直接嵌进 `blebridge` CLI；`nrfutil device xfer` 可作 bootloader 模式的兜底工具。
4. **方案 C（自研 DFU 命令）不推荐**：要把 MCUboot 已解决的可靠性（swap journal、签名、revert 状态机）在桥协议层重做一遍，成本高且风险自担。

---

## 1. 现状基线（实测）

### 1.1 固件尺寸（真实构建产物实测，非估算）

对现有两份 CDC 形态构建产物跑 `arm-zephyr-eabi-size`：

| 构建 | text | data | bss | flash(text+data) | RAM(data+bss) |
| --- | --- | --- | --- | --- | --- |
| `firmware/app/build-fixwave/app/zephyr/zephyr.elf`（2026-09-25） | 183,832 | 2,920 | 64,548 | **186,752 B ≈ 182.4 KiB** | 67,468 B ≈ 65.9 KiB |
| `<NCS>/build-final-fix-app-cdc/app/zephyr/zephyr.elf` | 185,136 | 2,920 | 63,018 | 188,056 B ≈ 183.7 KiB | 65,938 B ≈ 64.4 KiB |

即：当前实际 ~183 KiB / 256 KiB RAM。任务书给的"273KB，含 FR-H+DYNAMIC_DB 后 ~330KB"作为**上界**保留；两种数字下双槽均可行（§2.4）。

### 1.2 链路与 host 现状

- USB CDC 已实测全通（桥帧协议在 `/dev/cu.usbmodem*`，RTT 3–5ms）；桥帧格式：`SOF=0x55` 定长头 + LEN + CMD + SEQ + payload + CRC16（`firmware/proto/bridge_proto.h:57,62`）。
- host 包 `host/src/blebridge/`（framing/bridge/cli/daemon，argparse 子命令，entry point `blebridge`）。
- **nrfutil 现状**：`~/.blebridge/ncs-venv` 装的是 legacy nrfutil **5.0.0**，在当前 protobuf 下已损坏（`TypeError: Descriptors cannot be created directly`，实测执行 `nrfutil version` 即崩）。方案落地需 `pip install nrfutil-tool`（nrfutil 7.x 家族）或直接用 `smpmgr`（§2.6）。

---

## 2. 本地 NCS 树实证

### 2.1 smp_svr 存在，且有 USB CDC 变体

- **Zephyr 上游**：`<NCS>/zephyr/samples/subsys/mgmt/mcumgr/smp_svr/`
  - `cdc.conf`：`CONFIG_USB_DEVICE_STACK_NEXT=y` + `CONFIG_CDC_ACM_SERIAL_INITIALIZE_AT_BOOT=y` + `CONFIG_MCUMGR_TRANSPORT_UART=y`
  - `usb.overlay`：`chosen { zephyr,uart-mcumgr = &cdc_acm_uart0; };`
  - `README.rst:110-126`：官方构建命令 `-DEXTRA_CONF_FILE="cdc.conf" -DEXTRA_DTC_OVERLAY_FILE="usb.overlay"`
  - `prj.conf`：`CONFIG_MCUMGR=y / CONFIG_IMG_MANAGER=y / CONFIG_MCUMGR_GRP_IMG=y / CONFIG_MCUMGR_GRP_OS=y / CONFIG_BOOTLOADER_MCUBOOT=y`
- **NCS 侧**：`<NCS>/nrf/samples/dfu/smp_svr/`（BLE 形态为主，`sysbuild.conf` 一行 `SB_CONFIG_BOOTLOADER_MCUBOOT=y`——sysbuild 集成模式即此）。
- 传输层实现：`zephyr/subsys/mgmt/mcumgr/transport/src/smp_uart.c`（绑定 `zephyr,uart-mcumgr` chosen 设备）；`Kconfig.uart:19` 注意 **`MCUMGR_TRANSPORT_UART depends on CONSOLE`**——我们 prj.conf 现为 `CONFIG_CONSOLE=n`，集成时须翻回 `y`（`UART_CONSOLE=n` 不变，无后端副作用）。MTU：`MCUMGR_TRANSPORT_UART_MTU` 默认 256，可调大（须 ≤ `MCUMGR_TRANSPORT_NETBUF_SIZE+2`）。

### 2.2 MCUboot 升级模式（swap 家族）与 confirm API

`<NCS>/bootloader/mcuboot/boot/zephyr/Kconfig`（`if !SINGLE_APPLICATION_SLOT`，L699 起的 choice `BOOT_IMAGE_UPGRADE_MODE`）：

| 模式 | Kconfig | scratch 分区 | 回退（revert） | 说明 |
| --- | --- | --- | --- | --- |
| swap-using-scratch | `BOOT_SWAP_USING_SCRATCH`（L705） | 需要 | 有 | 最保守，异构页安全 |
| **swap-using-offset** | `BOOT_SWAP_USING_OFFSET`（L718，**无 scratch 节点时的默认**，L701） | 不需要 | 有 | 相邻页对倒移，同构页限制；nRF52840 全片 4KB 均匀页（`nrf52840.dtsi:440` `erase-block-size=<4096>`）满足 |
| swap-using-move | `BOOT_SWAP_USING_MOVE`（L729） | 不需要 | 有 | 已被 offset 取代，保留兼容 |
| overwrite-only | `BOOT_UPGRADE_ONLY`（L711） | 不需要 | **无** | 帮助文本明说"prevents the fallback recovery" |
| direct-xip / ram-load | L747/L756 | 不需要 | 各有 `*_WITH_REVERT` 变体（L774/L785） | XIP 要按槽链接 |

**结论**：默认的 swap-using-offset 即"试用 + 确认 + 回退"语义（test 型 swap）；防变砖强度最高族。确认 API（应用侧）：

- `boot_is_img_confirmed()` / `boot_write_img_confirmed()`：`<NCS>/zephyr/include/zephyr/dfu/mcuboot.h:185,199`（多镜像 `boot_write_img_confirmed_multi` :215）
- 启动期 revert 行为由 MCUboot 状态机保证：unconfirmed test 镜像在复位后被换回原槽（swap journal 断电安全，官方设计文档 https://docs.mcuboot.com/design.html ）。

### 2.3 串口恢复（serial recovery）= "最后手段"已内建

`<NCS>/bootloader/mcuboot/boot/zephyr/Kconfig.serial_recovery`：

- `MCUBOOT_SERIAL`（menuconfig）→ `BOOT_SERIAL_CDC_ACM`（choice，对 nRF52840 非 dongle 板需显式选；自带 USB 描述符 Kconfig，默认 VID 0x1915 / PID 0x8523——host 可靠 PID 判别"设备已进 bootloader"）
- USB 栈：CDC ACM 默认走 legacy `USB_DEVICE_STACK`（L1538 附近 depends 链），但树内有 `boot_serial_cdc_acm_usb_next.c`，`CMakeLists.txt:400-402` 显示 `BOOT_SERIAL_CDC_ACM && USB_DEVICE_STACK_NEXT` 时使用——bootloader 是独立镜像，栈选择不影响我们用 NEXT 栈的应用
- **进入方式（entrance methods 菜单）**：
  - `BOOT_SERIAL_ENTRANCE_GPIO`（默认 y）：检测 GPIO（DK 按键）
  - `BOOT_SERIAL_WAIT_FOR_DFU`：每次复位等待 N ms 收第一个 MCUmgr 命令（`BOOT_SERIAL_WAIT_FOR_DFU_TIMEOUT` 默认 500ms——代价是每次上电延迟，建议不开）
  - **`BOOT_SERIAL_BOOT_MODE`**：经 Zephyr retention boot-mode 子系统进入——**应用可"软重启进 bootloader"**；`zephyr/subsys/retention/Kconfig:46-52` 帮助文本原话："button-less bootloader serial recovery mode entering from the application"；nRF52840 有 `gpregret1/2` 节点（`nrf52840.dtsi:89,97`）作物理载体
  - `BOOT_SERIAL_PIN_RESET`：复位脚引起的复位即留在 bootloader（= 硬件"双击复位"等价物）
  - `BOOT_SERIAL_NO_APPLICATION`：无有效应用时停在 bootloader（**防全砖底线，建议开**）
- 注：`nrf/samples/bootloader/` 是 NSIB/B0，其 README 明说 NSIB **不做** SMP 升级、要 SMP 须加 MCUboot 第二级——印证我们选 MCUboot 而非 NSIB。

### 2.4 分区管理器与双槽数字（1MB 放不放得下）

- PM 实现：`<NCS>/nrf/scripts/partition_manager.py`、`nrf/cmake/partition_manager.cmake`（+ sysbuild 版 `nrf/cmake/sysbuild/partition_manager.cmake`）；分区声明 `nrf/subsys/partition_manager/pm.yml.*`
- MCUboot 分区默认：`nrf/modules/mcuboot/boot/zephyr/Kconfig:33` `PM_PARTITION_SIZE_MCUBOOT` 默认 **0xBE00（nRF52 多镜像，~47.5KB）**；`bootloader/mcuboot/boot/zephyr/pm.yml` 定义 `mcuboot`（before mcuboot_primary）、`mcuboot_pad`(0x200)、`mcuboot_secondary`（`share_size: [mcuboot_primary]`）、`mcuboot_scratch`（**仅 swap-using-scratch 才存在**）
- settings 默认：`nrf/subsys/partition_manager/Kconfig`（SETTINGS 段）`partition-size=0x2000`（8KB）
- **Nordic 官方参考布局**（`nrf/samples/dfu/smp_svr/boards/nrf52840dk_nrf52840.overlay`，2026 版直接用 `zephyr,mapped-partition`，不需要 PM 也成立）：

| 分区 | 起址 | 大小 |
| --- | --- | --- |
| mcuboot | 0x0 | 48 KB |
| slot0 (image-0) | 0xC000 | **456 KB** |
| slot1 (image-1) | 0x7E000 | **456 KB** |
| storage (settings) | 0xF0000 | 64 KB |
| 合计 | | 1024 KB（无 scratch——offset/move 模式不需要） |

- **对我们的数字**：应用实测 182 KiB / 最坏估 330 KiB，远小于单槽 456 KB；即便应用翻倍到 450 KiB 仍放得下。**结论：1MB 双槽可行性余量充足**（最坏口径也只用 ~70%）。
- 剩余风险仅一条：swap 用时——330KB 镜像全量对倒，nRF52840 NVMC 擦 22ms/页 + 写吞吐，估算全程 **约 5–15s**（MCUboot 启动期间桥离线，属预期行为，host 应有超时预算）。

### 2.5 sysbuild 集成点

- sysbuild 模式选择：`zephyr/share/sysbuild/images/bootloader/Kconfig` `MCUBOOT_MODE_*`：`SWAP_USING_OFFSET` 为默认（L37），另有 `SWAP_USING_MOVE`(L55)/`SWAP_SCRATCH`(L62)/`OVERWRITE_ONLY`(L72)/`FIRMWARE_UPDATER`(L131)；签名强制（L209 依赖签名类型非 NONE）
- 应用只需 `sysbuild.conf` 一行 `SB_CONFIG_BOOTLOADER_MCUBOOT=y`（`nrf/samples/dfu/smp_svr/sysbuild.conf` 同款）；带 MCUboot 的 sysbuild 构建会产出 `dfu_application.zip`（SMP DFU 标准载荷，nrfutil/mcumgr/smpmgr 通用）

### 2.6 host 工具链现状

| 工具 | 串口 SMP 支持 | 安装 | 评估 |
| --- | --- | --- | --- |
| `mcumgr`（Go） | `--conntype serial --connstring dev=/dev/tty.usbmodem…,baud=…`（https://docs.zephyrproject.org/services/device_mgmt ；https://docs.mcuboot.com/serial_recovery ） | Go/brew | 可用；Go 环境是额外依赖；macOS 复位后设备名会变 |
| **`smpmgr` / `smpclient`（Python）** | serial（USB CDC）/ BLE / UDP 三传输（https://github.com/intercreate/smpmgr ；https://pypi.org/project/smpclient/ ） | `pip install smpmgr` | **推荐**：与 host 同为 Python，`smpclient` 可直接作为库嵌入 `blebridge` CLI，不引入 Go 工具链 |
| `nrfutil device` | `nrfutil device list`（mcuboot trait）/ `nrfutil device program --traits mcuboot` / `nrfutil device xfer <fw.zip>`——面向 **MCUboot 串口恢复**口（DevZone 多帖实证：https://devzone.nordicsemi.com/nordic-q-a ；Nordic Developer Academy "Exercise 4: DFU over USB"：https://academy.nordicsemi.com/lessons/topic ） | `pip install nrfutil-tool`（现装 5.0.0 已坏，见 §1.2） | 仅作为进 bootloader 后的兜底通道；运行态 DFU 不依赖它 |

---

## 3. 三候选方案对比

### 3.1 方案 A：MCUboot + MCUmgr SMP over USB CDC（NCS 正统）

**CDC 口复用是唯一设计自由度，三个子案**：

| 子案 | 做法 | 优点 | 缺点 | 判定 |
| --- | --- | --- | --- | --- |
| A1 双 CDC 接口（推荐） | devicetree 加第二个 `cdc_acm_uart1` 节点，`zephyr,uart-mcumgr = &cdc_acm_uart1`；桥协议继续占 CDC0 | 零复用风险；SMP 与桥帧并行在线；bootloader 模式下 USB 枚举变化（PID 0x8523）天然区分两种状态 | macOS 出现两个 usbmodem 口，host 需识别（config.json 加 smp 口映射即可） | **采用** |
| A2 单口字节分流 | 桥帧 SOF=0x55，MCUmgr 串口帧首字节 0x02/0x03 + base64 行（`smp_uart.c`/`uart_mcumgr` 协议），在应用 RX 路径按首字节分流 | 单 USB 口 | 要绕开/自管 uart_mcumgr 驱动的 ISR 所有权；0x55 分流器是自研协议逻辑，桥 EVT 突发与 SMP 长帧交织时序复杂 | 备选，不采用 |
| A3 重启切换角色 | 桥 CMD 进 DFU → GPREGRET 置位 → 重启进 MCUboot 串口恢复（或独立 smp 角色） | 无需第二个口 | 升级期间桥 BLE 功能消失；本质退化成方案 B | 并入方案 B 作为入口之一 |

- **host 侧三选**：mcumgr(Go) ✗（多一套工具链）；自研 Python SMP 客户端 △（smpclient 已是现成实现，自研只增加签名/SLIP 细节风险）；**smpclient 库 + `blebridge ota-self` 封装 ✓**
- **A/B + confirm 闭环**（swap-using-offset 默认语义）：
  1. 上传到 slot1（MCUmgr `image upload`，逐包 hash 校验）
  2. `image test`（置 pending test）+ `reset`
  3. MCUboot swap → 新镜像试运行；**应用自检通过后调 `boot_write_img_confirmed()`**（建议条件：CDC 枚举成功 + 桥 PING 响应 + ≥N 秒稳定 + WDT 喂狗正常）
  4. 不 confirm 且复位 → MCUboot 自动 revert 回 slot0；坏镜像（签名不过）→ MCUboot 拒绝 swap/revert；任一时刻断电 → swap journal 断电安全（恢复完成或回滚）

### 3.2 方案 B：MCUboot 串口恢复（"双击复位"/软命令进 bootloader）

- 不占运行态 CDC：升级期整个系统只有一个 bootloader 在 CDC 上服务（nrfutil device xfer / mcumgr 皆可对话）
- 进入口选择：硬件 GPIO（DK 按键）/ **GPREGRET boot-mode（桥协议软命令，无需物理按键，自定义板友好）** / 复位脚检测 / `BOOT_SERIAL_NO_APPLICATION` 兜底
- 与 A 对比：可靠性最高（不经过应用层，应用彻底变不了砖）；体验弱（升级期 BLE 桥离线、每次进 bootloader 需重启、500ms WAIT_FOR_DFU 增加常规上电延迟不建议开）；依赖：bootloader 镜像要多开 `MCUBOOT_SERIAL`（flash 开销 +~20-30KB，仍在 48KB 分区内需验证，不够就 80KB——官方 larger_mcuboot overlay 即 80KB 版布局）
- **判定：作为 A 的兜底层一并启用**（同一套 MCUboot，边际成本只有 bootloader 镜像的 Kconfig/flash）

### 3.3 方案 C：自研桥协议 DFU 命令（CMD.fw_*）

- 少做的：无新框架依赖、复用已验证 CDC 流控与桥帧协议
- 多做的（全部是 MCUboot/mcumgr 已免费提供的）：分块传输可靠性（窗口/重传/断点续传）、镜像 hash+签名校验、slot1 写入与 flash 分区管理、swap 触发（或绕过 MCUboot 直接写 slot0——那等于放弃双槽防变砖）、confirm/revert 状态机、host+固件两端协议测试矩阵
- 风险：等于把"防变砖"这个最难的部分自研一遍；桥固件内 bug 在 DFU 路径上就是直接变砖路径
- **判定：不推荐**。唯一值得从 C 拿走的，是一个 `CMD.enter_dfu`（GPREGRET → 重启进 bootloader 串口恢复）作为 UX 入口，归属方案 B

---

## 4. 防变砖威胁模型逐项核销（推荐架构 A1+B 下）

| 威胁 | 结局 | 机制 |
| --- | --- | --- |
| 坏镜像（签名/哈希不过） | **不发生任何切换**，旧镜像继续跑 | MCUboot 启动验签失败拒绝 swap；上传期 MCUmgr 逐包 SHA+尾包 hash 校验，host 侧即可判失败 |
| 半程断电·上传中 | slot1 半成品被忽略，slot0 正常起桥 | swap 未开始，MCUboot 只认完整+合法镜像 |
| 半程断电·擦除/写入 slot1 中 | 同上，重传即可 | progressive erase 只作用于 slot1 |
| 半程断电·swap 进行中 | 复电后 MCUboot 完成 swap 或回滚，两槽均不损坏 | swap 状态 journal（中断安全），官方设计保证 |
| 新镜像启动即崩 | **自动回退**，桥回到旧版本 | unconfirmed test 镜像 + 复位 → revert；建议 app 开 WDT（TASK_WDT）确保"崩"必然产生复位 |
| confirm 超时/忘 confirm | 下次复位 revert 回旧版（保守但安全） | MCUboot test 态语义；host 可在 `ota-self` 末尾强制 confirm 检查 |
| bootloader 自身损坏 | **变砖（唯一未消项）**，需 SWD/J-Link 重救 | 本架构无任何路径写 mcuboot 分区（应用只写 slot1+storage；MCUboot 不自写）；可选加固：`CONFIG_FPROTECT` 保护 0x0-0xC000；硬件红线：自定义板保留 SWD 焊点（DK 自带） |
| 应用完全丢失/全片脏 | 停在 bootloader 串口恢复，可再救 | `BOOT_SERIAL_NO_APPLICATION=y` |

---

## 5. 移植性备注（换芯片，如 DA1469x）

- **MCUboot 主体可移植**：`boot/zephyr` 是 SoC 无关的（依赖 flash 驱动 + flash_map）；Renesas SmartBond DA1469x 在 Zephyr 有官方 SoC 支持，swap/confirm/revert 语义原样跟随
- **SMP 传输可换**：CDC ACM（DA1469x 自带 USB）同样用 `zephyr,uart-mcumgr`；BLE 传输（`MCUMGR_TRANSPORT_BT`）也可选——host 的 smpclient 同样支持 BLE 传输，桥协议层不变
- **分区管理要重算**：Nordic partition manager 不随行；用 devicetree 固定分区（本报告 §2.4 的官方 overlay 正是 devicetree 写法，可直接作为模板）或目标厂工具。DA1469x 内嵌 flash 容量与页几何不同，双槽尺寸按目标重算
- 结论：**该 OTA 机制的"存活度"高**——90% 的资产（Kconfig 集、confirm 自检逻辑、host CLI、威胁模型）是 Zephyr 通用的，芯片相关只剩分区表

---

## 6. 推荐方案落地清单（方案 A1 为主 + B 兜底）

### 6.1 关键 Kconfig / devicetree

```text
# firmware/app/sysbuild.conf（新增）
SB_CONFIG_BOOTLOADER_MCUBOOT=y

# child_image/mcuboot.conf（bootloader 子镜像；即方案 B 兜底）
CONFIG_MCUBOOT_SERIAL=y
CONFIG_BOOT_SERIAL_CDC_ACM=y
CONFIG_BOOT_SERIAL_BOOT_MODE=y          # GPREGRET 软进 bootloader
CONFIG_BOOT_SERIAL_NO_APPLICATION=y     # 无应用兜底
# 可选: CONFIG_BOOT_SERIAL_ENTRANCE_GPIO=y（DK 按键）

# firmware/app/prj.conf（应用）
CONFIG_CONSOLE=y                        # MCUMGR_TRANSPORT_UART 依赖（UART_CONSOLE 仍 =n）
CONFIG_UART_MCUMGR=y
CONFIG_MCUMGR=y
CONFIG_MCUMGR_TRANSPORT_UART=y
CONFIG_MCUMGR_TRANSPORT_UART_MTU=4096   # 提速（约束: ≤ NETBUF_SIZE+2）
CONFIG_MCUMGR_GRP_IMG=y
CONFIG_MCUMGR_GRP_OS=y                  # reset 用于 test 复位
CONFIG_IMG_MANAGER=y
CONFIG_IMG_ERASE_PROGRESSIVELY=y
# 应用自确认（src 内）: boot_is_img_confirmed()/boot_write_img_confirmed()
#   include/zephyr/dfu/mcuboot.h:185,199；自检条件: CDC 枚举 + PING + WDT + N 秒

# devicetree overlay（双 CDC，模板见 zephyr/samples/subsys/usb/cdc_acm_bridge
#   /boards/rpi_pico.overlay——同一 USBD 下两个 cdc_acm 节点的官方示例）
/ { chosen { zephyr,uart-mcumgr = &cdc_acm_uart1; }; };
&zephyr_udc0 {
  cdc_acm_uart1: cdc_acm_uart1 { compatible = "zephyr,cdc-acm-uart"; };
};

# 签名: SB_CONFIG_MCUBOOT_SIGNATURE_TYPE_ECDSA_P256 + 私钥（出库保管）；
# sysbuild 自动产出 dfu_application.zip
```

分区：先按官方 smp_svr overlay 同款（48K/456K/456K/64K）走 sysbuild+PM 默认；若 bootloader 加 serial recovery 超出 48KB，则切 larger_mcuboot 版布局（80K/440K/440K/64K，`nrf/samples/dfu/smp_svr/boards/nrf52840dk_nrf52840_larger_mcuboot.overlay`）。

### 6.2 host 命令草图

```text
blebridge ota-self <image.bin|dfu_application.zip> [--port auto] [--dry-run] [--no-confirm]
  1. ping/info 健康检查 → 2. smpclient(image upload → image test) → 3. smpclient reset
  4. 等待 USB 重枚举（bridge PID 恢复 / bootloader PID=0x8523 则报"卡在 bootloader"）
  5. 轮询 info + image state → 应用侧自确认后报成功；超时未确认 → 提示将 revert
blebridge ota-self --recover     # CMD.enter_dfu(GPREGRET) → 重启进 bootloader 串口恢复
                                 # → nrfutil device xfer / smpmgr 兜底重刷
# config.json 增: smp_port 映射（A1 双 CDC 后 macOS 出现第二个 /dev/cu.usbmodem*）
```

### 6.3 工作量拆分（S=半天内 / M=1-2 天 / L=3 天+）

| # | 任务 | 量 | 依赖 |
| --- | --- | --- | --- |
| 1 | sysbuild + MCUboot 接入（DK 上双槽起跑，验证 swap/revert 基线） | M | 无 |
| 2 | 双 CDC overlay + SMP 传输并入应用 + prj.conf 修订（CONSOLE 翻转回归） | M | 1 |
| 3 | 应用自确认逻辑（自检条件 + `boot_write_img_confirmed` + WDT 策略） | S/M | 1 |
| 4 | host：`pip install smpmgr`（或 vendored smpclient）+ `blebridge ota-self` 命令 + config.json smp 口映射 | M | 1,2 |
| 5 | `CMD.enter_dfu`（GPREGRET boot-mode 软进 bootloader）+ bootloader Kconfig（MCUBOOT_SERIAL/CDC） | M | 1 |
| 6 | 签名密钥生成与 CI 签名流程 | S | 1 |
| 7 | 防变砖矩阵实测：坏签名/上传中断电/swap 断电/启动即崩/不 confirm/串口恢复救援（6 场景脚本化，落 docs/reports 证据） | L | 1-5 |
| 8 | 自定义板验证 + 文档（README/协议契约登记 enter_dfu 命令） | M | 7 |

关键风险两条：(a) `MCUMGR_TRANSPORT_UART depends on CONSOLE` 与现有 `CONFIG_CONSOLE=n` 的翻转对桥协议无副作用（UART_CONSOLE/LOG 仍关），但需回归 CDC 链路；(b) bootloader 加 serial recovery 后的 flash 尺寸需一次实测决定 48K 还是 80K 分区。

---

## 7. 参考来源

本地树（全部只读核对）：
- `<NCS>/zephyr/samples/subsys/mgmt/mcumgr/smp_svr/`（cdc.conf / usb.overlay / README.rst:110-126 / prj.conf）
- `<NCS>/nrf/samples/dfu/smp_svr/`（sysbuild.conf、boards/nrf52840dk_nrf52840.overlay、boards/…_larger_mcuboot.overlay）
- `<NCS>/bootloader/mcuboot/boot/zephyr/Kconfig`（L699-760 upgrade modes；L711 BOOT_UPGRADE_ONLY；L774/L785 revert 变体）
- `<NCS>/bootloader/mcuboot/boot/zephyr/Kconfig.serial_recovery`（全文：BOOT_SERIAL_CDC_ACM、entrance methods、USB 描述符默认 VID/PID）
- `<NCS>/bootloader/mcuboot/boot/zephyr/pm.yml`；`<NCS>/nrf/modules/mcuboot/boot/zephyr/Kconfig:33`；`<NCS>/nrf/subsys/partition_manager/Kconfig`（SETTINGS 0x2000）
- `<NCS>/zephyr/include/zephyr/dfu/mcuboot.h:185,199`；`<NCS>/zephyr/subsys/retention/Kconfig:46-52`；`<NCS>/zephyr/dts/arm/nordic/nrf52840.dtsi`（flash0 1MB/4KB 页，gpregret1/2）
- `<NCS>/zephyr/subsys/mgmt/mcumgr/transport/Kconfig.uart`、`src/smp_uart.c`；`<NCS>/zephyr/share/sysbuild/images/bootloader/Kconfig`（MCUBOOT_MODE）
- `<NCS>/zephyr/samples/subsys/usb/cdc_acm_bridge/boards/rpi_pico.overlay`（双 CDC 官方示例）
- 本仓库：`firmware/app/prj.conf`、`firmware/proto/bridge_proto.h:57`、`host/src/blebridge/cli.py`、两份 elf 实测

网络（官方）：
- MCUboot 设计（swap 断电安全）：https://docs.mcuboot.com/design.html ；串口恢复：https://docs.mcuboot.com/serial_recovery
- Zephyr MCUmgr：https://docs.zephyrproject.org/services/device_mgmt
- smpmgr：https://github.com/intercreate/smpmgr ；smpclient：https://pypi.org/project/smpclient/
- nrfutil device（mcuboot trait / xfer / program）：https://devzone.nordicsemi.com/nordic-q-a （多篇 2023–2025 实证帖）；Nordic Developer Academy "Exercise 4: DFU over USB"：https://academy.nordicsemi.com/lessons/topic
- nRF Util 官方页：https://www.nordicsemi.com（"nrfutil device … supports MCUboot and J-Link"）
