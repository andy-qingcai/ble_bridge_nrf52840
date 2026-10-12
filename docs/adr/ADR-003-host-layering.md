# ADR-003: 上位机分层 —— 继承三层传输抽象 + daemon 骨架

日期：2026-09-24 ｜ 状态：已接受

## 决策

```
framing.py    帧编解码 + StreamParser(重同步)          ← 纯函数，模糊测试
transport.py  Transport 接口: SerialTransport / FakeTransport
bridge.py     BridgeHost: 命令 req/resp + 事件环(seq) + drain + wait_for
suota.py      SUOTA 协议状态机（只依赖 bridge 语义方法）   ← 换后端零改动
image.py      镜像头解析(DA1469x/DA1458x 族) + XOR 载荷
daemon.py     TCP JSON 常驻守护（15 命令对等 + suota_* 族）
cli.py        命令行
mcp_server.py MCP 工具服务器
```

分层理由：`Transport → Host(bridge) → 应用流程` 的抽象与 FakeTransport
故障注入 + 字节级校验的测试模式已被本仓库 host 测试矩阵验证有效。
daemon 的串口断开自动重连（close→sleep→reopen→原命令重试一次）沿用同一模式。

## 变更点

1. 传输后端从 USB-CDC/bleak 换成 UART 帧协议桥（BLE 细节全部下沉进固件）——
   host 端 GATT 语义命令（read/write/subscribe）变为"透传到固件 GATT client"。
2. host 不直接触碰系统蓝牙栈（CoreBluetooth 等），macOS 蓝牙守护进程的
   干扰类问题被架构整体消除。
3. Python venv 固定 3.12（NCS 工具链兼容性），包管理用 pyproject+pip。
