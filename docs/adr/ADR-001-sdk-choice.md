# ADR-001: SDK 选型 —— nRF Connect SDK (Zephyr)，否决 nRF5 SDK

日期：2026-09-24 ｜ 状态：已接受

## 背景

选型时需保证：central 角色能**正确处理对端（尤其外设角色设备）主动发起的
ATT_EXCHANGE_MTU_REQ**（自动回复 MTU_RSP），且 BLE host 栈源码可审计、可断点、
可配置。候选为 Nordic 两代 SDK。

## 决策

采用 **nRF Connect SDK (NCS) + Zephyr + SoftDevice Controller**：
1. Zephyr BLE host 的 ATT server 对**对端主动发起的 EXCHANGE_MTU_REQ 自动回复**
   `att_mtu_req()` → `bt_att_rsp_err`/MTU_RSP 路径（`subsys/bluetooth/att.c`，受
   `CONFIG_BT_L2CAP_RX_MTU` 等配置控制），源码可审计可断点 —— 从结构上保证该
   路径正确。
2. nRF5 SDK（S140）已进入维护终止（Nordic 官方建议新设计一律用 NCS），否决。
3. SoftDevice Controller 闭源但 Nordic 主力维护、central 角色成熟；host 层全开源。

## 后果

- 工具链引入 west/cmake/ninja/Zephyr SDK（macOS arm64 均官方支持）。
- 团队/后续 agent 需熟悉 Zephyr devicetree + prj.conf 模型（一次性成本）。
- 需锁定 NCS 版本（NCS v3.4.1 / Zephyr 4.4.2 / Zephyr SDK 1.0.1，见
  tools/bootstrap-env.sh）。
