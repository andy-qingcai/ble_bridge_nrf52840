# blebridge

**基于 nRF52840 的 BLE 全透传/全操控工具：central + 外设双角色，host 全权。**

blebridge 把 nRF52840 DK 变成 host 手里的"BLE 执行器"：固件不含任何应用层专用
逻辑，只做可靠的协议执行；全部智能（状态机、编排、测试编排）在上位机 Python 包
`blebridge`（framing → transport → bridge → CLI / daemon / MCP）。

- **central 角色**：扫描 / 连接 / 服务发现 / GATT 读写 / 订阅 notify / MTU、
  连接参数、PHY 调优 / SMP 配对。uuid16 与原始 handle 双寻址，不依赖发现即可
  操作任意 BLE 外设。
- **外设角色**：host 命令化广播（adv_start/adv_stop，断链自动恢复）、通用测试
  服务（0xFFE0 读/写/notify）、运行时动态 GATT 服务定义（gatt_define）。
- **应用流程全部长在这套透传原语上**：
  - **SUOTA/OTA 固件升级**（Dialog 公开协议，`blebridge ota`）：host 驱动的
    状态机（`blebridge/suota.py`），对固件而言与其它 GATT 应用完全等价；
  - **SUOTA 外设模拟器**（`firmware/suota_sim`）：用外设角色模拟一台标准 SUOTA
    设备（含连接后主动发 ATT_EXCHANGE_MTU_REQ 行为），提供无真机的完整 HIL 闭环。

系统形态：

```
+------------------+   UART (921600 8N1 + RTS/CTS)   +----------------+        BLE         +-------------+
|   上位机 (host)  | <-------------------------------> | nRF52840 桥固件 | <----------------> | BLE 设备     |
| blebridge (Py)   |      帧协议: 0x55 | LEN16 | ...  |  central+外设   |   任意 GATT 服务    | (SUOTA 示例) |
+------------------+                                 +----------------+                    +-------------+
```

## 目录

| 路径 | 内容 |
|---|---|
| `firmware/app` | nRF52840 桥固件（BLE central + 外设双角色 + GATT 透传 + UART 桥） |
| `firmware/suota_sim` | SUOTA 外设模拟器固件（BLE peripheral，标准 SUOTA 行为样例） |
| `firmware/proto` | 共享 C 帧协议编解码库（两侧固件共用，host 端测试亦引用） |
| `host/` | Python 包 `blebridge`：帧协议 / 传输层 / 桥客户端 / SUOTA 应用流程 / daemon / CLI / MCP |
| `tools/` | 环境引导、烧录、HIL 测试、OTA 压测脚本 |
| `docs/` | 需求规格、ADR 决策、协议契约、验证报告 |

## 快速开始

```bash
# 1) 环境引导（NCS 工具链 + python venv）+ host 包安装
./tools/bootstrap-env.sh
cd host && .venv/bin/pip install -e .

# 2) 串口配置一次（之后所有命令零参数）
#    不配置也行：恰一个 USB 串口时自动探测选用；rtscts 自动判定
#    （usbmodem/CDC → 关，usbserial/UART 转接板 → 开）
.venv/bin/blebridge config --port /dev/cu.usbmodem11301 --baud 115200 --no-rtscts
.venv/bin/blebridge config --show    # 查看/验证当前配置与解析结果

# 3) 日常使用：全部零参数
.venv/bin/blebridge info                 # 桥心跳/自检
.venv/bin/blebridge device-info AA:BB:CC:DD:EE:FF --suota
                                         # 设备信息一览（设备名/电量/SUOTA info）
.venv/bin/blebridge ota firmware.img --addr AA:BB:CC:DD:EE:FF
                                         # 一键 OTA：断旧链→连接→dm 就绪轮询→
                                         #   上传(pair+bank1)→设备存活验证
# 4) host 端测试（无需硬件）
.venv/bin/pytest -q

# 5) 固件构建烧录（nRF52840 DK）
cd firmware/app && west build -b nrf52840dk/nrf52840 && west flash
```

更细粒度的子命令（scan/connect/read/write/subscribe/adv/gatt_define/suota/…）
见 `blebridge --help`；串口三参数 `--port/--baud/--rtscts` 永远可显式覆盖解析
结果。

## 已知问题（真机验证沉淀，记录不修）

- WCH-Link SWD 偶发 wedge（`SWD WAIT ACK`，多见于板卡复位/USB 重枚举窗口、连续
  快速 pyocd 会话）：降到 300kHz 重连即恢复——`BRIDGE_PYOCD_FREQ=300000
  tools/build-dfu.sh --flash-only`（详见 tools/build-dfu.sh 头注释）。
- mcumgr 对与活动镜像 hash 相同的槽执行 `image test` 会被拒（`IMG_MGMT_ERR 33`
  IMAGE_SETTING_TEST_TO_ACTIVE_DENIED）：重传同版本镜像需先回退或改版本再传。
