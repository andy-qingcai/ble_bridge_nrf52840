# ADR-002: UART 桥协议 v2 —— 继承帧语义，升级健壮性

日期：2026-09-24 ｜ 状态：已接受

## 背景

CH592 桥协议 `[0x55][LEN:u8≤60][CMD][SEQ][PAYLOAD][CRC8]` 经 5 份抓帧日志验证可靠，
36 命令/14 事件/10 状态码语义清晰。但两个硬伤：
1. LEN 8-bit ≤60 字节 —— 装不下 244B 的 GATT 数据面载荷；
2. CRC8 (poly 0x07) 在 921600 波特率长帧下碰撞概率不可忽略。

## 决策

| 字段 | v1 (CH592) | v2 (本工程) | 理由 |
|---|---|---|---|
| SOF | 0x55 ×1 | 0x55 ×1（保留） | 旧解析器经验可迁移 |
| LEN | u8 ≤60 | **u16 LE ≤512** | 容纳 247B MTU 数据面帧+开销 |
| CRC | CRC8 poly07 | **CRC16-CCITT-FALSE**，覆盖 CMD..PAYLOAD | 长帧完整性 |
| SEQ | u8 | u8（保留，响应回显） | 请求/响应配对已验证 |
| 事件 | SEQ=0 | SEQ=0 + 独立 8-bit 事件环形序号 | host 增量拉取防丢 |
| 流控 | 无 | **credit 流控**（事件通道） | UART 反压不得阻塞射频 |
| 波特率 | USB-CDC | 921600 8N1 + RTS/CTS（可 1M） | UARTE EasyDMA 原生支持 |

> **修订 1（2026-09-24，依据 docs/research/nordic-stack-eval.md）**：DK 板载 JLink OB VCOM
> 不官方支持 921600——物理主通道改为 **nRF52840 原生 USB 控制器的 CDC ACM 虚拟串口**
> （对上位机仍是同一 `/dev/cu.usbmodem*` 串口语义，帧协议不变）；UART0 引脚 + 外接
> USB-UART 转接板作为备选物理层（RTS/CTS 可用）。固件经 Kconfig 选择物理层。

- 不做字节填充：依赖 LEN+CRC 滑动重同步（quick_log StreamParser 模式，模糊测试守护）。
- CMD 空间划分：0x01-0x3F 保留旧语义映射，0x40-0x5F 新增 SUOTA/流控族，0x8E 调试事件保留。
- 协议常量单一定义源：`firmware/proto/bridge_proto.h`，host 端契约测试对照。

## 后果

- host 与固件的帧编解码必须双实现（C + Python），以黄金向量测试集锁定一致性。
- 旧抓帧日志不能直接回放（CRC/LEN 变了）——需一个 v1→v2 转换小工具供回归对照（低优先级）。
