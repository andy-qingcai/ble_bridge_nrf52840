/*
 * main.c - BLE bridge firmware entry point (Task 9 data plane,
 * Task 10 BLE central, task-dfu-a self-confirm).
 *
 * Bring-up order: physical layer -> BLE stack (bt_enable before the link
 * threads exist, so no command can outrun it; on failure the data plane
 * still comes up and the BLE family answers ERR_NOT_READY) -> link threads
 * -> boot EVT.DEBUG on the "sys" namespace -> confirm thread.
 *
 * Self-confirm (task-dfu-a): the confirm thread waits
 * CONFIG_BRIDGE_CONFIRM_DELAY_MS of stable uptime, takes the bridge health
 * snapshot and only then calls boot_write_img_confirmed().  MCUboot booted
 * this image as an unconfirmed "test" swap; if the image dies (or was
 * never healthy) before the confirm, the next reset swaps back to the
 * previous slot -- the brick-proof core of the A1+B architecture
 * (docs/reports/dfu-usbcdc-eval-20260926.md §4).
 */

#include <stdbool.h>
#include <stdio.h>

#include <zephyr/kernel.h>
#include <zephyr/dfu/mcuboot.h>

#include "bridge_ble.h"
#include "bridge_dispatch.h"
#include "bridge_link.h"
#include "bridge_phy.h"

#define CONFIRM_POLL_MS 1000

/* Latched in main() before the confirm thread starts: read-only for the
 * thread afterwards, no locking needed. */
static bool bringup_ok;

static K_THREAD_STACK_DEFINE(confirm_stack,
                             CONFIG_BRIDGE_CONFIRM_THREAD_STACK);
static struct k_thread confirm_thread;

static void confirm_thread_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);

    const uint32_t min_uptime_s =
        (uint32_t)(CONFIG_BRIDGE_CONFIRM_DELAY_MS / 1000);

    for (;;) {
        k_sleep(K_MSEC(CONFIRM_POLL_MS));

        bool init_ok;
        uint32_t crc_errors;
        uint32_t tx_timeouts;
        uint32_t events_dropped;

        bridge_link_confirm_snapshot(&init_ok, &crc_errors, &tx_timeouts,
                                     &events_dropped);
        if (!init_ok) {
            init_ok = bringup_ok;
        }

        if (!bridge_confirm_ready((uint32_t)(k_uptime_get_32() / 1000U),
                                  min_uptime_s, init_ok, crc_errors,
                                  tx_timeouts, events_dropped)) {
            continue;
        }

        if (boot_is_img_confirmed()) {
            /* Reconfirmed boot of an already-confirmed image (or a build
             * without the MCUboot swap trailer): nothing to do. */
            return;
        }

        int err = boot_write_img_confirmed();
        uint8_t payload[64];
        char msg[48];

        int mlen = snprintf(msg, sizeof(msg), "img confirm %s (%d)",
                            (err == 0) ? "ok" : "FAIL", err);

        int plen = (mlen > 0)
                       ? bridge_evt_debug_build("sys", msg, payload,
                                                (uint16_t)sizeof(payload))
                       : -1;
        if (plen > 0) {
            (void)bridge_link_post_event(BRIDGE_EVT_DEBUG, payload,
                                         (uint16_t)plen);
        }
        if (err == 0) {
            return; /* confirmed: the thread's job is done */
        }
        /* Confirm write failed (I/O error): retry on the next poll; the
         * image stays unconfirmed (revert-on-reset remains armed). */
    }
}

int main(void)
{
    int err = bridge_phy_init();

    if (err != 0) {
        /* Bring-up failure: nothing to serve, park.  (MCUboot never gets a
         * confirm -> the next reset reverts to the previous slot.) */
        for (;;) {
            k_sleep(K_SECONDS(10));
        }
    }

    bool bt_ok = (bridge_ble_init() == 0);

    if (bt_ok) {
        /* CMD.reset drops the BLE link before rebooting. */
        bridge_link_set_reset_hook(bridge_ble_api()->shutdown);
    }

    err = bridge_link_init();
    if (err != 0) {
        for (;;) {
            k_sleep(K_SECONDS(10));
        }
    }
    bringup_ok = true;

    k_thread_create(&confirm_thread, confirm_stack,
                    K_THREAD_STACK_SIZEOF(confirm_stack), confirm_thread_fn,
                    NULL, NULL, NULL, 7, 0, K_NO_WAIT);
    k_thread_name_set(&confirm_thread, "bridge_confirm");

    uint8_t payload[64];
    char msg[48];

    int mlen = snprintf(msg, sizeof(msg), "boot %s %s", CONFIG_BRIDGE_VERSION,
                        bt_ok ? "bt" : "nobt");

    int plen = (mlen > 0) ? bridge_evt_debug_build("sys", msg, payload,
                                                   (uint16_t)sizeof(payload))
                          : -1;

    if (plen > 0) {
        (void)bridge_link_post_event(BRIDGE_EVT_DEBUG, payload,
                                     (uint16_t)plen);
    }
    return 0;
}
