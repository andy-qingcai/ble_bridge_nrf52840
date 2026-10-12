# 外设模式 central 侧验证报告（Mac CoreBluetooth → blebridge）

- 日期：执行于 2026-09-24（文件名沿用任务指定编号 20260926）
- 执行：BLE 验证 agent（central 角色，macOS 本机蓝牙，未派子 agent）
- 方法：`host/.venv` + `pyobjc-framework-CoreBluetooth` 12.2.2（pyobjc-core 12.2.2 / pyobjc-framework-Cocoa 12.2.2）；`CBCentralManager` 主线程 NSRunLoop 泵驱动状态机
- 脚本与原始数据：`/tmp/bridge_central.py`（验证脚本）、`/tmp/bridge_central_log.txt`（带时间戳全量日志）、`/tmp/bridge_central_result.json`（结构化结果）
- 边界：**全程未触碰 `/dev/cu.usbmodem11301` 及任何板上桥串口**；未修改仓库代码（本报告除外）；对端为板上外设模式（广播名 "blebridge"，host 侧声明广播中，桥串口由另一进程独占）

## 结论表（TL;DR）

| 清单项（central 视角） | 结果 | 证据 |
|---|---|---|
| 1. 扫描确认 "blebridge"（含 0xFFE0） | ❌ 未达成 | 多轮扫描累计 >2.5 min（含 45 s 全量清点 77 台设备）：**未见 "blebridge" 名称，未见目标 0xFFE0/0xAAAA 广播**；空中唯一 0xFFE0 广播者经 SCAN_RSP 名确认是 `ANT@BLE24AAUB-B96M`（ANT/BLE 适配器，非目标） |
| 2. 连接 → 服务发现 0xFFE0/0xAAAA | ⛔ 阻塞 | 目标空中不可达，无法连接（对误抓 ANT 设备 2×25 s connect 均超时，仅作环境对照） |
| 3. 读 0xFFE1 == "test-123" | ⛔ 阻塞 | 未连接 |
| 4. 写 0xFFE1 "from-central" | ⛔ 阻塞 | 未连接 |
| 5. 订阅 0xFFE2 CCCD 等 30 s | ⛔ 阻塞 | 未连接 |
| 6. 断开 | ⛔ 阻塞 | 未连接 |

**pyobjc 路线判定：技术走通** —— 蓝牙 `poweredOn`、扫描/广播解析/SCAN_RSP 名称/连接发起与取消/全回调链均正常工作（环境设备正常可见，最远收到 -102 dBm 广播）；**不通的是对端**："blebridge" 广播在本 Mac 空中始终不可见。按任务约定转入 §4 手机人工验证清单。

> **⚠️ 2026-09-24 重跑更新（根因定位后，见 §6）**：根因确认为"复位/断链后固件不自动恢复广播"（运维状态问题）。重跑后清单 6 步中 4 步 PASS（扫描/连接/读/写/断开），并新增两条固件侧发现：**0xAAAA gatt_define 协议成功但空口不可见**；**FFE2 空口表缺 CCCD 0x2902 导致订阅被 ATT 拒绝（error 10）**。详见 §6。
>
> **⚠️ 2026-09-24 build-fix2 复验（见 §7）**：缺陷 2（CCCD 缺失）与缺陷 3（断链不恢复广播）**修复验证通过**；通知回路暴露新缺陷"**影子 CCCD**"（写路径通、订阅未注册进 notify 路径，host notify 回 0x74），修复环 2 进行中，第三轮闭环。

## 6. 重跑结果（根因定位后，同日）

### 6.1 根因确认与状态恢复（host CLI，串口已归本 agent）

controller 定位：gatt_define 调试期间板卡被复位过，generic 固件复位后**不会自动恢复广播**，之后无人 `adv_start`。本 agent 实测补充：**断链后同样不自动恢复广播**——第一次重跑中脚本在连接态崩溃退出，随后 80 s 扫描不见目标；干净断开（err=None）后 12 s 扫描亦不见目标（复现 2 次）。恢复操作：

```
blebridge set-char    --uuid ffe1 --data 746573742d313233   → status=0   # 复位同时清掉了 FFE1 预设值（首跑读到空值）
blebridge gatt-define --uuid aaaa --chars '[{"uuid16":"aaa1","read":true,"write_wo_resp":true,"max_len":64},
                                          {"uuid16":"aaa2","read":true,"notify":true,"max_len":64}]'
                                                       → status=0 base_handle=32
blebridge adv-start                                        → status=0
```

### 6.2 重跑清单矩阵（pyobjc central，恢复状态后）

| 清单项 | 结果 | 原始数据 |
|---|---|---|
| 1. 扫描 blebridge + 0xFFE0 | ✅ | 0.086 s 即见（首轮重跑 0.64 s），RSSI **-38/-39 dBm**（板在近旁）；`kCBAdvDataLocalName="blebridge"`、`svc=[FFE0]`、connectable=1 |
| 2a. 连接 | ✅ | 0.67 s `didConnect`，无错 |
| 2b. 服务发现 | ⚠️ | FFE0 ✅（FFE1=read\|write_without_resp、FFE2=read\|notify，共 **2 特征**，清单预期 3 特征——第 3 特征未见）；**0xAAAA ❌ 空口不可见**（6.4） |
| 3. 读 0xFFE1 == "test-123" | ✅ | `746573742d313233` == `test-123`（set-char 恢复后；恢复前首跑读到**空值**——预设值非固件内置，为易失的 host 侧设置值） |
| 4. 写 0xFFE1 "from-central" | ✅ | 走 Write Without Response（props 0x06 无 write prop）；336 ms 后回读 `66726f6d2d63656e7472616c` == `from-central`，板侧值已更新 |
| 5. 订阅 0xFFE2 CCCD | ❌ | CCCD 写被 ATT 拒绝：`CBATTErrorDomain Code=10 "The attribute could not be found"`，`notifying=false`；30 s 窗口 0 notify——**不是**预期的"无 notify 源静默超时"，而是订阅根本未建立（6.4） |
| 6. 断开 | ✅ | `didDisconnect err=None`，干净断开 |

### 6.3 全量发现探针（佐证）

连接后 `discoverServices(None)`（无 UUID 过滤）→ 空口表**仅有 `['FFE0']`**（FFE1 props=6, FFE2 props=18）。0xAAAA 未上空口表不是 macOS 过滤问题。

### 6.4 新增固件侧发现（两条，均复现）

1. **FFE2 缺 CCCD**：声明 notify 属性但空口无 0x2902 描述符——`discoverDescriptorsForCharacteristic(FFE2)` 返回空（伴 `CBErrorDomain Code=0`），CCCD 写直接 ATT error 10。订阅路径当前**不可用**，`notify`(CMD 0x48) 推送在无订阅者时也无法落地。
2. **gatt_define 服务空口不可见**：`gatt_define --uuid aaaa` 协议层成功（status=0, base_handle=32），但过滤发现与全量发现均无 0xAAAA。结合 6.1（动态值/广播均易失）与近期 gatt_define 修复记录（commit `03efd90`/`a999c4f`），疑为 ATT 表重建/Service Change 缺失或残余缺陷，建议固件侧跟进。

### 6.5 运维注意（当前板态）

每次断链后需重新 `blebridge adv-start`；复位后还需 `set-char`（FFE1 预设值）与 `gatt-define`（0xAAAA，待 6.4-2 修复）。本报告收尾时板子已恢复广播态。

### 6.6 手机侧确认（非阻塞，待人工）

Mac central 已确认可见性与链路（-39 dBm）。手机 nRF Connect 复核可按 §4 清单执行；注意第 6 步（订阅 0xFFE2）将复现 ATT error 10，属固件空口表问题而非手机操作问题。

## 7. build-fix2 复验（2026-09-24，三缺陷修复后）

分工：桥串口归 controller（adv_start / notify / set_char / gatt_define），Mac 蓝牙 central 归本 agent。
脚本与原始数据：`/tmp/bridge_central_v2.py`（五步+150s 通知等待窗，日志 `/tmp/bridge_v2_log.txt`、结果 `/tmp/bridge_v2_result.json`）、`/tmp/bridge_renotify3.py`（自愈订阅观察窗，已按要求撤下）。

### 7.1 五步复验矩阵（对照上轮三 ❌）

| 步骤 | 结果 | 原始数据（/tmp/bridge_v2_log.txt） |
|---|---|---|
| 1. 扫描 blebridge | ✅ | adv_start 后 0.083 s 即见，RSSI -39 dBm，`svc=[FFE0]` |
| 2. 连接 | ✅ | 0.68 s didConnect，无错 |
| 2b. 发现 0xAAAA（上轮 ❌） | ⏸ 非缺陷，待第三轮 | 空中仍仅 `['FFE0']`、FFE0 2 特征。经确认 **0xAAAA 为 RAM 易失 + host 全权设计语义**：每次复位后须 host 重新 gatt_define，本轮未重定义属预期行为；重定义后的定点发现列入第三轮 |
| 2c. FFE2 CCCD（上轮 ❌） | ✅ 空口已补齐 | `descriptors of FFE2: ['Client Characteristic Configuration']`（=CCCD 0x2902，macOS 以友好名上报；上轮同查询为**空列表**）。注：FFE0 特征数保持 2（FFE1/FFE2）+1 描述符 |
| 3. 读 FFE1=="test-123" | ⚠️ | 复验时读到空值（复位同样清预设值）；host 已 set-char 恢复 `test-123`，第三轮复验读值 |
| 4. 写 FFE1 "from-central" | ✅ | without-response 写入，336 ms 后回读 `66726f6d2d63656e7472616c` 一致 |
| 5a. 订阅 0xFFE2 CCCD 写（上轮 ❌ error 10） | ✅ | **ATT error 10 消失**：`notify state FFE2 -> True err=None`（订阅耗时 0.06 s），`notifying=True`；反订阅同样干净（`-> False err=None`） |
| 5b. 通知回路 | ❌ 影子 CCCD | host `notify --data 41424344` 返回 0x74、固件订阅镜像 False；central 侧 150 s 窗口 0 通知（时间窗覆盖 host 发送尝试）。完整证据链见 7.2。FR-H 修复环 2 进行中，第三轮闭环 |
| 6. 断开 → 12 s → 自动恢复广播（上轮 ❌） | ✅ | 干净断开（err=None）→ **12.06 s 后重扫即见 blebridge（-38 dBm），本 agent 未发 adv-start**——缺陷 3（断链自动恢复广播）修复验证通过 |

### 7.2 缺陷 2"影子 CCCD"完整证据链

central 侧原始三行（Mac → 板，ATT 层）：

```
[   2.099] descriptors of FFE2: ['Client Characteristic Configuration'] err=None  # CCCD 空口存在
[   2.668] notify state FFE2 -> True err=None                                     # CCCD 写被 ATT 接受（handle 由 macOS 栈管理，值为使能位 0x0001）
[  152.790] notify state FFE2 -> False err=None                                   # 反订阅同样成功
[  152.753] STEP5 notify loop closed=False count=0                                # 覆盖 host 发送尝试的 150s 窗口内 0 通知
```

host 侧（controller 提供）：`notify --data 41424344` → **0x74**，固件订阅镜像 False。

定性：FFE2 的 CCCD 为"影子 CCCD"——**写路径通**（ATT 层接受使能/去使能），但订阅状态未注册进 Zephyr notify 推送路径，故 host 推送被 0x74 拒绝、空中无通知。与 FR-H 侧定位一致；待修复环 2 固件后第三轮闭环。

### 7.3 通知回路调试期新现象（central 侧观测，已转 R 定位）

1cd9596 复验中：host 开始周期性 `notify`（15s/发）后，central **每次都能 <1 s 建链，但 GATT 服务发现 100% 卡死**（45 s 看门狗，8+ 连续周期，连接保持不断）；对照实验：同一 Mac central 对周边其它 BLE 设备发现 0.42 s 正常完成——排除 Mac 栈。host 侧确认停发 notify 后卡死仍持续 → **假设 A（推送期间争用）排除，假设 B 实锤：首次 0x48 推送即永久楔死 GATT 服务器**（adv 重启是否可清由 R 实验定）。影响面：楔死期间一切 GATT 操作（不止 notify 回路）不可用。R agent 做封闭复现实验（板串口 + Mac central 两侧），本 agent central 已撤下避免污染。

### 7.4 第三轮待验清单（固件修复环 2 上板后）

1. 通知回路闭环：订阅 → host `notify` → central 收到 41424344
2. 0xAAAA：host 重新 gatt_define 后的空口发现（预期随缺陷 1 修复可见，主服务声明已改 0x2800 类型）
3. 读 FFE1 == "test-123"（预设值恢复后）
4. 广播自动恢复复跑（回归缺陷 3）

## 1. 环境与方法

- 主机：macOS 26（darwin 25.6.0, arm64），CoreBluetooth 状态回调 `poweredOn`（TCC 蓝牙权限已具备）。
- venv：`host/.venv`（Python 3.14），`pip install pyobjc-framework-CoreBluetooth` 直接成功。
- pyobjc 12 注意点（复跑参考）：
  - `CBCentralManager` 须经 `initWithDelegate_queue_(d, None)` 初始化；回调依赖主线程 `NSRunLoop.runMode_beforeDate_(…, 0.05~0.1 s)` 泵。
  - 广播字典 key 用字面量（`kCBAdvDataLocalName` / `kCBAdvDataServiceUUIDs` / `kCBAdvDataIsConnectable`）；`CBAdvertisementDataIsConnectableKey` 常量在本版 pyobjc 中不存在（会以 NSException 击穿回调，须防）。
  - 回调**必须** try/except 包裹：delegate 内 Python 异常会变成 ObjC 未捕获异常直接终止进程（实测踩坑一次）。
  - macOS 26 SDK 的 `CBPeripheralDelegate` 形式协议含 3 参变体 `peripheral:didDiscoverServices:error:`（self+3），经典 2 参 `peripheral:didDiscoverServices:` 两个签名均已实现。
  - GATT 读/写/订阅方法是 **CBPeripheral** 的（`readValueForCharacteristic_` / `writeValue_forCharacteristic_type_` / `setNotifyValue_forCharacteristic_`），CBCharacteristic 上只有 `isNotifying`——归属搞错会在运行时 AttributeError。
- 扫描策略：先按服务 0xFFE0 过滤扫描 → 全量扫描兜底，均开 `CBCentralManagerScanOptionAllowDuplicatesKey`（允许 SCAN_RSP 名称在后继广播包中补全缓存）；连接尝试期间保持扫描以提升弱链路连接成功率。

## 2. 逐项结果与原始数据

### 2.1 清单项 1：扫描（❌ 目标不可见）

**目标候选甄别过程**（第一轮）：

```
[  8.996] ADV F52D7570-080C-EDBD-9A8C-2E2D62DC7698 name='' rssi=-91 svc=['FFE0']
          connectable=1 mfr=88137b21a91f19f822113322
[ 19.009] -- adv-name: TIMEOUT after 10.0s        # 等待 SCAN_RSP 名称补全 10s 无果
[ 56.809] ADV F52D7570-… name='ANT@BLE24AAUB-B96M\r' rssi=-91 svc=['FFE0'] …
```

该设备最终带出 SCAN_RSP 名称 `ANT@BLE24AAUB-B96M\r` —— **判定为环境中的 ANT/BLE 适配器，非本桥板**（广播名不符；厂商数据 `8813 7b21…` 亦非 Nordic 0x0059）。

**专项全量清点**（45 s，allow-duplicates，判定条件：名称含 "blebridge" ∨ 服务含 FFE0/AAAA）：

```
distinct peripherals: 77
MATCHES (name~blebridge / svc FFE0 / svc AAAA): 1
  F52D7570-080C-EDBD-9A8C-2E2D62DC7698 best_rssi=-90
  names=['', 'ANT@BLE24AAUB-B96M\r'] svcs=['FFE0']
top10 by rssi: -44(某参考设备) … -75(其它环境设备)   # 环境射频正常
```

- 77 台设备中：名称含 "blebridge" = **0**；服务 0xAAAA = **0**；0xFFE0 广播者 = 仅上述 ANT 适配器。
- 灵敏度对照：同窗口可稳定收到 -85 ~ -102 dBm 的设备（如 `1RDAF2446J2604` -100 dBm、`4B803C16…` -102 dBm），Mac 侧扫描灵敏度无异常。

### 2.2 清单项 2：连接（⛔ 目标不可达；附环境对照）

对误抓的 ANT 设备发起连接（保持扫描辅助），两次尝试各 25 s 超时：

```
[ 30.8xx] connect attempt 1 … TIMEOUT after 25.0s   # didConnect / didFail 均未回调
[ 55.800] connect attempt 1 failed (None); cancel and retry
[ 55.801] disconnected (err=None)                    # cancelPeripheralConnection → didDisconnect 正常
[ 81.829] connect attempt 2 … TIMEOUT after 25.0s
```

连接发起/取消/回调链路完整可用（central 栈工作正常）。目标 "blebridge" 无地址可连，服务发现（0xFFE0 三特征 / 0xAAAA 动态 2-5 特征）未执行。

### 2.3 清单项 3-6：读 0xFFE1 / 写 0xFFE1 / 订阅 0xFFE2 / 断开（全部 ⛔ 阻塞）

未建立连接，未执行。预期判据（供复跑）：

- 读 0xFFE1 → `74 65 73 74 2d 31 32 33`（ASCII `test-123`）
- 写 0xFFE1 `from-central`（属性 read+write_wo_resp → 走 Write Without Response；回读观察板侧值）
- 订阅 0xFFE2 CCCD → `notifying=true`；当前无 notify 源，30 s 无数据**属预期**，记录之
- `cancelPeripheralConnection` → `didDisconnect` 无错

## 3. 目标不可见的根因假设（按可能性排序）

1. **板侧广播实际未启动**：host 侧 adv/gatt_define 配置写入成功 ≠ softdevice 广播开启；桥串口由另一进程独占，本 agent 无法查询板上状态（任务边界禁止触碰串口）。建议由持串口进程回读 adv 状态（如 `ble_adv` 状态帧）或看其日志。
2. 板与 Mac 距离/屏蔽：但环境可收 -102 dBm 设备，若板在正常桌面距离应可见。
3. 广播被占用：板可能已与其它 central 建链（连接后停止广播），host 侧"广播中"状态与空中事实脱节。
4. 广播参数异常：extended advertising / coded PHY / 超长 adv interval 等导致 macOS 侧不可见。

## 4. nRF Connect（手机）人工验证清单

请用手机（nRF Connect for Mobile）近距离验证板上外设模式，并回传逐步结果：

1. **扫描**：打开 nRF Connect → SCAN → 过滤框输入 `blebridge`。
   - 预期：出现 "blebridge"；条目展开应见 advertised service **0xFFE0**（若还广播 0xAAAA 一并记录）。
   - 若扫描不到 → 板侧广播未生效，转告 host/固件侧排查（对应 §3 假设 1/3/4）。
2. **连接**：点击该条目 CONNECT。记录连接是否成功、GATT 设备名。
3. **服务发现**：展开 `Unknown Service 0xFFE0`，应含 3 特征：
   - `0xFFE1`：属性 Read + Write Without Response
   - `0xFFE2`：属性 Read + Notify
   - （第三个特征按固件实际记录）
   同时确认动态服务 **0xAAAA**（0xAAA1 read+write_wo_resp、0xAAA2 read+notify，特征数 2-5）是否出现。
4. **读 0xFFE1**：点 0xFFE1 的 ↓(Read) → 期望值 ASCII `test-123`（hex `74 65 73 74 2D 31 32 33`）。
5. **写 0xFFE1**：点 0xFFE1 的 ↑(Write) → 输入类型选 Text、值 `from-phone`、写入方式选 **Write without response** → 无报错即成功。
6. **订阅 0xFFE2**：点 0xFFE2 的三道杠(Subscribe) → 状态变 subscribed；**等待 30 s**：当前无 notify 源，收不到数据属**预期**，记录即可。
7. **断开**：DISCONNECT，记录是否有错误。
8. 回传：每步 截图或文字（尤其第 1 步扫描结果与第 4 步读值）。

## 5. 复跑方法

```bash
host/.venv/bin/pip install pyobjc-framework-CoreBluetooth
host/.venv/bin/python /tmp/bridge_central.py   # 板子广播可见时应自动走完 6 步并输出 JSON
```

结果文件：`/tmp/bridge_central_result.json`（结构化）、`/tmp/bridge_central_log.txt`（时间戳日志）。
