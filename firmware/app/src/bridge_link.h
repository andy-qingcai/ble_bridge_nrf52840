/*
 * bridge_link.h - frame link: RX pump, single TX owner, event channel
 * (Task 9).
 *
 * Thread model (task-9 brief):
 *   - RX thread   : blocking bridge_phy_read() -> byte-by-byte
 *                   bridge_parser_feed() -> complete frame ->
 *                   bridge_dispatch_handle() -> response frame -> TX queue.
 *   - TX thread   : the ONLY sender; drains a k_msgq of encoded frames.
 *   - EVENT thread: drains the ISR-safe event k_msgq, applies the credit /
 *                   ring-seq decisions (bridge_evt_admit) and forwards
 *                   encoded event frames to the TX queue.
 *
 * Backpressure window (Task 10 fix round 1 note): the BLE command handlers
 * behind bridge_dispatch_env_t->ble run ON the RX thread and block on BT
 * completions with bounded waits - worst case roughly 5 s (CMD.connect) +
 * 1 s cancel-grace, 5 s per GATT read/write/subscribe, 3 s discovery wait.
 * During those windows new commands are not dequeued (the host's own
 * request timeout must bracket them); events and the TX queue keep
 * flowing independently on their own threads.
 *
 * BLE threads (Task 10) must only use bridge_link_post_event() - never the
 * physical layer or the TX queue directly.
 */

#ifndef BRIDGE_LINK_H
#define BRIDGE_LINK_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Initialise the dispatcher, queues and threads.  bridge_phy_init() must
 * have succeeded first.
 *
 * @return 0 on success, negative errno on failure
 */
int bridge_link_init(void);

/**
 * Queue one event for emission (ISR-safe, never blocks).
 *
 * EVT.DEBUG events are silently dropped right here when the current log
 * level is 0 (off); other event bytes pass through.  Credit accounting and
 * ring-sequence assignment happen later on the event thread.
 *
 * @param evt      event command byte (e.g. BRIDGE_EVT_DEBUG 0x8E)
 * @param payload  payload bytes (NULL when len == 0)
 * @param len      payload length, <= CONFIG_BRIDGE_EVENT_PAYLOAD_MAX
 * @return         0 queued; -EMSGSIZE payload too large; -ENOSPC queue full
 */
int bridge_link_post_event(uint8_t evt, const void *payload, uint16_t len);

/**
 * Register a best-effort teardown hook run on the RX thread just before
 * CMD.reset reboots (Task 10: bridge_ble disconnect).  Call once during
 * init; NULL clears it.
 */
void bridge_link_set_reset_hook(void (*hook)(void));

/**
 * Health snapshot for the image self-confirm gate (task-dfu-a).  Returns
 * the same surfaces CMD.info reports, evaluated by the caller with
 * bridge_confirm_ready().
 *
 * @param init_ok         set true when phy+link init completed cleanly
 * @param crc_errors      CRC failure counter since boot
 * @param tx_timeouts     TX write-timeout counter since boot
 * @param events_dropped  credit-exhaustion drop counter since boot
 */
void bridge_link_confirm_snapshot(bool *init_ok, uint32_t *crc_errors,
                                  uint32_t *tx_timeouts,
                                  uint32_t *events_dropped);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_LINK_H */
