/*
 * bridge_info.h - ping/info response payload builders (Task 9).
 *
 * Pure logic: no Zephyr, no libc beyond <stdint.h>/<stddef.h> - compiles
 * standalone for host-side unit tests together with bridge_dispatch.c and
 * firmware/proto/bridge_proto.c.
 *
 * Wire formats (authoritative host side: host/src/blebridge/daemon.py module
 * docstring + task-9 brief):
 *
 *   ping response body : "blebridge<semantic version>" as ASCII
 *                        (e.g. version "0.1.0" -> "blebridge0.1.0").
 *   info response body : compact JSON, keys exactly
 *                        version / uptime_s / events_sent / events_dropped /
 *                        tx_timeouts / rx_overflows / credits /
 *                        current_log_level / phy.
 *
 * (C-1/C-3: tx_timeouts = frames the TX owner lost on a phy write timeout;
 * rx_overflows = bytes the ISR dropped because the RX ring was full.  The
 * host treats the info body as opaque bytes, so appended keys stay
 * backwards compatible.)
 *
 * The response payload itself is [status byte][body]; the status byte is
 * prepended by the dispatcher, not here.
 */

#ifndef BRIDGE_INFO_H
#define BRIDGE_INFO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Snapshot of everything the info JSON reports. */
typedef struct {
    const char *version; /* semantic version, e.g. "0.1.0" (not NULL)     */
    const char *phy;     /* physical layer name "cdc" or "uart0" (not NULL)*/
    uint32_t uptime_s;   /* seconds since boot                             */
    uint32_t events_sent;
    uint32_t events_dropped;
    uint32_t tx_timeouts; /* TX frames lost: phy write timeout, no retry   */
    uint32_t rx_overflows; /* RX bytes dropped: ISR ring full              */
    uint16_t credits;    /* current event credit balance                   */
    uint8_t log_level;   /* 0=off 1=err 2=warn 3=info 4=debug              */
} bridge_info_snapshot_t;

/**
 * Build the ping response body "blebridge<version>".
 *
 * @param version semantic version string (not NULL)
 * @param out     output buffer (not NULL)
 * @param cap     capacity of out
 * @return        body length in bytes, or -(required capacity) when cap is
 *                too small (output is then incomplete and must be discarded)
 */
int bridge_info_ping_body(const char *version, uint8_t *out, uint16_t cap);

/**
 * Build the compact info JSON body.
 *
 * @param s    snapshot to render (not NULL)
 * @param out  output buffer (not NULL)
 * @param cap  capacity of out
 * @return     body length in bytes, or -(required capacity) when cap is
 *             too small (output is then incomplete and must be discarded)
 */
int bridge_info_json(const bridge_info_snapshot_t *s, uint8_t *out,
                     uint16_t cap);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_INFO_H */
