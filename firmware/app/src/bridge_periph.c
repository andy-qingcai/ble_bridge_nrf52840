/*
 * bridge_periph.c - BLE peripheral role (FR-H): advertising, the preset
 * 0xFFE0 test service and host-defined GATT services.  See bridge_periph.h
 * for the surface and the thread model; docs/protocol-contract.md is the
 * cross-end wire contract.
 */

#include "bridge_periph.h"

#include <errno.h>
#include <stdio.h>
#include <stdatomic.h>
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/kernel.h>

#include "bridge_ble_pure.h"
#include "bridge_dispatch.h"
#include "bridge_link.h"

/* ---- small helpers -------------------------------------------------------- */

static void periph_debug(const char *tag, const char *msg)
{
    uint8_t payload[64];
    int plen = bridge_evt_debug_build(tag, msg, payload,
                                      (uint16_t)sizeof(payload));

    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_DEBUG, payload,
                                     (uint16_t)plen);
    }
}

/* EVT.GATTX peripheral-role write event: subtype 0x01 =
 * [u16 LE uuid16][u16 LE value handle][data].  Payload cap is 256
 * (CONFIG_BRIDGE_EVENT_PAYLOAD_MAX) so a full 244-byte write fits. */
static void post_write_event(uint16_t uuid16, uint16_t handle,
                             const uint8_t *data, uint16_t len)
{
    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    int plen = bridge_ble_evt_gattx_write_build(uuid16, handle, data, len,
                                                payload, sizeof(payload));

    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_GATTX, payload,
                                     (uint16_t)plen);
    }
}

static atomic_t periph_ready;

/* ---- preset test service (0xFFE0) ----------------------------------------
 *
 * Compile-time registered "out of the box" service:
 *   0xFFE0        primary service
 *   0xFFE1 char A: Read + Write Without Response - a peer write is stored
 *                  and echoed to the host as an EVT.GATTX peripheral-role
 *                  event (subtype 0x01); CMD.set_char updates the same
 *                  value locally.
 *   0xFFE2 char B: Read + Notify (CCCD) - CMD.notify pushes the content.
 * Plain-text permissions on purpose (no bond, no pairing).               */

#define PRESET_SVC_UUID16 0xFFE0U
#define PRESET_CHR_A_UUID16 0xFFE1U
#define PRESET_CHR_B_UUID16 0xFFE2U

/* Default readable value of char B until the host pushes one. */
static const uint8_t chr_b_default[] = { 'b', 'r', 'i', 'd', 'g', 'e' };

/* Largest value a locally served characteristic holds or notifies: local
 * ATT MTU minus the 3-byte ATT handle allowance. */
#define LOCAL_VALUE_MAX (CONFIG_BT_L2CAP_TX_MTU - 3U)

static uint8_t chr_a_buf[LOCAL_VALUE_MAX];
static uint16_t chr_a_len;
static uint16_t chr_a_handle; /* value handle, resolved in init */

static uint8_t chr_b_buf[LOCAL_VALUE_MAX];
static uint16_t chr_b_len;

/* CCCD subscription state of the preset notify characteristic (FR-H fix
 * round 1): written from the BT RX thread via the cfg_changed callback
 * (value != 0) and cleared on link loss, read by CMD.notify on the link
 * RX thread.  The CCCD attribute itself sits right after the 0xFFE2 value
 * attribute in preset_svc below (attr [5], uuid 0x2902, PERM_READ|WRITE)
 * -- it is the subscription handle on the air, never remove it. */
static atomic_t chr_b_subscribed;

/* Guards the preset value buffers: written from the BT RX thread (peer
 * writes / reads) and the link RX thread (CMD.set_char / CMD.notify). */
static struct k_mutex preset_lock;

static ssize_t chr_a_read_cb(struct bt_conn *conn,
                             const struct bt_gatt_attr *attr, void *buf,
                             uint16_t len, uint16_t offset)
{
    ssize_t rc;

    ARG_UNUSED(attr);
    k_mutex_lock(&preset_lock, K_FOREVER);
    rc = bt_gatt_attr_read(conn, attr, buf, len, offset, chr_a_buf,
                           chr_a_len);
    k_mutex_unlock(&preset_lock);
    return rc;
}

static ssize_t chr_b_read_cb(struct bt_conn *conn,
                             const struct bt_gatt_attr *attr, void *buf,
                             uint16_t len, uint16_t offset)
{
    ssize_t rc;

    ARG_UNUSED(attr);
    k_mutex_lock(&preset_lock, K_FOREVER);
    rc = bt_gatt_attr_read(conn, attr, buf, len, offset, chr_b_buf,
                           chr_b_len);
    k_mutex_unlock(&preset_lock);
    return rc;
}

static ssize_t chr_a_write_cb(struct bt_conn *conn,
                              const struct bt_gatt_attr *attr,
                              const void *buf, uint16_t len, uint16_t offset,
                              uint8_t flags)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(attr);
    ARG_UNUSED(flags);

    if (offset != 0U) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
    }
    if (len > sizeof(chr_a_buf)) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }

    k_mutex_lock(&preset_lock, K_FOREVER);
    memcpy(chr_a_buf, buf, len);
    chr_a_len = len;
    k_mutex_unlock(&preset_lock);

    post_write_event(PRESET_CHR_A_UUID16, chr_a_handle,
                     (const uint8_t *)buf, len);
    return (ssize_t)len;
}

static void chr_b_ccc_changed_cb(const struct bt_gatt_attr *attr,
                                 uint16_t value)
{
    char msg[24];

    ARG_UNUSED(attr);
    atomic_set(&chr_b_subscribed, (value & BT_GATT_CCC_NOTIFY) != 0U);
    (void)snprintf(msg, sizeof(msg), "notify ccc: %u", (unsigned)value);
    periph_debug("periph", msg);
}

/* fix round 2 ("shadow CCCD"): the CCCD state MUST live in writable RAM.
 * The bare BT_GATT_CCC() macro embeds an anonymous compound literal into
 * the const attrs array of BT_GATT_SERVICE_DEFINE, so the instance landed
 * in flash: bt_gatt_attr_write_ccc "succeeded" at the ATT layer while the
 * cfg/value writes were silently dropped -- no subscriber was ever
 * registered, cfg_changed never fired and CMD.notify answered 0x74
 * (real device 2026-09-26).  A named static instance wired explicitly
 * below removes the placement ambiguity; cfg[] holds BT_GATT_CCC_MAX
 * peer entries (MAX_PAIRED + MAX_CONN = 3 on this build, >= 2). */
static struct bt_gatt_ccc_managed_user_data chr_b_ccc = {
    .cfg_changed = chr_b_ccc_changed_cb,
};

BT_GATT_SERVICE_DEFINE(preset_svc,
    BT_GATT_PRIMARY_SERVICE(BT_UUID_DECLARE_16(PRESET_SVC_UUID16)),
    /* char A (0xFFE1): Read + Write Without Response */
    BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(PRESET_CHR_A_UUID16),
        BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
        BT_GATT_PERM_READ | BT_GATT_PERM_WRITE,
        chr_a_read_cb, chr_a_write_cb, NULL),
    /* char B (0xFFE2): Read + Notify */
    BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(PRESET_CHR_B_UUID16),
        BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
        BT_GATT_PERM_READ,
        chr_b_read_cb, NULL, NULL),
    /* attr [5]: CCCD (0x2902) of char B -- the on-air subscription handle
     * for CMD.notify, wired to the named static instance above (fix round
     * 2; BT_GATT_CCC_MANAGED = user_data &chr_b_ccc + the stack's
     * read/write handlers).  Never go back to the bare BT_GATT_CCC()
     * compound-literal form here: it lands in rodata and silently kills
     * subscription registration. */
    BT_GATT_CCC_MANAGED(&chr_b_ccc, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* Attribute indices inside preset_svc: [0] svc, [1] decl A, [2] value A,
 * [3] decl B, [4] value B, [5] CCCD. */
#define PRESET_IDX_CHR_A_VALUE 2U
#define PRESET_IDX_CHR_B_VALUE 4U

/* ---- host-defined GATT services (CMD.gatt_define) -------------------------
 *
 * One slot per service; every structure the stack keeps pointers into is
 * static inside the slot (review red line inherited from the central
 * subscription design).  Value bytes bump-allocate from the slot's own
 * pool, so a re-definition simply rebuilds that slot - no cross-slot
 * fragmentation is possible.                                           */

#define DYN_CH_MAX CONFIG_BRIDGE_GATT_DYN_CHARS_MAX
#define DYN_POOL_SIZE CONFIG_BRIDGE_GATT_DYN_SVC_POOL

/* wire props byte (CMD.gatt_define) -> BT_GATT_CHRC_* mapping */
#define DYN_PROP_READ 0x01U
#define DYN_PROP_WRITE_WO_RESP 0x02U
#define DYN_PROP_WRITE 0x04U
#define DYN_PROP_NOTIFY 0x08U

/* Attribute layout per service slot (docs/protocol-contract.md):
 *   [0] primary service declaration
 *   per char: [decl][value] (+ [CCCD] when notify-capable)
 * The encoded (slot<<8 | char) index rides in a value attribute's
 * user_data so the shared read/write callbacks find their storage. */
#define DYN_ATTR_MAX (1U + 3U * DYN_CH_MAX)
#define DYN_UDX(slot_idx, chr_idx) \
    ((void *)(uintptr_t)((((uint16_t)(slot_idx)) << 8) | (chr_idx)))
#define DYN_UDX_SLOT(ud) ((uint8_t)(((uintptr_t)(ud) >> 8) & 0xFFU))
#define DYN_UDX_CHR(ud) ((uint8_t)((uintptr_t)(ud) & 0xFFU))

typedef struct {
    bool used;
    uint16_t svc_uuid16;
    uint8_t nchars;
    struct bt_gatt_service svc;  /* registered object (stack keeps it) */
    struct bt_uuid_16 svc_uuid;  /* service declaration value          */
    struct bt_uuid_16 chr_uuid[DYN_CH_MAX];
    struct bt_gatt_chrc chrc[DYN_CH_MAX]; /* declaration values        */
    uint16_t max_len[DYN_CH_MAX];
    uint16_t cur_len[DYN_CH_MAX];
    uint8_t *val[DYN_CH_MAX];    /* bump-allocated from the slot pool   */
    uint16_t val_handle[DYN_CH_MAX];
    atomic_t sub_flag[DYN_CH_MAX]; /* CCCD subscription state (fix r1)  */
    struct bt_gatt_ccc_managed_user_data ccc[DYN_CH_MAX];
    struct bt_gatt_attr attrs[DYN_ATTR_MAX];
    uint8_t pool[DYN_POOL_SIZE];
} dyn_slot_t;

static dyn_slot_t dyn_slots[CONFIG_BRIDGE_GATT_DYN_SVCS];

static void dyn_slot_reset(dyn_slot_t *slot)
{
    memset(slot, 0, sizeof(*slot));
}

/* Guards every dyn_slots field the BT threads touch (GATT server
 * callbacks) against the link RX thread (gatt_define / notify / set_char).
 * bt_gatt_notify is called outside the lock; the stack never calls back
 * into these callbacks, so no lock inversion exists. */
static struct k_mutex dyn_lock;

/* Shared CCC attr callbacks (bt_gatt_attr_read_ccc/write_ccc require the
 * static bt_gatt_ccc_managed_user_data per notify characteristic). */
static void dyn_ccc_changed_cb(const struct bt_gatt_attr *attr,
                               uint16_t value)
{
    char msg[40];
    uint8_t s, c;
    const struct bt_gatt_ccc_managed_user_data *ccc = attr->user_data;

    for (s = 0U; s < ARRAY_SIZE(dyn_slots); s++) {
        for (c = 0U; c < dyn_slots[s].nchars; c++) {
            if (&dyn_slots[s].ccc[c] == ccc) {
                /* fix round 1: track the CCCD subscription state so
                 * CMD.notify can answer 0x74 without touching the air. */
                atomic_set(&dyn_slots[s].sub_flag[c],
                           (value & BT_GATT_CCC_NOTIFY) != 0U);
                (void)snprintf(msg, sizeof(msg),
                               "svc 0x%04X ccc: %u",
                               (unsigned)dyn_slots[s].svc_uuid16,
                               (unsigned)value);
                periph_debug("periph", msg);
                return;
            }
        }
    }
}

static ssize_t dyn_read_cb(struct bt_conn *conn,
                           const struct bt_gatt_attr *attr, void *buf,
                           uint16_t len, uint16_t offset)
{
    dyn_slot_t *slot = &dyn_slots[DYN_UDX_SLOT(attr->user_data)];
    uint8_t i = DYN_UDX_CHR(attr->user_data);
    ssize_t rc;

    k_mutex_lock(&dyn_lock, K_FOREVER);
    rc = bt_gatt_attr_read(conn, attr, buf, len, offset, slot->val[i],
                           slot->cur_len[i]);
    k_mutex_unlock(&dyn_lock);
    return rc;
}

static ssize_t dyn_write_cb(struct bt_conn *conn,
                            const struct bt_gatt_attr *attr,
                            const void *buf, uint16_t len, uint16_t offset,
                            uint8_t flags)
{
    dyn_slot_t *slot = &dyn_slots[DYN_UDX_SLOT(attr->user_data)];
    uint8_t i = DYN_UDX_CHR(attr->user_data);
    uint16_t uuid16;
    uint16_t handle;

    ARG_UNUSED(conn);
    ARG_UNUSED(flags);

    if (offset != 0U) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
    }
    k_mutex_lock(&dyn_lock, K_FOREVER);
    if (len > slot->max_len[i]) {
        k_mutex_unlock(&dyn_lock);
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }
    memcpy(slot->val[i], buf, len);
    slot->cur_len[i] = len;
    uuid16 = slot->chr_uuid[i].val;
    handle = slot->val_handle[i];
    k_mutex_unlock(&dyn_lock);

    post_write_event(uuid16, handle, (const uint8_t *)buf, len);
    return (ssize_t)len;
}

/* ---- CMD.notify / CMD.set_char characteristic addressing ------------------
 *
 * DEFAULT mode (0) picks the preset characteristic of the command kind
 * (set_char -> 0xFFE1, notify -> 0xFFE2).  UUID16 mode (1) searches the
 * host-defined services first (the newest definition wins a collision),
 * then the preset service.                                               */

/* A resolved target view.  len_field points at the live length variable so
 * the caller can update the value under the right lock; sub_flag (notify
 * targets only) carries the CCCD subscription state, NULL otherwise. */
typedef struct {
    const struct bt_gatt_attr *value_attr;
    uint8_t *val_buf;
    uint16_t *len_field;
    atomic_t *sub_flag;
    uint16_t max_len;
    uint16_t handle;
    bool dyn; /* true: guard with dyn_lock, false: preset_lock */
} served_char_t;

static int preset_find_char(uint16_t uuid16, bool need_notify,
                            served_char_t *out)
{
    if (uuid16 == PRESET_CHR_A_UUID16) {
        if (need_notify) {
            return -ENOTSUP; /* exists but cannot notify */
        }
        out->value_attr = &preset_svc.attrs[PRESET_IDX_CHR_A_VALUE];
        out->val_buf = chr_a_buf;
        out->len_field = &chr_a_len;
        out->sub_flag = NULL;
        out->max_len = (uint16_t)sizeof(chr_a_buf);
        out->handle = chr_a_handle;
        out->dyn = false;
        return 0;
    }
    if (uuid16 == PRESET_CHR_B_UUID16) {
        /* set_char (need_notify = false) is allowed too: it only updates
         * the readable value, the CCCD stays untouched. */
        out->value_attr = &preset_svc.attrs[PRESET_IDX_CHR_B_VALUE];
        out->val_buf = chr_b_buf;
        out->len_field = &chr_b_len;
        out->sub_flag = &chr_b_subscribed;
        out->max_len = (uint16_t)sizeof(chr_b_buf);
        out->handle = 0U;
        out->dyn = false;
        return 0;
    }
    return -ENOENT;
}

static int dyn_find_char(uint16_t uuid16, bool need_notify,
                         served_char_t *out)
{
    uint8_t s, c;

    for (s = 0U; s < ARRAY_SIZE(dyn_slots); s++) {
        dyn_slot_t *slot = &dyn_slots[s];

        if (!slot->used) {
            continue;
        }
        for (c = 0U; c < slot->nchars; c++) {
            if (slot->chr_uuid[c].val != uuid16) {
                continue;
            }
            if (need_notify &&
                (slot->chrc[c].properties & BT_GATT_CHRC_NOTIFY) == 0U) {
                return -ENOTSUP;
            }
            /* walk the documented layout to the value attribute */
            uint16_t idx = 1U; /* past the service declaration */

            for (uint8_t j = 0U;; j++) {
                idx++; /* declaration */
                if (j == c) {
                    break;
                }
                idx++; /* value */
                if ((slot->chrc[j].properties & BT_GATT_CHRC_NOTIFY) != 0U) {
                    idx++; /* CCCD */
                }
            }
            out->value_attr = &slot->attrs[idx];
            out->val_buf = slot->val[c];
            out->len_field = &slot->cur_len[c];
            out->sub_flag = &slot->sub_flag[c];
            out->max_len = slot->max_len[c];
            out->handle = slot->val_handle[c];
            out->dyn = true;
            return 0;
        }
    }
    return -ENOENT;
}

static int resolve_char(uint8_t mode, uint16_t uuid16, bool need_notify,
                        served_char_t *out)
{
    int rc;

    if (mode == BRIDGE_CHAR_ADDR_DEFAULT) {
        uuid16 = need_notify ? PRESET_CHR_B_UUID16 : PRESET_CHR_A_UUID16;
    }
    rc = dyn_find_char(uuid16, need_notify, out);
    if (rc == 0 || rc == -ENOTSUP) {
        return rc;
    }
    return preset_find_char(uuid16, need_notify, out);
}

uint8_t bridge_periph_notify(uint8_t mode, uint16_t uuid16,
                             const uint8_t *data, uint16_t len)
{
    served_char_t target;
    int rc;
    int err;

    if (len > LOCAL_VALUE_MAX) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    rc = resolve_char(mode, uuid16, true, &target);
    if (rc == -ENOTSUP || rc != 0) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD; /* unknown / cannot notify */
    }
    if (len > target.max_len) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    /* fix round 1: CCCD subscription precondition -- without a subscribed
     * peer there is nothing to notify; answer 0x74 without touching the
     * characteristic value (bt_gatt_notify would return -ENOTCONN anyway,
     * checking first keeps the pushed-value semantics consistent). */
    if (atomic_get(target.sub_flag) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY; /* no subscribed peer (0x74) */
    }

    /* The pushed content becomes the readable value too; the notification
     * itself carries `data` (stable for this synchronous call - the request
     * payload lives in the RX thread frame buffer, same pattern as the
     * central gatt_write path). */
    if (target.dyn) {
        k_mutex_lock(&dyn_lock, K_FOREVER);
    } else {
        k_mutex_lock(&preset_lock, K_FOREVER);
    }
    memcpy(target.val_buf, data, len);
    *target.len_field = len;
    if (target.dyn) {
        k_mutex_unlock(&dyn_lock);
    } else {
        k_mutex_unlock(&preset_lock);
    }

    /* conn = NULL: notify every subscribed peer.  -ENOTCONN when nobody is
     * subscribed (verified against Zephyr gatt.c bt_gatt_notify_cb). */
    err = bt_gatt_notify(NULL, target.value_attr, data, len);

    if (err == 0) {
        return BRIDGE_STATUS_OK;
    }
    if (err == -ENOTCONN || err == -ENOENT) {
        return BRIDGE_STATUS_ERR_NOT_READY; /* no subscribed peer (0x74) */
    }
    if (err == -ENOMEM) {
        return BRIDGE_STATUS_ERR_BUSY; /* TX buffers exhausted (0x73) */
    }
    return BRIDGE_STATUS_ERR_BAD_PAYLOAD; /* -EINVAL etc. */
}

uint8_t bridge_periph_set_char(uint8_t mode, uint16_t uuid16,
                               const uint8_t *data, uint16_t len)
{
    served_char_t target;
    int rc;

    rc = resolve_char(mode, uuid16, false, &target);
    if (rc != 0) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD; /* unknown characteristic */
    }
    if (len > target.max_len) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    if (target.dyn) {
        k_mutex_lock(&dyn_lock, K_FOREVER);
    } else {
        k_mutex_lock(&preset_lock, K_FOREVER);
    }
    memcpy(target.val_buf, data, len);
    *target.len_field = len;
    if (target.dyn) {
        k_mutex_unlock(&dyn_lock);
    } else {
        k_mutex_unlock(&preset_lock);
    }
    return BRIDGE_STATUS_OK;
}

/* ---- advertising ---------------------------------------------------------- */

/* ms -> 0.625 ms advertising interval units, clamped to the LE range. */
static uint16_t adv_interval_units(uint16_t interval_ms)
{
    uint32_t units = ((uint32_t)interval_ms * 8U + 4U) / 5U;

    if (units < 0x20U) {
        units = 0x20U; /* 20 ms minimum */
    }
    if (units > 0x4000U) {
        units = 0x4000U; /* 10.24 s maximum */
    }
    return (uint16_t)units;
}

/* Advertising intent (fix round 1): set by CMD.adv_start, cleared only by
 * CMD.adv_stop -- a lost peripheral link restarts the last configuration
 * while it is set (BRIDGE_ADV_AUTO_RESTART).  Written on the link RX
 * thread, read on the BT RX thread / sysworkq: word-sized loads, benign
 * (same accepted discipline as periph_conn below). */
static atomic_t adv_wanted;
/* Last successfully applied configuration (auto-restart parameters). */
static uint16_t adv_last_units;
static bool adv_last_name;

/* Feed one event into the pure intent state machine (atomic_t <-> bool). */
static void adv_intent_event(bridge_adv_evt_t evt)
{
    bool wanted = atomic_get(&adv_wanted) != 0;

    (void)bridge_ble_adv_intent_step(&wanted, evt);
    atomic_set(&adv_wanted, wanted);
}

static int adv_launch(uint16_t units, bool name_on)
{
    struct bt_data ad[] = {
        BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),
        BT_DATA_BYTES(BT_DATA_UUID16_ALL, 0xE0U, 0xFFU),
    };
    struct bt_data sd[] = {
        BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BRIDGE_ADV_NAME,
                (uint8_t)sizeof(CONFIG_BRIDGE_ADV_NAME) - 1U),
    };
    struct bt_le_adv_param param = *BT_LE_ADV_CONN_FAST_2;

    /* Connectable, general discoverable, no BR/EDR.  The preset service
     * UUID rides the advertising packet; the optional local name rides the
     * scan response. */
    param.interval_min = units;
    param.interval_max = units;

    return bt_le_adv_start(&param, ad, ARRAY_SIZE(ad),
                           name_on ? sd : NULL, name_on ? 1U : 0U);
}

uint8_t bridge_periph_adv_start(uint16_t interval_ms, bool name_on)
{
    char msg[48];
    int err;

    if (interval_ms == 0xFFFFU) {
        interval_ms = (uint16_t)CONFIG_BRIDGE_ADV_INTERVAL_DEFAULT_MS;
    }
    uint16_t units = adv_interval_units(interval_ms);

    err = adv_launch(units, name_on);
    if (err == -EALREADY) {
        /* idempotent: already advertising (keep the RUNNING parameters as
         * the restart configuration; a change needs adv_stop first) */
        adv_intent_event(BRIDGE_ADV_EV_START);
        periph_debug("periph", "adv: already on");
        return BRIDGE_STATUS_OK;
    }
    if (err != 0) {
        (void)snprintf(msg, sizeof(msg), "adv start: %d", err);
        periph_debug("periph", msg);
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    adv_intent_event(BRIDGE_ADV_EV_START);
    adv_last_units = units;
    adv_last_name = name_on;
    (void)snprintf(msg, sizeof(msg), "adv: on %ums name=%u",
                   (unsigned)interval_ms, (unsigned)name_on);
    periph_debug("periph", msg);
    return BRIDGE_STATUS_OK;
}

uint8_t bridge_periph_adv_stop(void)
{
    char msg[24];
    int err = bt_le_adv_stop();

    if (err == -EALREADY) {
        /* not advertising; an explicit stop must also silence the
         * auto-restart (this is the "user wants it off" signal) */
        adv_intent_event(BRIDGE_ADV_EV_STOP);
        periph_debug("periph", "adv: already off");
        return BRIDGE_STATUS_OK; /* idempotent */
    }
    if (err != 0) {
        (void)snprintf(msg, sizeof(msg), "adv stop: %d", err);
        periph_debug("periph", msg);
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    adv_intent_event(BRIDGE_ADV_EV_STOP);
    periph_debug("periph", "adv: off");
    return BRIDGE_STATUS_OK;
}

/* ---- peripheral-role connection (own bt_conn_cb, gated on role) ----------- */

/* Single peripheral link by design (CONFIG_BT_MAX_CONN = 2 = 1 central + 1
 * peripheral).  Same documented single-pointer discipline as the central
 * `conn` in bridge_ble.c: adopted reference released exactly once in the
 * disconnected callback. */
static struct bt_conn *periph_conn;

static void periph_connected_cb(struct bt_conn *cb_conn, uint8_t err)
{
    struct bt_conn_info info;
    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    const bt_addr_le_t *dst;
    int plen;

    if (err != 0U) {
        return; /* peripheral accepts fail before this callback */
    }
    if (bt_conn_get_info(cb_conn, &info) != 0 ||
        info.role != BT_CONN_ROLE_PERIPHERAL) {
        return; /* central-role link: owned by bridge_ble.c */
    }
    if (periph_conn != NULL) {
        /* One peripheral link by design; refuse further ones. */
        (void)bt_conn_disconnect(cb_conn,
                                 BT_HCI_ERR_REMOTE_USER_TERM_CONN);
        periph_debug("periph", "second link refused");
        return;
    }
    periph_conn = bt_conn_ref(cb_conn); /* our reference; released below */

    dst = bt_conn_get_dst(cb_conn);
    plen = bridge_ble_evt_conn_build(BRIDGE_BLE_ROLE_PERIPHERAL, dst->type,
                                     dst->a.val, payload, sizeof(payload));
    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_CONN, payload,
                                     (uint16_t)plen);
    }
    periph_debug("periph", "peripheral connected");
}

/* fix round 1: link loss must not silently end advertising.  Runs on the
 * sysworkq -- the disconnected callback itself runs on the BT RX thread,
 * where a synchronous bt_le_adv_start (blocking HCI) must not be called. */
static void adv_restart_work_fn(struct k_work *work)
{
    char msg[32];
    int err;

    ARG_UNUSED(work);
    /* Re-check the intent: the user may have sent CMD.adv_stop between the
     * link loss and this work running (the serial plane never stopped). */
    if (atomic_get(&adv_wanted) == 0) {
        return;
    }
    err = adv_launch(adv_last_units, adv_last_name);
    if (err == 0) {
        periph_debug("periph", "adv: restarted after link loss");
    } else {
        (void)snprintf(msg, sizeof(msg), "adv restart: %d", err);
        periph_debug("periph", msg);
    }
}
static K_WORK_DEFINE(adv_restart_work, adv_restart_work_fn);

static void periph_disconnected_cb(struct bt_conn *cb_conn, uint8_t reason)
{
    uint8_t payload[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
    int plen;

    if (cb_conn != periph_conn) {
        return;
    }
    plen = bridge_ble_evt_disc_build(BRIDGE_BLE_ROLE_PERIPHERAL, reason,
                                     payload, sizeof(payload));
    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_DISC, payload,
                                     (uint16_t)plen);
    }
    bt_conn_unref(periph_conn); /* release the adopted reference once */
    periph_conn = NULL;
    /* fix round 1: the stack clears CCC configs on link loss (cfg_changed
     * fires with 0); clear our subscription mirrors too, in case the
     * callback ordering ever differs. */
    atomic_set(&chr_b_subscribed, 0);
    for (uint8_t s = 0U; s < ARRAY_SIZE(dyn_slots); s++) {
        for (uint8_t c = 0U; c < dyn_slots[s].nchars; c++) {
            atomic_set(&dyn_slots[s].sub_flag[c], 0);
        }
    }
    periph_debug("periph", "peripheral disconnected");

    if (IS_ENABLED(CONFIG_BRIDGE_ADV_AUTO_RESTART)) {
        /* Pure decision first (host-tested state machine); the actual
         * bt_le_adv_start runs on the sysworkq (see adv_restart_work_fn). */
        bool wanted = atomic_get(&adv_wanted) != 0;

        if (bridge_ble_adv_intent_step(&wanted,
                                       BRIDGE_ADV_EV_LINK_LOST) ==
            BRIDGE_ADV_ACT_RESTART) {
            k_work_submit(&adv_restart_work);
        }
    }
}

static struct bt_conn_cb periph_conn_cb = {
    .connected = periph_connected_cb,
    .disconnected = periph_disconnected_cb,
};

bool bridge_periph_active(void)
{
    return periph_conn != NULL;
}

uint8_t bridge_periph_disconnect(void)
{
    int err;

    if (periph_conn == NULL) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    /* Async: the response is status-only; EVT.DISC (role 1) arrives via
     * the callback and the pointer clears there. */
    err = bt_conn_disconnect(periph_conn,
                             BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    return (err == 0) ? BRIDGE_STATUS_OK : BRIDGE_STATUS_ERR_NOT_READY;
}

bool bridge_periph_addr_matches(uint8_t addr_type, const uint8_t addr[6])
{
    const bt_addr_le_t *dst;

    if (periph_conn == NULL) {
        return false;
    }
    dst = bt_conn_get_dst(periph_conn);
    return dst->type == addr_type &&
           memcmp(dst->a.val, addr, BRIDGE_BLE_ADDR_LEN) == 0;
}

/* ---- CMD.gatt_define -------------------------------------------------------- */

static uint16_t le16get(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

uint8_t bridge_periph_gatt_define(const uint8_t *payload, uint16_t len,
                                  uint8_t *body, uint16_t cap,
                                  uint16_t *body_len)
{
    dyn_slot_t *slot = NULL;
    uint16_t svc_uuid16;
    uint8_t n;
    uint32_t pool_need = 0U;
    uint8_t s;

    if (atomic_get(&periph_ready) == 0) {
        return BRIDGE_STATUS_ERR_NOT_READY;
    }
    /* Request shape: [svc_uuid16 2B LE][n 1B] + n x
     * [char_uuid16 2B LE][props 1B][max_len 2B LE] */
    if (len < 3U) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    svc_uuid16 = le16get(&payload[0]);
    n = payload[2];
    if (n == 0U || n > DYN_CH_MAX ||
        len != (uint16_t)(3U + (uint16_t)n * 5U)) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    for (uint8_t i = 0U; i < n; i++) {
        const uint8_t *ent = &payload[3U + (uint16_t)i * 5U];
        uint8_t props = ent[2];
        uint16_t max_len = le16get(&ent[3]);

        if (props == 0U ||
            (props & ~(DYN_PROP_READ | DYN_PROP_WRITE_WO_RESP |
                       DYN_PROP_WRITE | DYN_PROP_NOTIFY)) != 0U) {
            return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        if (max_len == 0U || max_len > LOCAL_VALUE_MAX) {
            return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        pool_need += max_len;
        for (uint8_t j = 0U; j < i; j++) {
            if (le16get(&payload[3U + (uint16_t)j * 5U]) == le16get(ent)) {
                return BRIDGE_STATUS_ERR_BAD_PAYLOAD; /* dup char uuid16 */
            }
        }
    }
    if (pool_need > DYN_POOL_SIZE) {
        periph_debug("periph", "define: value pool exhausted");
        return BRIDGE_STATUS_ERR_BUSY; /* 0x73 */
    }
    if (body == NULL || body_len == NULL || cap < 2U) {
        return BRIDGE_STATUS_ERR_BAD_PAYLOAD;
    }
    if (bridge_periph_active()) {
        /* A peripheral peer would see the DB change underneath it. */
        periph_debug("periph", "define: busy (link up)");
        return BRIDGE_STATUS_ERR_BUSY;
    }

    k_mutex_lock(&dyn_lock, K_FOREVER);

    /* Target slot: the same svc_uuid16 replaces its slot, else a free one. */
    for (s = 0U; s < ARRAY_SIZE(dyn_slots); s++) {
        if (dyn_slots[s].used && dyn_slots[s].svc_uuid16 == svc_uuid16) {
            slot = &dyn_slots[s];
            (void)bt_gatt_service_unregister(&slot->svc);
            dyn_slot_reset(slot);
            break;
        }
    }
    if (slot == NULL) {
        for (s = 0U; s < ARRAY_SIZE(dyn_slots); s++) {
            if (!dyn_slots[s].used) {
                slot = &dyn_slots[s];
                break;
            }
        }
    }
    if (slot == NULL) {
        k_mutex_unlock(&dyn_lock);
        periph_debug("periph", "define: no free slot");
        return BRIDGE_STATUS_ERR_BUSY; /* slot table full (0x73) */
    }

    slot->svc_uuid16 = svc_uuid16;
    slot->nchars = n;
    slot->svc_uuid.uuid.type = BT_UUID_TYPE_16;
    slot->svc_uuid.val = svc_uuid16;

    struct bt_gatt_attr *attr = &slot->attrs[0];
    uint8_t *pool_cursor = slot->pool;

    /* [0] primary service declaration.  The attribute UUID is the
     * DECLARATION TYPE (0x2800, BT_UUID_GATT_PRIMARY); the served service
     * UUID rides in user_data and bt_gatt_attr_read_service reads it from
     * there -- byte-equivalent to what BT_GATT_PRIMARY_SERVICE expands to.
     * fix round 1: the UUID also sat in attr->uuid, so "read by group
     * type 0x2800" never matched this attribute and the whole dynamic
     * service was invisible over the air (real-device CoreBluetooth
     * discovery, 2026-09-25). */
    attr->uuid = BT_UUID_GATT_PRIMARY;
    attr->perm = BT_GATT_PERM_READ;
    attr->read = bt_gatt_attr_read_service;
    attr->write = NULL;
    attr->user_data = &slot->svc_uuid.uuid;
    attr->handle = 0U;
    attr++;

    for (uint8_t i = 0U; i < n; i++) {
        const uint8_t *ent = &payload[3U + (uint16_t)i * 5U];
        uint8_t wire_props = ent[2];
        uint16_t max_len = le16get(&ent[3]);
        uint8_t chrc_props = 0U;
        bool can_read = (wire_props & DYN_PROP_READ) != 0U;
        bool can_write = (wire_props & (DYN_PROP_WRITE |
                                        DYN_PROP_WRITE_WO_RESP)) != 0U;
        bool can_notify = (wire_props & DYN_PROP_NOTIFY) != 0U;

        slot->chr_uuid[i].uuid.type = BT_UUID_TYPE_16;
        slot->chr_uuid[i].val = le16get(ent);
        if (can_read) {
            chrc_props |= BT_GATT_CHRC_READ;
        }
        if ((wire_props & DYN_PROP_WRITE_WO_RESP) != 0U) {
            chrc_props |= BT_GATT_CHRC_WRITE_WITHOUT_RESP;
        }
        if ((wire_props & DYN_PROP_WRITE) != 0U) {
            chrc_props |= BT_GATT_CHRC_WRITE;
        }
        if (can_notify) {
            chrc_props |= BT_GATT_CHRC_NOTIFY;
        }
        /* The declaration attribute serves this struct over the air
         * (bt_gatt_attr_read_chrc) and dyn_find_char()/the val_handle walk
         * read .properties from it - it must carry the real uuid + props. */
        slot->chrc[i].uuid = &slot->chr_uuid[i].uuid;
        slot->chrc[i].properties = chrc_props;
        slot->max_len[i] = max_len;
        slot->cur_len[i] = 0U;
        slot->val[i] = pool_cursor;
        memset(slot->val[i], 0, max_len); /* zero-initialised values */
        pool_cursor += max_len;
        slot->val_handle[i] = 0U;

        /* characteristic declaration */
        attr->uuid = BT_UUID_GATT_CHRC;
        attr->perm = BT_GATT_PERM_READ;
        attr->read = bt_gatt_attr_read_chrc;
        attr->write = NULL;
        attr->user_data = &slot->chrc[i];
        attr->handle = 0U;
        attr++;

        /* characteristic value */
        attr->uuid = &slot->chr_uuid[i].uuid;
        attr->perm = (uint16_t)((can_read ? BT_GATT_PERM_READ : 0U) |
                                (can_write ? BT_GATT_PERM_WRITE : 0U));
        attr->read = can_read ? dyn_read_cb : NULL;
        attr->write = can_write ? dyn_write_cb : NULL;
        attr->user_data = DYN_UDX(s, i);
        attr->handle = 0U;
        attr++;

        /* CCCD rides along automatically for notify characteristics.
         * fix round 2: standard wiring is mandatory here -- user_data MUST
         * be the slot's static (RAM) bt_gatt_ccc_managed_user_data and
         * read/write the stack's ccc handlers, or the subscription is a
         * "shadow": the ATT write succeeds but never registers (same
         * disease as the preset 0xFFE2 CCCD, where the state sat in
         * rodata).  Never make dyn_slots[] const. */
        if (can_notify) {
            memset(&slot->ccc[i], 0, sizeof(slot->ccc[i]));
            slot->ccc[i].cfg_changed = dyn_ccc_changed_cb;
            attr->uuid = BT_UUID_GATT_CCC;
            attr->perm = BT_GATT_PERM_READ | BT_GATT_PERM_WRITE;
            attr->read = bt_gatt_attr_read_ccc;
            attr->write = bt_gatt_attr_write_ccc;
            attr->user_data = &slot->ccc[i];
            attr->handle = 0U;
            attr++;
        }
    }

    slot->svc.attrs = slot->attrs;
    slot->svc.attr_count = (size_t)(attr - slot->attrs);

    int err = bt_gatt_service_register(&slot->svc);

    if (err != 0) {
        char msg[32];

        (void)snprintf(msg, sizeof(msg), "define: register %d", err);
        periph_debug("periph", msg);
        dyn_slot_reset(slot);
        k_mutex_unlock(&dyn_lock);
        return BRIDGE_STATUS_ERR_NOT_READY;
    }

    /* The stack assigned the handles synchronously during registration
     * (Zephyr gatt.c gatt_register populates attrs[].handle).  Documented
     * layout from base = service declaration handle:
     *   per char: decl = prev+1, value = prev+1, CCCD (notify) = prev+1. */
    uint16_t base = slot->attrs[0].handle;
    uint16_t idx = 0U;

    for (uint8_t i = 0U; i < n; i++) {
        idx++; /* declaration */
        idx++; /* value */
        slot->val_handle[i] = (uint16_t)(base + idx);
        if ((slot->chrc[i].properties & BT_GATT_CHRC_NOTIFY) != 0U) {
            idx++; /* CCCD */
        }
    }

    char msg[48];

    (void)snprintf(msg, sizeof(msg), "define 0x%04X: %u chars base %u",
                   (unsigned)svc_uuid16, (unsigned)n, (unsigned)base);
    periph_debug("periph", msg);

    slot->used = true;
    k_mutex_unlock(&dyn_lock);

    bridge_ble_put_le16(body, base);
    *body_len = 2U;
    return BRIDGE_STATUS_OK;
}

/* ---- lifecycle ------------------------------------------------------------ */

void bridge_periph_shutdown(void)
{
    (void)bt_le_adv_stop();
    if (periph_conn != NULL) {
        (void)bt_conn_disconnect(periph_conn,
                                 BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    }
    /* Host-defined services live in RAM: the reboot clears them. */
}

int bridge_periph_init(void)
{
    char msg[48];

    k_mutex_init(&preset_lock);
    k_mutex_init(&dyn_lock);
    for (uint8_t s = 0U; s < ARRAY_SIZE(dyn_slots); s++) {
        memset(&dyn_slots[s], 0, sizeof(dyn_slots[s]));
    }

    k_mutex_lock(&preset_lock, K_FOREVER);
    chr_a_len = 0U;
    memcpy(chr_b_buf, chr_b_default, sizeof(chr_b_default));
    chr_b_len = (uint16_t)sizeof(chr_b_default);
    k_mutex_unlock(&preset_lock);

    /* Handles of the compile-time registered service are assigned during
     * bt_enable() (already done when this runs). */
    chr_a_handle = bt_gatt_attr_get_handle(
        &preset_svc.attrs[PRESET_IDX_CHR_A_VALUE]);

    bt_conn_cb_register(&periph_conn_cb);

    atomic_set(&periph_ready, 1);
    (void)snprintf(msg, sizeof(msg), "periph ready, 0xFFE1 handle %u",
                   (unsigned)chr_a_handle);
    periph_debug("periph", msg);
    return 0;
}
