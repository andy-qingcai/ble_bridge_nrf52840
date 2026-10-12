/*
 * bridge_ble_pure.c - pure logic of the BLE central bridge (Task 10).
 * See bridge_ble_pure.h for the wire-format contract.
 */

#include "bridge_ble_pure.h"

#include <string.h>

#define SUOTA_SERVICE_UUID16 0xFEF5U

/* ---- advertisement parser ------------------------------------------------ */

int bridge_ble_adv_parse(const uint8_t *adv, size_t len, char *name_out,
                         size_t name_cap)
{
    int flags = 0;
    size_t pos = 0U;

    while (pos + 1U < len) { /* len byte + at least the type byte left */
        uint8_t field_len = adv[pos];

        if (field_len == 0U || pos + 1U + (size_t)field_len > len) {
            break; /* malformed/truncated: stop walking, keep what we have */
        }
        uint8_t type = adv[pos + 1U];
        const uint8_t *data = &adv[pos + 2U];
        size_t data_len = (size_t)field_len - 1U;

        if (type == 0x08U || type == 0x09U) { /* short / complete name */
            size_t n = data_len;

            if (name_out != NULL && name_cap > 0U) {
                if (n > name_cap - 1U) {
                    n = name_cap - 1U;
                }
                memcpy(name_out, data, n);
                name_out[n] = '\0';
            }
            flags |= BRIDGE_ADV_FLAG_NAME;
        } else if (type == 0x16U && data_len >= 2U) { /* service data */
            if (bridge_ble_get_le16(data) == SUOTA_SERVICE_UUID16) {
                flags |= BRIDGE_ADV_FLAG_SVC_FEF5;
            }
        }
        pos += 1U + (size_t)field_len;
    }
    return flags;
}

/* ---- scan cache ----------------------------------------------------------- */

void bridge_ble_scan_cache_init(bridge_scan_cache_t *c)
{
    memset(c, 0, sizeof(*c));
}

static bool rec_addr_eq(const bridge_scan_rec_t *a, uint8_t addr_type,
                        const uint8_t *addr)
{
    return a->used && a->addr_type == addr_type &&
           memcmp(a->addr, addr, BRIDGE_BLE_ADDR_LEN) == 0;
}

bridge_scan_rec_t *bridge_ble_scan_cache_update(bridge_scan_cache_t *c,
                                                const bridge_scan_rec_t *in)
{
    bridge_scan_rec_t *slot = NULL;

    for (uint8_t i = 0; i < c->count; i++) {
        if (rec_addr_eq(&c->recs[i], in->addr_type, in->addr)) {
            slot = &c->recs[i];
            break;
        }
    }
    if (slot == NULL) {
        if (c->count < BRIDGE_BLE_SCAN_CACHE_MAX) {
            slot = &c->recs[c->count];
            c->count++;
        } else {
            /* evict the weakest signal; with DK-scale device counts this
             * path is a formality, but it must not degrade into a leak */
            slot = &c->recs[0];
            for (uint8_t i = 1; i < c->count; i++) {
                if (c->recs[i].rssi < slot->rssi) {
                    slot = &c->recs[i];
                }
            }
        }
        *slot = *in;
        slot->used = true;
        return slot;
    }

    slot->rssi = in->rssi;
    if (in->name[0] != '\0') {
        memcpy(slot->name, in->name, sizeof(slot->name));
    }
    if (in->has_fef5) {
        slot->has_fef5 = true; /* sticky: seen once, connectable */
    }
    return slot;
}

const bridge_scan_rec_t *bridge_ble_scan_cache_find_addr(
    const bridge_scan_cache_t *c, uint8_t addr_type,
    const uint8_t addr[BRIDGE_BLE_ADDR_LEN])
{
    for (uint8_t i = 0; i < c->count; i++) {
        if (rec_addr_eq(&c->recs[i], addr_type, addr)) {
            return &c->recs[i];
        }
    }
    return NULL;
}

const bridge_scan_rec_t *bridge_ble_scan_cache_find_name(
    const bridge_scan_cache_t *c, const char *name)
{
    const bridge_scan_rec_t *best = NULL;
    size_t nlen = strlen(name);

    if (nlen == 0U) {
        return NULL;
    }
    for (uint8_t i = 0; i < c->count; i++) {
        const bridge_scan_rec_t *r = &c->recs[i];

        if (r->name[0] == '\0') {
            continue;
        }
        if (strncmp(r->name, name, nlen) != 0) {
            continue;
        }
        if (best == NULL || r->rssi > best->rssi) {
            best = r;
        }
    }
    return best;
}

/* ---- GATT character map --------------------------------------------------- */

uint16_t bridge_ble_uuid128_prefix(
    const uint8_t val_le[BRIDGE_BLE_UUID128_LEN])
{
    /* val[] is over-the-air little-endian: val[15] is the MSB of the
     * readable form ("8082caa8-..." -> val[14] = 0x82, val[15] = 0x80),
     * so the uuid16 prefix is val[14] | val[15] << 8.  The host sends
     * that same value little-endian (suota.py uuid16_le), i.e. the wire
     * bytes match val[14], val[15] in order. */
    return (uint16_t)((uint16_t)val_le[14] | ((uint16_t)val_le[15] << 8));
}

void bridge_ble_char_map_clear(bridge_char_map_t *m)
{
    memset(m, 0, sizeof(*m));
}

int bridge_ble_char_map_add(bridge_char_map_t *m, uint16_t uuid16,
                            uint16_t value_handle, uint16_t ccc_handle,
                            uint8_t props)
{
    for (uint8_t i = 0; i < m->count; i++) {
        if (m->ents[i].uuid16 == uuid16) {
            return 0; /* idempotent */
        }
    }
    if (m->count >= BRIDGE_BLE_CHAR_MAX) {
        return -1;
    }
    m->ents[m->count].uuid16 = uuid16;
    m->ents[m->count].value_handle = value_handle;
    m->ents[m->count].ccc_handle = ccc_handle;
    m->ents[m->count].props = props;
    m->count++;
    return 0;
}

bool bridge_ble_char_map_find(const bridge_char_map_t *m, uint16_t uuid16,
                              bridge_char_ent_t *out)
{
    for (uint8_t i = 0; i < m->count; i++) {
        if (m->ents[i].uuid16 == uuid16) {
            *out = m->ents[i];
            return true;
        }
    }
    return false;
}

/* ---- advertising intent state machine -------------------------------------- */

bridge_adv_act_t bridge_ble_adv_intent_step(bool *wanted,
                                            bridge_adv_evt_t evt)
{
    switch (evt) {
    case BRIDGE_ADV_EV_START:
        *wanted = true;
        break;
    case BRIDGE_ADV_EV_STOP:
        *wanted = false;
        break;
    case BRIDGE_ADV_EV_LINK_LOST:
        /* restart only what the user asked for (and did not explicitly
         * stop); the state itself stays set so later link losses keep
         * recovering */
        return *wanted ? BRIDGE_ADV_ACT_RESTART : BRIDGE_ADV_ACT_NONE;
    }
    return BRIDGE_ADV_ACT_NONE;
}

/* ---- event payload builders ------------------------------------------------ */

int bridge_ble_evt_scan_build(const bridge_scan_rec_t *rec,
                              const uint8_t *adv, size_t adv_len,
                              uint8_t *out, size_t cap)
{
    /* v1.4.1 (E2-T): 1B adv_type inserted after rssi, before the adv TLV. */
    const size_t need = (size_t)1U + 1U + BRIDGE_BLE_ADDR_LEN + 1U + 1U + adv_len;

    if (cap < need) {
        return -(int)need;
    }
    out[0] = 0x00; /* subtype */
    out[1] = rec->addr_type;
    /* v1.4.2: addr is packed in display order (left-to-right as a phone
     * shows it); Zephyr's bt_addr val is little-endian, reverse on the way
     * out.  The scan cache keeps storing val order internally. */
    for (unsigned int i = 0U; i < BRIDGE_BLE_ADDR_LEN; i++) {
        out[2 + i] = rec->addr[BRIDGE_BLE_ADDR_LEN - 1U - i];
    }
    out[8] = (uint8_t)rec->rssi;
    out[9] = rec->adv_type;
    if (adv_len > 0U) {
        memcpy(&out[10], adv, adv_len);
    }
    return (int)need;
}

int bridge_ble_evt_conn_build(uint8_t role, uint8_t addr_type,
                              const uint8_t addr[BRIDGE_BLE_ADDR_LEN],
                              uint8_t *out, size_t cap)
{
    const size_t need = (size_t)1U + 1U + 1U + BRIDGE_BLE_ADDR_LEN;

    if (cap < need) {
        return -(int)need;
    }
    out[0] = 0x00; /* subtype */
    out[1] = role; /* 0 = central role, 1 = peripheral role (FR-H) */
    out[2] = addr_type;
    /* v1.4.2: display order on the wire (see evt_scan_build). */
    for (unsigned int i = 0U; i < BRIDGE_BLE_ADDR_LEN; i++) {
        out[3 + i] = addr[BRIDGE_BLE_ADDR_LEN - 1U - i];
    }
    return (int)need;
}

int bridge_ble_evt_disc_build(uint8_t role, uint8_t reason, uint8_t *out,
                              size_t cap)
{
    if (cap < 3U) {
        return -3;
    }
    out[0] = 0x00; /* subtype */
    out[1] = role; /* 0 = central role, 1 = peripheral role (FR-H) */
    out[2] = reason;
    return 3;
}

int bridge_ble_evt_gattx_build(uint16_t uuid16, const uint8_t *data,
                               size_t len, uint8_t *out, size_t cap)
{
    const size_t need = (size_t)1U + 2U + len;

    if (cap < need) {
        return -(int)need;
    }
    out[0] = 0x00; /* subtype 0x00 = notification */
    bridge_ble_put_le16(&out[1], uuid16);
    if (len > 0U) {
        memcpy(&out[3], data, len);
    }
    return (int)need;
}

int bridge_ble_evt_gattx_write_build(uint16_t uuid16, uint16_t handle,
                                     const uint8_t *data, size_t len,
                                     uint8_t *out, size_t cap)
{
    const size_t need = (size_t)1U + 2U + 2U + len;

    if (cap < need) {
        return -(int)need;
    }
    out[0] = 0x01; /* subtype 0x01 = peripheral-role write (FR-H) */
    bridge_ble_put_le16(&out[1], uuid16);
    bridge_ble_put_le16(&out[3], handle);
    if (len > 0U) {
        memcpy(&out[5], data, len);
    }
    return (int)need;
}

int bridge_ble_evt_mtu_build(uint16_t mtu, uint8_t *out, size_t cap)
{
    if (cap < 3U) {
        return -3;
    }
    out[0] = 0x00; /* subtype */
    bridge_ble_put_le16(&out[1], mtu);
    return 3;
}
