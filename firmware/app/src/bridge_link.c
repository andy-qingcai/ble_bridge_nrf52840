/*
 * bridge_link.c - frame link: RX pump, single TX owner, event channel
 * (Task 9).  See bridge_link.h for the thread model.
 */

#include "bridge_link.h"

#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/reboot.h>
#if defined(CONFIG_RETENTION_BOOT_MODE)
#include <zephyr/retention/bootmode.h>
#endif

#include "bridge_dispatch.h"
#include "bridge_ble.h"
#include "bridge_phy.h"
#include "bridge_proto.h"

#define TX_TIMEOUT K_SECONDS(5)
#define READ_TIMEOUT K_MSEC(500)
#define RESET_FLUSH_BUDGET_MS 10 /* best-effort ACK flush before sys_reboot */
#define RESET_TEARDOWN_MS 60     /* best-effort BLE disconnect before reboot */

/* ---- queue item types -------------------------------------------------- */

typedef struct {
    uint16_t len;
    uint8_t data[BRIDGE_MAX_FRAME];
} tx_msg_t;

typedef struct {
    uint8_t evt;
    uint16_t len;
    uint8_t data[CONFIG_BRIDGE_EVENT_PAYLOAD_MAX];
} evt_msg_t;

/* ---- state ------------------------------------------------------------- */

static bridge_dispatch_t disp;
static bridge_parser_t parser;
/* Optional pre-reboot hook (Task 10: bridge_ble shutdown = disconnect).
 * Set once from main() before any command can arrive. */
static void (*reset_hook)(void);
/* task-dfu-a: latched by bridge_link_init() for the confirm snapshot. */
static bool link_init_ok;

/* K_MSGQ_DEFINE already declares its objects static in Zephyr 4.x (the
 * buffer is `static char`, the queue lands in a struct section). */
K_MSGQ_DEFINE(tx_q, sizeof(tx_msg_t), CONFIG_BRIDGE_TX_QUEUE_LEN, 4);
K_MSGQ_DEFINE(evt_q, sizeof(evt_msg_t), CONFIG_BRIDGE_EVENT_QUEUE_LEN, 4);

#define RX_STACK 2048
#define TX_STACK 1024
#define EVT_STACK 1536

static K_THREAD_STACK_DEFINE(rx_stack, RX_STACK);
static K_THREAD_STACK_DEFINE(tx_stack, TX_STACK);
static K_THREAD_STACK_DEFINE(evt_stack, EVT_STACK);
static struct k_thread rx_thread;
static struct k_thread tx_thread;
static struct k_thread evt_thread;

/* ---- TX owner ---------------------------------------------------------- */

/* Minimal u32-to-decimal formatter for diagnostic strings (this path has
 * no printf; returns the digit count written to dst, >= 1). */
static uint16_t u32_to_dec(uint32_t v, char *dst)
{
    char tmp[10];
    uint16_t n = 0U;

    do {
        tmp[n++] = (char)('0' + (v % 10U));
        v /= 10U;
    } while (v != 0U);
    for (uint16_t i = 0; i < n; i++) {
        dst[i] = tmp[n - 1U - i];
    }
    return n;
}

static void tx_send(const uint8_t *frame, uint16_t len)
{
    tx_msg_t msg;

    if (len > BRIDGE_MAX_FRAME) {
        return;
    }
    msg.len = len;
    memcpy(msg.data, frame, len);
    /* The queue exists exactly to absorb bursts; it can only overflow if
     * the physical layer is wedged, in which case the frame is lost. */
    (void)k_msgq_put(&tx_q, &msg, K_MSEC(100));
}

static void tx_thread_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);

    static tx_msg_t msg;

    for (;;) {
        if (k_msgq_get(&tx_q, &msg, K_FOREVER) != 0) {
            continue;
        }
        if (bridge_phy_write(msg.data, msg.len, TX_TIMEOUT) != 0) {
            /* Timeout: the frame is lost; no retry queue at this layer.
             * The host request path has its own timeout + retransmit-free
             * recovery (bridge.py raises BridgeTimeout).  C-1: the loss is
             * no longer silent - CMD.info reports the running total. */
            atomic_fetch_add(&disp.tx_timeouts, 1U);
        }
    }
}

/* ---- event channel ------------------------------------------------------ */

int bridge_link_post_event(uint8_t evt, const void *payload, uint16_t len)
{
    evt_msg_t msg;

    if (len > (uint16_t)CONFIG_BRIDGE_EVENT_PAYLOAD_MAX) {
        return -EMSGSIZE;
    }
    /* I-1: log_level is written by the RX thread (CMD.log_level) and read
     * from BLE callback contexts; it is a C11 atomic in the dispatcher. */
    if (evt == BRIDGE_EVT_DEBUG &&
        atomic_load_explicit(&disp.log_level, memory_order_acquire) == 0U) {
        return 0; /* log level off: silently filtered, never counted */
    }
    msg.evt = evt;
    msg.len = len;
    if (len > 0U && payload != NULL) {
        memcpy(msg.data, payload, len);
    }
    return k_msgq_put(&evt_q, &msg, K_NO_WAIT);
}

static void evt_thread_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);

    static evt_msg_t msg;
    static uint8_t frame[BRIDGE_MAX_FRAME];
    static tx_msg_t out;

    for (;;) {
        if (k_msgq_get(&evt_q, &msg, K_FOREVER) != 0) {
            continue;
        }

        bridge_evt_decision_t dec = bridge_evt_admit(&disp);

        if (dec.action != BRIDGE_EVT_EMIT) {
            continue; /* credit exhausted: dropped++, seq consumed */
        }
        int flen = bridge_encode(msg.evt, dec.seq, msg.data, msg.len, frame,
                                 (uint16_t)sizeof(frame));

        if (flen < 0) {
            continue; /* cannot happen with our buffer sizes */
        }
        out.len = (uint16_t)flen;
        memcpy(out.data, frame, (size_t)flen);
        if (k_msgq_put(&tx_q, &out, K_NO_WAIT) != 0) {
            /* TX queue wedged: undo the "sent" accounting, keep the loss
             * visible via the consumed ring sequence number. */
            bridge_evt_tx_lost(&disp);
        }
    }
}

/* ---- RX pump ------------------------------------------------------------ */

static void handle_frame(const bridge_frame_t *req)
{
    static uint8_t resp[BRIDGE_MAX_PAYLOAD + 1];
    static uint8_t frame[BRIDGE_MAX_FRAME];

    bridge_dispatch_env_t env = {
        .version = CONFIG_BRIDGE_VERSION,
        .phy = bridge_phy_name(),
        .uptime_ms = k_uptime_get_32(),
        .ble = bridge_ble_api(),
    };

    int plen = bridge_dispatch_handle(&disp, &env, req, resp,
                                      (uint16_t)sizeof(resp));

    if (plen <= 0) {
        return; /* 0: no response (RESERVED cmd); <0: internal sizing bug */
    }

    int flen = bridge_encode((uint8_t)(req->cmd | 0x80U), req->seq, resp,
                             (uint16_t)plen, frame, (uint16_t)sizeof(frame));
    if (flen < 0) {
        return;
    }

    if (disp.reset_requested || disp.dfu_requested) {
        /* Best-effort ACK: push it out, then give the TX owner up to
         * RESET_FLUSH_BUDGET_MS to drain before rebooting (brief). */
        tx_send(frame, (uint16_t)flen);
        int64_t deadline = k_uptime_get() + RESET_FLUSH_BUDGET_MS;

        while (k_msgq_num_used_get(&tx_q) > 0 && k_uptime_get() < deadline) {
            k_msleep(1);
        }
        k_msleep(2); /* let the final FIFO bytes hit the wire */
        if (reset_hook != NULL) {
            /* Task 10: drop the BLE link first so the peer sees the
             * disconnect instead of a dead air interface. */
            reset_hook();
            k_msleep(RESET_TEARDOWN_MS);
        }
        if (disp.dfu_requested) {
            /* task-dfu-a: latch the bootloader boot mode (GPREGRET1
             * through the board's boot_mode0 retention node) so MCUboot
             * stays in its serial recovery console after the reset.
             * GPREGRET survives a soft reset; a later normal boot starts
             * with BOOT_MODE_TYPE_NORMAL (MCUboot consumes/clears the
             * marker when it enters serial recovery). */
#if defined(CONFIG_RETENTION_BOOT_MODE)
            (void)bootmode_set(BOOT_MODE_TYPE_BOOTLOADER);
#endif
        }
        sys_reboot(SYS_REBOOT_COLD);
    }

    tx_send(frame, (uint16_t)flen);
}

static void rx_thread_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);

    static uint8_t buf[128];
    static uint32_t reported_overflows;
    static int64_t last_overflow_evt;

    for (;;) {
        size_t n = 0U;
        int rc = bridge_phy_read(buf, sizeof(buf), READ_TIMEOUT, &n);

        /* C-3: consume the ISR overflow counter - mirror it for CMD.info
         * and surface a rising edge as one EVT.DEBUG ("phy" tag), throttled
         * to one per second like the resync diagnostic below and gated with
         * it at log_level >= 1.  The EVT only needs log_level > 0: the
         * authoritative total lives in the info JSON either way. */
        uint32_t ovf = bridge_phy_rx_overflows();

        if (ovf > reported_overflows &&
            atomic_load(&disp.log_level) >= 1U &&
            k_uptime_get() - last_overflow_evt >= 1000) {
            static const char prefix[] = "rx ring overflow bytes=";
            char msg[40];
            uint8_t payload[48];
            uint16_t mlen = (uint16_t)(sizeof(prefix) - 1U);
            int plen;

            memcpy(msg, prefix, mlen);
            mlen = (uint16_t)(mlen + u32_to_dec(ovf, msg + mlen));
            msg[mlen] = '\0';
            plen = bridge_evt_debug_build("phy", msg, payload,
                                          (uint16_t)sizeof(payload));

            last_overflow_evt = k_uptime_get();
            reported_overflows = ovf;
            if (plen > 0) {
                (void)bridge_link_post_event(BRIDGE_EVT_DEBUG, payload,
                                             (uint16_t)plen);
            }
        }
        disp.rx_overflows = ovf;

        if (rc != 0) {
            continue; /* timeout: nothing to do */
        }
        for (size_t i = 0; i < n; i++) {
            bridge_frame_t fr;
            int r = bridge_parser_feed(&parser, buf[i], &fr);

            if (r == 1) {
                bridge_dispatch_refresh_stats(&disp, &parser);
                handle_frame(&fr);
            } else if (r < 0 &&
                       atomic_load(&disp.log_level) >= 1U) {
                static int64_t last_resync_evt;
                int64_t now = k_uptime_get();

                /* Throttle resync diagnostics to one per second. */
                if (now - last_resync_evt >= 1000) {
                    uint8_t payload[32];
                    const char msg[] = "parser resync";
                    int plen = bridge_evt_debug_build("disp", msg, payload,
                                                      (uint16_t)sizeof(payload));

                    last_resync_evt = now;
                    if (plen > 0) {
                        (void)bridge_link_post_event(BRIDGE_EVT_DEBUG, payload,
                                                     (uint16_t)plen);
                    }
                }
            }
        }
    }
}

/* ---- init ---------------------------------------------------------------- */

int bridge_link_init(void)
{
    bridge_dispatch_init(&disp, CONFIG_BRIDGE_CREDITS_INIT,
                         (uint8_t)CONFIG_BRIDGE_LOG_LEVEL);
    bridge_parser_init(&parser);
    k_thread_create(&rx_thread, rx_stack, RX_STACK, rx_thread_fn, NULL, NULL,
                    NULL, 5, 0, K_NO_WAIT);
    k_thread_name_set(&rx_thread, "bridge_rx");

    k_thread_create(&tx_thread, tx_stack, TX_STACK, tx_thread_fn, NULL, NULL,
                    NULL, 4, 0, K_NO_WAIT);
    k_thread_name_set(&tx_thread, "bridge_tx");

    k_thread_create(&evt_thread, evt_stack, EVT_STACK, evt_thread_fn, NULL,
                    NULL, NULL, 6, 0, K_NO_WAIT);
    k_thread_name_set(&evt_thread, "bridge_evt");

    link_init_ok = true;
    return 0;
}

void bridge_link_set_reset_hook(void (*hook)(void))
{
    reset_hook = hook;
}

void bridge_link_confirm_snapshot(bool *init_ok, uint32_t *crc_errors,
                                  uint32_t *tx_timeouts,
                                  uint32_t *events_dropped)
{
    /* link_init_ok latches bridge_phy_init()/bridge_link_init() success in
     * main(); the counters are the dispatch mirrors CMD.info reports. */
    if (init_ok != NULL) {
        *init_ok = link_init_ok;
    }
    if (crc_errors != NULL) {
        *crc_errors = disp.crc_errors;
    }
    if (tx_timeouts != NULL) {
        *tx_timeouts = atomic_load(&disp.tx_timeouts);
    }
    if (events_dropped != NULL) {
        *events_dropped = atomic_load(&disp.events_dropped);
    }
}
