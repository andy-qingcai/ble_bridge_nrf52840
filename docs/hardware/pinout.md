# nRF52840 × DAPLink（CMSIS-DAP）接线取证笔记

- 日期：2026-09-24
- 适用：blebridge 桥固件（NCS v3.4.1，安装路径以 `<NCS>` 指代），目标芯片 nRF52840（aQFN73，唯一封装）
- 目的：指导工程师直接用杜邦线接 DAPLink 做 SWD 烧录调试，并自选 UART 引脚接上位机
- 取证原则：所有引脚号均标注出处（官方文档 URL 或本地 NCS 树"文件路径:行号"），无凭记忆项

---

## 1. SWD 调试口（芯片级，固定引脚）

### 1.1 关键事实

| 信号 | nRF52840 上的位置 | 出处 |
|---|---|---|
| **SWDIO** | **专用调试脚，不是 P0.xx GPIO**。aQFN73 封装第 19 脚，裸片球 **AC24** | ①② |
| **SWDCLK** | **专用调试脚，不是 P0.xx GPIO**。aQFN73 封装第 20 脚，裸片球 **AA24** | ①② |
| **RESET（nRESET）** | **P0.18**（UICR.PSELRESET 的默认复位脚） | ③④⑤ |
| SWO（可选 trace） | 本接线方案不需要，不展开 | — |

> ⚠️ **纠错**：网上常见说法"nRF52840 SWDIO=P0.20 / SWCLK=P0.21"**是错的**。
> nRF52840 Product Specification 引脚表中，P0.20/P0.21 是普通 "Digital I/O — General purpose I/O"
> （物理脚 17 及球 A18/B20 区域），而 SWDIO/SWCLK 独占物理脚 19/20（球 AC24/AA24），行文为
> "SWDIO — Debug — Serial wire debug I/O"、"SWDCLK — Debug — Serial wire debug clock input"（出处①）。
> 板级佐证：nRF52840 DK 官方板卡文件把 P0.19–P0.23 用作 QSPI 闪存（含 P0.20/P0.21），若 SWD 真在
> P0.20/21 上，板上闪存与 SWD 会直接电气冲突——不可能（出处⑥⑦）。

### 1.2 出处

1. nRF52840 PS v1.1 引脚表的逐字转载（joric/nrfmicro wiki，开头注明引自官方 PDF
   `https://infocenter.nordicsemi.com/pdf/nRF52840_PS_v1.1.pdf` 第 576 页起）：
   `https://raw.githubusercontent.com/wiki/joric/nrfmicro/Pinout.md` —— aQFN73 表第 17/18/19/20 行
   （`|17|P0.20|Digital I/O|`、`|19|SWDIO|Debug|`、`|20|SWDCLK|Debug|`）、球坐标表
   `AA24|SWDCLK|`、`AC24|SWDIO|`（本次取证的 /tmp/nrfmicro_pinout.md 175–180、264、274 行）。
   官方 PS 现行页面（内容同源，docs.nordicsemi.com 有 Cloudflare 防抓取，浏览器可正常访问）：
   `https://docs.nordicsemi.com/bundle/ps_nrf52840/page/index.html`
2. makerdiary nRF52840 MDK USB Dongle 官方硬件页：调试接线表仅按 "SWDIO / SWDCLK / RESET"
   裸名连接，未给任何 P0.xx 编号，并单独注明 NFC 才与 P0.09/P0.10 共享
   `https://wiki.makerdiary.com/nrf52840-mdk-usb-dongle/hardware/`
3. 本地 NCS v3.4.1，Zephyr UICR 绑定，`<NCS>/zephyr/dts/bindings/arm/nordic,nrf-uicr.yaml:23-29`：
   `gpio-as-nreset` 说明原文 "nRESET pin in nRF52820/52833/52840 series: **P0.18**"
4. 本地 DK 板级 DTS：`<NCS>/zephyr/boards/nordic/nrf52840dk/nrf52840dk_nrf52840.dts:157-159`
   （`&uicr { gpio-as-nreset; }`）及 `:169-176`（`gpio-line-names` 索引 18 = "RESET"）
5. PS 表转载同上（①）：`P0.18/RESET ... usually reserved for RESET`
6. 本地 DK pinctrl：`<NCS>/zephyr/boards/nordic/nrf52840dk/nrf52840dk_nrf52840-pinctrl.dtsi:149-159`（QSPI 用 P0.17–P0.23）
7. 本地 DK DTS：`nrf52840dk_nrf52840.dts:171`（`gpio-reserved-ranges = <17 7>` 即 P0.17–P0.23 被 QSPI/板级功能保留）
8. 本地 MDK 头文件：`<NCS>/modules/hal/nordic/nrfx/bsp/stable/mdk/nrf52840.h:917-919`
   （UICR.PSELRESET[2] = "Mapping of the nRESET function"；UICR.APPROTECT = "Access port protection"）

### 1.3 DAPLink → nRF52840 接线表

| DAPLink 排针 | 接目标 nRF52840 | 必接？ | 说明 |
|---|---|---|---|
| SWDIO | 目标板 SWDIO 焊盘（专用脚，板上丝印一般直接标 SWDIO） | **必接** | 数据线 |
| SWCLK | 目标板 SWDCLK 焊盘 | **必接** | 时钟线 |
| GND | 目标 GND | **必接** | 共地，杜邦线场景强烈建议就近接地 |
| VTref | 目标 3.3 V 供电域（VDD 网络） | **必接** | Target Reference Voltage，探针以它判定目标电平；官方文档列入必接清单（出处 9） |
| VDD / 3V3（输出） | 目标 VDD | 可选 | 仅当目标板无自备电源时由 DAPLink 供电（出处 9） |
| nRST | nRF RESET = **P0.18** 网络 | 可选 | 官方文档标可选（出处 9）；接上可救"跑飞/深睡"的目标 |

- 出处 9：本地 NCS 官方文档
  `<NCS>/nrf/doc/nrf/app_dev/device_guides/custom/programming_custom_board.rst:30-42`
  —— 原文：必接 SWDCLK、SWDIO、GND、VTref；可选 VDD（供电）、RESET（复位）。
  同文件 :74 注明 "The NCS supports various debug interfaces like J-Link and **CMSIS-DAP**"。

### 1.4 注意事项

- **SWD 引脚勿复用**：nRF52840 的 SWDIO/SWCLK 是封装专用脚，本来就当不了 GPIO；
  真正要防的是自制板上把 SWD 焊盘就近引到杜邦排针后，误插了别的外设信号。
- **P0.18（RESET）严禁挪作他用**（如 GPIO/UART）。板上 nRESET 网络通常同时连到
  DAPLink nRST 与按键；被复用后烧录时探针拉 RESET 会打断正在运行的电路。
- **慎碰 UICR.APPROTECT / DEBUGCTRL**（`nrf52840.h:919-922`，出处 8）：访问保护一旦写入，
  SWD 会被锁死，须整片擦除（ERASEALL）才能恢复。
- 目标板**不上电则 SWD 不通**：VTref 无电压时多数探针（含 DAPLink）拒绝建立会话。

---

## 2. UART 引脚

### 2.1 UARTE 的 PSEL 灵活性与限制

- nRF52840 有 UARTE0/UARTE1，TX/RX/RTS/CTS 经 PSEL 可映射到**任意 GPIO**
  （Zephyr 里即 pinctrl 机制）。本地铁证：官方测试 overlay 把 DK 板的 uart0 整体挪到
  P1.01–P1.04 也能跑——
  `<NCS>/zephyr/tests/drivers/uart/uart_pm/boards/nrf52840dk_nrf52840.overlay:14-33`。
- 选脚限制（均出自 PS 引脚表转载①，行号为 /tmp/nrfmicro_pinout.md 行号）：

| 引脚 | 限制 | 表行号 |
|---|---|---|
| P0.02/P0.03、P0.28–P0.31、P1.10–P1.15 | "Standard drive, **low frequency** I/O only"（模拟偏置引脚，别当高速 UART 长线用） | 189–193、214–232 |
| P0.09/P0.10 | 默认 NFC 天线脚（作 GPIO 亦低频限定；改 GPIO 需动 UICR.NFCPINS） | 180–181 |
| P0.00/P0.01 | XL1/XL2（32.768 kHz 晶振） | 148 |
| P0.18 | RESET | 148 |
| （DK 板上）P0.17–P0.23 | 被 DK 板载 QSPI 闪存占用（出处⑥⑦） | — |

### 2.2 nrf52840dk 板级默认 uart0 引脚（本项目 uart0 备选模式沿用）

| 信号 | 引脚 | 出处（本地 NCS 树） |
|---|---|---|
| TX | **P0.06** | `<NCS>/zephyr/boards/nordic/nrf52840dk/nrf52840dk_nrf52840-pinctrl.dtsi:7-11`（`NRF_PSEL(UART_TX, 0, 6)`） |
| RX | **P0.08** | 同文件 `:13-17`（`NRF_PSEL(UART_RX, 0, 8)`，带 bias-pull-up） |
| RTS | **P0.05** | 同文件 `:10`（`NRF_PSEL(UART_RTS, 0, 5)`） |
| CTS | **P0.07** | 同文件 `:15`（`NRF_PSEL(UART_CTS, 0, 7)`） |

交叉验证：`nrf52840dk_nrf52840.dts:169-176` 的 `gpio-line-names` 索引 5–8 依次为
"RTS, TXD, CTS, RXD"；`:185-192` 确认 `&uart0` 使能并用 `uart0_default/uart0_sleep` 两个
pinctrl 态。

本项目现状：`<repo>/firmware/app/boards/nrf52840dk_nrf52840.overlay`
—— 注释（:9-10）明确 uart0 备选模式沿用板默认 TX=P0.06 RX=P0.08 RTS=P0.05 CTS=P0.07，
并叠加 `hw-flow-control` + `current-speed = <921600>`（:22-25）。

### 2.3 本项目换 UART 引脚的方法（NCS v3.4.1 = pinctrl 写法）

- 证据：UARTE 绑定 `<NCS>/zephyr/dts/bindings/serial/nordic,nrf-uart-common.yaml:1-13`
  include `pinctrl-device.yaml`，只有 `pinctrl-0/pinctrl-names`，**没有** `tx-pin/rx-pin`
  旧属性（旧写法属 Zephyr 2.x，v3.4.1 不可用）。
- 标准做法：在本项目 `firmware/app/boards/nrf52840dk_nrf52840.overlay` 里**新增**一组
  pinctrl 态并把 `&uart0` 指过去（与官方 uart_pm 测试 overlay 完全同款）：

```dts
&pinctrl {
	uart0_default_alt: uart0_default_alt {
		group1 { psels = <NRF_PSEL(UART_TX, 1, 1)>; };
		group2 { psels = <NRF_PSEL(UART_RX, 1, 2)>, <NRF_PSEL(UART_CTS, 1, 4)>; bias-pull-up; };
	};
	uart0_sleep_alt: uart0_sleep_alt {
		group1 { psels = <NRF_PSEL(UART_RX, 1, 2)>, <NRF_PSEL(UART_CTS, 1, 4)>; low-power-enable; };
		group2 { psels = <NRF_PSEL(UART_TX, 1, 1)>; low-power-enable; };
	};
};
&uart0 { pinctrl-0 = <&uart0_default_alt>; pinctrl-1 = <&uart0_sleep_alt>; pinctrl-names = "default", "sleep"; };
```

（示例即官方测试用的 P1.01/P1.02/P1.04，DK 上对应 Arduino D0/D1 排针，好插杜邦线；
换任意空脚只需改 `NRF_PSEL(信号, 端口, 号)` 三个数字。`NRF_PSEL` 宏由板级 include 链自动带入，
应用 overlay 无需额外 include——两个官方测试 overlay 即先例。）

### 2.4 与 SWD 的冲突警告

> 🔴 **[最重要] 任何 UART/外设都不要占用 P0.18（nRESET）**。
> P0.18 默认就是复位脚（出处③④⑤），DAPLink 的 nRST 会在烧录瞬间拉它；若被选作 UART RX/TX，
> 烧录时 UART 通信必被打断，且可能拖累目标复位。
>
> 默认 uart0 四脚 P0.05–P0.08 与 SWD **无引脚冲突**：nRF52840 的 SWDIO/SWCLK 是封装专用脚，
> 不在 P0.xx 里（见 §1.1 纠错）。注意仅是物理层面的：DK 的 SWD 排针与 IO 排针相邻，杜邦线
> 别把 SWDIO/SWCLK 和 TXD/RXD 插串排。
>
> 🟠 在 DK 板上另避开 P0.17–P0.23（板载 QSPI 闪存，出处⑥⑦）与 P0.13–P0.16（板载 LED）。

---

## 3. DAPLink 接 nRF52840 的实操注意

### 3.1 供电与 VTref

- nRF52840 供电：VDD 直供约 1.7–3.6 V（PS 电气规格，官方 PDF 见出处①链接）；亦支持 VDDH
  高压模式（内部 LDO/DC-DC，VDDH 可到 5.5 V，REGOUT0 可配 VDD 输出）——三种模式描述见
  PS 表转载 `/tmp/nrfmicro_pinout.md:125-131`。
- **DAPLink 3.3 V 直供可行**：落在 VDD 允许范围内（裸芯片/自制板场景）。目标板若已自供电
  （尤其 DK 板由 USB/LDO 供电），**不要再接 DAPLink 的 3V3**，避免双电源打架。
- **VTref 必接目标供电域**（3.3 V 网络）：官方必接清单含 VTref（出处 9）。VTref 是探针的
  电平参考，不接则探针无从判定目标电平，通常直接拒绝工作。

### 3.2 RESET 是否必须接？

- **不是必须**。官方文档把 RESET 列为可选连接（出处 9，`:41-42`：custom board 没有复位脚
  也可以不接）。nRF52 的 SWD 烧录/调试本身不依赖 RESET 线——调试器经 SWD 即可控制
  调试逻辑并复位内核（DHCSR/AIRCR），烧录前 halt 目标不靠物理 nRST。
- 建议接上（DAPLink nRST → P0.18 网络）：目标跑飞、进入深度休眠或时钟异常时，物理复位
  是最可靠的"救砖"手段；杜邦线场景稳定性收益明显。

### 3.3 目标是 nRF52840 DK 板时的板载调试器冲突

- DK 板载 J-Link OB（接口 MCU）**默认独占** nRF52840 的 SWD。SWD 总线只能有一个主人，
  外接 DAPLink 直接怼到芯片 SWD 焊盘/排针会与 OB 抢线。
- 官方规避方式（nRF52840 DK User Guide，"Debug input" 章节）：
  把 **SW10 拨到 nRF ONLY**（模拟开关断开接口 MCU 与目标 SWD 的连接），再把 DAPLink 接到
  **Debug in 排针 P18**；DEFAULT 挡位则是接口 MCU 经 USB 工作。
  出处：DK User Guide v3.0.3（"The Debug in connector P18 makes it possible to connect
  external debuggers when the interface MCU USB cable is not connected or the DK is in
  nRF only mode"）；官方页 `https://docs.nordicsemi.com/bundle/ug_nrf52840_dk/page/UG/dk/debug_in.html`
  （本机抓取被 Cloudflare 拦，内容已经 DevZone/Scribd 转载核对，现场请以板上丝印为准）。
- 反向场景（用 DK 的 OB 当探针调外部板）官方文档亦有整节说明：
  `programming_custom_board.rst:51-62`（接 DK 的 SWD debug output 排针，参考 DK 用户指南
  "Debug output" 页）。

---

## 4. 结论速查

| 项 | 值 |
|---|---|
| SWDIO | 专用脚（aQFN73 第 19 脚 / 球 AC24），**不是 P0.xx** |
| SWDCLK | 专用脚（aQFN73 第 20 脚 / 球 AA24），**不是 P0.xx** |
| RESET | **P0.18**（勿复用） |
| DAPLink 必接线 | SWDIO、SWCLK、GND、VTref（VTref 接目标 3.3 V 域）；VDD/nRST 可选 |
| DK 默认 uart0 | TX=P0.06，RX=P0.08，RTS=P0.05，CTS=P0.07 |
| 换脚方案 | overlay 里自定义 `uart0_default_alt/uart0_sleep_alt` pinctrl 态，`&uart0` 重指 `pinctrl-0/1`（勿用旧 tx-pin 写法） |
| DK 板接外部 DAPLink | SW10 → nRF ONLY，DAPLink 接 P18（Debug in） |
