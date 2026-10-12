# firmware/proto - 桥帧协议 C 库（协议 v2，ADR-002）

`bridge_proto.h/.c` 是 UART 桥协议 v2 的 C 实现，被两个 Zephyr 固件
（`firmware/app`、`firmware/suota_sim`）共用，并作为**协议常量与帧语义的单一
定义源**（host 侧由 L2 契约测试对照锁定）。

## 跨语言契约（L2，ADR-005）

本库与 `host/src/blebridge/framing.py` 必须**逐字节、逐计数**一致：

- 编码输出：`bridge_encode()` == `encode_frame()`（黄金向量
  `host/tests/golden_frames.json` 逐条锁定）。
- 解析器：`bridge_parser_feed()` 复刻 `StreamParser` 状态机（WAIT_SOF →
  GOT_SOF → READ_LEN → READ_HEADER_REST → READ_PAYLOAD → CHECK_CRC），含
  LEN>512 / CRC 错的滑动重同步。
- 四个统计计数 `frames_ok / crc_errors / resyncs / bytes_dropped` 语义与
  Python 侧完全相同（WAIT_SOF 态非 SOF 字节也计入 bytes_dropped；坏 LEN
  丢 3 字节；坏 CRC 丢 5+LEN+2 字节并 `resyncs+1, crc_errors+1`）。
- 语义权威：`host/src/blebridge/framing.py` 源码；改任何一侧必须先跑
  `host/tests/test_proto_c.py`（含 ≥2000 条随机流差分模糊）。

## API 一览

| 函数 | 说明 |
|---|---|
| `bridge_crc16(data, len)` | CRC16-CCITT-FALSE（poly 0x1021, init 0xFFFF），锚点 `crc16("123456789",9)==0x29B1` |
| `bridge_encode(cmd, seq, payload, len, out, cap)` | 成功返回帧总长；cap 不足返回 `-(所需长度)`；`len>512` 返回 `BRIDGE_ERR_TOO_LONG` |
| `bridge_parser_init(p)` | 清状态与四个计数器 |
| `bridge_parser_feed(p, byte, out)` | 喂 1 字节：`1`=解出完整帧（`out->payload` 指向内部缓冲，下次 feed 前有效）；`0`=未完成；`-1`=该字节触发重同步丢弃 |

## 约束

- C11，仅依赖 `<stdint.h>`：零 libc（不用 memcpy/strlen）、零动态内存。
  `bridge_parser_t` 约 540 B，可放栈或静态区。
- 编译干净：`-Wall -Wextra -Werror -std=c11`。
- `bridge_parser_t` 字段布局由 `host/tests/conftest.py` 的 ctypes 镜像依赖，
  有 `_Static_assert` 守护——不要改动字段顺序。

## 构建

独立构建（host 测试亦可直接 `cc -shared` 编译单文件）：

```sh
cmake -S . -B build [-DBRIDGE_PROTO_SHARED=ON]
cmake --build build
```

Zephyr 固件集成（app 的 `CMakeLists.txt`）：

```cmake
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../proto proto_build)
target_link_libraries(app PUBLIC bridge_proto)
```
