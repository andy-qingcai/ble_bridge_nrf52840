/*
 * bridge_dispatch.c - command dispatcher + event credit/flow-control logic
 * (Task 9; Task 10 adds BLE command delegation + I-1 atomics).
 * Pure C11, no Zephyr; see bridge_dispatch.h.
 */

#include "bridge_dispatch.h"

#include <stdatomic.h>

#include "bridge_info.h"

/* Standalone host-test builds have no autoconf.h; production takes the
 * value from the Kconfig option (both end at the same 5 s default). */
#ifndef CONFIG_BRIDGE_CONNECT_TIMEOUT_MS
#define CONFIG_BRIDGE_CONNECT_TIMEOUT_MS 5000
#endif

/* Commands that stay unimplemented after Task 10 (SUOTA sessions are
 * driven by the host through the gatt_* primitives; ruling on record in
 * task-10-brief.md / task-10-report.md). */
static bool is_ble_placeholder(uint8_t cmd)
{
    switch (cmd) {
    case BRIDGE_CMD_SUOTA_START:
    case BRIDGE_CMD_SUOTA_ABORT:
    case BRIDGE_CMD_SUOTA_STATUS:
        return true;
    default:
        return false;
    }
}

static uint8_t clamp_level(uint8_t level)
{
    return (level > 4U) ? 4U : level;
}

void bridge_dispatch_init(bridge_dispatch_t *d, uint16_t credits,
                          uint8_t level)
{
    /* Parameterised so host tests can drive edge cases; production passes
     * the cross-end constants (credits 128) via CONFIG_BRIDGE_CREDITS_INIT
     * and the initial log level via CONFIG_BRIDGE_LOG_LEVEL. */
    atomic_store(&d->credits, credits);
    d->evt_seq = 0U;
    atomic_store(&d->log_level, clamp_level(level));
    atomic_store(&d->events_sent, 0U);
    atomic_store(&d->events_dropped, 0U);
    atomic_store(&d->tx_timeouts, 0U);
    d->frames_ok = 0U;
    d->crc_errors = 0U;
    d->resyncs = 0U;
    d->bytes_dropped = 0U;
    d->rx_overflows = 0U;
    d->reset_requested = false;
    d->dfu_requested = false;
}

void bridge_dispatch_refresh_stats(bridge_dispatch_t *d,
                                   const bridge_parser_t *p)
{
    if (p != NULL) {
        d->frames_ok = p->frames_ok;
        d->crc_errors = p->crc_errors;
        d->resyncs = p->resyncs;
        d->bytes_dropped = p->bytes_dropped;
    }
}

/* ---- BLE command plumbing ---------------------------------------------- */

static uint16_t le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

int bridge_dispatch_handle(bridge_dispatch_t *d,
                           const bridge_dispatch_env_t *env,
                           const bridge_frame_t *req, uint8_t *out,
                           uint16_t cap)
{
    if (req->cmd == BRIDGE_CMD_RESERVED) {
        /* Never answer: 0x0E | 0x80 == 0x8E == EVT.DEBUG would inject a
         * fake event into the host ring.  Documented in bridge.py. */
        return 0;
    }

    if (cap == 0U) {
        return -1; /* need room for at least the status byte */
    }

    const bridge_ble_api_t *ble = (env != NULL) ? env->ble : NULL;
    uint8_t *body = (cap > 0U) ? &out[1] : out;
    uint16_t body_cap = (cap > 0U) ? (uint16_t)(cap - 1U) : 0U;
    uint8_t status = BRIDGE_STATUS_OK;
    int blen = 0;

    switch (req->cmd) {
    case BRIDGE_CMD_PING:
        blen = bridge_info_ping_body(env->version, body, body_cap);
        break;

    case BRIDGE_CMD_INFO: {
        bridge_info_snapshot_t s = {
            .version = env->version,
            .phy = env->phy,
            .uptime_s = env->uptime_ms / 1000U,
            .events_sent = atomic_load(&d->events_sent),
            .events_dropped = atomic_load(&d->events_dropped),
            .tx_timeouts = atomic_load(&d->tx_timeouts),
            .rx_overflows = d->rx_overflows,
            .credits = atomic_load(&d->credits),
            .log_level = atomic_load(&d->log_level),
        };
        blen = bridge_info_json(&s, body, body_cap);
        break;
    }

    case BRIDGE_CMD_RESET:
        d->reset_requested = true;
        blen = 0;
        break;

    case BRIDGE_CMD_ENTER_DFU:
        /* task-dfu-a: ACK then reboot into the MCUboot serial recovery
         * console (link latches the bootloader boot mode before the
         * sys_reboot; GPREGRET survives a soft reset). */
        d->dfu_requested = true;
        blen = 0;
        break;

    case BRIDGE_CMD_LOG_LEVEL:
        if (req->len == 1U && req->payload[0] <= 4U) {
            atomic_store(&d->log_level, clamp_level(req->payload[0]));
            blen = 0;
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    case BRIDGE_CMD_CREDITS:
        if (req->len == 2U) {
            bridge_dispatch_grant_credits(d, le16(req->payload));
            blen = 0;
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    /* ---- BLE family (Task 10): delegate to the api table; a NULL table
     * (skeleton/host-test builds) keeps answering NOT_READY. ---- */

    case BRIDGE_CMD_SCAN_START:
        if (req->len == 2U) {
            status = (ble != NULL && ble->scan_start != NULL)
                         ? ble->scan_start(le16(req->payload))
                         : BRIDGE_STATUS_ERR_NOT_READY;
            blen = 0;
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    case BRIDGE_CMD_SCAN_STOP:
        status = (ble != NULL && ble->scan_stop != NULL)
                     ? ble->scan_stop()
                     : BRIDGE_STATUS_ERR_NOT_READY;
        blen = 0;
        break;

    case BRIDGE_CMD_CONNECT:
        if (req->len >= 1U) {
            uint16_t bl = 0U;

            if (ble != NULL && ble->connect != NULL) {
                status = ble->connect(req->payload, req->len,
                                      (uint32_t)CONFIG_BRIDGE_CONNECT_TIMEOUT_MS,
                                      body, body_cap, &bl);
                blen = (status == BRIDGE_STATUS_OK) ? (int)bl : 0;
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    case BRIDGE_CMD_DISCONNECT:
        /* FR-H: empty = peripheral link first (then central), or
         * 1B addr_type + 6B addr to drop the matching link. */
        status = (ble != NULL && ble->disconnect != NULL)
                     ? ble->disconnect(req->payload, req->len)
                     : BRIDGE_STATUS_ERR_NOT_READY;
        blen = 0;
        break;

    case BRIDGE_CMD_MTU: {
        uint16_t mtu = 0U;

        if (req->len != 0U) {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        } else if (ble != NULL && ble->mtu_get != NULL) {
            status = ble->mtu_get(&mtu);
            if (status == BRIDGE_STATUS_OK) {
                if (body_cap >= 2U) {
                    body[0] = (uint8_t)(mtu & 0xFFU);
                    body[1] = (uint8_t)(mtu >> 8);
                    blen = 2;
                } else {
                    status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
                }
            }
        } else {
            status = BRIDGE_STATUS_ERR_NOT_READY;
        }
        break;
    }

    case BRIDGE_CMD_GATT_READ:
        /* uuid addressing: u16 LE uuid16.  Handle addressing: uuid16 field
         * 0x0000 followed by the raw attribute handle (u16 LE). */
        if (req->len == 2U) {
            uint16_t dl = 0U;

            if (ble != NULL && ble->gatt_read != NULL) {
                status = ble->gatt_read(le16(req->payload), body, body_cap,
                                        &dl);
                blen = (status == BRIDGE_STATUS_OK) ? (int)dl : 0;
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else if (req->len == 4U && req->payload[0] == 0U &&
                   req->payload[1] == 0U) {
            uint16_t dl = 0U;

            if (ble != NULL && ble->gatt_read_handle != NULL) {
                status = ble->gatt_read_handle(le16(&req->payload[2]), body,
                                               body_cap, &dl);
                blen = (status == BRIDGE_STATUS_OK) ? (int)dl : 0;
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    case BRIDGE_CMD_GATT_WRITE:
        /* uuid addressing: [type][uuid16 LE][data...].  Handle addressing:
         * [type][00 00][handle_type][handle LE][data...] with handle_type
         * 0 = characteristic value handle, 1 = descriptor handle (both a
         * raw ATT write today; the byte documents intent, reserved). */
        if (req->len >= 3U && req->payload[0] <= 1U &&
            !(req->payload[1] == 0U && req->payload[2] == 0U)) {
            if (ble != NULL && ble->gatt_write != NULL) {
                status = ble->gatt_write(req->payload[0],
                                         le16(&req->payload[1]),
                                         &req->payload[3],
                                         (uint16_t)(req->len - 3U));
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else if (req->len >= 6U && req->payload[0] <= 1U &&
                   req->payload[1] == 0U && req->payload[2] == 0U &&
                   req->payload[3] <= 1U) {
            if (ble != NULL && ble->gatt_write_handle != NULL) {
                status = ble->gatt_write_handle(req->payload[0],
                                                le16(&req->payload[4]),
                                                &req->payload[6],
                                                (uint16_t)(req->len - 6U));
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_SUBSCRIBE:
        /* uuid addressing: u16 LE uuid16.  Handle addressing: 0x0000 +
         * value handle (u16 LE) + CCCD handle (u16 LE). */
        if (req->len == 2U) {
            if (ble != NULL && ble->subscribe != NULL) {
                status = ble->subscribe(le16(req->payload));
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else if (req->len == 6U && req->payload[0] == 0U &&
                   req->payload[1] == 0U) {
            if (ble != NULL && ble->subscribe_handle != NULL) {
                status = ble->subscribe_handle(le16(&req->payload[2]),
                                               le16(&req->payload[4]));
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_UNSUBSCRIBE:
        /* uuid addressing: u16 LE uuid16.  Handle addressing: 0x0000 +
         * value handle (u16 LE) of the handle-mode subscription. */
        if (req->len == 2U) {
            if (ble != NULL && ble->unsubscribe != NULL) {
                status = ble->unsubscribe(le16(req->payload));
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else if (req->len == 4U && req->payload[0] == 0U &&
                   req->payload[1] == 0U) {
            if (ble != NULL && ble->unsubscribe_handle != NULL) {
                status = ble->unsubscribe_handle(le16(&req->payload[2]));
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_DISC_SVC:
        /* Generic bridge: u16 LE service UUID runs bt_gatt_dm NOW (0x0000
         * = re-discover the last service); the legacy empty shape follows
         * the same re-discovery rule.  Body on success = discovered
         * characteristic count (u16 LE). */
        if (req->len == 0U || req->len == 2U) {
            uint16_t nchars = 0U;

            if (ble != NULL && ble->disc_svc != NULL) {
                status = ble->disc_svc(
                    (req->len == 2U) ? le16(req->payload) : 0U, &nchars);
                if (status == BRIDGE_STATUS_OK) {
                    if (body_cap >= 2U) {
                        body[0] = (uint8_t)(nchars & 0xFFU);
                        body[1] = (uint8_t)(nchars >> 8);
                        blen = 2;
                    } else {
                        status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
                    }
                }
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    case BRIDGE_CMD_CONN_UPDATE:
        /* 4 x u16 LE: interval_min/max (1.25 ms), latency, timeout (10 ms).
         * Async: the settled parameters land on EVT.DEBUG. */
        if (req->len == 8U) {
            if (ble != NULL && ble->conn_update != NULL) {
                status = ble->conn_update(le16(&req->payload[0]),
                                          le16(&req->payload[2]),
                                          le16(&req->payload[4]),
                                          le16(&req->payload[6]));
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_PHY:
        /* 1B phy preference: 1 = 1M, 2 = 2M (both directions).  Async: the
         * result lands on EVT.DEBUG via le_phy_updated. */
        if (req->len == 1U) {
            if (ble != NULL && ble->phy_update != NULL) {
                status = ble->phy_update(req->payload[0]);
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_PAIR:
        /* FR-B5: empty payload; body (on success) = [encrypted u8,
         * sec_level u8].  Blocks the link RX thread for up to the
         * firmware-side pairing timeout while SMP runs on BT threads. */
        if (req->len == 0U) {
            uint16_t bl = 0U;

            if (ble != NULL && ble->pair != NULL) {
                status = ble->pair(body, body_cap, &bl);
                blen = (status == BRIDGE_STATUS_OK) ? (int)bl : 0;
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    case BRIDGE_CMD_ADV_START:
        /* FR-H: u16 LE interval_ms (0xFFFF = firmware default) + 1B name
         * switch.  Tolerant shapes: empty payload = both defaults,
         * 2 bytes = interval with the name on.  Idempotent when already
         * advertising; the state rides EVT.DEBUG. */
        if (req->len == 0U) {
            status = (ble != NULL && ble->adv_start != NULL)
                         ? ble->adv_start(0xFFFFU, 1U)
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else if (req->len == 2U) {
            status = (ble != NULL && ble->adv_start != NULL)
                         ? ble->adv_start(le16(req->payload), 1U)
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else if (req->len == 3U && req->payload[2] <= 1U) {
            status = (ble != NULL && ble->adv_start != NULL)
                         ? ble->adv_start(le16(req->payload), req->payload[2])
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_ADV_STOP:
        /* FR-H: empty payload; idempotent. */
        if (req->len == 0U) {
            status = (ble != NULL && ble->adv_stop != NULL)
                         ? ble->adv_stop()
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_NOTIFY:
        /* FR-H: [0x00][data...] = preset notify characteristic, or
         * [0x01][char uuid16 LE][data...] = address any locally served
         * characteristic.  0x74 when nobody is subscribed. */
        if (req->len >= 1U && req->payload[0] == 0U) {
            status = (ble != NULL && ble->notify != NULL)
                         ? ble->notify(0U, 0U, &req->payload[1],
                                       (uint16_t)(req->len - 1U))
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else if (req->len >= 3U && req->payload[0] == 1U) {
            status = (ble != NULL && ble->notify != NULL)
                         ? ble->notify(1U, le16(&req->payload[1]),
                                       &req->payload[3],
                                       (uint16_t)(req->len - 3U))
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_SET_CHAR:
        /* FR-H: [0x00][data...] = preset write characteristic, or
         * [0x01][char uuid16 LE][data...] = address any locally served
         * characteristic. */
        if (req->len >= 1U && req->payload[0] == 0U) {
            status = (ble != NULL && ble->set_char != NULL)
                         ? ble->set_char(0U, 0U, &req->payload[1],
                                         (uint16_t)(req->len - 1U))
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else if (req->len >= 3U && req->payload[0] == 1U) {
            status = (ble != NULL && ble->set_char != NULL)
                         ? ble->set_char(1U, le16(&req->payload[1]),
                                         &req->payload[3],
                                         (uint16_t)(req->len - 3U))
                         : BRIDGE_STATUS_ERR_NOT_READY;
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        blen = 0;
        break;

    case BRIDGE_CMD_GATT_DEFINE:
        /* FR-H5: [svc_uuid16 LE][n][n x [char_uuid16 LE][props][max_len
         * LE]]; success body = service base handle (u16 LE).  BUSY while
         * a peripheral-role link is up. */
        if (req->len >= 3U) {
            uint16_t bl = 0U;

            if (ble != NULL && ble->gatt_define != NULL) {
                status = ble->gatt_define(req->payload, req->len, body,
                                          body_cap, &bl);
                blen = (status == BRIDGE_STATUS_OK) ? (int)bl : 0;
            } else {
                status = BRIDGE_STATUS_ERR_NOT_READY;
            }
        } else {
            status = BRIDGE_STATUS_ERR_BAD_PAYLOAD;
        }
        break;

    default:
        if (is_ble_placeholder(req->cmd)) {
            status = BRIDGE_STATUS_ERR_NOT_READY;
        } else {
            status = BRIDGE_STATUS_ERR_UNKNOWN_CMD;
        }
        blen = 0;
        break;
    }

    if (blen < 0) {
        /* Body buffer too small: report as a bad-payload coding error.
         * With the link sizing out[] >= 1 + 512 this is unreachable. */
        return blen - 1;
    }

    out[0] = status;
    return (int)(1U + (uint16_t)blen);
}

bridge_evt_decision_t bridge_evt_admit(bridge_dispatch_t *d)
{
    bridge_evt_decision_t dec;

    dec.seq = d->evt_seq; /* ring slot is consumed either way */
    d->evt_seq = (uint8_t)(d->evt_seq + 1U);

    /* Single decrementer (the event thread) races at most a CMD.credits
     * grant; the CAS loop keeps the balance exact without ever wrapping. */
    for (;;) {
        uint16_t c = atomic_load(&d->credits);

        if (c == 0U) {
            atomic_fetch_add(&d->events_dropped, 1U);
            dec.action = BRIDGE_EVT_DROP;
            break;
        }
        if (atomic_compare_exchange_weak(&d->credits, &c,
                                         (uint16_t)(c - 1U))) {
            atomic_fetch_add(&d->events_sent, 1U);
            dec.action = BRIDGE_EVT_EMIT;
            break;
        }
    }
    return dec;
}

void bridge_dispatch_grant_credits(bridge_dispatch_t *d, uint16_t n)
{
    /* fetch_add can only saturate here (never wrap); the saturating store
     * is benign against a concurrent admit decrement - both only happen
     * when the balance is already near the u16 ceiling, unreachable with
     * the cross-end 128-grant constant. */
    uint16_t old = atomic_fetch_add(&d->credits, n);

    if ((uint32_t)old + (uint32_t)n > 0xFFFFU) {
        atomic_store(&d->credits, 0xFFFFU);
    }
}

void bridge_evt_tx_lost(bridge_dispatch_t *d)
{
    uint32_t sent = atomic_load(&d->events_sent);

    if (sent > 0U) {
        atomic_store(&d->events_sent, sent - 1U);
    }
    atomic_fetch_add(&d->events_dropped, 1U);
}

int bridge_evt_debug_build(const char *tag, const char *msg, uint8_t *out,
                           uint16_t cap)
{
    uint16_t tag_len = 0U;
    uint16_t msg_len = 0U;

    while (tag[tag_len] != '\0') {
        tag_len++;
        if (tag_len > 255U) {
            return -2; /* -EINVAL-ish; tag namespace must stay short */
        }
    }
    if (msg != NULL) {
        while (msg[msg_len] != '\0') {
            msg_len++;
        }
    }
    if ((uint32_t)1U + tag_len + msg_len > (uint32_t)cap) {
        return -(int)(1U + tag_len + msg_len);
    }
    out[0] = (uint8_t)tag_len;
    for (uint16_t i = 0; i < tag_len; i++) {
        out[1U + i] = (uint8_t)tag[i];
    }
    for (uint16_t i = 0; i < msg_len; i++) {
        out[1U + tag_len + i] = (uint8_t)msg[i];
    }
    return (int)(1U + tag_len + msg_len);
}

/* ---- image self-confirm decision (task-dfu-a) -------------------------- */

bool bridge_confirm_ready(uint32_t uptime_s, uint32_t min_uptime_s,
                          bool init_ok, uint32_t crc_errors,
                          uint32_t tx_timeouts, uint32_t events_dropped)
{
    if (uptime_s < min_uptime_s) {
        return false; /* still inside the trial window */
    }
    if (!init_ok) {
        return false; /* data plane never came up: let the revert win */
    }
    /* The trial window must have been wire-clean: any lost frame, CRC
     * failure or credit exhaustion is a symptom the image misbehaves --
     * leave it unconfirmed so the next reset reverts it. */
    if (crc_errors != 0U || tx_timeouts != 0U || events_dropped != 0U) {
        return false;
    }
    return true;
}
