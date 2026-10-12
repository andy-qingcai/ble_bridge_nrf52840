# 需求规格 (REQUIREMENTS)

状态：v1.3（定位重塑：工具本质 = nRF52840 BLE 全透传/全操控双角色工具（central+外设，host 全权），SUOTA/OTA 是这套透传原语上的应用流程；v1.1 增补通用 BLE 工具模式；原 v1.0 统筹者签署）。
本文件是实现工作的约束权威（spec）。工具名 **blebridge**（host 包 `blebridge`，CLI `blebridge`）；文中历史提交号与旧路径保留不动。

## 0. 项目背景（核心动机）

**BLE 设备在连续 OTA/升级场景可能失联**（无广播、无 GATT 响应）——生产级连续
升级必须能应对这类设备侧状态，本系统必须提供：

1. **协议级观测**：判定设备所处状态（正常 / OTA 瞬态 / 授权门锁定 / 睡眠）；
2. **协议级恢复原语**：MEM_DEV 事务解锁 → ABORT → END → MEM_DEV_REBOOT
   （实测 MEM_DEV_REBOOT 后设备 ~10s 内重新广播）；
3. **连续升级能力**：10 轮压测实证 bank 1 连续 6 次同镜像 OTA 全部成功——连续
   升级在协议层可行。

OTA 后首启动瞬态（几十秒 GATT 静默，自愈）、MEM_DEV 授权门（订阅 0x78，需先
完成一次 MEM_DEV 事务）等形态均为协议级状态，恢复手段都建立在这组透传原语上。

定位裁定：blebridge 的本质是 **BLE 全透传/全操控工具**
（central + 外设双角色，host 全权）——升级（SUOTA，FR-C/FR-F5）等都是同一组
透传原语（扫描/连接/发现/读写/订阅/广播）之上的**应用流程编排**，不是工具的
身份本身；工具的身份是通用 BLE 能力。

## 1. 目标

以 nRF52840 为 BLE central + 外设双角色执行器，上位机通过串口（USB CDC / UART）
对**任意 BLE 设备**完成：扫描 / 连接 / 服务发现 / GATT 读写与订阅 / 本地服务与
动态服务定义 / 广播控制 / 链路调优 / 事件流回传；在此之上以 host 驱动的应用流程
支撑具体场景——Dialog SUOTA 固件升级（SUOTA 全流程，公开协议 AN-B-022）。
所有智能（协议状态机、应用编排、恢复阶梯）放上位机（Python 包 `blebridge`）；
固件做可靠的"协议执行器"（通用 BLE 原语，无任何应用层专用行为）。

## 2. 功能需求

### FR-A UART 桥协议（固件 ↔ host）
- FR-A1: 帧格式 `SOF(0x55) | LEN(2B LE) | CMD(1B) | SEQ(1B) | PAYLOAD(0..512B) | CRC16(2B LE, CCITT-FALSE, 覆盖 CMD..PAYLOAD)`。
- FR-A2: 请求/响应配对（响应 CMD|0x80，携带同 SEQ）；事件帧独立 CMD 空间，带 8-bit 事件序号环形缓冲。
- FR-A3: 事件通道 credit 流控：host 授予额度，固件超额度丢弃并计数上报（不允许阻塞 BLE 数据面）。
- FR-A4: 逐字节解析 + CRC 失败/丢字节时滑动重同步（借鉴 quick_log StreamParser 设计）。
- FR-A5: 物理层主选 nRF52840 原生 USB CDC ACM（DK USB 口，USB 高速可靠），备选 UART0@921600 8N1+RTS/CTS（外接转接板）；帧协议层与物理层解耦（ADR-002 修订 1）。

### FR-B BLE central（固件）
- FR-B1: 扫描（按 0xFEF5 服务数据/地址过滤），结果以事件回传。
- FR-B2: 连接/断开/连接参数更新；**必须正确处理对端主动发起的 ATT_EXCHANGE_MTU_REQ**（自动回复，验收核心）。
- FR-B3: MTU ≥ 247 + 数据长度扩展 DLE 自动协商；2M PHY 可选。
- FR-B4: GATT 服务发现（0xFEF5）、读、写（带响应/无响应两种）、CCCD 订阅，notify 以事件回传。
- FR-B5: SMP 具备 pairing 能力（可发起/接受，便于未来加密要求）。
  注：交付形态为明文 SUOTA（CONFIG_BT_SMP 未开），与参考设备行为一致，
  不阻塞 G1/G2；pairing 能力延期至 L3 后加固任务补齐。

### FR-C SUOTA 引擎（host 驱动，固件执行）
- FR-C1: 完整实现 Dialog AN-B-022 SUOTA 状态机（MEM_DEV/GPIO_MAP/PATCH_LEN/PATCH_DATA/SERV_STATUS/END/REBOOT）。
- FR-C2: 写类型严格区分：控制特征带响应写，PATCH_DATA 无响应写。
- FR-C3: 块调度：chunk = min(244, MTU-3)，每块 9 片连发后等 notify 0x02（30s 超时）；末块按余量重写 PATCH_LEN。
- FR-C4: 载荷 = 镜像文件 + 1 字节全文件 XOR；镜像头解析支持 DA1469x/DA1468x/DA1458x 三族（image.py）。
- FR-C5: 全程进度/错误事件流；失败可中止（ABORT 0xFF000000）与恢复（重连续传由 host 决策）。

### FR-D 上位机
- FR-D1: Python 包分层：`framing`（编解码+解析器）→ `transport`（Serial/Fake 双后端）→ `bridge`（命令+事件环）→ `suota`（升级状态机）→ `image`（镜像解析）。
- FR-D2: daemon：常驻进程独占串口，TCP 127.0.0.1 换行 JSON API，一问一答 + 事件增量拉取；串口断开自动重连。
- FR-D3: CLI（脚本化）与 MCP 服务器（AI 工具集成，含 flash_firmware、逃生口 raw_command）。
- FR-D4: 旧 daemon 的 15 个命令语义对等移植（ping/info/scan/connect/.../drain/events），新增 suota_* 命令族。

### FR-E SUOTA 外设模拟器（suota_sim 固件）
- FR-E1: BLE peripheral，服务 0xFEF5 全部 10 特征，属性严格复刻（PATCH_DATA 无响应写、SERV_STATUS notify）。
- FR-E2: **连接后立刻主动发 ATT_EXCHANGE_MTU_REQ**（外设主动 MTU 协商行为，作为桥固件的行为测试矢量）。
- FR-E3: 完整 SUOTA 从机行为：0x10 IMAGE_STARTED、按块 0x02 ACK（可配 120–135ms 延迟）、SAME_IMAGE 0x15 拒绝开关、块数据 XOR-CRC 校验。
- FR-E4: 内置合成镜像（大小可配）；通过桥命令注入镜像内容的模式留接口。

### FR-F 通用 BLE 工具模式（P1，2026-09-24 通用化裁定：固件 = 通用 BLE 执行器，SUOTA 全流程在 host）
- FR-F1: 固件不含任何 SUOTA 专用行为：连接后**不自动做服务发现**，**任何情况下
  不主动断连**（发现未执行/失败/服务不存在一律保持连接，状态经 EVT.DEBUG 回报）。
- FR-F2: 发现由 host 触发：CMD.disc_svc(0x0C) 携带 16 位服务 UUID，固件即时跑
  bt_gatt_dm，把发现的全部特征并入 GATT map（uuid16→handle，128 位 UUID 取 SIG
  前缀；跨多次发现累积，断连清空），响应携带本次发现特征数；0x0000/空载荷 =
  重发现上一服务。
- FR-F3: GATT 读写/订阅支持**原始 handle 寻址**（uuid16 字段 0x0000 哨兵 + 后续
  handle 字段，见 protocol-contract §4），不依赖发现 map，任意设备连接即可用；
  uuid16 寻址语义不变（需先发现）。
- FR-F4: 链路调优命令：CMD.conn_update(0x44) 连接参数更新（异步，结果走
  EVT.DEBUG）、CMD.phy(0x45) PHY 偏好（1=1M/2=2M）。CMD.pair 保持 0x43。
- FR-F5: SUOTA 是 host 驱动的一种应用流程（`blebridge/suota.py`）：会话开始显式
  `disc_svc(0xFEF5)`（兼容自动发现的旧固件，幂等）；订阅遇 ATT 0x78（OTA 重启后
  授权态，实测与配对无关）以"一次 MEM_DEV 事务（start 写 + ABORT
  回退）"解锁后重试一次；MEM_DEV 遇 ATT 0x0F 走 pair→重试。两条拒绝码两条恢复
  路径并存。

### FR-G 设备状态观测原语（P1，协议级诊断/恢复操作集）
- FR-G1: 状态观测：扫描可见性 → 连接探测 → GATT 探针（suota_version 读取）→
  状态判定（正常 / OTA 瞬态 / 授权门锁定 / 睡眠不可达）。
- FR-G2: 恢复操作阶梯（由轻到重）：MEM_DEV 事务解锁 → ABORT → END →
  MEM_DEV_REBOOT（实测 ~10s 重广播）；每级之间 GATT 探针复测。
  CLI/daemon 不内置捆绑命令，操作经既有 gatt_* 原语即可编排。

### FR-H BLE 外设模式（2026-09-24，与 central 能力合成完整通用 BLE 工具；
本特性内为纯通用 BLE 能力，不含任何应用层专用引用）
- FR-H1: 外设广播控制 host 命令化：CMD.adv_start(0x46)（interval_ms u16 LE，
  0xFFFF = 固件默认；1B 本地名字开关；已广播 = 幂等 OK）与 CMD.adv_stop(0x47)
  （幂等）；广播状态经 EVT.DEBUG 回报。
- FR-H2: 通用测试 GATT 服务（编译期注册，uuid16=0xFFE0）：0xFFE1 Read +
  Write-Without-Response（对端写 → 固件存值 + EVT.GATTX subtype 0x01 事件回传
  host，payload 带角色标记/uuid16/handle/数据）；0xFFE2 Read + Notify（CCCD），
  host 可经命令推送 notify 内容（默认静态值）。
- FR-H3: notify(0x48) / set_char(0x49) host 命令：向 0xFFE2 推 notify（无已
  订阅对端 = 0x74）/ 更新 0xFFE1 当前值；支持 uuid16 寻址本地服务特征。
- FR-H4: 外设角色连接/断连事件回传：EVT.CONN/EVT.DISC 载荷增加 1B 角色字段
  （0=central 角色链路，1=peripheral 角色链路）；central 与 peripheral 角色可
  并存（BT_MAX_CONN=2 = 1+1）；CMD.disconnect 空载荷 = 外设链路优先断开，
  带 addr = 按地址断开匹配链路。
- FR-H5: 动态 GATT 服务定义 CMD.gatt_define(0x4A)：host 定义任意服务形态
  （svc_uuid16 + 特征表 {uuid16, 属性位 read/write-wo-resp/write/notify,
  max_len}），固件运行时注册（CONFIG_BT_GATT_DYNAMIC_DB），值存固件 RAM
  （初始全零），对端写回存 + EVT.GATTX 回传，notify 特征自动挂 CCCD；响应携带
  服务基 handle；仅无外设连接时可重定义（否则 0x73），重复定义同 uuid16 =
  覆盖旧定义；上限 Kconfig（默认 4 服务 × 8 特征）；UUID16-only v1。

## 3. 验收门 (Gates)

| 门 | 内容 | 验证方式 |
|---|---|---|
| G1 | 桥连 suota_sim：自动应答外设主动 MTU_REQ，MTU≥247 协商成功 | HIL 日志 + bt monitor |
| G2 | suota_sim 全流程 OTA：合成镜像零损坏，吞吐 ≥ 参考链路实测 14.8KB/s | HIL 自动化测试 |
| G3 | host 离线测试全绿：帧模糊重同步、SUOTA 状态机、Fake 传输端到端 | pytest（无硬件） |
| G4 | daemon/CLI/MCP 命令语义对等旧版 | pytest 契约测试 |
| G5 | 真实 SUOTA 设备验证 | 经 `blebridge ota` 对任意 SUOTA 设备实测 |

## 4. 非功能需求

- 固件 BLE 数据面与 UART 事件面解耦：UART 流控异常不得阻塞射频收发。
- host 端 Python ≥3.9 兼容语法（venv 用 3.12）；类型注解 + mypy 通过。
- 全部第三方依赖锁版本；无硬件时整套 host 测试可跑（CI-ready）。
- 协议/常量单一定义源：`firmware/proto`（C 头）与 host `framing` 数值一致，以契约测试守护。
