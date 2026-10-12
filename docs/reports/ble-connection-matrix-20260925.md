# BLE 随机设备连接实测矩阵（bring-up 后首次真机 central 验证）

- 日期：2026-09-25
- 执行：HW bring-up agent（任务 B）
- 链路：WCH-Link CDC 桥板上 UART 口，**115200 / rtscts=False**
（bring-up 实测欠账，任务 A 已把 `--baud/--no-rtscts` 暴露进 CLI/daemon，commit `1639026`）
- 固件：build-t10-uart-custom 115200 变体（uart0 PHY + 自定义引脚），桥固件版本 `blebridge-0.1.0`，BLE central（SDC）bt_enable 正常
- 边界：全程只读（无 GATT write / 无 CCCD 订阅 / 无配对）；未触碰 pyocd/探针；未改 firmware/**、tools/**
- 原始数据：`/tmp/bt_matrix/`（smoke.json / scan_devices.json / matrix.json / matrix_random.json / mtu_pass.json / diag_*.py 等）

## 结论表（TL;DR）

| 能力 | 结果 | 证据 |
|---|---|---|
| 扫描 | ✅ | 3×10s 窗口 639 条广播 / 18 台设备 / 11 public + 7 random；active scan，SCAN_RSP 名称可见；零丢失 |
| 连接 | ✅ | 有名设备 5/5 连上（38–1568ms）；random 地址模式 1 台连上；RPA 陈旧设备 2 台 5s 超时（预期类失败） |
| MTU 协商 | ✅（两台达成 ≥247） | BH4768391=**256**、S8503_19af81=**247**；另 3 台停 23（链路早断所致，见异常 3） |
| 服务发现 | ⚠️ 设计内受限 | `disc_svc` 一律 `0x74 NOT_READY`：固件连接后**只自动发现 0xFEF5(SUOTA)**，对无此服务的设备 `gatt_dm: 0xFEF5 not found` |
| GATT 读（GAP 0x2A00） | ❌（对随机设备） | 全部 `0x74`：字符映射表只含 0xFEF5 服务特征，0x2A00 不在表内（设计如此，见 bridge_ble.c `api_gatt_read`） |
| 断开 | ✅ | 全部 `status=0` + EVT.DISC（0x16=本端发起；0x3E=远端/LL 拆链） |
| 帧链路质量 | ✅ | 全会话 wire_gaps=0、crc_errors=0、host ring dropped=0（数千帧）；固件侧 dropped=28/907（≈3%，归因未闭环，见异常 5） |

## 1. 连通性冒烟

open 后 drain 0.6s，ping/info 各 ×3：**6/6 通过**（ping 4.8–4.9ms，info 14.1–14.2ms）。

```
{"version":"blebridge-0.1.0","uptime_s":1533,"events_sent":1,"events_dropped":0,
 "credits":127,"current_log_level":3,"phy":"uart0"}
```

**开口毛刺**：本会话多次 open 的 0.6s 窗口均未捕获到毛刺帧（glitch_last.hex=0 字节），首请求全部首试即过——与 bring-up 进展3 注意②（"开口毛刺毁第一只请求"）相比未复现，疑与板上桥状态有关；drain 0.6s 继续作为标准安全措施保留。

## 2. 扫描

- 单窗 10s：208 条广播 / 12 设备；累计 3 窗：**639 广播 / 18 设备**（public 11 / random 7）。
- 环境以 Apple 匿名广播（厂商数据 0xFF4C00、随机地址轮换、无名称）为主；带 SCAN_RSP 名称的 5 台：

| 名称 | 地址/类型 | RSSI | 类型判读 |
|---|---|---|---|
| BH4768391 | 1F:05:41:4D:24:64 / public | -55 | 串号型设备（支持 247+ MTU，无 0xFEF5） |
| S8503_19af81 | 19:AF:81:19:96:E0 / public | -67 | 手环类（支持 247 MTU） |
| YD_B171 | 71:B1:09:00:00:C0 / public | -75 | 键盘（0x1812 HID） |
| AIMA-C10A02 | 02:0A:C1:03:22:F1 / public | -79 | 键盘（0x1812，appearance 962 keyboard） |
| midea | 69:60:B9:93:20:3C / public | -80 | 家电 |

扫描期间 EVT.SCAN 帧序连续、credits 自动补给正常（固件 events_sent 与 host 收到数一致）。

## 3. 连接矩阵（5 台有名设备 + random 地址补充）

### 3.1 首轮矩阵（matrix.json，MTU 读取过早，见 3.3 复测）

全部 5 台 connect `status=0`（38.4 / 884.4 / 252.6 / 223.3 / 1568.1ms）；disc_svc、GAP 读名（0x2A00）全部 `0x74`；disconnect 全部 `status=0`。

| 设备 | 连接 | MTU(首轮读) | disc_svc | GAP读名 | 断开 | EVT.DISC reason |
|---|---|---|---|---|---|---|
| BH4768391 | ✓ 38ms | 23 | 0x74 | 0x74 | ✓ | 0x16（本端） |
| S8503_19af81 | ✓ 884ms | 23 | 0x74 | 0x74 | ✓ | 0x16（本端） |
| YD_B171 | ✓ 253ms | 23 | 0x74 | 0x74 | ✓ | 0x3E（远端/LL） |
| AIMA-C10A02 | ✓ 223ms | 23 | 0x74 | 0x74 | ✓ | 0x3E（远端/LL） |
| midea | ✓ 1568ms | 23 | 0x74 | 0x74 | ✓ | 0x3E（远端/LL） |

### 3.2 MTU 复测（mtu_pass.json，连接后轮询 EVT.MTU 至 2s）

| 设备 | EVT.MTU 序列 | 协商 MTU | ≥247 | 备注 |
|---|---|---|---|---|
| BH4768391 | 23 → **256** | 256 | ✅ | DEBUG: `blephy: 1M tx 1M rx` |
| S8503_19af81 | 23 → **247** | 247 | ✅ | |
| YD_B171 | 23 | 23 | ❌ | DEBUG: `gatt_dm: 0xFEF5 not found`，链路随后自毁 |
| AIMA-C10A02 | 23 | 23 | ❌ | 同上 |
| midea | 23 | 23 | ❌ | 同上 |

方法学教训：`CMD.mtu` 读回值在连接后瞬即返回交换前初值 23，**协商结果必须等第二条 EVT.MTU**（首轮矩阵的 "MTU=23" 是采样过早的假阴性）。

### 3.3 random 地址补充（matrix_random.json，模式 1）

- `4C:EA:17:E7:91:53`（t1, -71）：connect `status=0`（223ms），body=`01+6B addr` ✓；MTU 23；disconnect ✓，EVT.DISC 0x16。**random 地址连接路径可用**。
- 两台 Apple RPA（-28/-28，地址轮换）：connect 5s 超时；固件 EVT.DEBUG: `connect err: 2`（HCI Unknown Conn Id）——广播停止后直连陈旧 RPA 的典型 LL 失败，预期类失败，非固件缺陷。

### 3.4 事件流复核（recheck_conn.py）

EVT.CONN **确实发出**（`0x21/00:001f05414d2464`），只是事件帧与命令响应帧无顺序保证，可能晚于 connect 应答到达（首轮矩阵的立即采样因此漏采）。host 端不得假设 EVT.CONN 先于 connect 响应可见。全事件序列：EVT.MTU(23) → EVT.CONN → EVT.MTU(256) → DEBUG(phy 1M/1M) → DEBUG(gatt_dm 0xFEF5 not found)。

## 4. 链路质量（全会话）

| 指标 | 值 | 说明 |
|---|---|---|
| wire_gaps（事件帧 SEQ 跳变） | **0** | 设备侧无事件丢失（除显式记录的固件 drop） |
| crc_errors（host StreamParser） | **0** | 115200 下数千帧全干净（921600 桥不可靠问题的规避有效） |
| host ring dropped | **0** | host 侧无环形溢出 |
| 固件 events_dropped | 28 / ~907（≈3%，自上次复位） | 归因未闭环：所有 host 连续泵水的阶段均为 0 drop；疑集中在 host 阻塞于 5s connect 超时（RPA 尝试）期间固件事件积压的时刻。需 RTT 复核（本次禁区） |

## 异常清单

1. **【host 协议缺陷，建议跟进】跨串口会话 credit 失步**：固件 credit 余额跨串口重连持久，而 `BridgeHost` 每会话假设 128。实测（diag_credits.py）：连续 3 个短会话后广播接收 81→35（恰=剩余 credit）→0，固件 `events_dropped` +215；open 后主动 `grant_credits(128)` 立即恢复（83 条）。该矩阵全部脚本已内置此缓解；**根治建议**：`BridgeHost` 建立/重连后主动补一次 credit（host 侧小改，未在本次范围）。这也解释了"连续扫描窗口收不到广播"的假象——扫描器本身 12 连窗稳定（diag_scan2.py）。
2. **【设计使然】连接后自动服务发现仅限 0xFEF5**：无此服务的设备 `gatt_dm: 0xFEF5 not found` → 固件按 task-10 简报主动拆链（`dm_not_found_cb`→`bt_conn_disconnect(BT_HCI_ERR_UNSPECIFIED)`），后续 GATT 命令 `0x74`。对随机设备做通用 central 的用途受限；对 SUOTA 笔恰是所需行为。
3. **3/5 设备 MTU 停 23**：均为连接后数秒内链路自毁（0x3E 远端拆链 + 0xFEF5 not found 拆链竞争）导致交换未完成；两台存活设备均 ≥247（256/247）。MTU≥247 断言对真实 SUOTA 设备有意义，对随机设备受此自毁行为牵连。
4. **GAP 读名（0x2A00）不可达**：字符映射表只收 0xFEF5 服务特征，0x2A00 查表失败按设计回 `0x72/0x74`。对随机设备的"读名字"只能靠广播数据（本次已从 adv 提取）。
5. **固件 dropped=28 归因未闭环**：见第 4 节，需 RTT（本次禁碰探针）。
6. **开口毛刺未复现**（本次会话），drain 0.6s 保留为安全措施。
7. **EVT.CONN 与 connect 响应无顺序保证**：host 采样需容忍事件晚到（见 3.4）。

## CH592 功能对等性结论（扫描✓ 连接✓ MTU✓ 发现✗ 读写✗ 断开✓）

- **扫描/连接/断开**：与 CH592 legacy central 功能对等达成——active 扫描含 SCAN_RSP 名称、public/random 两种地址直连、干净断开 + reason 事件，真机 6 类设备上全通。
- **MTU**：机制与 ≥247 断言在支持设备上达成（256/247），对等成立。
- **服务发现/读读写**：**尚不对等**——当前固件把 GATT 数据面收窄到 SUOTA(0xFEF5) 一个服务（自动发现、按 uuid16 查表读写、disc_svc 只回缓存状态），对随机设备不可用。这是设计取舍而非回归；是否放宽（如全库发现 + 任意 handle 读写）属产品决策，建议单独立 FR。
- 数据面（帧协议/credit/事件流）在 115200 变体链路上经数千帧实测零 CRC 错、零 SEQ 跳变， bring-up 结论（921600 桥不可靠、115200 全通）再次验证。

## 附：原始数据文件（/tmp/bt_matrix/）

- `smoke.json` 冒烟 6/6 + 链路计数
- `scan_devices.json` 18 设备清单（addr/type/RSSI/name/uuid16/adv hex）
- `matrix.json` 首轮 5 台矩阵全步骤原始输出
- `mtu_pass.json` MTU 复测（含 EVT.MTU 序列与 DEBUG 文本）
- `matrix_random.json` random 地址补充
- `diag_scan*.py / diag_credits.py` credit 失步与扫描稳定性诊断脚本及结论出处
- `matrix_run.py` 矩阵驱动脚本（含 credit 重同步缓解）
