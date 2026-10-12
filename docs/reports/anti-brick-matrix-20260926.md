# 自 OTA 六场景防变砖矩阵实测报告（MCUboot 双槽落地终验）

- 日期：2026-09-26（真机执行 2026-10-11 会话）
- 仓库：本仓库 @ `51ca62d`（main）
- 硬件：nRF52840DK + WCH-Link（SWD 独占）+ 板上双 CDC（桥协议口 / SMP 口）
- 上游依据：`docs/reports/dfu-usbcdc-eval-20260926.md`（A1+B 方案与核销表）、
  `task-dfu-fixes-report.md`（D1/D3 修复）
- 结论速览：**6+1 场景全部 PASS，防变砖闭环成立**；另获 4 项新发现（F1-F4，
  其中 F2 签名钥配置缺陷与 F3 swap 期 NVMC 卡死需跟进）。

---

## 0. 现场与口径

- 板上基线：slot0 = `build-cdc-prod` v1（imgtool 头 0.1.0+0，桥 ping `blebridge0.1.0`，
  hash `680f7f7a`）confirmed/active；slot1 = 遗留旧 v1（`603d05c4`，0.0.0+0）。
- **保命镜像修正**：任务书指定 `build-dfu/merged hex`，实测其内容（0.0.0+0，CI 钥签）
  与板上现役镜像不一致（哈希比对定案：板上 slot0 = `build-cdc-prod`）。本矩阵
  保命镜像改用 `build-cdc-prod/merged_nrf52840dk_nrf52840.hex`（忠实还原板态），
  `build-dfu` 整 hex 保留为次级兜底。
- 签名口径（**F2，重要**）：`build-cdc-prod` 是裸 `west build`（未带
  `SB_CONFIG_BOOT_SIGNATURE_KEY_FILE` 覆盖），bootloader 与 app 均落 MCUboot 树
  测试钥 `root-ec-p256.pem`——恰是 `tools/build-dfu.sh` 头注释警告的回退陷阱
  （公钥指纹比对：树钥 `e30466f6…` ≠ CI 钥 `2cc76584…`）。因此本矩阵所有上传
  载荷（v-crash/v2m）都按**板上 bootloader 实际信任的树钥**签名；CI 钥签的
  `dfu_application.zip`（build-dfu / build-dfu-v2 / vfix1 / vfix2）在现板上
  swap 时必然被拒。
- 纪律执行：每场景前整片读出备份（`/tmp/matrix_backup/` 共 8 份，pre_matrix～
  post_matrix）；每场景后 WCH-Link 整片重刷保命镜像（每次输出
  `programmed 327680 bytes (80 pages)` @ ~14 kB/s）+ 桥 ping 冒烟后才进下一场景。
- crash 变体源码改动（main.c 崩溃注入 + CMakeLists 版本号）**构建后即
  `git checkout` 还原，未提交**；工作树 firmware/ 无残留。

## 1. 六场景结果表

| 场景 | 构造 | 断言 | 实测 | 结局 |
| --- | --- | --- | --- | --- |
| S1 坏签名镜像 | 抛掷密钥（imgtool keygen ecdsa-p256）签 vfix2 同载荷（头 0.2.0+0，hash `1a4b0816`）；imgtool verify：真钥 FAIL/抛掷钥 PASS | 上传成功；test+复位后 MCUboot 拒签、slot1 作废、slot0 v1 照常启动 | 上传 100%（259,775 B/19.5s，MCUmgr 确实不验签）；SMP 复位后 v1 继续运行，slot0 仍 confirmed 无损；**slot1 被 MCUboot 整槽擦除**（img list 消失，flash 读 0x82000=FF；loader.c 函数注释原文："If the image in the secondary slot is invalid, it is erased"） | **PASS**（附 F1：拒签后首启 USB 迟滞，见 §3） |
| S2 上传中断电·写入中 | smpclient 上传 v2 zip，offset=103945（40%）时 SIGKILL 自身（rc=137） | slot0 v1 照常（swap 未触发）；slot1 半镜像不影响 | kill 后桥持续应答（uptime 连续未复位）；SMP 复位 → v1 2.9s 回归（boot 速度=无 swap 尝试）；img list 无 slot1 条目（半镜像无效） | **PASS** |
| S3 上传中断电·擦除中 | 同上，kill 在 ~2KB（<1%，progressive erase 阶段） | 同 S2 | kill 后桥持续应答（未复位）；SMP 复位 → MCUboot 正常选 slot0 启动（SWD 取证 app 在跑）；本 boot 遭 F1（USB 未枚举），探针再复位一次即恢复 | **PASS** |
| S4 启动即崩不 confirm | 临时补丁：main() 起崩溃线程 3s 后 `k_panic()` + 覆写 `k_sys_fatal_error_handler` 冷复位（生产等价物=WDT）；版本 0.2.1-crash；树钥签（hash `6714aa9d`）。测后还原未提交 | 崩溃可观测 → MCUboot 自动 revert 回 v1；记录重试次数 | 时间线（复位=0）：t+27.4s `blebridge0.2.1-crash` ping（uptime 1s，**崩溃镜像确实运行**）→ 3s 崩溃冷复位 → t+57.0s `blebridge0.1.0`（uptime 1s，revert 完成）→ t+71s v1 稳定。**重试次数 N=1**（MCUboot 无重试计数，一次试跑即在下一次复位 revert，符合设计）；revert 后 slot1 的 v-crash 完整保留 | **PASS** |
| S5 忘 confirm（好镜像） | v2m = 0.2.0-dfu-test、树钥签（hash `fea2bb3f`）；test+复位，v2 ping（uptime 1s）时立刻再复位（"忘 confirm"） | 不确认即回退；再上传+confirm 流程恢复 v2 confirmed | Phase1：t+27.3s v2 ping → 即刻复位#2 → t+55s v1 回归 → t+69s 稳定（**revert 实锤**）。Phase2：重传 v2+test+复位 → t+27.7s v2 起跑 → 自确认窗过后 img list：slot0=v2 **confirmed=True**。（首试撞上 F3 见 §3，重刷后重做通过） | **PASS** |
| S6 应用全灭走 bootloader | CMD.enter_dfu(0x4B) → bootloader 串口恢复（1201 变 VID 0x1915:0x8523 "blebridge-boot"）；pyocd 扇区擦 0x14000 起 236 扇区（双槽+settings，**mcuboot 保留**，擦后读回验证 0x0=SP/复位向量在位）；探针复位 | 无应用时 MCUboot 停留 bootloader 串口恢复模式（CDC 枚举+协议应答）；恢复后桥复活 | 断言1：复位后 CDC 仅 0x1915:0x8523，无应用 CDC（**PASS**）。断言2：bootloader 口 SMP 应答（ImageStatesRead 空表；boot_serial.c 分发仅 STATE 读/UPLOAD/OS reset，ERASE=ENOTSUP）。**加验**：boot-serial 通道上传 v2（259,768 B/17s）→ SMP reset → 2s 后 v2 双 CDC 起跑且随后 confirmed——**双槽全灭不经 SWD 也可救活** | **PASS**（超预期：串口救援全闭环） |
| S7 notify 并发回归 | adv_start(0x46) → Mac central（ble_central_probe.py --subscribe --notify-wait 10）+ 主机并发 CMD.notify(0x48)×3 | 发现/连接/订阅/notify 无回归 | 3/3 发现轮 PASS（FFE0/FFE1/FFE2，0.39s/0.8ms/0.8ms）；CCCD 订阅 True；**3/3 notify 收到**（hex 41/42/43+"matrix-s7"），期间桥协议口并发正常 | **PASS** |

## 2. 防变砖核销表·最终版（对照 eval §4 逐项销项）

| 威胁 | eval 预期 | 实测结局 | 证据 |
| --- | --- | --- | --- |
| 坏镜像（签名/哈希不过） | 不切换，旧镜像继续跑 | ✅ 拒签不 swap；**且 MCUboot 主动整槽擦除坏镜像**（防重试，loader.c 设计） | S1 |
| 半程断电·上传中 | slot1 半成品被忽略 | ✅ 写入期/擦除期两个 kill 点均无影响 | S2/S3 |
| 半程断电·swap 进行中 | journal 断电安全 | ⚠️ **受控用例未做**；间接覆盖：F3 事故中探针复位两次打断 swap，journal 恢复后正确完成（S5） | F3 |
| 新镜像启动即崩 | 自动回退 | ✅ 崩溃被空口观测到，N=1 次试跑后 revert，71s 恢复 | S4 |
| confirm 超时/忘 confirm | 下次复位回退 | ✅ v2 uptime 1s 时复位即回退；自确认语义正常 | S5 |
| bootloader 自身损坏 | 变砖（唯一未消项），需 SWD | ⛔ 未测（矩阵不动 mcuboot 分区）；SWD 重救路径=整片重刷保命镜像，本矩阵每场景演练了 6 次 | 恢复纪律 |
| 应用全灭/全片脏 | 停 bootloader 串口恢复可再救 | ✅✅ 停留串口恢复 + **纯串口上传救活**（259,768B/17s → 复位 2s 起跑 → confirmed） | S6 |

## 3. 新发现（按严重度）

- **F3（高，跟进修复）swap 期 NVMC 写循环卡死，~9 分钟后自愈**：S5 首试中，SMP
  复位进入 swap 后板子 5 分钟+无任何 CDC/应答；SWD 取证核在 MCUboot 空间
  `nrf_nvmc_ready_check`/`nvmc_word_write`（nrfx_nvmc.c:198）打转，两槽 trailer
  同时全 FF（journal 擦除后未写回态），USBPULLUP=0。~9 分钟后自行解开并正确完成
  swap（v2 confirmed）。本地 errata 库无 NVMC 条目（INDEX 明令勿臆造），根因未定
  （嫌疑：复位/NVMC 忙交互）。运维口径：**swap 期偶发最长 ~10min 假死，勿在其间
  强救**；host CLI 的 swap 超时预算需 ≥10min 兜底。
- **F1（中）复位后首启 USB 假死，一发复位即恢复**：3/10 次左右 SMP 复位后的
  boot 出现无 CDC，两种形态：(a) USBPULLUP=1 但 macOS 侧 ~4min 不出 /dev 节点
  （S1，纯主机侧迟滞）；(b) app 泊车 bring-up 失败循环、USBPULLUP=0（S3，
  bridge_phy_init 在 USB 前失败——泊车正是 main.c 的防呆设计）。与防变砖正交
  （应用都活着），但会让"升级失败"误判成变砖；host CLI 应内建"30s 无 ping →
  再复位一次"的熔断。
- **F2（高，发布前必修）现役保命镜像用了 MCUboot 树测试钥**：见 §0。后果：
  仓库内全部 CI 钥签载荷在现板上不可安装；部署链签名公信力为公开测试钥。
  修法：保命镜像与部署镜像一律经 `tools/build-dfu.sh`（显式 CI 钥）重建+整刷。
- **F4（低，观测）swap 吞吐与时长**：260KB 全量对倒实测 26–28s（eval 估算
  5–15s 偏乐观）；"test+复位到 revert 稳定"全程 69–71s。boot-serial 上传吞吐
  ~15.3 kB/s，与应用内 SMP 上传（~13.3 kB/s）同量级。

## 4. MCUboot 行为观察（汇总）

- swap-using-move 语义完整：test→崩溃/未确认复位→revert；revert 保留 slot1
  新镜像（可再 test 转正，D4 的同 hash 防呆在"active==目标"时仍生效）。
- 无内建重试计数：unconfirmed test 镜像一次复位即 revert（N=1）；NCS 的
  `CONFIG_NCS_MCUBOOT_IMG_VALIDATE_ATTEMPT_COUNT=1` 未体现多次试跑。
- 无效 secondary 在 boot 时被主动擦除（防反复验签耗时/防重试坏镜像）。
- boot_serial 能力面：IMG STATE 读 + UPLOAD + OS reset（SMP v2 帧兼容
  smpclient）；MCUMgr 参数读 ENOTSUP、STATE 写与 ERASE ENOTSUP——串口救援
  只能"灌镜像"，不能"标 test"；空 primary 时上传镜像直接被换入运行并转正。
- 每次整片重刷 merged 后 slot0 以 unconfirmed test 态起跑，应用自确认线程
  （≥10s + 健康快照）在 ~20–34s 内转正——重刷后桥"立即可用、稍后转正"。

## 5. 产物与证据

- 整片备份 ×8：`/tmp/matrix_backup/chip_{pre_matrix,pre_S1..pre_S6,post_matrix}_*.bin`
- 矩阵脚本：`/tmp/matrix_work/`（m_util.py 动态口识别/D1 防假成功断言/D2 降频重试；
  smp_cmd.py list/upload+kill/test/confirm/reset/erase）
- S7 空口日志：`/tmp/matrix_work/s7_probe.log`、`s7_probe2.log`
- crash 变体与 v2m 构建目录：`build-dfu-vcrash/`、`build-dfu-v2m/`（源码改动已还原）
