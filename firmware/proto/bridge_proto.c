/*
 * bridge_proto.c - UART bridge protocol v2 codec (see bridge_proto.h).
 *
 * Byte-for-byte and counter-for-counter faithful to StreamParser /
 * encode_frame / crc16_ccitt_false in host/src/blebridge/framing.py (the single
 * authority for the parser semantics).  C11, no libc, no dynamic memory.
 */

#include "bridge_proto.h"

/* Null pointer constant without pulling in <stddef.h> (include set stays at
 * <stdint.h> only). */
#define BRIDGE_NULL_ ((void *)0)

/* Parser states - internal, mirror StreamParser in framing.py. */
#define BRIDGE_ST_WAIT_SOF 0U /* scanning for SOF */
#define BRIDGE_ST_GOT_SOF 1U  /* SOF seen, expecting LEN byte 0 */
#define BRIDGE_ST_READ_LEN 2U /* LEN byte 0 seen, expecting LEN byte 1 */
#define BRIDGE_ST_READ_HEADER_REST 3U /* expecting CMD then SEQ */
#define BRIDGE_ST_READ_PAYLOAD 4U     /* collecting LEN payload bytes */
#define BRIDGE_ST_CHECK_CRC 5U        /* collecting the 2 CRC bytes */

/*
 * Layout contract with host/tests/conftest.py (ctypes.Structure mirror of
 * bridge_parser_t).  __builtin_offsetof keeps the include set at <stdint.h>.
 */
#define BRIDGE_OFFSETOF_(type_, member_) __builtin_offsetof(type_, member_)

_Static_assert(BRIDGE_OFFSETOF_(bridge_parser_t, frame_len) ==
                   BRIDGE_MAX_PAYLOAD + 4U,
               "bridge_parser_t layout is mirrored by host/tests/conftest.py");
_Static_assert(BRIDGE_OFFSETOF_(bridge_parser_t, frames_ok) ==
                   BRIDGE_MAX_PAYLOAD + 12U,
               "bridge_parser_t layout is mirrored by host/tests/conftest.py");
_Static_assert(sizeof(bridge_parser_t) == BRIDGE_MAX_PAYLOAD + 28U,
               "bridge_parser_t layout is mirrored by host/tests/conftest.py");

/* 256-entry CRC16-CCITT-FALSE table (poly 0x1021), matching the table
 * built by _build_crc16_table() in framing.py. */
static const uint16_t BRIDGE_CRC_TABLE[256] = {
    0x0000U, 0x1021U, 0x2042U, 0x3063U, 0x4084U, 0x50A5U, 0x60C6U, 0x70E7U,
    0x8108U, 0x9129U, 0xA14AU, 0xB16BU, 0xC18CU, 0xD1ADU, 0xE1CEU, 0xF1EFU,
    0x1231U, 0x0210U, 0x3273U, 0x2252U, 0x52B5U, 0x4294U, 0x72F7U, 0x62D6U,
    0x9339U, 0x8318U, 0xB37BU, 0xA35AU, 0xD3BDU, 0xC39CU, 0xF3FFU, 0xE3DEU,
    0x2462U, 0x3443U, 0x0420U, 0x1401U, 0x64E6U, 0x74C7U, 0x44A4U, 0x5485U,
    0xA56AU, 0xB54BU, 0x8528U, 0x9509U, 0xE5EEU, 0xF5CFU, 0xC5ACU, 0xD58DU,
    0x3653U, 0x2672U, 0x1611U, 0x0630U, 0x76D7U, 0x66F6U, 0x5695U, 0x46B4U,
    0xB75BU, 0xA77AU, 0x9719U, 0x8738U, 0xF7DFU, 0xE7FEU, 0xD79DU, 0xC7BCU,
    0x48C4U, 0x58E5U, 0x6886U, 0x78A7U, 0x0840U, 0x1861U, 0x2802U, 0x3823U,
    0xC9CCU, 0xD9EDU, 0xE98EU, 0xF9AFU, 0x8948U, 0x9969U, 0xA90AU, 0xB92BU,
    0x5AF5U, 0x4AD4U, 0x7AB7U, 0x6A96U, 0x1A71U, 0x0A50U, 0x3A33U, 0x2A12U,
    0xDBFDU, 0xCBDCU, 0xFBBFU, 0xEB9EU, 0x9B79U, 0x8B58U, 0xBB3BU, 0xAB1AU,
    0x6CA6U, 0x7C87U, 0x4CE4U, 0x5CC5U, 0x2C22U, 0x3C03U, 0x0C60U, 0x1C41U,
    0xEDAEU, 0xFD8FU, 0xCDECU, 0xDDCDU, 0xAD2AU, 0xBD0BU, 0x8D68U, 0x9D49U,
    0x7E97U, 0x6EB6U, 0x5ED5U, 0x4EF4U, 0x3E13U, 0x2E32U, 0x1E51U, 0x0E70U,
    0xFF9FU, 0xEFBEU, 0xDFDDU, 0xCFFCU, 0xBF1BU, 0xAF3AU, 0x9F59U, 0x8F78U,
    0x9188U, 0x81A9U, 0xB1CAU, 0xA1EBU, 0xD10CU, 0xC12DU, 0xF14EU, 0xE16FU,
    0x1080U, 0x00A1U, 0x30C2U, 0x20E3U, 0x5004U, 0x4025U, 0x7046U, 0x6067U,
    0x83B9U, 0x9398U, 0xA3FBU, 0xB3DAU, 0xC33DU, 0xD31CU, 0xE37FU, 0xF35EU,
    0x02B1U, 0x1290U, 0x22F3U, 0x32D2U, 0x4235U, 0x5214U, 0x6277U, 0x7256U,
    0xB5EAU, 0xA5CBU, 0x95A8U, 0x8589U, 0xF56EU, 0xE54FU, 0xD52CU, 0xC50DU,
    0x34E2U, 0x24C3U, 0x14A0U, 0x0481U, 0x7466U, 0x6447U, 0x5424U, 0x4405U,
    0xA7DBU, 0xB7FAU, 0x8799U, 0x97B8U, 0xE75FU, 0xF77EU, 0xC71DU, 0xD73CU,
    0x26D3U, 0x36F2U, 0x0691U, 0x16B0U, 0x6657U, 0x7676U, 0x4615U, 0x5634U,
    0xD94CU, 0xC96DU, 0xF90EU, 0xE92FU, 0x99C8U, 0x89E9U, 0xB98AU, 0xA9ABU,
    0x5844U, 0x4865U, 0x7806U, 0x6827U, 0x18C0U, 0x08E1U, 0x3882U, 0x28A3U,
    0xCB7DU, 0xDB5CU, 0xEB3FU, 0xFB1EU, 0x8BF9U, 0x9BD8U, 0xABBBU, 0xBB9AU,
    0x4A75U, 0x5A54U, 0x6A37U, 0x7A16U, 0x0AF1U, 0x1AD0U, 0x2AB3U, 0x3A92U,
    0xFD2EU, 0xED0FU, 0xDD6CU, 0xCD4DU, 0xBDAAU, 0xAD8BU, 0x9DE8U, 0x8DC9U,
    0x7C26U, 0x6C07U, 0x5C64U, 0x4C45U, 0x3CA2U, 0x2C83U, 0x1CE0U, 0x0CC1U,
    0xEF1FU, 0xFF3EU, 0xCF5DU, 0xDF7CU, 0xAF9BU, 0xBFBAU, 0x8FD9U, 0x9FF8U,
    0x6E17U, 0x7E36U, 0x4E55U, 0x5E74U, 0x2E93U, 0x3EB2U, 0x0ED1U, 0x1EF0U,
};

/** Feed `len` bytes at `data` into a running CRC (see bridge_crc16). */
static uint16_t crc_update(uint16_t crc, const uint8_t *data, uint16_t len)
{
    uint16_t i;

    for (i = 0U; i < len; i++) {
        crc = (uint16_t)((uint16_t)(crc << 8) ^
                         BRIDGE_CRC_TABLE[(uint8_t)(crc >> 8) ^ data[i]]);
    }
    return crc;
}

uint16_t bridge_crc16(const uint8_t *data, uint16_t len)
{
    return crc_update(0xFFFFU, data, len);
}

int bridge_encode(uint8_t cmd, uint8_t seq, const uint8_t *payload,
                  uint16_t len, uint8_t *out, uint16_t cap)
{
    uint16_t frame_len;
    uint16_t crc;
    uint16_t i;

    if (len > (uint16_t)BRIDGE_MAX_PAYLOAD) {
        return BRIDGE_ERR_TOO_LONG;
    }

    frame_len = (uint16_t)(BRIDGE_HEADER_LEN + len + BRIDGE_CRC_LEN);
    if (out == BRIDGE_NULL_ || cap < frame_len) {
        /* Capacity report is negative; no bytes are written. */
        return -(int)frame_len;
    }

    out[0] = (uint8_t)BRIDGE_SOF;
    out[1] = (uint8_t)(len & 0xFFU);
    out[2] = (uint8_t)(len >> 8);
    out[3] = cmd;
    out[4] = seq;
    for (i = 0U; i < len; i++) {
        out[BRIDGE_HEADER_LEN + i] = payload[i];
    }

    /* CRC covers CMD..PAYLOAD (out[3] .. out[4+len]). */
    crc = bridge_crc16(&out[3], (uint16_t)(len + 2U));
    out[BRIDGE_HEADER_LEN + len] = (uint8_t)(crc & 0xFFU);
    out[BRIDGE_HEADER_LEN + len + 1U] = (uint8_t)(crc >> 8);

    return (int)frame_len;
}

void bridge_parser_init(bridge_parser_t *p)
{
    p->state = BRIDGE_ST_WAIT_SOF;
    p->len_low = 0U;
    p->frame_len = 0U;
    p->header_pos = 0U;
    p->payload_pos = 0U;
    p->crc_pos = 0U;
    p->frames_ok = 0U;
    p->crc_errors = 0U;
    p->resyncs = 0U;
    p->bytes_dropped = 0U;
    /* Buffer *contents* are irrelevant: positions restart with each attempt,
     * exactly like _reset_attempt() in framing.py. */
}

int bridge_parser_feed(bridge_parser_t *p, uint8_t byte, bridge_frame_t *out)
{
    switch (p->state) {
    case BRIDGE_ST_WAIT_SOF:
        if (byte == (uint8_t)BRIDGE_SOF) {
            p->state = BRIDGE_ST_GOT_SOF;
        } else {
            p->bytes_dropped++;
        }
        return 0;

    case BRIDGE_ST_GOT_SOF:
        p->len_low = byte;
        p->state = BRIDGE_ST_READ_LEN;
        return 0;

    case BRIDGE_ST_READ_LEN: {
        uint16_t announced = (uint16_t)((uint16_t)p->len_low |
                                        (uint16_t)((uint16_t)byte << 8));
        if (announced > (uint16_t)BRIDGE_MAX_PAYLOAD) {
            /* SOF + 2 LEN bytes are bogus: drop and rescan from next byte. */
            p->bytes_dropped += 3U;
            p->resyncs++;
            p->state = BRIDGE_ST_WAIT_SOF;
            return -1;
        }
        p->frame_len = announced;
        p->header_pos = 0U;
        p->payload_pos = 0U;
        p->crc_pos = 0U;
        p->state = BRIDGE_ST_READ_HEADER_REST;
        return 0;
    }

    case BRIDGE_ST_READ_HEADER_REST:
        p->header_rest[p->header_pos] = byte;
        p->header_pos++;
        if (p->header_pos == 2U) {
            /* Zero-length payload skips straight to the CRC bytes. */
            p->state = (p->frame_len == 0U) ? BRIDGE_ST_CHECK_CRC
                                            : BRIDGE_ST_READ_PAYLOAD;
        }
        return 0;

    case BRIDGE_ST_READ_PAYLOAD:
        p->payload[p->payload_pos] = byte;
        p->payload_pos++;
        if (p->payload_pos == p->frame_len) {
            p->state = BRIDGE_ST_CHECK_CRC;
        }
        return 0;

    case BRIDGE_ST_CHECK_CRC:
    default:
        p->crc_bytes[p->crc_pos] = byte;
        p->crc_pos++;
        if (p->crc_pos < BRIDGE_CRC_LEN) {
            return 0;
        } else {
            uint16_t rx_crc = (uint16_t)(
                (uint16_t)p->crc_bytes[0] | (uint16_t)((uint16_t)p->crc_bytes[1] << 8));
            uint16_t calc = crc_update(0xFFFFU, p->header_rest, 2U);
            calc = crc_update(calc, p->payload, p->frame_len);

            if (rx_crc == calc) {
                p->frames_ok++;
                p->state = BRIDGE_ST_WAIT_SOF;
                if (out != BRIDGE_NULL_) {
                    out->cmd = p->header_rest[0];
                    out->seq = p->header_rest[1];
                    out->len = p->frame_len;
                    out->payload = p->payload;
                }
                return 1;
            }

            /* Whole attempt (SOF..CRC) is consumed and dropped. */
            p->bytes_dropped +=
                (uint32_t)(BRIDGE_HEADER_LEN + p->frame_len + BRIDGE_CRC_LEN);
            p->resyncs++;
            p->crc_errors++;
            p->state = BRIDGE_ST_WAIT_SOF;
            return -1;
        }
    }
}
