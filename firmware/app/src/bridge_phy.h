/*
 * bridge_phy.h - physical layer abstraction (Task 9).
 *
 * Two compile-time backends selected by CONFIG_BRIDGE_PHY (string Kconfig,
 * mapped to BRIDGE_PHY_MODE_* by CMakeLists.txt):
 *   - "cdc"   (primary, ADR-002 revision 1): nRF52840 native USB CDC ACM
 *             on zephyr_udc0 (new USB device stack).  Bit rate is
 *             negotiated by USB; DTR is waited for with a bounded timeout
 *             so the bridge also comes up with no host attached.
 *   - "uart0" (backup): DK UART0, 921600 8N1 + RTS/CTS (devicetree).
 *
 * Both backends speak the Zephyr serial API and hide it behind a byte-pipe
 * interface: one ISR feeds an RX ring buffer (and the RX thread drains it),
 * while writes go straight out through the driver's polling path (the
 * interrupt-driven TX path corrupts multi-chunk frames on nRF UARTE).
 * Single-writer contract: bridge_phy_write() must be called from
 * one thread only (the link's TX thread).  bridge_phy_read() is likewise
 * single-consumer (the link's RX thread).
 */

#ifndef BRIDGE_PHY_H
#define BRIDGE_PHY_H

#include <stddef.h>
#include <stdint.h>

#include <zephyr/kernel.h>

#ifdef __cplusplus
extern "C" {
#endif

enum bridge_phy_kind {
    BRIDGE_PHY_KIND_CDC = 0,
    BRIDGE_PHY_KIND_UART0,
};

/**
 * Bring the physical layer up (USB stack + CDC class, or UART0).
 *
 * @return 0 on success, negative errno on failure
 */
int bridge_phy_init(void);

/** Name reported by CMD.info ("cdc" or "uart0"). */
const char *bridge_phy_name(void);

/** Which backend was compiled in. */
enum bridge_phy_kind bridge_phy_kind(void);

/**
 * Read a chunk of received bytes (blocks up to `timeout`).
 *
 * @param buf      destination buffer (not NULL)
 * @param cap      capacity of buf (> 0)
 * @param timeout  max wait for at least one byte
 * @param out_len  receives the number of bytes read, 1..cap (not NULL)
 * @return         0 on success; -EAGAIN on timeout; negative errno else
 */
int bridge_phy_read(uint8_t *buf, size_t cap, k_timeout_t timeout,
                    size_t *out_len);

/**
 * Write `len` bytes (single writer: link TX thread).
 * UART: 每字节忙等发到线上才前进,全程占用调用线程;CDC: 入驱动 ring 即返回,
 * ring 满时该字节被驱动静默丢弃(poll_out 语义,逐后端不同)。
 *
 * @param buf      bytes to send (not NULL)
 * @param len      byte count (> 0)
 * @param timeout  max total wait
 * @return         0 on success; -EAGAIN/-ETIMEDOUT on timeout; else errno
 */
int bridge_phy_write(const uint8_t *buf, size_t len, k_timeout_t timeout);

/**
 * Bytes dropped by the ISR because the RX ring was full (the RX thread did
 * not keep up).  Diagnostics only; a parser resync recovers the stream.
 */
uint32_t bridge_phy_rx_overflows(void);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_PHY_H */
