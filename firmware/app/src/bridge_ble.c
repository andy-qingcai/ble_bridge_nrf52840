/*
 * bridge_ble.c - BLE central bridge (Task 10).  See bridge_ble.h for the
 * thread and reference model, bridge_ble_pure.h for the wire formats.
 */

#include "bridge_ble.h"

#include <errno.h>
#include <stdio.h>
#include <stdatomic.h>
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci_types.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/kernel.h>

/* NCS GATT Discovery Manager: lives in the NCS tree (nrf/include), not
 * under zephyr/ (docs/research/nordic-stack-eval.md, subsys/bluetooth/
 * gatt_dm.c). */
#include <bluetooth/gatt_dm.h>

#include "bridge_ble_pure.h"
#include "bridge_dispatch.h"
#include "bridge_link.h"
#include "bridge_periph.h"

/* Generic BLE bridge (controller revision 2026-09-24): no SUOTA-specific
 * behaviour lives here any more.  Nothing is discovered automatically at
 * connect and the firmware never drops the link on its own -- discovery is
 * host-triggered via CMD.disc_svc for ANY service UUID, and every failure
 * is reported on EVT.DEBUG while the connection is kept. */

/* Initial connection parameters: 30-50 ms interval, latency 0, 4 s
 * supervision timeout (1.25 ms / 10 ms units per the LE spec). */
#define CONN_INT_MIN 24
#define CONN_INT_MAX 40
#define CONN_LATENCY 0
#define CONN_TIMEOUT 400

/* GATT completion waits (bounded; the stack answers far faster in practice,
 * and disconnected() releases every waiter on link loss). */
#define GATT_WAIT K_SECONDS(5)
#define DM_WAIT K_SECONDS(3)

/* FR-B5: SMP pairing target (unauthenticated LE pairing = Just Works with
 * NoInputNoOutput IO, L2 encryption). */
#define PAIR_TARGET BT_SECURITY_L2

/* link states (atomic: touched from BT threads and the link RX thread) */
enum {
    ST_IDLE = 0,
    ST_CONNECTING = 1,
    ST_CONNECTED = 2,
};

/* ---- state --------------------------------------------------------------- */

static atomic_t state;   /* ST_* */
static atomic_t ready;   /* bt_enable() finished ok */
static atomic_t cur_mtu; /* negotiated TX ATT MTU (23 before the exchange) */
static atomic_t dm_done; /* service map of the current link is valid */

static struct bt_conn *conn; /* the one create-reference; see bridge_ble.h */

static struct k_sem sem_conn; /* connected / connect-cancelled */
static struct k_sem sem_dm;   /* discovery finished (ok or failed) */
static struct k_sem sem_gatt; /* read / write response */
static struct k_sem sem_sub;  /* CCC write response */
static struct k_sem sem_pair; /* SMP pairing settled (FR-B5) */

/* FR-B5 pairing-waiter state machine (atomic: written from BT threads under
 * security_changed / pairing_failed / pairing_complete, read on the link RX
 * thread after sem_pair). */
enum {
    PAIR_PENDING = 0,
    PAIR_OK = 1,
    PAIR_FAILED = 2,
    PAIR_LINK_LOST = 3,
};
static atomic_t pair_state;
static atomic_t pair_level; /* security level carried by security_changed */

/* Fix round 1 (late-err reference leak): set when the api_connect timeout
 * path gave up while the create was still pending.  The create-reference
 * then still lives in `conn` and its release is owed by the (arbitrarily
 * late) connected(err) callback - the ONLY code path that ever unrefs a
 * pending-create object.  api_connect refuses to start a new create while
 * this flag is set. */
static atomic_t conn_abandoned;

static int gatt_err; /* ATT error of the pending read/write */
static int sub_err;  /* ATT error of the pending CCC write */

static uint8_t rd_buf[CONFIG_BT_L2CAP_TX_MTU]; /* read response value */
static uint16_t rd_len;

static bridge_scan_cache_t scan_cache;
static atomic_t scanning;

static bridge_char_map_t char_map;

/* Host-triggered discovery state: the service UUID of the last SUCCESSFUL
 * disc_svc (0 = none on this link; re-discovery placeholder) and the
 * characteristic count of the most recent completed run (both read on the
 * link RX thread, written on the gatt_dm workq / RX thread). */
static atomic_t dm_nchars;
static uint16_t last_svc;

/* Subscription slots: static, because the stack keeps pointers into the
 * params across asynchronous responses (review red line).  A slot is owned
 * between subscribe() and unsubscribe() (or disconnected()).  Slots taken
 * through the handle-addressing form carry by_handle = true and key their
 * EVT.GATTX source field with the VALUE HANDLE (no uuid16 is known). */
typedef struct {
    bool active;
    bool by_handle;
    uint16_t uuid16;
    struct bt_gatt_subscribe_params params;
} sub_slot_t;

static sub_slot_t subs[BRIDGE_BLE_CHAR_MAX];

/* Scan window auto-stop (duration_ms from CMD.scan_start). */
static void scan_stop_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(scan_stop_work, scan_stop_fn);

/* forward decls (connected_cb starts the discovery) */
static void dm_completed_cb(struct bt_gatt_dm *dm, void *ctx);
static void dm_not_found_cb(struct bt_conn *cb_conn, void *ctx);
static void dm_error_cb(struct bt_conn *cb_conn, int err, void *ctx);

static const struct bt_gatt_dm_cb dm_cb = {
    .completed = dm_completed_cb,
    .service_not_found = dm_not_found_cb,
    .error_found = dm_error_cb,
};

/* ---- small helpers --------------------------------------------------------- */

static void post_debug(const char *tag, const char *msg)
{
    uint8_t payload[64];
    int plen = bridge_evt_debug_build(tag, msg, payload,
                                      (uint16_t)sizeof(payload));

    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_DEBUG, payload,
                                     (uint16_t)plen);
    }
}

/* disconnected(): release every command-side waiter; stale tokens are
 * flushed first so a previous link cannot pre-satisfy the next one
 * (sem_conn is additionally reset at every api_connect entry). */
static void release_all_sems(void)
{
    gatt_err = -ENOTCONN;
    sub_err = -ENOTCONN;
    k_sem_reset(&sem_dm);
    k_sem_reset(&sem_gatt);
    k_sem_reset(&sem_sub);
    k_sem_reset(&sem_conn);
    k_sem_reset(&sem_pair);
    k_sem_give(&sem_dm);
    k_sem_give(&sem_gatt);
    k_sem_give(&sem_sub);
    k_sem_give(&sem_conn);
    atomic_set(&pair_state, PAIR_LINK_LOST); /* api_pair: NOT_READY, not 0x75 */
    k_sem_give(&sem_pair);
}

static sub_slot_t *slot_by_params(const struct bt_gatt_subscribe_params *p)
{
    for (uint8_t i = 0; i < BRIDGE_BLE_CHAR_MAX; i++) {
        if (subs[i].active && &subs[i].params == p) {
            return &subs[i];
        }
    }
    return NULL;
}

static sub_slot_t *slot_by_uuid(uint16_t uuid16)
{
    for (uint8_t i = 0; i < BRIDGE_BLE_CHAR_MAX; i++) {
        if (subs[i].active && !subs[i].by_handle && subs[i].uuid16 == uuid16) {
            return &subs[i];
        }
    }
    return NULL;
}

static sub_slot_t *slot_by_handle(uint16_t value_handle)
{
    for (uint8_t i = 0; i < BRIDGE_BLE_CHAR_MAX; i++) {
        if (subs[i].active && subs[i].by_handle &&
            subs[i].uuid16 == value_handle) {
            return &subs[i];
        }
    }
    return NULL;
}

static void subs_table_clear(void)
{
    for (uint8_t i = 0; i < BRIDGE_BLE_CHAR_MAX; i++) {
        subs[i].active = false;
    }
}

/* ---- BT callbacks (BT RX thread / gatt_dm workq) ----------------------------- */

static void scan_recv_cb(const struct bt_le_scan_recv_info *info,
                         struct net_buf_simple *buf)
{
    bridge_scan_rec_t rec = {0};
    char name[BRIDGE_BLE_NAME_MAX];
    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    int flags;

    if (buf->len == 0U) {
        return;
    }
    rec.addr_type = info->addr->type;
    memcpy(rec.addr, info->addr->a.val, BRIDGE_BLE_ADDR_LEN);
    rec.rssi = info->rssi;
    rec.adv_type = info->adv_type; /* v1.4.1 (E2-T): raw LE adv PDU type */

    flags = bridge_ble_adv_parse(buf->data, buf->len, name, sizeof(name));
    if (flags & BRIDGE_ADV_FLAG_NAME) {
        memcpy(rec.name, name, sizeof(rec.name));
    }
    rec.has_fef5 = (flags & BRIDGE_ADV_FLAG_SVC_FEF5) != 0;

    (void)bridge_ble_scan_cache_update(&scan_cache, &rec);

    if (IS_ENABLED(CONFIG_BRIDGE_SCAN_FILTER_FEF5) && !rec.has_fef5) {
        return; /* optional filter; default off: the host filters */
    }

    int plen = bridge_ble_evt_scan_build(&rec, buf->data, buf->len, payload,
                                         sizeof(payload));

    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_SCAN, payload,
                                     (uint16_t)plen);
    }
}

static struct bt_le_scan_cb scan_cb = {
    .recv = scan_recv_cb,
};

static void scan_stop_fn(struct k_work *work)
{
    ARG_UNUSED(work);

    if (atomic_cas(&scanning, 1, 0)) {
        (void)bt_le_scan_stop();
    }
}

/* EVT.MTU fix (measured against a reference device): the stack fires
 * att_mtu_updated on EVERY ATT bearer transition -- bearer up, our
 * CONFIG_BT_GATT_AUTO_UPDATE_MTU exchange response, a peer-initiated
 * Exchange MTU Request (peripheral-initiated: suota_sim prj.conf "ATT_EXCHANGE_MTU_REQ
 * immediately after connect"), EATT reconfig -- and those transitional
 * emissions can carry a pre-negotiation value (tx == 0: bearer initialised,
 * MTU not yet known).  The reference device's link surfaced 3 such events, all
 * value 0, while the Zephyr-to-Zephyr matrix showed the canonical
 * 23/256/247 sequence.  The bridge contract only wants the effective TX
 * MTU, so forward a value iff it is valid (>= minimum) and differs from
 * the last one reported for this link. */
/* Minimum ATT MTU (Bluetooth Core Vol 3, Part F 3.2.8).  Zephyr keeps this
 * in the internal att_internal.h, so it is spelled out here (host suota.py
 * MIN_ATT_MTU). */
#define BRIDGE_MIN_ATT_MTU 23U

static uint16_t mtu_reported; /* last EVT.MTU value sent for this link */

static void mtu_changed_cb(struct bt_conn *cb_conn, uint16_t tx, uint16_t rx)
{
    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    int plen;

    ARG_UNUSED(rx);
    if (cb_conn != conn) {
        return;
    }
    if (tx < BRIDGE_MIN_ATT_MTU) {
        /* Transitional stack value (bearer setup / pre-exchange): never
         * valid on the wire.  Drop it instead of reporting 0 and stomping
         * cur_mtu, which api_mtu_get uses to size PATCH_DATA writes. */
        return;
    }
    if (tx == mtu_reported) {
        return; /* re-emission of the already-reported value */
    }
    mtu_reported = tx;
    atomic_set(&cur_mtu, (atomic_val_t)tx);
    plen = bridge_ble_evt_mtu_build(tx, payload, sizeof(payload));
    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_MTU, payload,
                                     (uint16_t)plen);
    }
}

static void connected_cb(struct bt_conn *cb_conn, uint8_t err)
{
    struct bt_conn_info info;

    /* FR-H multi-role: peripheral-role links (accepted while advertising)
     * belong to bridge_periph.c, which registers its own bt_conn_cb and
     * gates on the same role.  Return before touching any central state -
     * in particular before the defensive disconnect below. */
    if (bt_conn_get_info(cb_conn, &info) == 0 &&
        info.role == BT_CONN_ROLE_PERIPHERAL) {
        return;
    }

    if (err != 0U) {
        /* Fix round 1 (late-err reference leak): gate on object identity
         * (cb_conn == conn), not only on the state.  Invariant: the
         * create-reference handed out by bt_conn_le_create() is released
         * exactly once, HERE, and `conn` is cleared the moment it is
         * released.  A callback that arrives after the timeout path gave
         * up (conn_abandoned set, conn still tracked) still matches and
         * releases; once released, conn == NULL makes any even-later
         * duplicate a no-op. */
        if (cb_conn == conn) {
            char msg[32];

            atomic_set(&state, ST_IDLE); /* before unref: timeout-path gate */
            atomic_set(&conn_abandoned, 0);
            (void)snprintf(msg, sizeof(msg), "connect err: %u",
                           (unsigned)err);
            post_debug("ble", msg);
            bt_conn_unref(conn); /* release the create-reference */
            conn = NULL;
            k_sem_give(&sem_conn);
        }
        return;
    }

    if (cb_conn != conn) {
        /* Not the tracked object.  With HCI event ordering (a success
         * LE Connection Complete precedes the cancel resolution) this can
         * only mean the timeout path already gave up on an earlier object;
         * never adopt blindly - if the link actually came up while we had
         * stopped waiting, tear it down without taking ownership. */
        if (conn == NULL) {
            (void)bt_conn_disconnect(cb_conn,
                                     BT_HCI_ERR_REMOTE_USER_TERM_CONN);
        }
        return;
    }
    atomic_set(&state, ST_CONNECTED);
    atomic_set(&cur_mtu, 23); /* pre-exchange ATT MTU */
    mtu_reported = 0; /* fresh link: even 23 must reach the host via EVT.MTU */

    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    const bt_addr_le_t *dst = bt_conn_get_dst(conn);
    int plen = bridge_ble_evt_conn_build(BRIDGE_BLE_ROLE_CENTRAL, dst->type,
                                         dst->a.val, payload,
                                         sizeof(payload));

    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_CONN, payload,
                                     (uint16_t)plen);
    }
    k_sem_give(&sem_conn);

    /* Generic bridge: NO automatic service discovery.  The link is simply
     * up; the host triggers discovery per service with CMD.disc_svc and
     * keeps the connection even when discovery fails.  uuid-addressed GATT
     * commands answer NOT_READY until the host discovers (handle-addressed
     * commands work without any map). */
    atomic_set(&dm_done, 0);
}

static void disconnected_cb(struct bt_conn *cb_conn, uint8_t reason)
{
    if (cb_conn != conn) {
        return; /* peripheral-role links: bridge_periph.c handles them */
    }

    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    int plen = bridge_ble_evt_disc_build(BRIDGE_BLE_ROLE_CENTRAL, reason,
                                         payload, sizeof(payload));

    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_DISC, payload,
                                     (uint16_t)plen);
    }

    bt_conn_unref(conn); /* release the create-reference exactly once */
    conn = NULL;
    atomic_set(&state, ST_IDLE);
    atomic_set(&dm_done, 0);
    atomic_set(&cur_mtu, 0);
    last_svc = 0U; /* re-discovery placeholder is per-link */
    bridge_ble_char_map_clear(&char_map);
    subs_table_clear();
    release_all_sems();
}

static void le_param_updated_cb(struct bt_conn *cb_conn, uint16_t interval,
                                uint16_t latency, uint16_t timeout)
{
    char msg[64];

    if (cb_conn != conn) {
        return;
    }
    (void)snprintf(msg, sizeof(msg), "conn params: %uu %lu %ut",
                   (unsigned)interval, (unsigned long)latency,
                   (unsigned)timeout);
    post_debug("ble", msg);
}

#if defined(CONFIG_BT_USER_PHY_UPDATE)
static void le_phy_updated_cb(struct bt_conn *cb_conn,
                              struct bt_conn_le_phy_info *param)
{
    char msg[64];

    if (cb_conn != conn) {
        return;
    }
    (void)snprintf(msg, sizeof(msg), "phy: %uM tx %uM rx",
                   (unsigned)(param->tx_phy == BT_GAP_LE_PHY_2M ? 2 : 1),
                   (unsigned)(param->rx_phy == BT_GAP_LE_PHY_2M ? 2 : 1));
    post_debug("ble", msg);
}
#endif

/* ---- FR-B5: SMP pairing callbacks -------------------------------------- */

#if defined(CONFIG_BT_SMP)
static void security_changed_cb(struct bt_conn *cb_conn, bt_security_t level,
                                enum bt_security_err err)
{
    char msg[48];

    if (cb_conn != conn) {
        return;
    }
    (void)snprintf(msg, sizeof(msg), "sec: L%u err %u", (unsigned)level,
                   (unsigned)err);
    post_debug("ble", msg);

    if (err != BT_SECURITY_ERR_SUCCESS) {
        atomic_set(&pair_state, PAIR_FAILED);
        k_sem_give(&sem_pair);
        return;
    }
    atomic_set(&pair_level, (atomic_val_t)level);
    if (level >= PAIR_TARGET) {
        atomic_set(&pair_state, PAIR_OK);
        k_sem_give(&sem_pair);
    }
    /* A level below the target (e.g. an L1 key refresh): not a pairing
     * verdict -- keep the waiter waiting for the real transition. */
}

static void pairing_complete_cb(struct bt_conn *cb_conn, bool bonded)
{
    char msg[32];

    ARG_UNUSED(bonded);
    if (cb_conn != conn) {
        return;
    }
    (void)snprintf(msg, sizeof(msg), "pairing complete bond=%u",
                   (unsigned)bonded);
    post_debug("ble", msg);
    /* Normally security_changed already settled the waiter; if it has not
     * (ordering), conclude here when the link is actually secured. */
    if (atomic_get(&pair_state) == PAIR_PENDING &&
        bt_conn_get_security(conn) >= PAIR_TARGET) {
        atomic_set(&pair_level, (atomic_val_t)bt_conn_get_security(conn));
        atomic_set(&pair_state, PAIR_OK);
        k_sem_give(&sem_pair);
    }
}

static void pairing_failed_cb(struct bt_conn *cb_conn,
                              enum bt_security_err reason)
{
    char msg[48];

    if (cb_conn != conn) {
        return;
    }
    (void)snprintf(msg, sizeof(msg), "pairing failed: %u", (unsigned)reason);
    post_debug("ble", msg);
    atomic_set(&pair_state, PAIR_FAILED);
    k_sem_give(&sem_pair);
}

static struct bt_conn_auth_info_cb auth_info_cb = {
    .pairing_complete = pairing_complete_cb,
    .pairing_failed = pairing_failed_cb,
};
#endif /* CONFIG_BT_SMP */

static struct bt_conn_cb conn_cb = {
    .connected = connected_cb,
    .disconnected = disconnected_cb,
    .le_param_updated = le_param_updated_cb,
#if defined(CONFIG_BT_USER_PHY_UPDATE)
    .le_phy_updated = le_phy_updated_cb,
#endif
#if defined(CONFIG_BT_SMP)
    .security_changed = security_changed_cb,
#endif
};

static void dm_completed_cb(struct bt_gatt_dm *dm, void *ctx)
{
    const struct bt_gatt_dm_attr *attr;
    bridge_char_ent_t ent;
    uint8_t nchars = 0U; /* successfully mapped (new-entry) characteristics */
    uint8_t ndisc = 0U;  /* characteristics seen in the discovery */

    ARG_UNUSED(ctx);
    /* Generic bridge: accumulate across runs (one disc_svc per service);
     * the map is only cleared when the link drops.  Re-discovering the
     * same service keeps the first (identical) entries: the dedup guard
     * below warns loudly instead of silently collapsing a key. */

    attr = bt_gatt_dm_char_next(dm, NULL);
    while (attr != NULL) {
        const struct bt_gatt_chrc *chrc = bt_gatt_dm_attr_chrc_val(attr);
        const struct bt_gatt_dm_attr *ccc;
        uint16_t uuid16;
        bool known = true;

        if (chrc == NULL) {
            break;
        }
        ndisc++;

        /* Fix C-1 (final review G2 blocker): the SUOTA characteristics
         * are all 128-bit vendor UUIDs.  Their addressing key is the
         * first 16 bits of the readable UUID form (host suota.py
         * uuid16_prefix, e.g. 0x8082 for 8082caa8-...).  bt_uuid_128.val
         * is the over-the-air little-endian array, so that prefix lives
         * in val[14] (LSB) / val[15] (MSB) -- leaving it 0 (as before)
         * collapsed all ten characteristics onto the single key 0. */
        if (chrc->uuid->type == BT_UUID_TYPE_16) {
            uuid16 = ((const struct bt_uuid_16 *)chrc->uuid)->val;
        } else if (chrc->uuid->type == BT_UUID_TYPE_128) {
            uuid16 = bridge_ble_uuid128_prefix(
                ((const struct bt_uuid_128 *)chrc->uuid)->val);
        } else {
            known = false; /* 32-bit never appears on the LE ATT bearer */
        }

        if (known) {
            ccc = bt_gatt_dm_desc_by_uuid(dm, attr, BT_UUID_GATT_CCC);
            if (bridge_ble_char_map_find(&char_map, uuid16, &ent)) {
                /* Dedup hit (uuid16 key collision): warn loudly with the
                 * key instead of silently collapsing the entry. */
                char warn[48];

                (void)snprintf(warn, sizeof(warn),
                               "WARN char key dup 0x%04X", (unsigned)uuid16);
                post_debug("ble", warn);
            } else if (bridge_ble_char_map_add(&char_map, uuid16,
                                               chrc->value_handle,
                                               (ccc != NULL) ? ccc->handle : 0U,
                                               chrc->properties) == 0) {
                nchars++;
            }
        }
        attr = bt_gatt_dm_char_next(dm, attr);
    }

    bt_gatt_dm_data_release(dm);

    char msg[32];

    (void)snprintf(msg, sizeof(msg), "gatt_dm ok: %u/%u chars",
                   (unsigned)nchars, (unsigned)ndisc);
    post_debug("ble", msg);
    atomic_set(&dm_nchars, (atomic_val_t)ndisc);
    atomic_set(&dm_done, 1);
    k_sem_give(&sem_dm);
}

static void dm_not_found_cb(struct bt_conn *cb_conn, void *ctx)
{
    ARG_UNUSED(ctx);
    if (cb_conn != conn) {
        return;
    }
    /* Generic bridge: a service that is not there is a host-visible
     * verdict (disc_svc answers NOT_READY), never a reason to drop the
     * link.  Details ride EVT.DEBUG. */
    post_debug("ble", "disc_svc: service not found");
    atomic_set(&dm_done, 0);
    k_sem_give(&sem_dm);
}

static void dm_error_cb(struct bt_conn *cb_conn, int err, void *ctx)
{
    char msg[48];

    ARG_UNUSED(ctx);
    if (cb_conn != conn) {
        return;
    }
    (void)snprintf(msg, sizeof(msg), "disc_svc error: %d", err);
    post_debug("ble", msg);
    atomic_set(&dm_done, 0);
    k_sem_give(&sem_dm);
    /* Keep the connection: the host decides what to do next. */
}

/* ---- GATT op callbacks --------------------------------------------------------- */

static uint8_t read_rsp_cb(struct bt_conn *cb_conn, uint8_t err,
                           struct bt_gatt_read_params *params,
                           const void *data, uint16_t length)
{
    ARG_UNUSED(cb_conn);
    ARG_UNUSED(params);

    if (err != 0U) {
        gatt_err = err;
    } else if (data != NULL) {
        uint16_t n = (length <= sizeof(rd_buf)) ? length
                                                : (uint16_t)sizeof(rd_buf);

        memcpy(rd_buf, data, n);
        rd_len = n;
    }
    k_sem_give(&sem_gatt);
    return BT_GATT_ITER_STOP;
}

static void write_rsp_cb(struct bt_conn *cb_conn, uint8_t err,
                         struct bt_gatt_write_params *params)
{
    ARG_UNUSED(cb_conn);
    ARG_UNUSED(params);

    gatt_err = err;
    k_sem_give(&sem_gatt);
}

static void ccc_rsp_cb(struct bt_conn *cb_conn, uint8_t err,
                       struct bt_gatt_subscribe_params *params)
{
    ARG_UNUSED(cb_conn);
    ARG_UNUSED(params);

    sub_err = err;
    k_sem_give(&sem_sub);
}

static uint8_t notify_cb(struct bt_conn *cb_conn,
                         struct bt_gatt_subscribe_params *params,
                         const void *data, uint16_t length)
{
    sub_slot_t *slot;
    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    int plen;

    ARG_UNUSED(cb_conn);
    if (data == NULL) {
        /* Peer removed the subscription / unscribed from its side. */
        slot = slot_by_params(params);
        if (slot != NULL) {
            slot->active = false;
        }
        return BT_GATT_ITER_STOP;
    }
    slot = slot_by_params(params);
    if (slot == NULL) {
        return BT_GATT_ITER_STOP;
    }
    plen = bridge_ble_evt_gattx_build(slot->uuid16, data, length, payload,
                                      sizeof(payload));
    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_GATTX, payload,
                                     (uint16_t)plen);
    }
    return BT_GATT_ITER_CONTINUE;
}

/* ---- preconditions --------------------------------------------------------------- */

static uint8_t need_connected(void)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    if (atomic_get(&state) != ST_CONNECTED) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    return BRIDGE_STATUS_OK;
}

static uint8_t wait_map(void)
{
    /* Generic bridge: the map only exists after a host-triggered
     * disc_svc completed.  Commands are serialised on the link RX thread,
     * so no discovery can be in flight behind a caller's back -- answer
     * immediately instead of blocking (handle addressing skips this
     * precondition entirely). */
    if (atomic_get(&dm_done) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    return BRIDGE_STATUS_OK;
}

/* ---- api: scan ---------------------------------------------------------------------- */

static uint8_t api_scan_start(uint16_t duration_ms)
{
    int err;

    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    if (!atomic_cas(&scanning, 0, 1)) {
        return BRIDGE_STATUS_ERR_BUSY; /* already scanning */
    }

    struct bt_le_scan_param param = {0};

    param.type = BT_LE_SCAN_TYPE_ACTIVE;
    param.options = BT_LE_SCAN_OPT_NONE; /* report every advert: host dedups */
    param.interval = BT_GAP_SCAN_FAST_INTERVAL;
    param.window = BT_GAP_SCAN_FAST_WINDOW;
    param.timeout = 0; /* the firmware owns the window via the delayable */

    err = bt_le_scan_start(&param, NULL);
    if (err != 0) {
        atomic_set(&scanning, 0);
        char msg[32];

        (void)snprintf(msg, sizeof(msg), "scan start: %d", err);
        post_debug("ble", msg);
        return BRIDGE_STATUS_ERR_NOT_READY;
    }

    if (duration_ms == 0U) {
        duration_ms = (uint16_t)CONFIG_BRIDGE_SCAN_DURATION_MS;
    }
    k_work_reschedule(&scan_stop_work, K_MSEC(duration_ms));
    return BRIDGE_STATUS_OK;
}

static uint8_t api_scan_stop(void)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    (void)k_work_cancel_delayable(&scan_stop_work);
    if (atomic_cas(&scanning, 1, 0)) {
        int err = bt_le_scan_stop();

        if (err != 0) {
            return BRIDGE_STATUS_ERR_NOT_READY;
        }
    }
    return BRIDGE_STATUS_OK; /* idempotent */
}

/* ---- api: connect / disconnect -------------------------------------------------------- */

static uint8_t api_connect(const uint8_t *payload, uint16_t len,
                           uint32_t timeout_ms, uint8_t *body, uint16_t cap,
                           uint16_t *body_len)
{
    bt_addr_le_t peer = {0};
    const bridge_scan_rec_t *rec;
    int err;

    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    if (atomic_get(&state) != ST_IDLE) {
        return BRIDGE_STATUS_ERR_BUSY; /* connecting or connected */
    }
    if (conn != NULL) {
        /* Fix round 1 (late-err reference leak): a previous attempt timed
         * out and abandoned its create; the connected(err) callback still
         * owes the reference release (HCI cancel resolution guarantees it
         * arrives).  Drain it briefly; refuse this attempt if it has not
         * landed.  `conn` is never overwritten with a live reference. */
        if (atomic_get(&conn_abandoned) == 0) {
            return BRIDGE_STATUS_ERR_BUSY; /* defensively: unowned link */
        }
        (void)k_sem_take(&sem_conn, K_MSEC(200));
        if (conn != NULL) {
            post_debug("ble", "connect: create still pending");
            return BRIDGE_STATUS_ERR_BUSY;
        }
    }

    uint8_t mode = payload[0];

    if (mode == 2U) {
        char name[BRIDGE_BLE_NAME_MAX];
        uint16_t nlen = (uint16_t)(len - 1U);

        if (nlen == 0U || nlen >= sizeof(name)) {
            return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        memcpy(name, &payload[1], nlen);
        name[nlen] = '\0';
        rec = bridge_ble_scan_cache_find_name(&scan_cache, name);
        if (rec == NULL) {
            post_debug("ble", "connect: name not in scan cache");
            return BRIDGE_STATUS_ERR_NOT_READY;
        }
        peer.type = rec->addr_type;
        memcpy(peer.a.val, rec->addr, BRIDGE_BLE_ADDR_LEN);
    } else if ((mode == 0U || mode == 1U) && len == 7U) {
        peer.type = (mode == 0U) ? BT_ADDR_LE_PUBLIC : BT_ADDR_LE_RANDOM;
        /* v1.4.2: payload addr arrives in display order (left-to-right as a
         * phone shows it); Zephyr's val is little-endian, reverse on the way
         * in.  The name path above consumes the val-order scan cache. */
        for (unsigned int i = 0U; i < BRIDGE_BLE_ADDR_LEN; i++) {
            peer.a.val[i] = payload[1 + BRIDGE_BLE_ADDR_LEN - 1U - i];
        }
    } else {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }

    /* Scanning must be off before initiating a connection. */
    (void)k_work_cancel_delayable(&scan_stop_work);
    if (atomic_cas(&scanning, 1, 0)) {
        (void)bt_le_scan_stop();
    }

    k_sem_reset(&sem_conn);
    atomic_set(&state, ST_CONNECTING);
    err = bt_conn_le_create(&peer, BT_CONN_LE_CREATE_CONN,
                            BT_LE_CONN_PARAM(CONN_INT_MIN, CONN_INT_MAX,
                                             CONN_LATENCY, CONN_TIMEOUT),
                            &conn);
    if (err != 0) {
        atomic_set(&state, ST_IDLE);
        char msg[32];

        (void)snprintf(msg, sizeof(msg), "create: %d", err);
        post_debug("ble", msg);
        return BRIDGE_STATUS_ERR_NOT_READY;
    }

    if (timeout_ms == 0U) {
        timeout_ms = (uint32_t)CONFIG_BRIDGE_CONNECT_TIMEOUT_MS;
    }
    if (k_sem_take(&sem_conn, K_MSEC(timeout_ms)) != 0) {
        /* Timeout: cancel the pending create.  bt_conn_disconnect() also
         * cancels it; the resulting connected(err) callback normally
         * releases the reference within the grace window below. */
        if (atomic_get(&state) == ST_CONNECTING && conn != NULL) {
            (void)bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
        }
        (void)k_sem_take(&sem_conn, K_SECONDS(1));
        if (atomic_get(&state) == ST_CONNECTING) {
            /* Fix round 1 (late-err reference leak): the err callback is
             * later than the grace window.  Do NOT unref here - the
             * create-reference keeps living in `conn` and its release is
             * owed by the identity-gated connected(err) path (see there);
             * mark the attempt abandoned so the next connect drains or
             * refuses instead of overwriting the live reference. */
            atomic_set(&state, ST_IDLE);
            atomic_set(&conn_abandoned, 1);
            post_debug("ble", "connect timeout, abandoned");
            return BRIDGE_STATUS_ERR_NOT_READY;
        }
        post_debug("ble", "connect timeout");
        return BRIDGE_STATUS_ERR_NOT_READY;
    }

    if (atomic_get(&state) != ST_CONNECTED) {
        /* The err callback consumed the wait: it already released the
         * reference and posted EVT.DEBUG with the HCI cause. */
        return BRIDGE_STATUS_ERR_NOT_READY;
    }

    const bt_addr_le_t *dst = bt_conn_get_dst(conn);

    if (body != NULL && cap >= 7U && body_len != NULL) {
        body[0] = dst->type;
        /* v1.4.2: display order on the wire (see api_connect). */
        for (unsigned int i = 0U; i < BRIDGE_BLE_ADDR_LEN; i++) {
            body[1 + i] = dst->a.val[BRIDGE_BLE_ADDR_LEN - 1U - i];
        }
        *body_len = 7U;
    }
    return BRIDGE_STATUS_OK;
}

/* FR-H: CMD.disconnect semantics.
 *   - empty payload: drop the PERIPHERAL-role link when one exists (the
 *     central link outlives it), otherwise the central link as before;
 *   - 1B addr_type + 6B addr (displayed order): drop whichever link
 *     matches that peer address, peripheral or central. */
static uint8_t api_disconnect(const uint8_t *payload, uint16_t len)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }

    if (len == 7U) {
        uint8_t val[BRIDGE_BLE_ADDR_LEN];

        /* v1.4.2: payload addr is display order; match in Zephyr val order
         * (see api_connect). */
        for (unsigned int i = 0U; i < BRIDGE_BLE_ADDR_LEN; i++) {
            val[i] = payload[1 + BRIDGE_BLE_ADDR_LEN - 1U - i];
        }
        if (bridge_periph_addr_matches(payload[0], val)) {
            return bridge_periph_disconnect();
        }
        if (conn != NULL && atomic_get(&state) == ST_CONNECTED) {
            const bt_addr_le_t *dst = bt_conn_get_dst(conn);

            if (dst->type == payload[0] &&
                memcmp(dst->a.val, val,
                       BRIDGE_BLE_ADDR_LEN) == 0) {
                int err = bt_conn_disconnect(
                    conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);

                return (err == 0) ? BRIDGE_STATUS_OK
                                  : BRIDGE_STATUS_ERR_NOT_READY;
            }
        }
        return BRIDGE_STATUS_ERR_NOT_READY; /* no link with that address */
    }
    if (len != 0U) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }

    if (bridge_periph_active()) {
        return bridge_periph_disconnect();
    }
    if (atomic_get(&state) == ST_CONNECTING && conn != NULL) {
        (void)bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
        return BRIDGE_STATUS_OK; /* cancel; EVT.DISC/err lands via callback */
    }
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    /* Async: the response is status-only; EVT.DISC arrives via callback
     * and state clears in disconnected_cb. */
    int err = bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);

    return (err == 0) ? BRIDGE_STATUS_OK : BRIDGE_STATUS_ERR_NOT_READY;
}

/* ---- api: mtu --------------------------------------------------------------------------- */

static uint8_t api_mtu_get(uint16_t *out_mtu)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    *out_mtu = (uint16_t)atomic_get(&cur_mtu);
    return BRIDGE_STATUS_OK;
}

/* ---- api: gatt (uuid16 + raw-handle addressing) -------------------------- */

static uint8_t read_at_handle(uint16_t handle, uint8_t *data, uint16_t cap,
                              uint16_t *data_len)
{
    /* single op in flight: the whole read/write path is serialised on the
     * link RX thread, so one static params block suffices */
    static struct bt_gatt_read_params params;

    params.func = read_rsp_cb;
    params.handle_count = 1U;
    params.single.handle = handle;
    params.single.offset = 0U;
    gatt_err = 0;
    rd_len = 0U;
    k_sem_reset(&sem_gatt);
    int err = bt_gatt_read(conn, &params);

    if (err != 0) {
        return (uint8_t)(-err); /* raw errno/ATT code; nonzero = failure */
    }
    if (k_sem_take(&sem_gatt, GATT_WAIT) != 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    if (gatt_err != 0) {
        return (uint8_t)gatt_err;
    }
    if (rd_len > cap) {
        rd_len = cap; /* host body cap is 512; unreachable in practice */
    }
    if (rd_len > 0U) {
        memcpy(data, rd_buf, rd_len);
    }
    *data_len = rd_len;
    return BRIDGE_STATUS_OK;
}

static uint8_t api_gatt_read(uint16_t uuid16, uint8_t *data, uint16_t cap,
                             uint16_t *data_len)
{
    bridge_char_ent_t ent;
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    pre = wait_map();
    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    if (!bridge_ble_char_map_find(&char_map, uuid16, &ent)) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD; /* uuid16 not in the map */
    }
    return read_at_handle(ent.value_handle, data, cap, data_len);
}

static uint8_t api_gatt_read_handle(uint16_t handle, uint8_t *data,
                                    uint16_t cap, uint16_t *data_len)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    return read_at_handle(handle, data, cap, data_len); /* no map needed */
}

static uint8_t write_at_handle(uint8_t wtype, uint16_t handle,
                               const uint8_t *data, uint16_t len)
{
    static struct bt_gatt_write_params params;

    if (wtype == 1U) {
        /* Without response: capped by the negotiated MTU-3 (244 at MTU
         * 247).  Chunking is the host's (suota layer) business; the
         * request payload arrives on the RX thread's frame buffer, which
         * is stable for the duration of this synchronous call. */
        uint16_t max_len = (uint16_t)(atomic_get(&cur_mtu) - 3U);

        if (len > max_len) {
            return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        int err = bt_gatt_write_without_response(conn, handle, data, len,
                                                 false);

        return (err == 0) ? BRIDGE_STATUS_OK : (uint8_t)(-err);
    }

    params.func = write_rsp_cb;
    params.handle = handle;
    params.offset = 0U;
    params.data = data;
    params.length = len;
    gatt_err = 0;
    k_sem_reset(&sem_gatt);
    int err = bt_gatt_write(conn, &params);

    if (err != 0) {
        return (uint8_t)(-err);
    }
    if (k_sem_take(&sem_gatt, GATT_WAIT) != 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    if (gatt_err != 0) {
        return (uint8_t)gatt_err;
    }
    return BRIDGE_STATUS_OK;
}

static uint8_t api_gatt_write(uint8_t wtype, uint16_t uuid16,
                              const uint8_t *data, uint16_t len)
{
    bridge_char_ent_t ent;
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    pre = wait_map();
    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    if (!bridge_ble_char_map_find(&char_map, uuid16, &ent)) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    return write_at_handle(wtype, ent.value_handle, data, len);
}

static uint8_t api_gatt_write_handle(uint8_t wtype, uint16_t handle,
                                     const uint8_t *data, uint16_t len)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    return write_at_handle(wtype, handle, data, len);
}

/* CCCD value written by CMD.subscribe: notification in uuid mode is chosen
 * from the discovered properties; the handle-addressed form carries no
 * properties, so it pins the notification value (contract section 4). */
#define CCCD_NOTIFY 0x0001U

static uint8_t subscribe_common(uint16_t key, bool by_handle,
                                uint16_t value_handle, uint16_t ccc_handle,
                                uint16_t cccd_value)
{
    sub_slot_t *slot;

    if (by_handle) {
        if (slot_by_handle(key) != NULL) {
            return BRIDGE_STATUS_OK; /* idempotent */
        }
    } else if (slot_by_uuid(key) != NULL) {
        return BRIDGE_STATUS_OK; /* idempotent */
    }

    slot = NULL;
    for (uint8_t i = 0; i < BRIDGE_BLE_CHAR_MAX; i++) {
        if (!subs[i].active) {
            slot = &subs[i];
            break;
        }
    }
    if (slot == NULL) {
        return BRIDGE_STATUS_ERR_BUSY; /* slot table full */
    }

    memset(&slot->params, 0, sizeof(slot->params));
    slot->uuid16 = key;
    slot->by_handle = by_handle;
    slot->params.notify = notify_cb;
    slot->params.subscribe = ccc_rsp_cb;
    slot->params.value_handle = value_handle;
    slot->params.ccc_handle = ccc_handle;
    slot->params.value = cccd_value;
    slot->active = true; /* stack owns the params from here on */

    sub_err = 0;
    k_sem_reset(&sem_sub);
    int err = bt_gatt_subscribe(conn, &slot->params);

    if (err != 0) {
        slot->active = false;
        return (uint8_t)(-err);
    }
    if (k_sem_take(&sem_sub, GATT_WAIT) != 0) {
        return BRIDGE_STATUS_ERR_NOT_READY; /* CCC write wedged */
    }
    if (sub_err != 0) {
        /* CCC write rejected: the stack dropped the subscription, so the
         * slot is free again (params re-filled on the next subscribe). */
        slot->active = false;
        return (uint8_t)sub_err;
    }
    return BRIDGE_STATUS_OK;
}

static uint8_t api_subscribe(uint16_t uuid16)
{
    bridge_char_ent_t ent;
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    pre = wait_map();
    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    if (!bridge_ble_char_map_find(&char_map, uuid16, &ent)) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    if (ent.ccc_handle == 0U ||
        (ent.props & (BT_GATT_CHRC_NOTIFY | BT_GATT_CHRC_INDICATE)) == 0U) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD; /* char cannot notify */
    }
    uint16_t value = (ent.props & BT_GATT_CHRC_NOTIFY) ? CCCD_NOTIFY : 0x0002U;

    return subscribe_common(uuid16, false, ent.value_handle, ent.ccc_handle,
                            value);
}

static uint8_t api_subscribe_handle(uint16_t value_handle, uint16_t ccc_handle)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    if (ccc_handle == 0U) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    return subscribe_common(value_handle, true, value_handle, ccc_handle,
                            CCCD_NOTIFY);
}

static uint8_t unsubscribe_common(sub_slot_t *slot)
{
    sub_err = 0;
    k_sem_reset(&sem_sub);
    int err = bt_gatt_unsubscribe(conn, &slot->params);

    if (err == -ENOENT || err == -EINVAL) {
        slot->active = false;
        return BRIDGE_STATUS_OK; /* already gone */
    }
    if (err != 0) {
        return (uint8_t)(-err);
    }
    if (k_sem_take(&sem_sub, GATT_WAIT) != 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    slot->active = false; /* params no longer referenced by the stack */
    if (sub_err != 0) {
        return (uint8_t)sub_err;
    }
    return BRIDGE_STATUS_OK;
}

static uint8_t api_unsubscribe(uint16_t uuid16)
{
    sub_slot_t *slot;
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    slot = slot_by_uuid(uuid16);
    if (slot == NULL) {
        return BRIDGE_STATUS_OK; /* idempotent */
    }
    return unsubscribe_common(slot);
}

static uint8_t api_unsubscribe_handle(uint16_t value_handle)
{
    sub_slot_t *slot;
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    slot = slot_by_handle(value_handle);
    if (slot == NULL) {
        return BRIDGE_STATUS_OK; /* idempotent */
    }
    return unsubscribe_common(slot);
}

static uint8_t api_disc_svc(uint16_t svc_uuid16, uint16_t *nchars)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    if (nchars == NULL) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }

    uint16_t svc = svc_uuid16;

    if (svc == 0U) {
        /* Re-discovery placeholder (and the legacy empty-payload shape):
         * run the last successful service again. */
        if (last_svc == 0U) {
            return BRIDGE_STATUS_ERR_NOT_READY; /* nothing discovered yet */
        }
        svc = last_svc;
    }

    k_sem_reset(&sem_dm);
    atomic_set(&dm_done, 0);
    int err = bt_gatt_dm_start(conn, BT_UUID_DECLARE_16(svc), &dm_cb, NULL);

    if (err != 0) {
        char msg[32];

        (void)snprintf(msg, sizeof(msg), "disc_svc start: %d", err);
        post_debug("ble", msg);
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    /* Block the (serialised) link RX thread for the discovery; the link
     * stays up whichever way it concludes. */
    if (k_sem_take(&sem_dm, DM_WAIT) != 0) {
        post_debug("ble", "disc_svc wait timeout");
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    if (atomic_get(&dm_done) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY; /* not found / dm error */
    }
    last_svc = svc;
    *nchars = (uint16_t)atomic_get(&dm_nchars);
    return BRIDGE_STATUS_OK;
}

/* ---- api: pair (FR-B5) ------------------------------------------------------ */

#if defined(CONFIG_BT_SMP)
static void pair_report(uint8_t *body, uint16_t cap, uint16_t *body_len)
{
    if (body == NULL || body_len == NULL || cap < 2U) {
        return;
    }
    body[0] = (bt_conn_enc_key_size(conn) > 0U) ? 1U : 0U; /* encrypted */
    body[1] = (uint8_t)bt_conn_get_security(conn);         /* sec level  */
    *body_len = 2U;
}
#endif

static uint8_t api_pair(uint8_t *body, uint16_t cap, uint16_t *body_len)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    if (cap < 2U || body == NULL || body_len == NULL) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
#if defined(CONFIG_BT_SMP)
    /* Already secured to the target: bt_conn_set_security() would take the
     * "nothing to do" path and never fire security_changed, so answer from
     * the live state instead (idempotent re-pair). */
    if (bt_conn_get_security(conn) >= PAIR_TARGET) {
        pair_report(body, cap, body_len);
        return BRIDGE_STATUS_OK;
    }

    /* Single in-flight procedure: the dispatcher serialises commands on the
     * link RX thread, so reset-then-wait below cannot interleave with
     * another CMD.pair.  A peer-initiated SMP procedure landing between the
     * reset and set_security() is indistinguishable from our own result and
     * satisfies the same contract (link secured to >= L2). */
    k_sem_reset(&sem_pair);
    atomic_set(&pair_state, PAIR_PENDING);
    atomic_set(&pair_level, 0);

    int err = bt_conn_set_security(conn, PAIR_TARGET);

    if (err != 0) {
        char msg[32];

        (void)snprintf(msg, sizeof(msg), "set_security: %d", err);
        post_debug("ble", msg);
        return BRIDGE_STATUS_ERR_PAIR_FAILED;
    }
    /* Blocking wait, bounded by BRIDGE_PAIR_TIMEOUT_MS (Kconfig, default
     * 8 s).  The link RX thread parks here; BLE callbacks keep running on
     * BT threads and the UART event plane keeps draining (no link lock is
     * taken).  The host asks the bridge to pair before its first MEM_DEV
     * write, so the added latency is one command, once per session. */
    if (k_sem_take(&sem_pair, K_MSEC(CONFIG_BRIDGE_PAIR_TIMEOUT_MS)) != 0) {
        post_debug("ble", "pair timeout");
        return BRIDGE_STATUS_ERR_PAIR_FAILED;
    }
    if (atomic_get(&pair_state) == PAIR_LINK_LOST) {
        return BRIDGE_STATUS_ERR_NOT_READY; /* link died mid-pair */
    }
    if (atomic_get(&pair_state) != PAIR_OK) {
        return BRIDGE_STATUS_ERR_PAIR_FAILED;
    }
    pair_report(body, cap, body_len);
    return BRIDGE_STATUS_OK;
#else
    ARG_UNUSED(body);
    ARG_UNUSED(cap);
    ARG_UNUSED(body_len);
    return BRIDGE_STATUS_ERR_NOT_READY; /* SMP not compiled in */
#endif
}

static uint8_t api_conn_update(uint16_t interval_min, uint16_t interval_max,
                               uint16_t latency, uint16_t timeout)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    /* Encoding per the LE spec (host passes 1.25 ms / 10 ms units raw);
     * range sanity is the controller stack's job (-EINVAL surfaces). */
    const struct bt_le_conn_param param =
        BT_LE_CONN_PARAM_INIT(interval_min, interval_max, latency, timeout);
    int err = bt_conn_le_param_update(conn, &param);

    if (err != 0) {
        char msg[32];

        (void)snprintf(msg, sizeof(msg), "param update: %d", err);
        post_debug("ble", msg);
        return (uint8_t)(-err);
    }
    /* Async: the settled parameters arrive via le_param_updated (EVT.DEBUG
     * "conn params: ..." above); the response only confirms submission. */
    return BRIDGE_STATUS_OK;
}

static uint8_t api_phy_update(uint8_t phy)
{
    uint8_t pre = need_connected();

    if (pre != BRIDGE_STATUS_OK) {
        return pre;
    }
    if (phy != 1U && phy != 2U) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD; /* 1 = 1M, 2 = 2M only */
    }
    uint8_t gap_phy = (phy == 2U) ? BT_GAP_LE_PHY_2M : BT_GAP_LE_PHY_1M;
    struct bt_conn_le_phy_param param = {
        .pref_tx_phy = gap_phy,
        .pref_rx_phy = gap_phy,
    };
    int err = bt_conn_le_phy_update(conn, &param);

    if (err != 0) {
        char msg[32];

        (void)snprintf(msg, sizeof(msg), "phy update: %d", err);
        post_debug("ble", msg);
        return (uint8_t)(-err);
    }
    /* Async: the result arrives via le_phy_updated (EVT.DEBUG "phy: ..."). */
    return BRIDGE_STATUS_OK;
}

static void api_shutdown(void)
{
    /* Best-effort, RX thread, right before CMD.reset reboots. */
    bridge_periph_shutdown(); /* adv off + peripheral link down */
    if (conn != NULL && atomic_get(&state) != ST_IDLE) {
        (void)bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    }
}

/* ---- api: peripheral family (FR-H; implemented in bridge_periph.c) -------- */

static uint8_t api_adv_start(uint16_t interval_ms, uint8_t name_on)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    return bridge_periph_adv_start(interval_ms, name_on != 0U);
}

static uint8_t api_adv_stop(void)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    return bridge_periph_adv_stop();
}

static uint8_t api_notify(uint8_t mode, uint16_t uuid16,
                          const uint8_t *data, uint16_t len)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    return bridge_periph_notify(mode, uuid16, data, len);
}

static uint8_t api_set_char(uint8_t mode, uint16_t uuid16,
                            const uint8_t *data, uint16_t len)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    return bridge_periph_set_char(mode, uuid16, data, len);
}

static uint8_t api_gatt_define(const uint8_t *payload, uint16_t len,
                               uint8_t *body, uint16_t cap,
                               uint16_t *body_len)
{
    if (atomic_get(&ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    return bridge_periph_gatt_define(payload, len, body, cap, body_len);
}

/* ---- api table / init ------------------------------------------------------------------------ */

static const bridge_ble_api_t ble_api = {
    .scan_start = api_scan_start,
    .scan_stop = api_scan_stop,
    .connect = api_connect,
    .disconnect = api_disconnect,
    .mtu_get = api_mtu_get,
    .gatt_read = api_gatt_read,
    .gatt_read_handle = api_gatt_read_handle,
    .gatt_write = api_gatt_write,
    .gatt_write_handle = api_gatt_write_handle,
    .subscribe = api_subscribe,
    .unsubscribe = api_unsubscribe,
    .subscribe_handle = api_subscribe_handle,
    .unsubscribe_handle = api_unsubscribe_handle,
    .disc_svc = api_disc_svc,
    .pair = api_pair,
    .conn_update = api_conn_update,
    .phy_update = api_phy_update,
    .adv_start = api_adv_start,
    .adv_stop = api_adv_stop,
    .notify = api_notify,
    .set_char = api_set_char,
    .gatt_define = api_gatt_define,
    .shutdown = api_shutdown,
};

bridge_ble_api_t *bridge_ble_api(void)
{
    return (bridge_ble_api_t *)&ble_api;
}

static struct bt_gatt_cb gatt_cb = {
    .att_mtu_updated = mtu_changed_cb,
};

int bridge_ble_init(void)
{
    int err;

    k_sem_init(&sem_conn, 0, 1);
    k_sem_init(&sem_dm, 0, 1);
    k_sem_init(&sem_gatt, 0, 1);
    k_sem_init(&sem_sub, 0, 1);
    k_sem_init(&sem_pair, 0, 1);

    bridge_ble_scan_cache_init(&scan_cache);
    bridge_ble_char_map_clear(&char_map);
    atomic_set(&state, ST_IDLE);
    atomic_set(&ready, 0);
    atomic_set(&cur_mtu, 0);
    atomic_set(&dm_done, 0);
    atomic_set(&dm_nchars, 0);
    atomic_set(&conn_abandoned, 0);
    atomic_set(&pair_state, PAIR_PENDING);
    atomic_set(&pair_level, 0);
    last_svc = 0U;
    subs_table_clear();

    err = bt_enable(NULL);
    if (err != 0) {
        return err;
    }

    bt_conn_cb_register(&conn_cb);
#if defined(CONFIG_BT_SMP)
    /* pairing_complete/pairing_failed live on the auth-info callback list
     * (Zephyr 4.4 conn.h), not on struct bt_conn_cb; registration is
     * runtime, the struct itself is static (the list keeps it forever). */
    err = bt_conn_auth_info_cb_register(&auth_info_cb);
    if (err != 0 && err != -EALREADY) {
        return err;
    }
#endif
    bt_gatt_cb_register(&gatt_cb);
    bt_le_scan_cb_register(&scan_cb);

    /* FR-H: peripheral role (advertising + test services).  Runs after
     * bt_enable() so the preset service handles are assigned. */
    err = bridge_periph_init();
    if (err != 0) {
        return err;
    }

    atomic_set(&ready, 1);
    post_debug("ble", "bt ready");
    return 0;
}
