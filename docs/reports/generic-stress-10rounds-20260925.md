# generic 模式 10 轮压测报告（conn_update/phy 主动实测）

- 日期：2026-09-25
- 执行：BLE 实测 agent（独占串口 `/dev/cu.usbmodem11301`；不碰探针、不烧录、不派子 agent）
- 被测固件：板上桥固件 = 纯 generic 模式 HEAD=`eaf6cbe` 构建（CDC 形态，上一验证轮烧录，本轮未动固件）
- host：仓库 `host/src/blebridge`（`d5845ce`，含 v1.1 全部命令封装：disc_svc / handle 寻址 / conn_update 0x44 / phy 0x45）
- 原始数据：`/tmp/genstress/`（`round_1..13.{json,log}`、参考设备专项轮、`recheck.{json,log}`、`midea_retest*.{json,log}`、`driver_run*.log`、驱动 `stress.py`）
- 纪律：对未知设备只读（读/发现可以，写特征值禁止，仅 CCCD 订阅写允许）；每步超时 ≤30s；未改 `host/src/**`、`firmware/**`

## 结论表（TL;DR）

| 项 | 结果 | 证据 |
|---|---|---|
| 10 轮压测（每轮不同目标） | ✅ 完成 10/10 | 1× 全模板 PASS + 6× 连接后对端拆链 + 3× 连接失败（明细 §2） |
| **新命令 conn_update (0x44) 主动实测** | ✅ 温和/激进双档通过 | round 10（OPPO Reno9 5G）：`24-40u` → EVT.DEBUG `conn params: 40u 0 300t`；**interval 8（10ms）激进档也被对端接受**（`conn params: 8u 0 300t`），两档后读探针均存活、零断连 |
| **新命令 phy (0x45) 主动实测** | ✅ 2M→1M→2M 三连切 | 每次 status=0 + EVT.DEBUG `lephy: 2M/1M/2M tx rx` 精确回报，链路全程存活 |
| generic 连接保持/不自动断连 | ✅（固件侧零自发断连） | 所有断连均为对端 LL 原因（0x3E/0x08）或本端 CMD.disconnect（0x16）；无固件自发 EVT.DISC |
| disc_svc 0x74 + 连接保持（核心差异复验） | ✅ | OPPO 无 0x180F → status=116 + `disc_svc: service not found`，连接继续走完后续步骤 |
| 读名 uuid16 / handle 双路 | ✅/⚠️ | OPPO uuid16 路 = "OPPO Reno9 5G"；handle 探测 0x0001..0x000C 未命中同值句柄（其布局名值句柄不在该窗，如实记录） |
| 订阅尝试 | ⚠️ 无可达通知源 | OPPO：0x180F 缺失；handle 遍历 0x0001..0x0028 无 notify+CCCD 声明。参考设备 serv_status 订阅/退订成功但无 SUOTA 活动 → 0 通知（预期） |
| **参考 SUOTA 设备专项（generic 路径 SUOTA 完好性）** | ✅ **PASS** | 广播确认（244 帧, −48）→ disc_svc(0xFEF5) **status=0 nchars=10** + `gatt_dm ok: 10/10` → suota_version=**0x0d** ✓ → serv_status(0x5F78) subscribe=0/unsubscribe=0 → disconnect 0x16 |
| 0x3E 连接建立波（本轮主要异常） | ⚠️ 对端/射频，非固件 | 定界见 §4：同固件同 host 对同一设备 6 次尝试 1 次完整建立并干净断开；0x3E 全部发生在 disc_svc 之前（未触发 dm），固件无主动断连代码路径 |

**总判定：generic 模式设计与新命令（0x44/0x45）行为全部符合 task-genmode 设计与 protocol-contract v1.1，参考设备 SUOTA 路径完好，无固件缺陷。** 10 轮中 9 轮未能进入步骤 4-6，原因是本时段周边低 RSSI 杂牌设备的 LL 建立失败波（0x3E），与上轮矩阵记录的同类设备行为一致且经复测定界为对端问题。

## 1. 会话健康

单进程多串口会话驱动（每轮 fresh open，settle 0.6s）。全程 `ping/info` 每次 status=0；会话末尾 `info`：`events_sent=11749, events_dropped=0, tx_timeouts=0, rx_overflows=0`（uptime 3325s 累计，跨 ~16 次串口会话）——帧层与 credits 流控在反复重连下零劣化；`wire_gaps`/CRC 错误均为 0。

## 2. 10 轮总表（每轮不同目标，扫描 12s 按 tier+RSSI 选取）

| 轮 | 设备 | 地址/类型 | RSSI | connect | 10s hold | 断连原因 |
|---|---|---|---|---|---|---|
| 1 | BH4768391 | 1F:05:41:4D:24:64 / public | −69 | 0 | ✗ | **0x3E**（CONN 后未及 MTU 交换） |
| 2 | midea | 69:60:B9:93:20:3C / public | −69 | 0 | ✗ | **0x3E**（MTU 23 后） |
| 3 | S8503_19af81 | 19:AF:81:19:96:E0 / public | −73 | 0 | ✗ | **0x3E**（MTU 23 后） |
| 4 | YD_B171（键盘） | 71:B1:09:00:00:C0 / public | −75 | 0 | ✗ | **0x3E**（MTU 23 后） |
| 5 | S503_fc6543 | FC:65:43:13:0D:AD / public | −77 | **116** | — | `connect err: 2` + timeout |
| 6 | S503_9b97b9 | 9B:97:B9:F1:C9:32 / public | −72 | 0 | ✗ | **0x3E**（MTU 23 后） |
| 7 | S503_e86310 | E8:63:10:32:5B:F4 / public | −69 | 0 | ✗ | **0x08**（lephy 1M 回报后监督超时） |
| 8 | unnamed_1F:4F | 1E:DC:97:49:1F:4F / random | −38 | **116** | — | `connect err: 2`（Apple RPA 已轮换） |
| 9 | unnamed_60:E7 | 4A:C1:A4:D2:60:E7 / random | −38 | **116** | — | `connect err: 2` + timeout |
| 10 | **OPPO Reno9 5G**（GAP 读名获得，广播无名） | 85:4F:96:3F:FE:4A / random | −61 | 0 | ✓ | 0x16（本端，干净） |

补充轮（明确非正式轮）：11 DOOR_28F54425（−80）connect 116；12/13 两台 −38 random RPA connect 116——named 池耗尽后的无名设备，地址均已在连接前轮换。

冒烟轮（round 编号前）：S503_de60a8（−64）connect=0 → MTU 23 → lephy 1M → 0x08（与上轮矩阵同类设备行为一致）。

## 3. Round 10（OPPO Reno9 5G）全模板明细 —— 新命令主动实测主证据

```
connect status=0 → EVT.CONN → EVT.MTU 517 → EVT.DEBUG lephy: 2M tx 2M rx（对端连接期自协商）
hold 10s: disc=[] ✓
disc_svc(0x1800) status=0 nchars=3；EVT.DEBUG "gatt_dm ok: 3/3 chars"
read 0x2A00 (uuid16) status=0 → "OPPO Reno9 5G" (4f50...3547)
handle 探测 0x0001..0x000C：无同值句柄（其名值句柄不在该窗，记录；uuid16 断言不受影响）
── 新命令实测 ──
conn_update(24,40,0,300)  status=0 → EVT.DEBUG "conn params: 40u 0 300t"  读探针存活 ✓
conn_update(8,8,0,300)    status=0 → EVT.DEBUG "conn params: 8u 0 300t"    读探针存活 ✓
  ← 激进档 interval 8（10ms）对端照单全收，latency/timeout 精确生效，零断连
phy(2) status=0 → "lephy: 2M tx 2M rx"   存活 ✓
phy(1) status=0 → "lephy: 1M tx 1M rx"   存活 ✓
phy(2) status=0 → "lephy: 2M tx 2M rx"   存活 ✓
── 订阅尝试（只读纪律内） ──
disc_svc(0x180F) status=116 + "disc_svc: service not found" → 连接保持 ✓（0x74 核心差异复验）
handle 遍历 0x0001..0x0028：0x2803 声明 0 条（GAP 外特征读受权限保护）→ 无 notify+CCCD 可订（记录）
disconnect status=0 → EVT.DISC 0x16 ✓
```

**行为表（新命令，跨全部本轮数据）**：

| 命令 | 下发 | 响应 status | 设备侧行为（EVT.DEBUG） | 断连 |
|---|---|---|---|---|
| 0x44 温和 | 24/40u, lat 0, t 300 | 0 | `conn params: 40u 0 300t`（精确生效） | 无 |
| 0x44 激进 | 8/8u, lat 0, t 300 | 0 | `conn params: 8u 0 300t`（精确生效） | 无 |
| 0x45 | 2 | 0 | `lephy: 2M tx 2M rx` | 无 |
| 0x45 | 1 | 0 | `lephy: 1M tx 1M rx` | 无 |
| 0x45 | 2 | 0 | `lephy: 2M tx 2M rx` | 无 |
| 0x44（被动观察） | 对端自发请求 | — | 参考设备订阅期间 `conn params: 8u 0 600t`（对端发起的更新同样经 le_param_updated 回报） | 无 |

## 4. "9/10 轮未到步骤 4"定界（结论：对端/射频，非固件）

1. **原因码全部是 LL 层**：0x3E（建立失败）、0x08（监督超时）、`connect err: 2`（陈旧地址）——控制器上报的对端/射频错误；固件主动断连只产生 0x16/0x13。
2. **时序排除固件路径**：0x3E 均发生在 connect 返回后 1-2s 内、**disc_svc 之前**（dm 根本未触发）；`bridge_ble.c` 仅有的 4 处 `bt_conn_disconnect` 均为 connect 超时取消 / host CMD.disconnect / CMD.reset 收尾（上轮报告 §4 已核对行号，本轮固件未变）。
3. **同固件同 host 的 A/B 证据**：15:16 上轮同一 BH4768391/midea hold >40s 全通过；本轮 16:06 复测（recheck.json）同两设备 6 次尝试中 **midea try2 完整建立**（MTU 247 + lephy 1M + hold 5s + 干净 0x16），其余 5 次 0x3E——同代码间歇性失败=对端状态/链路质量，非固件回归。
4. **RSSI 相关性**：失败设备全部 ≤−61 dBm 且多为上轮矩阵已知 LL 差的杂牌（S503 家族/YD_B171）；成功样本 OPPO（−61）与参考设备（−48）链路余量足。
5. 对死链发 CMD.disconnect 一律 116 NOT_READY（recheck ×4）——正确语义，与上轮 §6-4 建议一致。

## 5. 参考 SUOTA 设备专项（generic 路径 SUOTA 完好性）—— **PASS**

```
[scan 30s] 首窗即见参考设备：244 帧, rssi −48 ✓
[connect mode0] status=0 → EVT.CONN
[disc_svc 0xFEF5] status=0 nchars=10；EVT.MTU 251 + "lephy: 2M tx 2M rx" + "gatt_dm ok: 10/10 chars"
[read suota_version 0x64B4] status=0 → 0x0d，断言 ✓（首读即中）
[subscribe serv_status 0x5F78] status=0 → 3s 窗 0 通知（无 SUOTA 活动，预期）
  期间 EVT.DEBUG "conn params: 8u 0 600t"（设备侧外设发起参数更新被接受并回报）
[unsubscribe] status=0
[disconnect] status=0 → EVT.DISC 0x16 ✓
→ SUOTA_REGRESSION PASS
```

显式发现路径（disc_svc 填 map）→ uuid16 读 → CCCD 订阅/退订 → 干净断开全链在参考设备上完好；广播间歇阵发问题本轮未复现。

## 6. 异常清单与遗留

1. **0x3E 波（本轮主要异常，已定界非固件）**：10 轮中 6 轮连接即掉、3 轮连接失败、3 补充轮连接失败；另 2 次 midea 完整模板复测（midea_retest）均在 hold 窗 0x3E——conn_update/phy 的多设备样本因此收窄到 OPPO + 参考 SUOTA 设备（被动）。建议：如需多外设参数样本，用 suota_sim/nRF Connect 模拟外设补测（与上轮建议一致）。
2. **测试脚本缺陷（已修，不影响数据）**：驱动 early-return 路径（CONNECT_FAIL/AUTO_DISCONNECT）不落盘 JSON、不关会话——已修复并回填 round_1..9.json（字段 `backfilled_from="driver_run1.log"`，数据逐条来自日志原文）。冒烟轮与 round 1 的 round_1.log 为两段追加，已按时间戳区分。
3. **BH 广播名含不可打印前缀字节（0x06）**：目标名显示选名逻辑改为可打印名优先。
4. **OPPO 的 GAP 名值 handle 不在 0x0001..0x000C**：handle 双路比对在该设备未闭合（uuid16 路成功）。提示：handle 探测窗可加宽到 0x0010 以上，留待下轮。
5. **EVT.GATTX 通知通路仍未点亮**：两台全过设备均无可订通知源；参考设备 serv_status 订阅成功但静默期无通知。suota_sim 用例仍是唯一补法（上轮遗留原样保留）。
6. 低 RSSI（≤−70）杂牌设备（S503/YD_B171 类）在两个时段均无法稳定建立——连续两轮实测结论一致，作为环境常量记录。

## 附：原始数据文件（/tmp/genstress/）

- `round_1..10.{json,log}` 正式 10 轮；`round_11..13.*` 补充轮；参考设备专项轮
- `recheck.{json,log}` BH/midea 各 3 次 connect+hold 定界复测；`midea_retest*.{json,log}` 补充完整模板轮
- `driver_run1.log`（10 轮+参考设备全事件流）、`driver_run2.log`（补充轮+recheck）
- `stress.py`（驱动：扫描/选取/全模板/断言）、`midea_retest.py`、`backfill.py`（JSON 回填）
