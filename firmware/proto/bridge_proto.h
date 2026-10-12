/*
 * bridge_proto.h - UART bridge protocol v2 (ADR-002), C implementation.
 *
 * Single definition source for the wire protocol constants shared by both
 * Zephyr firmwares (firmware/app, firmware/suota_sim).  This library is the C
 * half of a cross-language contract: it must agree byte-for-byte and
 * counter-for-counter with host/src/blebridge/framing.py, enforced by the L2
 * contract tests (host/tests/test_proto_c.py, ADR-005) against the shared
 * golden vectors in host/tests/golden_frames.json.
 *
 * Frame layout (offsets from the start of the frame):
 *
 *     offset  field    size  notes
 *     0       SOF      1B    fixed 0x55
 *     1       LEN      2B LE payload length, 0..BRIDGE_MAX_PAYLOAD (512)
 *     3       CMD      1B    request 0x01..0x7F; response = request CMD | 0x80;
 *                            events 0x20..0x2F and 0x8E
 *     4       SEQ      1B    request sequence; responses echo it; events carry
 *                            the event ring sequence number here
 *     5       PAYLOAD  LEN B <= 512
 *     5+LEN   CRC      2B LE CRC16-CCITT-FALSE (poly 0x1021, init 0xFFFF,
 *                            no reflection, no final XOR) over CMD..PAYLOAD
 *
 * Sliding resynchronisation (ADR-002, FR-A4): there is no byte stuffing.  On
 * any invalid step (LEN > 512, CRC mismatch) the bytes consumed by the failed
 * frame attempt are dropped, the parser returns to WAIT_SOF and resumes from
 * the next incoming byte.  A non-SOF byte seen while in WAIT_SOF is dropped
 * immediately.  This mirrors StreamParser in framing.py exactly.
 *
 * Constraints (kept on purpose for firmware deployment):
 *   - C11, no libc: <stdint.h> is the only include; memcpy/strlen/malloc are
 *     not used anywhere.
 *   - Zero dynamic memory: the caller owns all buffers, including the parser
 *     state (on stack or static - the struct is ~540 B).
 *
 * Build:
 *   - Standalone / host tests: see CMakeLists.txt (static or
 *     -DBRIDGE_PROTO_SHARED=ON shared library) or compile bridge_proto.c
 *     directly.  Clean under -Wall -Wextra -Werror -std=c11.
 *   - Zephyr: add_subdirectory() this directory and link `bridge_proto`.
 */

#ifndef BRIDGE_PROTO_H
#define BRIDGE_PROTO_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* -------------------------------------------------------------------------
 * Protocol constants (single definition source, ADR-002)
 * ---------------------------------------------------------------------- */

/** Start-of-frame byte. */
#define BRIDGE_SOF 0x55U

/** Maximum payload length carried by one frame (LEN field, u16 LE). */
#define BRIDGE_MAX_PAYLOAD 512U

/** SOF(1) + LEN(2) + CMD(1) + SEQ(1). */
#define BRIDGE_HEADER_LEN 5U

/** CRC16-CCITT-FALSE trailer length (u16 LE). */
#define BRIDGE_CRC_LEN 2U

/** Total non-payload bytes per frame: header + CRC. */
#define BRIDGE_FRAME_OVERHEAD (BRIDGE_HEADER_LEN + BRIDGE_CRC_LEN)

/** Largest frame bridge_encode() can produce: 5 + 512 + 2 bytes. */
#define BRIDGE_MAX_FRAME \
    (BRIDGE_HEADER_LEN + BRIDGE_MAX_PAYLOAD + BRIDGE_CRC_LEN)

/*
 * Error code returned by bridge_encode() when `len` exceeds
 * BRIDGE_MAX_PAYLOAD.  The magnitude (1000) is deliberately above
 * BRIDGE_MAX_FRAME (519) so it can never be confused with the
 * "-(required length)" capacity report, which is also negative.
 */
#define BRIDGE_ERR_TOO_LONG (-1000)

/* -------------------------------------------------------------------------
 * Response status codes (Task 9)
 * ---------------------------------------------------------------------- */

/**
 * Status codes carried as the FIRST payload byte of every response frame
 * (response CMD = request CMD | 0x80).  Host-side contract: the remaining
 * payload bytes are the response body; ``status == 0`` means success
 * (see request() in host/src/blebridge/bridge.py and the daemon docstring).
 *
 * Command 0x0E is RESERVED and must NOT be answered at all: its response
 * byte 0x8E would collide with EVT.DEBUG (bridge.py CMD docstring).
 */
typedef enum {
    BRIDGE_STATUS_OK             = 0x00, /* request succeeded                */
    BRIDGE_STATUS_ERR_UNKNOWN_CMD = 0x71, /* command byte not implemented   */
    BRIDGE_STATUS_ERR_BAD_PAYLOAD = 0x72, /* payload length/content invalid */
    BRIDGE_STATUS_ERR_BUSY        = 0x73, /* resource busy, retry later     */
    BRIDGE_STATUS_ERR_NOT_READY   = 0x74, /* feature not ready (BLE Task 10)*/
    /**
     * CMD.pair (0x43, FR-B5): the SMP pairing procedure did not deliver an
     * encrypted link within BRIDGE_PAIR_TIMEOUT_MS (or failed outright --
     * pairing_failed callback / bt_conn_set_security() error).  Mirrors the
     * host CMD authority (bridge.py); see docs/protocol-contract.md §1.
     */
    BRIDGE_STATUS_ERR_PAIR_FAILED = 0x75, /* SMP pairing failed/timed out   */
} bridge_status_t;

/* -------------------------------------------------------------------------
 * CRC
 * ---------------------------------------------------------------------- */

/**
 * CRC16-CCITT-FALSE over `len` bytes: poly 0x1021, init 0xFFFF, no
 * reflection, no final XOR.  256-entry static const table lookup.
 *
 * Check value: bridge_crc16("123456789", 9) == 0x29B1.
 *
 * @param data  bytes to checksum (may be NULL only when len == 0)
 * @param len   byte count
 * @return      16-bit CRC
 */
uint16_t bridge_crc16(const uint8_t *data, uint16_t len);

/* -------------------------------------------------------------------------
 * Encoding
 * ---------------------------------------------------------------------- */

/**
 * Encode one frame into `out`.
 *
 * Layout written: SOF, LEN (u16 LE), CMD, SEQ, PAYLOAD[0..len), CRC (u16 LE).
 * The CRC covers CMD..PAYLOAD, exactly like crc16_ccitt_false() over
 * bytes((cmd, seq)) + payload in framing.py.
 *
 * @param cmd      command byte (0..255, passed as uint8_t)
 * @param seq      sequence byte (0..255, passed as uint8_t)
 * @param payload  payload bytes; may be NULL only when len == 0
 * @param len      payload length in bytes, 0..BRIDGE_MAX_PAYLOAD
 * @param out      output buffer; may be NULL only when the frame will not fit
 *                 (see return value)
 * @param cap      capacity of `out` in bytes
 * @return         total frame length (5 + len + 2) on success;
 *                 -(total frame length) when `out` is NULL or `cap` is
 *                 smaller than the required length (no bytes are written);
 *                 BRIDGE_ERR_TOO_LONG when len > BRIDGE_MAX_PAYLOAD.
 */
int bridge_encode(uint8_t cmd, uint8_t seq, const uint8_t *payload,
                  uint16_t len, uint8_t *out, uint16_t cap);

/* -------------------------------------------------------------------------
 * Stream parser
 * ---------------------------------------------------------------------- */

/**
 * One decoded frame.  `payload` points into the parser's internal buffer and
 * stays valid only until the next bridge_parser_feed() call on the same
 * parser.  `len` is 0..BRIDGE_MAX_PAYLOAD.
 */
typedef struct {
    uint8_t cmd;
    uint8_t seq;
    uint16_t len;
    const uint8_t *payload;
} bridge_frame_t;

/**
 * Byte-by-byte streaming parser state.
 *
 * NOTE: the field layout below is mirrored by host/tests/conftest.py
 * (ctypes.Structure).  Do not reorder or remove fields without updating that
 * mirror and the _Static_assert guards in bridge_proto.c.
 *
 * Statistics (uint32_t, monotonically non-decreasing; semantics replicate
 * StreamParser in host/src/blebridge/framing.py byte for byte):
 *   - frames_ok:     frames decoded and CRC-verified
 *   - crc_errors:    attempts rejected because the CRC did not match
 *   - resyncs:       aborted frame attempts (bad LEN or bad CRC)
 *   - bytes_dropped: bytes consumed that did not become part of a valid
 *                    frame.  For any byte stream fed to completion:
 *                    total_bytes == bytes_dropped
 *                                  + sum over frames of
 *                                    BRIDGE_FRAME_OVERHEAD + frame.len
 */
typedef struct {
    /* Buffers (first, so the layout has no interior padding). */
    uint8_t payload[BRIDGE_MAX_PAYLOAD]; /* decoded payload buffer */
    uint8_t header_rest[2];              /* CMD, SEQ */
    uint8_t crc_bytes[2];                /* CRC trailer as received */
    /* Current attempt bookkeeping. */
    uint16_t frame_len;   /* payload length announced by the LEN field */
    uint16_t payload_pos; /* bytes collected into payload[] */
    uint8_t len_low;      /* first LEN byte while the second is pending */
    uint8_t header_pos;   /* bytes collected into header_rest[] */
    uint8_t crc_pos;      /* bytes collected into crc_bytes[] */
    uint8_t state;        /* parser state (BRIDGE_ST_*, internal) */
    /* Statistics. */
    uint32_t frames_ok;
    uint32_t crc_errors;
    uint32_t resyncs;
    uint32_t bytes_dropped;
} bridge_parser_t;

/**
 * Initialise a parser: clears state, buffers and all four statistics.
 * Re-initialising a used parser is supported.
 *
 * @param p  parser instance owned by the caller (not NULL)
 */
void bridge_parser_init(bridge_parser_t *p);

/**
 * Feed one byte into the parser (sliding resync state machine, mirroring
 * StreamParser._feed_byte in framing.py).
 *
 * @param p    parser instance (not NULL)
 * @param byte next stream byte
 * @param out  receives the decoded frame when the return value is 1; may be
 *             NULL if the caller only cares about the statistics
 * @return     1  a complete frame was decoded; *out (if given) is valid and
 *                out->payload points into the parser's internal buffer
 *             0  more bytes needed (a non-SOF byte in WAIT_SOF was dropped
 *                and counted in bytes_dropped - that is not a resync)
 *             -1 this byte triggered a resynchronisation drop (bad LEN or
 *                bad CRC); the failed attempt's bytes were added to
 *                bytes_dropped and resyncs (and crc_errors, for a CRC
 *                mismatch) were incremented
 */
int bridge_parser_feed(bridge_parser_t *p, uint8_t byte, bridge_frame_t *out);

#ifdef __cplusplus
}
#endif

#endif /* BRIDGE_PROTO_H */
