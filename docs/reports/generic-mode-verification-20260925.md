# generic 模式上板验证报告（eaf6cbe 重建固件 + 参考 SUOTA 设备回归）

- 日期：2026-09-25
- 执行：硬件验证 agent（独占板上桥串口 + WCH-Link 探针 @1MHz）
- 被测固件：从 HEAD=`eaf6cbe` scratch 重建（`west build -p -b nrf52840dk/nrf52840 firmware/app -d build-verif`，**exit 0**），默认 cdc 形态（`CONFIG_BRIDGE_PHY="cdc"`、`BRIDGE_GATT_CHAR_MAX=32`，与 e0de707 评审配置一致——所测=所评审）
- host：仓库内 `host/.venv` + `host/src/blebridge`（eaf6cbe，含 disc_svc/handle 寻址封装）
- 原始日志：会话内 JSONL 落盘（本次会话工具，可复现）
- 边界：未改任何代码（本报告文件除外）；未派子 agent

## 结论表（TL;DR）

| 项 | 结果 | 证据 |
|---|---|---|
| 重建+烧录+枚举+冒烟 | ✅ | build exit 0；pyocd 1MHz 烧录 OK；硬复位后 0.7s 枚举 CDC；ping×3 status=0 RTT 3.1–3.6ms；info 新键 `tx_timeouts=0`/`rx_overflows=0` 在位，`phy=cdc` |
| generic 连接保持（10s 无自动断连） | ✅ | AltoBeam/midea 多轮 hold 通过；全 8 个完成会话零固件自发 EVT.DISC |
| disc_svc host 触发（0x1800 GAP） | ✅ | 两台设备 status=0 nchars=3 + EVT.DEBUG `gatt_dm ok: 3/3 chars` |
| 服务不存在不拆链（genmode 核心差异） | ✅ | 0x180F 缺失 → status 0x74 + `service not found`，**连接保持**（共 4 次跨两台设备） |
| 读 GAP Device Name uuid16 + handle 双路 | ✅ | AltoBeam=`AltoBeam`（uuid16 ✓ / handle 0x0003 ✓）；midea=`midea\x00`（uuid16 ✓ / handle 0x0007 ✓） |
| 第三台不同设备 | ⚠️ 环境受限 | S503×4 MAC、S8503、YD_B171 共 9 次尝试全部**对端**链路失败（0x08/0x3E/connect err 2），见 §4 定界 |
| 通知订阅（步骤 e） | ⚠️ 无可达通知源 | 0x180F 缺失×2；handle 遍历未见可读 0x2803 声明×2；EVT.GATTX 通路本次未点亮（遗留） |
| 参考 SUOTA 设备显式发现路径回归 | ✅ | connect mode0 → disc_svc(0xFEF5) **status=0 nchars=10** + `gatt_dm ok: 10/10 chars` → suota_version=**0x0d** → disconnect 0x16（第 1 次尝试遇对端建立抖动 0x3E，第 2 次干净通过） |

**总判定：generic 固件行为全部符合 task-genmode 设计，未发现固件缺陷，无需 BLOCKED/defect。** 三台目标中两台全流程 PASS；第三台不可得系周边杂牌设备链路质量（对端 LL 失败），非固件问题（定界见 §4）。

## 1. 重建、烧录、复位与冒烟

```
west build -p -b nrf52840dk/nrf52840 firmware/app -d build-verif   → exit 0
  FLASH 218616B (20.85%) / RAM 73332B (27.97%)
  .config: CONFIG_BRIDGE_PHY="cdc", CONFIG_BRIDGE_GATT_CHAR_MAX=32
pyocd flash -e sector -t nrf52840 -u <probe-serial> -f 1000000 .../build-verif/app/zephyr/zephyr.hex
  → Erased 48 sectors, programmed 196608B, exit 0
复位: pyocd ResetType.HARDWARE（WCH-Link RST 线），RESETREAS before=0x1 after=0x1（PIN 位，无 vector-catch 伪影）
枚举: /dev/cu.usbmodem11301 节点在位（Zephyr CDC，复位后即复用）
```

冒烟（`--no-rtscts`，settle 0.6s）：

```
ping#1 status=0 rtt=3.6ms / ping#2 3.2ms / ping#3 3.1ms
info status=0 → {"version":"blebridge-0.1.0","uptime_s":3,"events_sent":1,
  "events_dropped":0,"tx_timeouts":0,"rx_overflows":0,"credits":255,
  "current_log_level":3,"phy":"cdc"}
→ tx_timeouts / rx_overflows 新键 PRESENT；phy=cdc ✓
```

## 2. 扫描与设备池

15s 扫描×5 轮合计 15 台设备，稳定在场：`BH4768391`(1F:05:41:4D:24:64, rssi −56..−61)、
`midea`(69:60:B9:93:20:3C, −67)、`S8503_19af81`(19:AF:81:19:96:E0, −62..−74)、
`S503_*`×4（E8:63:10 / 9B:97:B9 / DE:60:A8 / F0:EE:F5，MAC 轮换的同类设备）、`YD_B171`(−77)、
一台参考 SUOTA 设备（−48, 每轮 38–57 帧）及若干无名 random。
选取策略：RSSI 降序、排除参考设备与 Apple RPA 名。

## 3. 三设备 generic 验证明细（c–f 流程）

### 3.1 BH4768391 / AltoBeam（1F:05:41:4D:24:64, public）——**PASS ×2 轮**

```
connect status=0 → EVT.MTU 23 → EVT.MTU 256 → EVT.DEBUG lephy: 1M tx 1M rx
hold 10s: disc_seen=False（后续操作合计驻留 >40s 亦无自发断连）
disc_svc(0x1800) status=0 nchars=3；EVT.DEBUG "gatt_dm ok: 3/3 chars"
read 0x2A00 (uuid16) status=0 → "AltoBeam" (416c746f4265616d)
read handle 0x0003 status=0 → "AltoBeam"（探测 0x0001..0x0C 命中，与 uuid16 读逐字节一致）
disc_svc(0x180F) status=116(0x74) + EVT.DEBUG "disc_svc: service not found" → 连接保持 ✓
handle 遍历 0x0001..0x0040：除 GAP 3 特征外无可读 0x2803 声明 → 无可达通知源（记录）
disconnect status=0 → EVT.DISC reason=0x16 ✓
```

### 3.2 midea（69:60:B9:93:20:3C, public）——**PASS ×2 轮**（另 1 轮 f 步前被对端拆链，见 §4）

```
connect status=0 → EVT.MTU 23 → EVT.MTU 247 → lephy: 1M tx 1M rx
hold 10s: disc_seen=False
disc_svc(0x1800) status=0 nchars=3；EVT.DEBUG "gatt_dm ok: 3/3 chars"
read 0x2A00 (uuid16) status=0 → "midea\x00"
read handle 0x0007 status=0 → "midea\x00"（与 AltoBeam 的 0x0003 布局不同，证明 handle 探测必要）
disc_svc(0x180F) status=0x74 + "service not found" → 连接保持 ✓
disconnect status=0 → EVT.DISC reason=0x16 ✓（两轮干净通过）
```

第 1 轮（未跳过 handle 遍历）：a–e 全通过后、f 步前收到 EVT.DISC **0x13（远端发起）**，
CMD.disconnect 正确应答 116 NOT_READY（链路已被对端拆）——midea 模块自有连接时长策略，固件侧零异常。

### 3.3 第三台候选（S503 家族 / S8503 / YD_B171）——**不可得，全部对端链路失败**

| 设备 | 尝试 | 现象（EVT 原始） |
|---|---|---|
| S503_e86310 (E8:63:10) | ×2 | 连上后 hold 窗内 `EVT.DISC reason=0x08`（监督超时），MTU 停 23 |
| S503_9b97b9 (9B:97:B9) | ×1 | `connect status=116` + `EVT.DEBUG connect err: 2`（连接建立失败） |
| S503_de60a8 (DE:60:A8) | ×1 | MTU 23 → lephy 1M → `EVT.DISC 0x08` |
| S503_f0eef5 (F0:EE:F5) | ×1 | `connect status=116` + `connect err: 2` |
| S8503_19af81 | ×2 | MTU 247 + lephy 2M → `EVT.DISC 0x3E/0x08`（建立失败/监督超时） |
| YD_B171 (71:B1:09) | ×4 | 每次连上即 `EVT.DISC 0x3E`，从未完成 MTU 交换 |

### 步骤 (e) 订阅说明

两台全过设备均无可达通知源：battery 0x180F 缺失（0x74×2，且均不拆链）；handle 遍历
0x0001..0x0040 无可读 0x2803 声明（GAP 外特征读受权限保护）。**EVT.GATTX 通知通路本次
未点亮**，属遗留项：建议下一轮用 suota_sim 或已bonding 设备补一条 subscribe→GATTX→unsubscribe
用例（handle 寻址订阅同样待点亮，uuid 与 handle 读通路已证）。

## 4. "第三台失败是否 genmode 缺陷"定界（结论：非固件）

1. **失败原因码全是链路层错误**：0x08（监督超时）、0x3E（建立失败）、connect err 2——由控制器
   上报，只能来自对端/射频；固件主动断连只可能产生 0x16/0x13。
2. **固件代码无自动断连路径**：`bridge_ble.c` 现存 4 处 `bt_conn_disconnect` 调用点均为
   connect 超时取消 / host CMD.disconnect / CMD.reset 收尾（341/846/887/1387 行）；dm
   失败路径只发 EVT.DEBUG（e0de707 设计）。
3. **同板同固件对 BH/midea 稳定驻留**：AltoBeam 单会话驻留 >40s、midea 两轮完整 c–f、
   hold 10s 多轮零自发 DISC——排除板卡射频/固件普遍问题。
4. 与既有《BLE 随机设备连接实测矩阵》记录一致：S503 类设备当时也"停 23 系链路早断"。

## 5. 参考 SUOTA 设备显式发现路径回归（新路径首验）

```
[scan 20s] 参考设备广播确认（54 帧, rssi −48）
[connect mode0] status=0 → EVT.CONN
[disc_svc 0xFEF5] status=0 nchars=10；EVT.DEBUG "gatt_dm ok: 10/10 chars"  ← 新显式发现路径
[read suota_version 0x64B4] 首读即 status=0 → raw=0d → 断言 0x0d ✓
[disconnect] status=0 → EVT.DISC reason=0x16 ✓
→ SUOTA_REGRESSION PASS
```

- 与 task-genmode §6-1 预期一致：disc_svc 应答 nchars=10、EVT.DEBUG 10/10；SUOTA 10 特征
  uuid16 键 map 由显式发现填充成功。
- 第 1 次尝试遇对端建立抖动（EVT.CONN 后无 MTU 交换、dm 与垂死链路竞态报 `service not
  found`、随后 `EVT.DISC 0x3E`）；按 ota_bench 惯例（连接前 best-effort disconnect + 1s +
  drain）重试即干净通过。与"设备广播间歇阵发"的既有记录同类，非固件问题。
- 未跑完整 OTA（任务边界为 disc_svc+版本断言；吞吐回归归压测排程）。

## 6. 遗留与建议

1. **EVT.GATTX / handle 订阅未点亮**（§3 步骤 e）：建议补 suota_sim 用例。
2. S503/S8503/YD_B171 类设备连接即掉，属对端 LL 质量；如需三设备全 PASS 的合规证据，
   建议换环境或引入已知良好外设（suota_sim、nRF Connect 模拟外设）。
3. conn_update / phy 的显式上板用例（task-genmode §6-4）本次未主动下发；仅被动观察到
   `lephy: 1M/2M tx rx` EVT.DEBUG 回报通路正常。
4. midea 类"远端先拆链"场景下 CMD.disconnect 返回 116 NOT_READY 属正确语义，host 侧如需
   幂等可按 status==116 && 最近 EVT.DISC 判定忽略。
