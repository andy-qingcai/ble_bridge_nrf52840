/*
 * bridge_periph.h - BLE peripheral role (FR-H): advertising, the preset
 * 0xFFE0 test service, and host-defined GATT services (CMD.gatt_define).
 *
 * Fully generic BLE server capability - no application-specific behaviour
 * lives here.  Split from bridge_ble.c so the central and peripheral roles
 * stay independently readable; the two modules share the dispatcher api
 * table (bridge_ble.c wires the peripheral entries in).
 *
 * Thread model (mirrors bridge_ble.h):
 *   - Command-side entry points (adv_start/adv_stop/notify/set_char/
 *     gatt_define) run on the link RX thread and never block on BLE
 *     completions (all calls are synchronous into the stack).
 *   - GATT server callbacks (read/write/ccc_changed) run on BT-owned
 *     threads; they only touch slot state under dyn_lock, post events
 *     through bridge_link_post_event() and never block indefinitely.
 *   - The peripheral link is one connection by design (CONFIG_BT_MAX_CONN
 *     = 2: one central + one peripheral).  periph_conn follows the same
 *     documented single-pointer discipline as the central `conn` in
 *     bridge_ble.c (adopted reference released exactly once, in the
 *     disconnected callback).
 */

#ifndef BRIDGE_PERIPH_H
#define BRIDGE_PERIPH_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* uuid16 addressing selectors shared by CMD.notify / CMD.set_char. */
#define BRIDGE_CHAR_ADDR_DEFAULT 0x00U /* preset char (FFE1/FFE2 per cmd) */
#define BRIDGE_CHAR_ADDR_UUID16  0x01U /* address by char uuid16          */

/** Bring the peripheral role up.  Call once, from bridge_ble_init(), after
 * bt_enable() succeeded; on failure the peripheral entries answer
 * BRIDGE_STATUS_ERR_NOT_READY.  Resolves the preset characteristic handles
 * for the EVT.GATTX write events. */
int bridge_periph_init(void);

/**
 * CMD.adv_start: start connectable advertising.
 *
 * @param interval_ms advertising interval in ms; 0xFFFF = firmware default
 *                    (CONFIG_BRIDGE_ADV_INTERVAL_DEFAULT_MS).  Other values
 *                    are clamped to the LE range (20 ms .. 10.24 s).
 * @param name_on     include the complete local name
 *                    (CONFIG_BRIDGE_ADV_NAME) in the scan response.
 * @return status byte (OK; already advertising = idempotent OK).
 */
uint8_t bridge_periph_adv_start(uint16_t interval_ms, bool name_on);

/** CMD.adv_stop: stop advertising (idempotent OK when not advertising). */
uint8_t bridge_periph_adv_stop(void);

/**
 * CMD.notify: push `data` as the notification content of one locally served
 * characteristic and notify every subscribed peer.  The pushed value also
 * becomes the characteristic's readable value.
 *
 * @param mode    BRIDGE_CHAR_ADDR_DEFAULT (0) = preset notify characteristic
 *                (0xFFE2); BRIDGE_CHAR_ADDR_UUID16 (1) = look `uuid16` up in
 *                the host-defined services first, then the preset service.
 * @param uuid16  characteristic uuid16 (mode == UUID16 only)
 * @param data    notification content (<= local ATT MTU - 3 and <= the
 *                characteristic's value capacity)
 * @return status byte: OK; 0x72 bad payload (unknown uuid16 / length /
 *         characteristic cannot notify); 0x73 busy (TX buffers); 0x74 not
 *         ready (no subscribed peer).
 */
uint8_t bridge_periph_notify(uint8_t mode, uint16_t uuid16,
                             const uint8_t *data, uint16_t len);

/**
 * CMD.set_char: update a locally served characteristic's current value
 * (what a peer reads back).  Never touches the air.
 *
 * Same addressing rules as bridge_periph_notify; the default (mode 0)
 * target is the preset write characteristic 0xFFE1.
 */
uint8_t bridge_periph_set_char(uint8_t mode, uint16_t uuid16,
                               const uint8_t *data, uint16_t len);

/**
 * CMD.gatt_define: register (or replace) one host-defined GATT service at
 * runtime.
 *
 * Request payload: [svc_uuid16 2B LE][n_chars 1B] + n_chars x
 * [char_uuid16 2B LE][props 1B][max_len 2B LE]; props bit0 = read,
 * bit1 = write-without-response, bit2 = write-with-response, bit3 = notify.
 * Values live in firmware RAM (zero-initialised); every peer write is
 * stored and echoed to the host as an EVT.GATTX peripheral-role event
 * (subtype 0x01); notify-capable characteristics get a CCCD automatically.
 *
 * On success `body` carries the service declaration handle (2B LE) - the
 * base of the attribute layout documented in docs/protocol-contract.md.
 *
 * Constraints: refused while a peripheral-role link is up (BUSY 0x73 - a
 * peer would see the DB change underneath it); re-defining an svc_uuid16
 * replaces the previous definition (handles move to the DB tail); at most
 * CONFIG_BRIDGE_GATT_DYN_SVCS services of CONFIG_BRIDGE_GATT_DYN_CHARS_MAX
 * characteristics, value bytes per service <= CONFIG_BRIDGE_GATT_DYN_SVC_POOL.
 */
uint8_t bridge_periph_gatt_define(const uint8_t *payload, uint16_t len,
                                  uint8_t *body, uint16_t cap,
                                  uint16_t *body_len);

/** True while a peripheral-role link exists (CMD.disconnect preference). */
bool bridge_periph_active(void);

/** Drop the peripheral-role link (async; EVT.DISC lands via callback). */
uint8_t bridge_periph_disconnect(void);

/** True when the peripheral link's peer address matches (displayed order). */
bool bridge_periph_addr_matches(uint8_t addr_type,
                                const uint8_t addr[6]);

/** Best-effort teardown before CMD.reset reboots: stop advertising + drop
 * the peripheral link.  Host-defined services stay registered in RAM - the
 * reboot clears them. */
void bridge_periph_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_PERIPH_H */
