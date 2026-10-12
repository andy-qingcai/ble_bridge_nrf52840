/*
 * bridge_ble_api.h - function-pointer surface from the dispatcher to the
 * BLE central implementation (Task 10).
 *
 * Pure C11 (no Zephyr) so bridge_dispatch.c stays host-testable:
 *   - production (bridge_ble.c) provides the real table;
 *   - the firmware dispatcher receives it through bridge_dispatch_env_t;
 *   - a NULL table (host unit tests) keeps the Task 9 behaviour: every BLE
 *     command answers STATUS_ERR_NOT_READY.
 *
 * All functions are called on the link RX thread (the command dispatcher's
 * context) and may block internally on semaphores - the BLE callbacks they
 * wait for run on BT-owned threads and only use k_sem_give / atomics /
 * bridge_link_post_event().  Return values are bridge_status_t bytes
 * (bridge_proto.h): 0x00 = OK, anything else is reported to the host
 * verbatim as the response status byte.
 */

#ifndef BRIDGE_BLE_API_H
#define BRIDGE_BLE_API_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct bridge_ble_api {
    /** CMD.scan_start, payload u16 LE duration_ms (0 = firmware default). */
    uint8_t (*scan_start)(uint16_t duration_ms);

    /** CMD.scan_stop (no payload). */
    uint8_t (*scan_stop)(void);

    /**
     * CMD.connect, raw payload:
     *   1B mode (0 public addr / 1 random addr / 2 name) +
     *   mode 0/1: 6B address in displayed order ("AA:BB:.." -> AA BB ..)
     *   mode 2:   device name bytes (UTF-8)
     * Blocks until connected or timeout_ms elapsed.  On success the body
     * carries the resolved peer: 1B addr_type + 6B addr (displayed order).
     */
    uint8_t (*connect)(const uint8_t *payload, uint16_t len,
                       uint32_t timeout_ms, uint8_t *body, uint16_t cap,
                       uint16_t *body_len);

    /**
     * CMD.disconnect:
     *   - empty payload: drop the peripheral-role link when one exists
     *     (FR-H preference), otherwise the central link (legacy meaning);
     *   - 1B addr_type + 6B addr (displayed order): drop whichever link
     *     matches that peer address.
     * Async: the response only reflects submission; EVT.DISC arrives via
     * callback with the role byte telling the links apart.
     */
    uint8_t (*disconnect)(const uint8_t *payload, uint16_t len);

    /** CMD.mtu (no payload): current negotiated ATT MTU (23 before/between). */
    uint8_t (*mtu_get)(uint16_t *out_mtu);

    /**
     * CMD.gatt_read, uuid16 addressing: payload u16 LE uuid16; the
     * characteristic must be in the map (host-triggered discovery).
     */
    uint8_t (*gatt_read)(uint16_t uuid16, uint8_t *data, uint16_t cap,
                         uint16_t *data_len);

    /** CMD.gatt_read handle addressing: raw attribute handle, no map. */
    uint8_t (*gatt_read_handle)(uint16_t handle, uint8_t *data, uint16_t cap,
                                uint16_t *data_len);

    /** CMD.gatt_write, uuid16 addressing (see gatt_read). */
    uint8_t (*gatt_write)(uint8_t wtype, uint16_t uuid16,
                          const uint8_t *data, uint16_t len);

    /** CMD.gatt_write handle addressing: wtype + raw handle + value. */
    uint8_t (*gatt_write_handle)(uint8_t wtype, uint16_t handle,
                                 const uint8_t *data, uint16_t len);

    /** CMD.subscribe / CMD.unsubscribe, uuid16 addressing. */
    uint8_t (*subscribe)(uint16_t uuid16);
    uint8_t (*unsubscribe)(uint16_t uuid16);

    /**
     * CMD.subscribe handle addressing: subscribe the characteristic value
     * at `value_handle` by writing its CCCD at `cccd_handle` (notification
     * value 0x0001; no property check is possible without the map).
     */
    uint8_t (*subscribe_handle)(uint16_t value_handle, uint16_t ccc_handle);

    /** CMD.unsubscribe handle addressing: value handle of the subscription. */
    uint8_t (*unsubscribe_handle)(uint16_t value_handle);

    /**
     * CMD.disc_svc (generic bridge): run bt_gatt_dm for the 16-bit service
     * UUID `svc_uuid16` NOW and merge every discovered characteristic into
     * the map (uuid16 -> value/CCC handles, 128-bit UUIDs keyed by their
     * SIG prefix).  `svc_uuid16 == 0` re-discovers the last successfully
     * discovered service (NOT_READY when there is none yet).  On success
     * *nchars carries the number of characteristics found in THIS run.
     * The firmware never discovers or disconnects on its own.
     */
    uint8_t (*disc_svc)(uint16_t svc_uuid16, uint16_t *nchars);

    /**
     * CMD.pair (FR-B5): run SMP pairing on the current link and block until
     * the procedure settles (security level >= L2) or fails/times out.
     * On success the body carries 2 bytes: [0] = 1 when the link is
     * encrypted (bt_conn_enc_key_size() > 0), [1] = the resulting security
     * level (bt_conn_get_security()).  Failure answers
     * BRIDGE_STATUS_ERR_PAIR_FAILED.
     */
    uint8_t (*pair)(uint8_t *body, uint16_t cap, uint16_t *body_len);

    /**
     * CMD.conn_update: LE connection parameter update (1.25 ms interval /
     * 10 ms timeout units, BT_LE_CONN_PARAM encoding).  Async: the response
     * only reflects the request submission; the settled parameters are
     * reported on EVT.DEBUG by le_param_updated.
     */
    uint8_t (*conn_update)(uint16_t interval_min, uint16_t interval_max,
                           uint16_t latency, uint16_t timeout);

    /**
     * CMD.phy: LE PHY preference, 1 = 1M, 2 = 2M (both directions).
     * Async: the result is reported on EVT.DEBUG by le_phy_updated.
     */
    uint8_t (*phy_update)(uint8_t phy);

    /**
     * CMD.adv_start (FR-H): start connectable advertising for the locally
     * served GATT services.  `interval_ms` in ms, 0xFFFF = firmware
     * default; `name_on` includes the complete local name.  Already
     * advertising = idempotent OK.
     */
    uint8_t (*adv_start)(uint16_t interval_ms, uint8_t name_on);

    /** CMD.adv_stop (FR-H): stop advertising (idempotent). */
    uint8_t (*adv_stop)(void);

    /**
     * CMD.notify (FR-H): push `data` as notification content of a locally
     * served characteristic and notify subscribed peers.  `mode` 0 = the
     * preset notify characteristic, 1 = address by char `uuid16`.
     * NOT_READY (0x74) when no peer is subscribed.
     */
    uint8_t (*notify)(uint8_t mode, uint16_t uuid16, const uint8_t *data,
                      uint16_t len);

    /**
     * CMD.set_char (FR-H): update a locally served characteristic's value
     * (what peers read back); never touches the air.  Same `mode` rules as
     * notify; mode 0 targets the preset write characteristic.
     */
    uint8_t (*set_char)(uint8_t mode, uint16_t uuid16, const uint8_t *data,
                        uint16_t len);

    /**
     * CMD.gatt_define (FR-H5): register or replace one host-defined GATT
     * service; the raw payload is parsed by the peripheral module and the
     * success body carries the service base handle (2B LE).  BUSY while a
     * peripheral-role link is up.
     */
    uint8_t (*gatt_define)(const uint8_t *payload, uint16_t len,
                           uint8_t *body, uint16_t cap, uint16_t *body_len);

    /** Best-effort teardown before CMD.reset reboots (link reset hook). */
    void (*shutdown)(void);
} bridge_ble_api_t;

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_BLE_API_H */
