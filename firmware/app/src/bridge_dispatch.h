/*
 * bridge_dispatch.h - command dispatcher + event credit/flow-control logic
 * (Task 9 / Task 10 I-1).
 *
 * Pure logic: no Zephyr, no libc beyond <stdint.h>/<stdbool.h>/<stddef.h>
 * and the compiler-provided C11 <stdatomic.h> (Task 10 review I-1: the
 * cross-thread counters below are _Atomic so the link RX thread, the event
 * thread and the BT stack threads observe them without data races; the
 * compiler-provided header keeps this file host-testable).
 * Compiles standalone for host-side unit tests together with
 * bridge_info.c and firmware/proto/bridge_proto.c.  All I/O (frame
 * encoding, queues, threads, reboot) lives in bridge_link.c.
 *
 * CMD values are the authority of host/src/blebridge/bridge.py (CMD enum).
 * Response payload wire format: [status byte][body], status codes are the
 * BRIDGE_STATUS_* enum from firmware/proto/bridge_proto.h.
 *
 * Event flow control (FR-A2/A3, task-9 brief - cross-end constants,
 * followed verbatim):
 *   - firmware starts with 128 credits;
 *   - each emitted event consumes 1 credit;
 *   - CMD.credits (payload u16 LE) grants more;
 *   - credit == 0 -> the event is dropped, events_dropped++ and the 8-bit
 *     event ring sequence number STILL ADVANCES, so the host sees a wire
 *     gap (bridge.py: "a jump means device-side loss") and the counters in
 *     info quantify it.
 */

#ifndef BRIDGE_DISPATCH_H
#define BRIDGE_DISPATCH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>

#include "bridge_ble_api.h"
#include "bridge_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- command bytes (mirrors of bridge.py CMD; keep in sync) ---------- */

#define BRIDGE_CMD_PING 0x01
#define BRIDGE_CMD_INFO 0x02
#define BRIDGE_CMD_SCAN_START 0x03
#define BRIDGE_CMD_SCAN_STOP 0x04
#define BRIDGE_CMD_CONNECT 0x05
#define BRIDGE_CMD_DISCONNECT 0x06
#define BRIDGE_CMD_MTU 0x07
#define BRIDGE_CMD_GATT_READ 0x08
#define BRIDGE_CMD_GATT_WRITE 0x09
#define BRIDGE_CMD_SUBSCRIBE 0x0A
#define BRIDGE_CMD_UNSUBSCRIBE 0x0B
#define BRIDGE_CMD_DISC_SVC 0x0C
#define BRIDGE_CMD_RESET 0x0D
#define BRIDGE_CMD_RESERVED 0x0E /* RESERVED: never answer (0x8E would    */
                                 /* collide with EVT.DEBUG); disabled.    */
#define BRIDGE_CMD_LOG_LEVEL 0x0F
#define BRIDGE_CMD_SUOTA_START 0x40
#define BRIDGE_CMD_SUOTA_ABORT 0x41
#define BRIDGE_CMD_SUOTA_STATUS 0x42
#define BRIDGE_CMD_PAIR 0x43 /* FR-B5: SMP pair -> L2 (Just Works)       */
#define BRIDGE_CMD_CONN_UPDATE 0x44 /* LE connection parameter update    */
#define BRIDGE_CMD_PHY 0x45         /* LE PHY preference (1=1M, 2=2M)    */
#define BRIDGE_CMD_ADV_START 0x46   /* FR-H: start peripheral advertising */
#define BRIDGE_CMD_ADV_STOP 0x47    /* FR-H: stop peripheral advertising  */
#define BRIDGE_CMD_NOTIFY 0x48      /* FR-H: push local characteristic    */
                                  /*       notify content               */
#define BRIDGE_CMD_SET_CHAR 0x49    /* FR-H: set local characteristic     */
                                  /*       value                        */
#define BRIDGE_CMD_GATT_DEFINE 0x4A /* FR-H5: define a GATT service       */
#define BRIDGE_CMD_ENTER_DFU 0x4B /* task-dfu-a: GPREGRET boot mode ->   */
                                  /* MCUboot serial recovery + reboot    */
#define BRIDGE_CMD_CREDITS 0x50

/* ---- event bytes (mirrors of bridge.py EVT) -------------------------- */

#define BRIDGE_EVT_DEBUG 0x8E
#define BRIDGE_EVT_SCAN 0x20
#define BRIDGE_EVT_CONN 0x21
#define BRIDGE_EVT_DISC 0x22
#define BRIDGE_EVT_GATTX 0x23
#define BRIDGE_EVT_MTU 0x24
#define BRIDGE_EVT_SUOTA 0x25

/* ---- dispatcher state ------------------------------------------------- */

typedef struct {
    /* flow control / event ring.
     *
     * Cross-thread fields (Task 10 I-1) are C11 atomics:
     *   - credits       : written by the event thread (admit, CAS loop)
     *                     and the link RX thread (CMD.credits grant);
     *                     read by CMD.info on the RX thread.
     *   - log_level     : written by the link RX thread (CMD.log_level);
     *                     read by bridge_link_post_event() from BLE
     *                     callback contexts (BT RX / sys workqueue) and
     *                     by the RX thread itself.
     *   - events_sent / events_dropped : written by the event thread only;
     *                     read by CMD.info on the RX thread.
     *   - tx_timeouts  : written by the link TX thread (bridge_phy_write
     *                     timed out, frame lost); read by CMD.info on the
     *                     RX thread.
     * evt_seq, frames_ok, ... and reset_requested stay plain: each is
     * confined to a single thread (see bridge_link.h thread model). */
    atomic_ushort credits;   /* current credit balance                   */
    uint8_t evt_seq;         /* next 8-bit event wire sequence number    */
    atomic_uchar log_level;  /* 0=off 1=err 2=warn 3=info 4=debug        */
    atomic_uint events_sent;
    atomic_uint events_dropped;
    /* OPTIMIZATION-PLAN C-1: frames lost when the single TX owner could not
     * push a frame out within its timeout ("silent" loss, no retry). */
    atomic_uint tx_timeouts;

    /* parser statistics mirror (refreshed by the link from its parser so
     * info can report link health without exposing the parser itself) */
    uint32_t frames_ok;
    uint32_t crc_errors;
    uint32_t resyncs;
    uint32_t bytes_dropped;
    /* OPTIMIZATION-PLAN C-3: mirror of bridge_phy_rx_overflows() (bytes the
     * ISR dropped because the RX ring was full).  Plain like the parser
     * mirror above: written and read only on the link RX thread (per-loop
     * refresh + CMD.info). */
    uint32_t rx_overflows;

    /* latched by CMD.reset; the link performs the ACK-then-reboot */
    bool reset_requested;
    /* latched by CMD.enter_dfu (task-dfu-a); the link performs the
     * ACK-then-bootmode_set(BOOTLOADER)-then-reboot into the MCUboot
     * serial recovery console */
    bool dfu_requested;
} bridge_dispatch_t;

/** Per-request environment supplied by the caller (link layer). */
typedef struct {
    const char *version; /* semantic version, e.g. "0.1.0" (not NULL)     */
    const char *phy;     /* "cdc" or "uart0" (not NULL)                    */
    uint32_t uptime_ms;  /* caller snapshot at request time                */
    const bridge_ble_api_t *ble; /* BLE command surface; NULL = Task 9
                                  * skeleton behaviour (BLE answers
                                  * ERR_NOT_READY; host tests pass NULL)   */
} bridge_dispatch_env_t;

/**
 * Initialise the dispatcher: credits = initial (cross-end constant 128),
 * log level = initial (3), all counters zero, evt_seq = 0.
 *
 * @param d        dispatcher instance (not NULL)
 * @param credits  initial credit balance (cross-end constant: 128)
 * @param level    initial log level 0..4 (clamped)
 */
void bridge_dispatch_init(bridge_dispatch_t *d, uint16_t credits,
                          uint8_t level);

/** Copy the parser statistics mirror from `p` (both may be NULL-free). */
void bridge_dispatch_refresh_stats(bridge_dispatch_t *d,
                                   const bridge_parser_t *p);

/**
 * Handle one decoded request frame and build the response payload
 * "[status byte][body]" into `out`.
 *
 * Special cases:
 *   - CMD 0x0E is RESERVED and returns 0 WITHOUT writing a response: the
 *     response byte 0x8E would collide with EVT.DEBUG on the wire.
 *   - CMD.reset latches reset_requested in the dispatcher; the caller
 *     flushes the ACK (best effort, 10 ms) and reboots.
 *   - CMD.enter_dfu latches dfu_requested; the caller flushes the ACK and
 *     reboots with the bootloader boot-mode latched (serial recovery).
 *
 * @param d    dispatcher instance (not NULL)
 * @param env  per-request environment (not NULL)
 * @param req  decoded request frame (not NULL)
 * @param out  response payload buffer (not NULL; >= 1 + body bytes)
 * @param cap  capacity of out
 * @return     response payload length (>= 1), 0 = "no response", or
 *             -(required capacity) when the buffer is too small
 */
int bridge_dispatch_handle(bridge_dispatch_t *d,
                           const bridge_dispatch_env_t *env,
                           const bridge_frame_t *req, uint8_t *out,
                           uint16_t cap);

/* ---- event credit / ring-sequence decisions --------------------------- */

typedef enum {
    BRIDGE_EVT_EMIT = 0,  /* credit consumed; send with the returned seq  */
    BRIDGE_EVT_DROP,      /* credit == 0: drop; seq still consumed        */
} bridge_evt_action_t;

typedef struct {
    bridge_evt_action_t action;
    uint8_t seq; /* wire SEQ for the event frame (valid in both cases)   */
} bridge_evt_decision_t;

/**
 * Admit one event for emission: assign and advance the ring sequence
 * number, consume a credit, or drop when credits are exhausted.
 * (log-level gating of EVT.DEBUG happens before this, at post time.)
 *
 * @param d   dispatcher instance (not NULL)
 * @return    decision (action + assigned wire SEQ)
 */
bridge_evt_decision_t bridge_evt_admit(bridge_dispatch_t *d);

/** Grant `n` event credits (CMD.credits; u16 LE payload on the wire). */
void bridge_dispatch_grant_credits(bridge_dispatch_t *d, uint16_t n);

/**
 * The event thread lost a frame after admit (TX queue full): undo the
 * "sent" accounting and count it as dropped (seq was already consumed,
 * so the loss stays visible as a wire gap).
 */
void bridge_evt_tx_lost(bridge_dispatch_t *d);

/**
 * Build an EVT.DEBUG payload: [tag_len(1B)][tag][msg].
 *
 * @param tag  short namespace ("sys"/"phy"/"disp"); >255 bytes rejected
 * @param msg  free-form message (may be NULL -> empty)
 * @param out  output buffer (not NULL)
 * @param cap  capacity of out
 * @return     payload length, -(required capacity) when too small,
 *             or -EINVAL for a bad tag
 */
int bridge_evt_debug_build(const char *tag, const char *msg, uint8_t *out,
                           uint16_t cap);

/* ---- image self-confirm decision (task-dfu-a, pure) -------------------- */

/**
 * Pure decision for the image self-confirm gate (main.c confirm thread):
 * confirm only after a stable-uptime window AND a clean health snapshot.
 *
 * "Clean" mirrors the bridge's own health surfaces (the exact counters
 * CMD.info exposes), evaluated once at the deadline:
 *   - init_ok        : phy + link brought up without error (BLE may be
 *                      degraded -- bt_ok=false still confirms, the data
 *                      plane is the recovery-critical function);
 *   - crc_errors     : frame CRC failures observed since boot;
 *   - tx_timeouts    : frames lost by TX write timeout since boot;
 *   - events_dropped : events lost to credit exhaustion since boot.
 * Any non-zero counter means the trial run already misbehaved: leave the
 * image unconfirmed so the next reset reverts it.
 *
 * @param uptime_s       seconds since boot at the check
 * @param min_uptime_s   required stable window
 *                       (CONFIG_BRIDGE_CONFIRM_DELAY_MS / 1000; the
 *                       caller scales, keeping this function host-testable
 *                       without Zephyr)
 * @param init_ok        true when phy+link init succeeded
 * @param crc_errors     CRC failure counter snapshot
 * @param tx_timeouts    TX timeout counter snapshot
 * @param events_dropped dropped-event counter snapshot
 * @return               true = call boot_write_img_confirmed() now
 */
bool bridge_confirm_ready(uint32_t uptime_s, uint32_t min_uptime_s,
                          bool init_ok, uint32_t crc_errors,
                          uint32_t tx_timeouts, uint32_t events_dropped);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_DISPATCH_H */
