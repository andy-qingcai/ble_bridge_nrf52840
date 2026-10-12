# 外设模式第三轮验证报告（build-fix3：三缺陷修复空口验证 + 通知闭环）

- 日期：2026-09-26（执行于 controller 放行"开跑"之后）
- 执行：BLE 验证 agent（Mac 本机蓝牙 central 角色；未派子 agent）
- 固件：板上 build-fix3（含三修复：影子 CCCD / 动态服务可见性 / 断链自动恢复广播）
- 方法：`host/.venv` + pyobjc-framework-CoreBluetooth 12.2.2；`/tmp/bridge_central_r3.py`
  驻留式 central（双选择器实现继承自 `tools/ble_central_probe.py`，动作文件轮询支撑
  与 host 侧的写→事件比对协调）；原始数据 `/tmp/bridge_r3/{events.jsonl,session.log,resp/}`
- 边界：本 agent 不触碰桥串口（host 侧命令全部由 controller 执行）；未修改仓库代码（本报告除外）

## 0. 前置异常与流程修正（开跑前确认）

1. **串口节点变化**：任务书端口 `/dev/cu.usbmodem11301` 在开跑前已不存在
   （build-fix3 双 CDC 固件枚举为 `usbmodem1201`/`usbmodem1203`，桥协议口/SMP 口
   角色以 ping 帧 `55 00 00 00` 实测为准，开跑时复核）。
2. **HEAD 与任务书不符**：main HEAD=1ed7595 ≠ 任务书 6d54d4e（该 commit 不在历史中）。
   经 controller 确认无影响：验证对象是板上已刷固件，后续提交均为 host/CI 侧。
3. **pyobjc 重装**：host/.venv 被重建后 pyobjc 缺失，已重装
   pyobjc-core/Cocoa/CoreBluetooth 12.2.2（基解释器 /opt/homebrew python@3.14 未变，
   TCC 蓝牙权限随基二进制保留）。
4. **步骤 6 时序修正（固件硬约束，controller 已采纳）**：`bridge_periph.c` gatt_define
   在链路存在时直接拒绝 0x73（"define: busy (link up)"，L841-845）——原任务书
   "重连→gatt_define" 顺序调整为：**断开 → host 在无链路窗口 gatt-define 0xAAAA →
   central 重连发现**。并增补验证项：FFE2 通知闭环（影子 CCCD 修复的空口闭环），
   广播恢复回归移至终态断开后执行。最终版时序七步（controller 定稿）。

## 1. 结论表（最终版时序六项：增补通知闭环，广播恢复回归移至终态）

| # | 清单项（central 视角） | 结果 | 证据 |
|---|---|---|---|
| 1 | 扫描 "blebridge" → 连接 → 发现：FFE0=2 特征 + FFE2 带 CCCD 0x2902 | （执行中） | |
| 2 | 读 0xFFE1 == "test-123" | （执行中） | |
| 3 | 写 0xFFE1 → 桥侧 EVT.GATTX 比对（[uuid16][handle][data]） | （执行中） | |
| 4 | 通知闭环（影子 CCCD 空口验证）：订阅 FFE2 → host notify 41424344 → central 收到并比对 | （执行中） | |
| 5 | host gatt-define 0xAAAA（无链路窗口）→ 重连发现 2 特征 → 读/写 0xAAA1 → EVT.GATTX 比对 | （执行中） | |
| 6 | 终态断开 → 12 s → 自动重回广播（053818b 回归） | （执行中） | |

## 2. 与前两轮三 ❌ 的对照

| 缺陷 | 前两轮表现 | 本轮修复验证 |
|---|---|---|
| 影子 CCCD（c9e5..9a85c53 修复） | CCCD 空口存在、ATT 写接受，但订阅未注册，host notify 回 0x74，空中 0 通知 | （执行中） |
| 动态服务空口不可见（fix r1 0x2800 声明修复） | gatt_define 协议成功但 0xAAAA 发现不到 | （执行中） |
| 断链不恢复广播（053818b） | 断链后广播静默，需手动 adv-start | （执行中） |

## 3. 事件比对

（执行中填充：写 FFE1/AAA1 的空口字节 vs 桥侧 EVT.GATTX [uuid16][handle][data]；
notify 回路 host 推送字节 vs central 收到字节）

## 4. 原始数据索引

- `/tmp/bridge_r3/events.jsonl` — 全部空口事件（时间戳 JSONL）
- `/tmp/bridge_r3/session.log` — 全量日志
- `/tmp/bridge_r3/resp/` — 协调命令逐条应答
- 桥侧事件捕获：controller 提供（EVT.GATTX 原文）

## 5. 遗留与建议

（执行后填充）
