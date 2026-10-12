/*
 * bridge_ble_pure.h - pure logic of the BLE central bridge (Task 10):
 * scan cache, advertisement parser, GATT character map and the event
 * payload builders shared with the wire format of host/src/blebridge/daemon.py.
 *
 * Pure C11 (no Zephyr, no libc beyond the standard headers) so it compiles
 * standalone for host-side unit tests:
 *   cc -std=c11 -Wall -Wextra -I firmware/proto \
 *      -c firmware/app/src/bridge_ble_pure.c
 *
 * All wire formats follow daemon.py verbatim:
 *   EVT.SCAN  (0x20): [0x00 subtype][1B addr_type][6B addr displayed]
 *                     [1B rssi i8][adv bytes]           (test_daemon.py)
 *   EVT.CONN  (0x21): [0x00 subtype][1B role][1B addr_type][6B addr
 *                     displayed]  (FR-H: role 0 = central, 1 = peripheral)
 *   EVT.DISC  (0x22): [0x00 subtype][1B role][1B reason HCI]
 *   EVT.GATTX (0x23): [0x00 subtype][u16 LE uuid16][data]  (subtype 0x00 =
 *                     notification from a central-role peer; brief-pinned)
 *                     [0x01 subtype][u16 LE uuid16][u16 LE value handle]
 *                     [data]  (subtype 0x01 = FR-H peripheral-role write to
 *                     a locally served characteristic; protocol contract §2)
 *   EVT.MTU   (0x24): [0x00 subtype][u16 LE mtu]      (test_daemon.py)
 */

#ifndef BRIDGE_BLE_PURE_H
#define BRIDGE_BLE_PURE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Sizes: production takes them from Kconfig (autoconf.h is visible to all
 * app sources); the host-standalone build falls back to the defaults. */
#ifdef CONFIG_BRIDGE_GATT_CHAR_MAX
#define BRIDGE_BLE_CHAR_MAX CONFIG_BRIDGE_GATT_CHAR_MAX
#else
#define BRIDGE_BLE_CHAR_MAX 10
#endif

#ifdef CONFIG_BRIDGE_SCAN_CACHE_SIZE
#define BRIDGE_BLE_SCAN_CACHE_MAX CONFIG_BRIDGE_SCAN_CACHE_SIZE
#else
#define BRIDGE_BLE_SCAN_CACHE_MAX 8
#endif

#define BRIDGE_BLE_ADDR_LEN 6
#define BRIDGE_BLE_NAME_MAX 32
#define BRIDGE_BLE_UUID128_LEN 16

#define BRIDGE_ADV_FLAG_NAME 0x01    /* complete/shortened name present   */
#define BRIDGE_ADV_FLAG_SVC_FEF5 0x02 /* service data with UUID16 0xFEF5  */

/* EVT.CONN/EVT.DISC role byte values (FR-H). */
#define BRIDGE_BLE_ROLE_CENTRAL 0x00U    /* host-initiated link            */
#define BRIDGE_BLE_ROLE_PERIPHERAL 0x01U /* link accepted while advertising */

/* ---- LE helpers --------------------------------------------------------- */

static inline uint16_t bridge_ble_get_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline void bridge_ble_put_le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFU);
    p[1] = (uint8_t)(v >> 8);
}

/* ---- advertisement parser ----------------------------------------------- */

/**
 * Walk one advertisement payload (raw AD structures) and extract the
 * device name (complete or shortened) and whether SUOTA service data
 * (UUID16 0xFEF5) is present.
 *
 * Tolerates truncated/malformed structures (stops at the first bogus
 * record instead of running off the buffer).
 *
 * @param adv       raw advertisement bytes (NULL allowed when len == 0)
 * @param len       advertisement length
 * @param name_out  buffer for the NUL-terminated name (may be NULL)
 * @param name_cap  capacity of name_out
 * @return          BRIDGE_ADV_FLAG_* bit-set (0 = neither found)
 */
int bridge_ble_adv_parse(const uint8_t *adv, size_t len, char *name_out,
                         size_t name_cap);

/* ---- scan cache ---------------------------------------------------------- */

typedef struct {
    bool used;
    uint8_t addr_type; /* 0 public / 1 random (daemon parse_scan_payload) */
    uint8_t addr[BRIDGE_BLE_ADDR_LEN]; /* displayed order */
    int8_t rssi;
    uint8_t adv_type; /* raw HCI LE adv report event type (v1.4.1, E2-T):
                       * 0 conn-undirected, 2 scannable, 3 non-connectable,
                       * 4 scan response -- host interprets */
    char name[BRIDGE_BLE_NAME_MAX]; /* "" when unknown */
    bool has_fef5;
} bridge_scan_rec_t;

typedef struct {
    uint8_t count;
    bridge_scan_rec_t recs[BRIDGE_BLE_SCAN_CACHE_MAX];
} bridge_scan_cache_t;

void bridge_ble_scan_cache_init(bridge_scan_cache_t *c);

/**
 * Insert or update: keyed on (addr_type, addr); refreshes rssi, name (when
 * the advertisement carries one) and the FEF5 flag.  When the cache is
 * full the worst-RSSI slot is evicted.
 *
 * @return the cache slot holding the record (never NULL)
 */
bridge_scan_rec_t *bridge_ble_scan_cache_update(bridge_scan_cache_t *c,
                                                const bridge_scan_rec_t *in);

/** Exact match on (addr_type, addr); NULL when absent. */
const bridge_scan_rec_t *bridge_ble_scan_cache_find_addr(
    const bridge_scan_cache_t *c, uint8_t addr_type,
    const uint8_t addr[BRIDGE_BLE_ADDR_LEN]);

/**
 * Best-RSSI record whose name starts with `name` (case-sensitive prefix,
 * empty name never matches); NULL when absent.
 */
const bridge_scan_rec_t *bridge_ble_scan_cache_find_name(
    const bridge_scan_cache_t *c, const char *name);

/* ---- GATT character map (service 0xFEF5, max 1 connection) --------------- */

/**
 * GATT addressing key of a 128-bit UUID, given its over-the-air
 * little-endian value array (the Zephyr ``bt_uuid_128.val`` layout; the
 * ``BT_UUID_128_ENCODE(w32, w1, w2, w3, w48)`` byte order).
 *
 * The key is the first 16 bits of the readable UUID form
 * ("8082caa8-..." -> 0x8082, host suota.py ``uuid16_prefix``), which in
 * the little-endian array sit at val[14] (LSB) and val[15] (MSB) -- NOT
 * val[12]/val[13] (those hold the readable form's trailing "caa8").
 *
 * Cross-end contract (docs/protocol-contract.md section 4): the host
 * addresses characteristics with this same 16-bit value sent
 * little-endian (suota.py ``uuid16_le``), so firmware map keys and host
 * request payloads agree byte-for-byte.  The 10 SUOTA characteristics
 * map to 10 distinct, non-colliding keys -- asserted no-collapse by
 * host/tests/test_ble_pure_map.py.
 *
 * @param val_le 16-byte over-the-air (little-endian) UUID value
 * @return       the uuid16 addressing key
 */
uint16_t bridge_ble_uuid128_prefix(
    const uint8_t val_le[BRIDGE_BLE_UUID128_LEN]);

typedef struct {
    uint16_t uuid16;
    uint16_t value_handle;
    uint16_t ccc_handle; /* 0 when the characteristic has no CCC */
    uint8_t props;       /* bt_gatt_chrc.properties bit-set */
} bridge_char_ent_t;

typedef struct {
    uint8_t count;
    bridge_char_ent_t ents[BRIDGE_BLE_CHAR_MAX];
} bridge_char_map_t;

void bridge_ble_char_map_clear(bridge_char_map_t *m);

/** @return 0 stored; -ENOSPC when the map is full (entry dropped) */
int bridge_ble_char_map_add(bridge_char_map_t *m, uint16_t uuid16,
                            uint16_t value_handle, uint16_t ccc_handle,
                            uint8_t props);

/** @return true and fill *out when found */
bool bridge_ble_char_map_find(const bridge_char_map_t *m, uint16_t uuid16,
                              bridge_char_ent_t *out);

/* ---- advertising intent state machine (FR-H fix round 1) ----------------- */

/** Events driving the advertising-intent state. */
typedef enum {
    BRIDGE_ADV_EV_START = 0, /* CMD.adv_start completed OK (or -EALREADY) */
    BRIDGE_ADV_EV_STOP = 1,  /* CMD.adv_stop completed OK (or -EALREADY)  */
    BRIDGE_ADV_EV_LINK_LOST = 2, /* peripheral-role link disconnected     */
} bridge_adv_evt_t;

/** Actions the state machine asks for. */
typedef enum {
    BRIDGE_ADV_ACT_NONE = 0,    /* no change                            */
    BRIDGE_ADV_ACT_RESTART = 1, /* re-start advertising now             */
} bridge_adv_act_t;

/**
 * Advertising-intent transition (pure, host-testable).
 *
 * "intent" = the user asked for advertising via CMD.adv_start; only an
 * explicit CMD.adv_stop clears it.  A lost peripheral link restarts
 * advertising when the intent is set (defect 3, fix round 1: the link
 * dropping -- clean or crashed -- must not silently end advertising),
 * which is why the decision lives apart from the BT call sites.
 *
 * @param wanted current intent state (updated in place)
 * @param evt    driving event
 * @return       BRIDGE_ADV_ACT_RESTART when advertising must be restarted
 */
bridge_adv_act_t bridge_ble_adv_intent_step(bool *wanted,
                                            bridge_adv_evt_t evt);

/* ---- event payload builders (all return len or -ENOSPC-ish negative) ----- */

/** EVT.SCAN body: [0x00][addr_type][addr6][rssi i8][adv_type][adv...] */
int bridge_ble_evt_scan_build(const bridge_scan_rec_t *rec,
                              const uint8_t *adv, size_t adv_len,
                              uint8_t *out, size_t cap);

/** EVT.CONN body: [0x00][role][addr_type][addr6] (role: 0 central, 1 peripheral) */
int bridge_ble_evt_conn_build(uint8_t role, uint8_t addr_type,
                              const uint8_t addr[BRIDGE_BLE_ADDR_LEN],
                              uint8_t *out, size_t cap);

/** EVT.DISC body: [0x00][role][reason] (role: 0 central, 1 peripheral) */
int bridge_ble_evt_disc_build(uint8_t role, uint8_t reason, uint8_t *out,
                              size_t cap);

/** EVT.GATTX body (subtype 0x00, notify): [0x00][uuid16 LE][data...] */
int bridge_ble_evt_gattx_build(uint16_t uuid16, const uint8_t *data,
                               size_t len, uint8_t *out, size_t cap);

/**
 * EVT.GATTX body (subtype 0x01, FR-H peripheral-role write):
 * [0x01][uuid16 LE][value handle LE][data...]
 */
int bridge_ble_evt_gattx_write_build(uint16_t uuid16, uint16_t handle,
                                     const uint8_t *data, size_t len,
                                     uint8_t *out, size_t cap);

/** EVT.MTU body: [0x00][mtu u16 LE] */
int bridge_ble_evt_mtu_build(uint16_t mtu, uint8_t *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_BLE_PURE_H */
