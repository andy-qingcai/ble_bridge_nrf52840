# ADR-005: 测试策略 —— 四层金字塔，硬件不是开发的依赖

日期：2026-09-24 ｜ 状态：已接受

| 层 | 对象 | 手段 | 硬件 |
|---|---|---|---|
| L0 | host framing/SUOTA/image 纯逻辑 | pytest + 模糊测试（随机字节流重同步、黄金帧向量） | 无 |
| L1 | host 端到端 | FakeTransport + 假 BLE 行为模型（FakeClient 注入、故障注入、字节级校验） | 无 |
| L2 | C 帧编解码固件库 | host cc 编译 proto 库 → ctypes 桥 → 同一黄金向量集（跨语言契约测试） | 无 |
| L3 | 全链路 HIL | tools/hil_test.py：JLink 刷 2×DK → 串口驱动完整 OTA → 断言吞吐/完整性 | 2×nRF52840 DK |

原则：
1. L0/L1/L2 必须做到"零硬件绿灯"——开发主体（协议、状态机、host 工具链）不因等板卡阻塞。
2. 黄金向量集单一定义（host/tests/golden_frames.json），L0 与 L2 共用，防双实现漂移。
3. SUOTA 状态机的行为测试矢量来自协议规格（AN-B-022）与参考设备实测捕获
   （连接后主动 MTU_REQ、SAME_IMAGE、STARTED 被 SMP 吃掉需回读兜底等），
   逐条转化为 L1 用例。
4. L3 同时测量吞吐（对齐参考链路实测 14.8KB/s 基线）与零损坏（XOR-CRC 校验）。
