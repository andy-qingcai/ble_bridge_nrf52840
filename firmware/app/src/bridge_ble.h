/*
 * bridge_ble.h - BLE central bridge (Task 10): scan / connect / GATT
 * pass-through onto the bridge event channel.
 *
 * Thread model (task-10 brief):
 *   - Command-side functions (the bridge_ble_api_t table) run on the link
 *     RX thread and may block on semaphores with bounded timeouts.
 *   - Every Zephyr BT callback (scan recv, conn, gatt_dm, GATT completions,
 *     notifications) runs on BT-owned threads: it only touches state under
 *     the module lock, posts events through bridge_link_post_event() and
 *     gives semaphores - it never blocks and never touches the physical
 *     layer.
 *   - bt_conn reference discipline: bt_conn_le_create() hands over exactly
 *     one application reference held in `conn`; it is released exactly
 *     once - in the connected(err != 0) path or in disconnected().
 *   - Subscription params and all GATT op params are static (review red
 *     line: the stack keeps pointers into them across the async responses).
 *
 * Known limitation (task(12) final review, accepted): the module-local bare
 * `struct bt_conn *conn` is read/cleared from both the link RX thread and
 * the BT callback threads without a dedicated lock.  This is a data race in
 * the strict C11 sense; correctness rests on the Zephyr BT threading model
 * (all connection callbacks serialize on the BT RX work queue) and the
 * semaphore ordering around api_connect.  Hardening (RCU/lock or conn
 * handle indirection) is deferred to the post-L3 hardening task.
 */

#ifndef BRIDGE_BLE_H
#define BRIDGE_BLE_H

#include "bridge_ble_api.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Bring the BLE central up: bt_enable() + callback registration.  Call
 * once from main() before bridge_link_init() so no command can outrun it.
 *
 * @return 0 on success; the BLE api table then answers
 *         BRIDGE_STATUS_ERR_NOT_READY for every command.
 */
int bridge_ble_init(void);

/** The command surface handed to the dispatcher through the env. */
bridge_ble_api_t *bridge_ble_api(void);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_BLE_H */
