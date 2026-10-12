# 跨端协议契约 (protocol-contract) —— blebridge（nRF52840 BLE 全透传双角色工具）

状态：v1.4.2（2026-10-12，**地址字节序正名**：全部含地址的字段（CMD.connect/0x05、CMD.disconnect/0x06 的载荷与成功 body、EVT.SCAN/0x20、EVT.CONN/0x21）统一为**显示序**（人/手机从左到右阅读序，如 "AA:BB:CC:DD:EE:FF" → 线上字节 AA BB CC DD EE FF）；固件在边界处与 Zephyr 小端 `bt_addr.val` 互转。**勘误**：v1.4.1 及之前文档虽写"显示序"，固件实际按 val 线序收发且 host 不翻转——本次起文档与实现一致。host `parse_scan_payload` 输出与 `connect_payload` 输入同步改为显示序，跨端必须同仓同步升级。上一版 v1.4.1（2026-10-12，E2-T 诊断字段：EVT.SCAN 在 rssi 之后、adv TLV 之前插入 1B adv_type（透传 HCI LE adv report Event Type：0=可连接未定向 2=可扫描 3=不可连接 4=scan rsp），扫描上报自此可区分可连/不可连广播，connect 决策由 host 依据该字段做出，见 §2。上一版 v1.4.0（2026-10-10，task-dfu-a 自 OTA 基础设施：新增 CMD 0x4B enter_dfu（GPREGRET 软进 MCUboot 串口恢复），见 §1；升级路径的 SMP 通道走第二个 CDC 口（MCUmgr，`zephyr,uart-mcumgr`），不属于本桥协议，不占桥协议 CMD 空间。上一版 v1.3.1（2026-09-24，FR-H 外设模式 + fix round 1）见 git 历史）。）

> 线上身份约定：广播名（CMD.adv_start scan response 本地名）、ping 串 "blebridge<version>"、info JSON version 前缀、USB product 字符串均为 "blebridge"；预设特征 0xFFE2 默认可读值为 ASCII "bridge"。
本文件是 nRF52840 桥固件与 host（daemon/CLI/MCP）之间**串行线上
协议**的单一契约索引；改任何一端前先对本文件登记的格式与权威实现。权威裁定：
**host `host/src/blebridge/bridge.py` 为 CMD/EVT 数值的权威**（固件
`firmware/app/src/bridge_dispatch.h` 与其逐值镜像，由 host/tests/test_proto_c.py
一族契约测试守护帧层）。

**v1.1 语义裁定（controller 2026-09-24）**：固件不再含任何 SUOTA 专用行为——
连接后**不自动做服务发现**、**任何情况下不主动断连**（发现失败/服务不存在
只发 EVT.DEBUG，连接保持）。服务发现由 host 经 CMD.disc_svc 按需触发；SUOTA
成为 **host 驱动的一种应用流程**（`blebridge/suota.py`），与其它 GATT 应用在
固件视角完全等价。

- 帧层（SOF/LEN/CRC/重同步）规范：`docs/adr/ADR-002-uart-bridge-protocol.md`
  与 `firmware/proto/bridge_proto.h` / `host/src/blebridge/framing.py`。
- 本文件只管**语义层**：命令表、事件表、credits、GATT 编址、开销、保留值。

## 1. CMD 表（请求命令，权威：host/src/blebridge/bridge.py `CMD`）

响应帧 CMD = 请求 CMD | 0x80，SEQ 与请求一致；响应载荷首字节为 status
（0x00 = 成功，非 0 原样回传固件状态码），其余为 body。

| 值    | 名称        | 请求载荷                                                                 | 固件实现指针 |
|------|-------------|--------------------------------------------------------------------------|--------------|
| 0x01 | ping        | 空；body 空                                                              | bridge_dispatch.c |
| 0x02 | info        | 空；body = 紧凑 JSON，键集：`version` / `uptime_s` / `events_sent` / `events_dropped` / `tx_timeouts`（TX 写超时静默丢帧数，C-1） / `rx_overflows`（RX 环满丢弃字节数，C-3） / `credits` / `current_log_level` / `phy`；host 侧按不透明字节透传，新增键向后兼容 | bridge_info.c |
| 0x03 | scan_start  | u16 LE duration_ms（0 = 固件默认 8000ms）                                  | bridge_ble.c `api_scan_start` |
| 0x04 | scan_stop   | 空（幂等）                                                               | bridge_ble.c |
| 0x05 | connect     | 1B mode（0 public 地址 / 1 random 地址 / 2 广播名）+ mode 0/1: 6B 地址（**显示序**，"AA:BB:.." → AA BB ..；**v1.4.2 起固件实际执行**：内部翻转为 Zephyr val，见状态行勘误）+ mode 2: UTF-8 名；成功 body = 1B addr_type + 6B 地址（**v1.4.2 起为显示序**） | bridge_ble.c `api_connect` |
| 0x06 | disconnect  | **v1.3 FR-H 扩展**：空载荷 = **外设链路优先**断开（无外设链路时回退 central 链路，即旧语义）；`1B addr_type + 6B 地址（显示序；**v1.4.2 起固件按显示序匹配**）` = 按地址断开匹配的链路（外设或 central）。其余载荷 = 0x72。异步：响应仅 status，EVT.DISC（带角色字节）随后 | bridge_ble.c `api_disconnect` |
| 0x07 | mtu         | 空；body 空；协商结果经 EVT.MTU 事件回传                                   | bridge_ble.c |
| 0x08 | gatt_read   | **uuid16 寻址**：u16 LE uuid16（见 §4 编址）；**handle 寻址**：u16 LE 0x0000 + u16 LE 原始 handle；body = 读回值 | bridge_ble.c `api_gatt_read` / `api_gatt_read_handle` |
| 0x09 | gatt_write  | 1B write_type（0 带响应 / 1 无响应）+ **uuid16 寻址**：u16 LE uuid16 + 值；**handle 寻址**：u16 LE 0x0000 + 1B handle_type（0 值 handle / 1 descriptor handle，当前实现同为裸 ATT 写）+ u16 LE handle + 值 | bridge_ble.c `api_gatt_write` / `api_gatt_write_handle` |
| 0x0A | subscribe   | **uuid16 寻址**：u16 LE uuid16（固件写该特征 0x2902 CCCD）；**handle 寻址**：u16 LE 0x0000 + u16 LE value_handle + u16 LE cccd_handle（CCCD 固定写通知值 0x0001；无 map 也可用）。EVT.GATTX 源字段：uuid 模式 = uuid16，handle 模式 = value_handle | bridge_ble.c `api_subscribe` / `api_subscribe_handle` |
| 0x0B | unsubscribe | **uuid16 寻址**：u16 LE uuid16；**handle 寻址**：u16 LE 0x0000 + u16 LE value_handle（只撤销 handle 模式订阅）。幂等 | bridge_ble.c |
| 0x0C | disc_svc    | **host 触发发现（通用桥核心）**：u16 LE svc_uuid16（0x0000 = 重发现上一成功服务）或空载荷（同 0x0000 语义）。固件**立即**对该 16 位服务 UUID 跑 bt_gatt_dm，把发现的**全部特征**并入 GATT map（128 位 UUID 取 SIG 前缀，见 §4），**绝不因结果断连**。成功 body = 本次发现特征数（u16 LE）；服务不存在/失败 = status 0x74 + EVT.DEBUG | bridge_ble.c `api_disc_svc` |
| 0x0D | reset       | 空；固件软复位（先尽力断开 BLE）                                           | main.c / bridge_link.c |
| 0x0E | **RESERVED**| **禁用，永不指派**（原因见 §7 末）                                          | — |
| 0x0F | log_level   | 1B level（0..4；EVT.DEBUG 门控）                                           | bridge_dispatch.c |
| 0x40 | suota_start | host 侧占位：SUOTA 是 **host 驱动**的状态机（blebridge/suota.py），经 gatt_* 透传执行；固件对 0x40 应答 `ERR_NOT_READY` 桩（bridge_dispatch.c `is_ble_placeholder`） | — |
| 0x41 | suota_abort | 同上（host 侧 = MEM_DEV 0xFF000000 写，经 0x09 透传）                        | — |
| 0x42 | suota_status| 同上（host 侧 = 读 SERV_STATUS，经 0x08 透传）                               | — |
| 0x43 | pair        | 空；**阻塞命令**：固件对当前连接发起 SMP 配对（L2 Just Works，NoInputNoOutput）并等配对结算（超时 `BRIDGE_PAIR_TIMEOUT_MS`，默认 8 s）。成功 body = `[加密态 u8（1=bt_conn_enc_key_size>0）][sec_level u8（bt_conn_get_security）]`；失败 status `0x75`。已 ≥L2 的链路幂等应答当前状态 | bridge_ble.c `api_pair` |
| 0x44 | conn_update | 4 × u16 LE：interval_min、interval_max（单位 1.25 ms）+ latency + supervision timeout（单位 10 ms）。**异步**：响应仅确认提交（bt_conn_le_param_update 的errno，非 0 原样回传）；最终参数经 EVT.DEBUG（`conn params: …`）回报 | bridge_ble.c `api_conn_update` |
| 0x45 | phy         | 1B phy 偏好（1 = 1M，2 = 2M，收发同向）。**异步**：结果经 EVT.DEBUG（`phy: …`）回报；非 1/2 = status 0x72 | bridge_ble.c `api_phy_update` |
| 0x46 | adv_start   | **FR-H 外设模式**：u16 LE interval_ms（单位 ms；`0xFFFF` = 固件默认 `CONFIG_BRIDGE_ADV_INTERVAL_DEFAULT_MS`）+ 1B 名字开关（1 = scan response 带完整本地名 "blebridge"）。**宽容形态**：空载荷 = 双默认；仅 2B = interval + 名字开。固件启动**可连接广播**（adv 包含 0xFFE0 服务 UUID16 列表 + flags）；**已广播 = 幂等 OK**（`-EALREADY` → 0x00；参数变更须先 adv_stop）。广播状态经 EVT.DEBUG（`adv: on/off/already …`）回报。**v1.3.1 fix r1**：adv_start 设置"广播意图"；`CONFIG_BRIDGE_ADV_AUTO_RESTART`（默认 y）下外设链路断开（含对端崩溃断链）自动按最近一次成功配置恢复广播（EVT.DEBUG `adv: restarted after link loss`） | bridge_periph.c `bridge_periph_adv_start` |
| 0x47 | adv_stop    | **FR-H**：空载荷；停止广播，未广播 = 幂等 OK。**v1.3.1 fix r1**：显式 adv_stop 清除广播意图（此后断链不再自动恢复——区分"用户要停"与"链路断了要恢复"） | bridge_periph.c |
| 0x48 | notify      | **FR-H**：`0x00 + 数据` = 预设 notify 特征（0xFFE2）；`0x01 + u16 LE char_uuid16 + 数据` = 在本地服务（动态服务优先、其次预设服务）中按 uuid16 寻址。数据成为特征**可读值**并以 notify 推给所有已订阅对端（≤ 本地 ATT MTU−3 = 244）。**无已订阅对端 = 0x74**；特征不存在/不能 notify/超长 = 0x72；TX buf 耗尽 = 0x73 | bridge_periph.c |
| 0x49 | set_char    | **FR-H**：寻址同 0x48（默认目标 = 预设写特征 0xFFE1）；只更新本地特征当前值（对端读回），**不上空口**。未知特征/超长 = 0x72 | bridge_periph.c |
| 0x4A | gatt_define | **FR-H5 动态 GATT 服务定义**：`u16 LE svc_uuid16 + 1B n_chars + n × { u16 LE char_uuid16 + 1B props + u16 LE max_len }`；props 位：bit0 read / bit1 write-without-response / bit2 write-with-response / bit3 notify。成功 body = **服务基 handle（u16 LE）**（服务声明 handle；后续属性 handle 按 §4.2 布局推算）。约束：n 1..8、max_len 1..244、服务内 uuid16 重复 = 0x72；值字节总和 ≤ 每服务池（默认 1024B）→ 0x73；**外设链路存活时 = 0x73**（对端不能看着 DB 变）；同 svc_uuid16 重复定义 = 覆盖旧定义（先 unregister，handle 移到 DB 尾部，**旧 handle 作废**）；槽位 `CONFIG_BRIDGE_GATT_DYN_SVCS`（默认 4）满 = 0x73。值存固件 RAM（初始全零）；对端写回存 + EVT.GATTX（subtype 0x01）回传 host；notify 特征自动挂 CCCD。UUID16-only（v1） | bridge_periph.c `bridge_periph_gatt_define` |
| 0x4B | enter_dfu | **task-dfu-a 自 OTA 入口**：空载荷。固件先尽力回 ACK（flush 预算 10 ms）→ 断开 BLE（同 CMD.reset 的 teardown hook）→ 经 retention boot-mode 子系统把 **GPREGRET**（板级 `boot_mode0` 节点，`zephyr,boot-mode`）写为 `BOOT_MODE_TYPE_BOOTLOADER`（0x01）→ `sys_reboot(COLD)`。**效果**：MCUboot 检测到 boot mode 后清除标记并停在**串口恢复控制台**（自带 USB CDC，默认 VID:PID 1915:**8523**，与桥 CDC 区分）；桥协议离线，直到下一次正常复位。host 从该口可用 smpclient / `nrfutil device` 兜底重刷镜像。不变量保持：0x4B\|0x80=0xCB ∉ EVENT_CMDS | bridge_dispatch.c（case ENTER_DFU）/ bridge_link.c（bootmode_set+reboot） |
| 0x50 | credits     | u16 LE 授予额度（**host → 固件单向**，见 §3）                               | bridge_dispatch.c `grant_credits` |

响应 status 附加码（权威枚举：`firmware/proto/bridge_proto.h` `bridge_status_t`）：
`0x71` UNKNOWN_CMD / `0x72` BAD_PAYLOAD / `0x73` BUSY / `0x74` NOT_READY /
`0x75` **PAIR_FAILED**（FR-B5：SMP 配对失败或超时，CMD.pair 专用）。

不变量（Task 4 裁定）：任何 CMD 值须满足 `(cmd | 0x80) ∉ EVENT_CMDS`——
响应帧永不与事件帧混淆。

## 2. EVT 表（事件帧，权威：bridge.py `EVT`；载荷格式权威：firmware/app/src/bridge_ble_pure.h）

事件帧使用独立 CMD 空间，wire SEQ 为固件侧 0..255 事件环号（host 侧另行编号
app_seq，SEQ 跳变 = 设备侧丢事件，host 注入本地 EVT.DEBUG/0xFF）。下列载荷
均指**剥去 subtype 字节后**的 ring 载荷（host 侧 `Event.payload`）：

| 值    | 名称 | 线上载荷（subtype 之后的语义） | 实现指针 |
|------|------|-------------------------------|----------|
| 0x20 | SCAN  | `[0x00 subtype][1B addr_type][6B addr 显示序][1B rssi i8][1B adv_type][adv 原始 TLV...]`。**v1.4.1（E2-T）**：新增 1B adv_type，取 Zephyr `bt_le_scan_recv_info.adv_type`（= HCI LE adv report Event Type 原值透传：0=可连接未定向 ADV_IND，2=可扫描 ADV_SCAN_IND，3=不可连接 ADV_NONCONN_IND，4=scan response），host 负责解读——connect 只应对可连广播（0）发起；固件与 host 同仓同步升级，无新旧帧混布场景。**v1.4.2**：addr 字节序自此为真显示序（此前实现误为 Zephyr val 线序，见状态行勘误） | bridge_ble_pure.c `bridge_ble_evt_scan_build`；host 解码 `daemon.parse_scan_payload` |
| 0x21 | CONN  | `[0x00][1B role][1B addr_type][6B addr 显示序（v1.4.2）]`。**v1.3 FR-H 角色字节裁定**：role 0 = central 角色（host 经 CMD.connect 发起的链路），role 1 = peripheral 角色（广播期间被对端连入的链路）——**选择加字段**而非"host 按上下文区分"：双角色并存（BT_MAX_CONN=2）时两个异步 CONN/DISC 无法靠上下文消歧，1B role 使事件自描述；host 对 CONN/DISC 载荷本就不透明消费（hex 透传），无破坏 | `bridge_ble_evt_conn_build`；host `bridge.ROLE_CENTRAL/ROLE_PERIPHERAL` |
| 0x22 | DISC  | `[0x00][1B role][1B reason HCI]`（role 同 CONN） | `bridge_ble_evt_disc_build` |
| 0x23 | GATTX | subtype 空间（v1.3 登记，见 §7）：`0x00` = notification，`[0x00][u16 LE uuid16][notify 值...]`（central 角色对端 notify，brief 钉死不变）；`0x01` = **FR-H 外设角色写事件**，`[0x01][u16 LE uuid16][u16 LE value handle][数据...]`（对端对本地服务特征的写，subtype 即角色+类型标记；uuid16 为跨端稳定标识，value handle 为本次定义时的栈分配值）。host 消费 `suota._serv_status_of`（0x00）与 `daemon.parse_gattx_payload`（0x00/0x01） | `bridge_ble_evt_gattx_build` / `bridge_ble_evt_gattx_write_build` |
| 0x24 | MTU   | `[0x00][u16 LE mtu]` | `bridge_ble_evt_mtu_build` |
| 0x25 | SUOTA | **固件侧未启用**（保留）；现网该通道上的事件均为 **host 本地注入**（`bridge.inject_local_event`，不走串口），子类型空间见 §7 | host `daemon._suota_start` / `mcp_server.tool_suota_start` |
| 0x8E | DEBUG | `[1B tag_len][tag][msg]`（文本诊断通道；受 CMD.log_level 门控）。host 本地注入 subtype 0xFF = wire_seq_gap 报告（`bridge._check_wire_seq`） | `bridge_dispatch.c bridge_evt_debug_build` |

## 3. 事件 credits 流控（FR-A3，host 为授予方）

权威实现：host `bridge.py`（`BridgeHost` 构造参数与 `_consume_credit`）、固件
`bridge_dispatch.c`（`bridge_evt_admit` CAS 计数器）。契约：

- 载荷：u16 LE 授予额度（CMD.credits 0x50，host → 固件单向）。
- 初始额度：**128**（host `credit_grant=128`；固件 `CONFIG_BRIDGE_CREDITS_INIT=128`）。
- 消耗：每收到**一帧线上事件**消耗 1 credit（host 本地注入事件不消耗）。
- 阈值：余额 < **32** 时 host 自动续授 128（`credit_threshold=32`）。
- 额度耗尽：固件**丢弃事件并保留环号递增**（SEQ 跳变即丢事件的可观测痕迹），
  绝不阻塞 BLE 数据面；额度上限 0xFFFF 饱和。

## 4. GATT 编址（write_type + uuid16 LE + 原始 handle）

权威实现：host `suota.py`（模块 docstring + `uuid16_prefix` / `uuid16_le`）与
`bridge.py` 模块级编码器（`gatt_read_payload` 等），固件 `bridge_ble.c
dm_completed_cb` + `bridge_ble_pure.c bridge_ble_uuid128_prefix`
（host/tests/test_ble_pure_map.py 跨语言钉死）。

- 请求编址：CMD.gatt_write 首字节 write_type（0 带响应 / 1 无响应，FR-C2
  严格区分），随后 u16 LE uuid16；gatt_read / subscribe / unsubscribe 无
  write_type 字节，直接 u16 LE uuid16。
- **handle 寻址（v1.1 新增，向后兼容）**：uuid16 字段 == 0x0000 时进入
  handle 寻址——uuid 模式的 uuid16 键恰无 0 值用户特征，0x0000 作哨兵安全：
  - gatt_read：`0000 + handle(2B LE)`；
  - gatt_write：`write_type(1B) + 0000 + handle_type(1B) + handle(2B LE) + 值`
    （handle_type 0 = 特征值 handle，1 = descriptor handle，现实现均为裸
    ATT 写，字节留作语义标注）；
  - subscribe：`0000 + value_handle(2B LE) + cccd_handle(2B LE)`，CCCD 固定
    写通知值 0x0001（无属性可查，不自动选 indicate）；
  - unsubscribe：`0000 + value_handle(2B LE)`（只匹配 handle 模式订阅槽）。
  handle 寻址**不依赖发现 map**，连接即可用。
- **GATT map（uuid16 → handle 的固件内缓存）语义（v1.1）**：无连接期自动
  发现；host 每次 CMD.disc_svc 触发一次 dm，命中的特征**逐次累积**进 map
  （键重复保首条 + EVT.DEBUG 告警，断连清空；容量
  `BRIDGE_GATT_CHAR_MAX`，默认 32）。每条目 = uuid16 键 + value_handle +
  CCCD handle（该特征的 0x2902 descriptor 并入条目，其余 descriptor 与服务
  本身不入 map——用 handle 寻址可达）+ properties。uuid 寻址的读/写/订阅
  在无 map 时应答 0x74。
- **uuid16 语义**（本节为 task(12) C-1 修复的裁定记录）：
  - 16 位（SIG base）特征：key = 该 16 位值本身（固件 `bt_uuid_16.val`）。
  - 128 位特征：key = 可读形式（如 `8082caa8-…`）**首 4 个十六进制字符**
    （= `int(uuid[:4], 16)`，即 host `uuid16_prefix`）。对 SUOTA 全部 10 特征
    即 0x8082 / 0x7242 / 0x6C53 / 0x9D84 / 0x4578 / 0x5F78 / 0x64B4 / 0x42C3 /
    0xB7DE / 0x61C8。
  - **字节序验证记录**：Zephyr `bt_uuid_128.val` 是线上小端数组
    （`BT_UUID_128_ENCODE(w32,w1,w2,w3,w48)` = LE48(w48)…LE32(w32)），可读形式
    首 16 位落在 `val[14]（LSB）| val[15]<<8（MSB）`——不是 val[12]/val[13]
    （那是可读形式第 3-4 字节，如 mem_dev 会得 0xCAA8）。固件以
    `bridge_ble_uuid128_prefix` 提取后，与 host 发送的同值 u16 LE 线上字节
    逐字节一致。10 特征 key 互异、无坍缩由 test_ble_pure_map.py 断言。
  - 事件方向同契约：EVT.GATTX 源特征字段 = 同一 u16 LE（host 用
    `uuid16_prefix` 比对）。
- 无响应写长度上限：协商 MTU − 3（固件 `api_gatt_write` 校验，分块是 host
  suota 层职责）。

### 4.1 本地 GATT 服务（FR-H 外设模式，本桥作为 GATT server）

固件侧被服务的特征（CMD.notify / CMD.set_char 的寻址目标）：

- **预设测试服务（编译期注册，"开箱即用"）**：
  - `0xFFE0` primary service；
  - `0xFFE1` 特征 A：Read + Write-Without-Response——对端写 → 固件存值 +
    EVT.GATTX（subtype 0x01）回传 host；CMD.set_char 默认目标；
  - `0xFFE2` 特征 B：Read + Notify（CCCD）——CMD.notify 默认目标
    （默认可读值 = ASCII `"bridge"`）。
  - 明文权限（无 bond/pairing 要求；`CONFIG_BT_SETTINGS=n` 维持）。
- **动态服务（CMD.gatt_define，运行时注册）**：属性布局（决定 handle 推算）：
  `[0] 服务声明` + 每特征 `[声明][值]`（notify 特征再加 `[CCCD]`）。
  响应携带**基 handle**（服务声明 handle），host 推算：第 i 特征的
  值 handle = 按布局逐项 +1 累进（声明 +1、值 +1、notify 的 CCCD 再 +1），
  EVT.GATTX（0x01）的 value handle 字段即该值；**重复定义覆盖后全部 handle
  变化（DB 尾部重新分配），host 不得缓存跨定义使用**。
- **CMD.notify / CMD.set_char 寻址**：`0x00` = 预设特征（set_char→0xFFE1、
  notify→0xFFE2）；`0x01 + uuid16` = 在**动态服务（先、新定义优先）**中查，
  再回退预设服务。动态服务可遮蔽 0xFFE1/0xFFE2（有意允许，新定义优先）。
- 广播内容（CMD.adv_start）：flags（GENERAL+NoBR/EDR）+ 16 位服务 UUID 列表
  `0xFFE0`（adv 包）+ 完整本地名（scan response，可用名字开关关闭）。
- **真笔 MEM_DEV 加密要求（FR-B5 实证 2026-09-25）**：明文链路上读/订阅全通，
  但 MEM_DEV 带响应写回 **ATT 0x0F（Insufficient Encryption）**；host
  `suota.py` 对 0x0F 自动走 CMD.pair → 单次重试，仍 0x0F 抛
  `EncryptionRequired`（与 SAME_IMAGE 0x15=21 区分：0x0F=15）。

## 5. 帧开销

权威实现：`firmware/proto/bridge_proto.c` / `host/src/blebridge/framing.py`
（FRAME_OVERHEAD）。

- 一帧 = SOF(1B, 0x55) + LEN(2B LE) + CMD(1B) + SEQ(1B) + PAYLOAD(0..512B)
  + CRC16(2B LE, CCITT-FALSE，覆盖 CMD..PAYLOAD)，**固定开销 7 字节**。
- 应用层含义：247 MTU 链路上 SUOTA chunk 244B ⇒ 线上有效载荷率
  244/(7+244)；512B 载荷上限对应最长帧 519B。
- 请求/响应/事件帧同构；逐字节流式解析 + CRC 失败滑动重同步（FR-A4）。

## 6. 回帧时序与窗口流控（R-1，v1.2 新增；权威：suota.py `DEFAULT_PIPELINE_WINDOW` + bridge.py `request_noreply`/`join_noreply`）

**完成语义（跨层契约核心条款）**：桥对 write-without-response 片写
（CMD.gatt_write，write_type=1）的响应帧，在 `bt_gatt_write_without_response`
**调用实际完成后才回**（`bridge_ble.c api_gatt_write`：err==0 → status OK；
非 0 → status = `(uint8_t)(-err)`，如 ACL TX 缓冲耗尽 ENOBUFS=105）。
"完成" = 该片已进入 BT 栈/ACL 发送队列（或已在桥上失败），**不代表笔已收到**
——write-without-response 无 ATT ACK，笔侧数据完整性边界仍是块尾
SERV_STATUS notify（0x02，见 §2 EVT.GATTX）。

**窗口保护以此完成语义为正确性前提**：host 流水化窗口
（`SuotaSession.run(pipeline_window=…)`，默认 `DEFAULT_PIPELINE_WINDOW = 3`）
在窗口末用 `BridgeHost.join_noreply` 等到最老在飞片的响应帧——响应帧到达即
"该片已离开桥的 BLE 写路径"，因此两个同步点之间 host 至多再写
3×254B = 762B < 桥 CDC RX 环（`bridge_phy.c RX_RING_SIZE=1024`，ISR 放不下
即静默丢字节，仅 `info.rx_overflows` 计数），且在飞未确认 ACL TX buf ≤ 3
（池 `CONFIG_BT_BUF_ACL_TX_COUNT=10`）。

**变更约束**：若固件改为**先回帧后执行 BLE 写**，上述保护**静默失效**（窗口
不再对应桥的真实消化进度）——任何此类时序变更必须同步本节与
`host/src/blebridge/suota.py` 的 `DEFAULT_PIPELINE_WINDOW`（及固件 RX 环扩容后
对 `pipeline_window=None` 全速形态的重新评估）。无界突发（9×254B=2286B >
1024B）已被实机证伪（2026-09-25 首跑：片帧丢失 → 块 ACK 永不到达 → 30s
TIMEOUT；fix round 1 引入窗口）。

**orphan / join_noreply 语义登记**：fire-and-forget 片写
（`bridge.request_noreply`）的迟到响应帧按 **orphan** 消费——按 SEQ 识别且
仅作用于响应分支（`cmd|0x80`；事件帧 bit7=0，无论 wire SEQ 为何永不进入
orphan 路径），status 字节记录待查，帧不入响应匹配表（SEQ 回收时清除标记，
永不污染后续 request）。`orphan_responses` / `orphan_errors` 计数所有/非零
verdict 孤儿。`join_noreply(seq)` 返回该片的桥级 verdict：非 0 = 片写失败，
host 在窗口末立即上抛（`SuotaError(status,'block')`，不 ABORT）；超时
（BridgeTimeout）= 片帧在 host↔桥之间丢失，host best-effort ABORT 后快速
失败；"SEQ 不在飞"（已 join / SEQ 已回收）是 host 簿记违例，抛
RuntimeError（消息带 SEQ），调用方不得翻译为 ack timeout（M-2）。

**与 MEM_DEV 解锁（0x78）的交互**：订阅被拒 0x78 后的单次 MEM_DEV 事务解锁
探针（`suota._mem_dev_unlock`）只在**任何 chunk 发出之前**执行（subscribe
阶段），此刻流水窗口结构性为空——探针与在飞片写无并发，其带响应写不受窗口
影响。join 超时路径的 ABORT（MEM_DEV 0xFF000000）可与其余在飞片的迟到
verdict 并发：后者按 orphan 记账，不占用响应匹配表。

## 7. 保留值与登记约定

- **EVT.GATTX (0x23) 子类型空间**（v1.3 FR-H 登记）：
  - `0x00` notification：`[u16 LE uuid16][数据]`（central 角色对端 notify，
    brief 钉死，**永不改义**）；
  - `0x01` peripheral-role write：`[u16 LE uuid16][u16 LE value handle][数据]`
    （FR-H 外设角色写事件，`bridge.GATTX_PERIPH_WRITE`）；
  - `0x02..0xFE`：未分配（固件启用时在此登记 + host 消费端同步）；
  - `0xFF`：保留（与 wire_seq_gap 语义空间一致，永不指派）。
- **EVT.SUOTA (0x25) 子类型空间划分**（裁定：固件启用固件侧 SUOTA 事件前，
  必须先在本节登记，防止与 host 本地注入撞码）：
  - `0x00` SUOTA_PROGRESS：`[u16 block][u16 total_blocks][u32 bytes]`（LE，
    host 本地注入；daemon.py `SUOTA_PROGRESS`）
  - `0x01` SUOTA_DONE：`[u32 bytes_written][f32 elapsed]`（LE；`SUOTA_DONE`）
  - `0x02` SUOTA_FAILED：ASCII `"<status_code>:<phase>"`（`SUOTA_FAILED`）
  - `0x03..0xFE`：未分配（固件启用时在此登记 + host 三处消费端同步）
  - `0xFF`：保留（与 EVT.DEBUG 的 wire_seq_gap 子类型同一语义空间，永不
    指派给其他含义）
- **CMD 0x0E 保留原因**：其响应字节 `cmd|0x80 = 0x8E` 与 EVT.DEBUG 撞码
  （log_level 曾占用 0x0E，后迁至 0x0F）；任何新命令不得使用 0x0E。
- **flash 命令裁定**（FR-D3 "flash_firmware" 语义备案）：本项目没有也不需要
  独立的 `flash*` 桥命令。两种"刷固件"的等价映射：
  1. 给 **nRF52840 桥自身**刷固件 = `tools/flash.sh`（JLink/west，串口协议之外）；
  2. 对**笔**的 "flash" = SUOTA 升级 = host `suota_start`（daemon/MCP 命令族
     + suota.py 状态机，经 GATT 透传执行；桥上 0x40-0x42 为 NOT_READY 桩，
     见 §1）。
- 新增命令/事件/子类型的工作流：改 bridge.py 权威表 → 固件
  bridge_dispatch.h 镜像 → 更新本文件 → 补 test_proto_c / test_daemon 契约
  测试，四者一个提交。
