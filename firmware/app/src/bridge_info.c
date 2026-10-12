/*
 * bridge_info.c - ping/info response payload builders (Task 9).
 * Pure C11, no libc; see bridge_info.h for the wire contract.
 */

#include "bridge_info.h"

/* Tiny growing writer with an overflow flag; on overflow the output is
 * left incomplete (prefix up to cap only) - the caller must discard it and
 * report -(needed capacity) like bridge_encode() does. */
struct wr {
    uint8_t *buf;
    uint16_t cap;
    uint16_t pos;
    uint16_t would; /* size the result would need */
};

static void wr_bytes(struct wr *w, const uint8_t *p, uint16_t n)
{
    uint16_t end = (uint16_t)(w->pos + n);

    w->would = (uint16_t)(w->would + n);
    if (n != 0U && w->buf != NULL && end <= w->cap) {
        for (uint16_t i = 0; i < n; i++) {
            w->buf[w->pos + i] = p[i];
        }
    }
    if (w->buf != NULL && end > w->cap) {
        w->buf = NULL; /* overflowed: stop committing */
    }
    w->pos = end;
}

static void wr_cstr(struct wr *w, const char *s)
{
    uint16_t n = 0U;

    while (s[n] != '\0') {
        n++;
    }
    wr_bytes(w, (const uint8_t *)s, n);
}

static void wr_u32(struct wr *w, uint32_t v)
{
    char tmp[10];
    uint16_t n = 0U;

    do {
        tmp[n++] = (char)('0' + (v % 10U));
        v /= 10U;
    } while (v != 0U);
    for (uint16_t i = 0; i < n / 2U; i++) {
        char c = tmp[i];
        tmp[i] = tmp[n - 1U - i];
        tmp[n - 1U - i] = c;
    }
    wr_bytes(w, (const uint8_t *)tmp, n);
}

int bridge_info_ping_body(const char *version, uint8_t *out, uint16_t cap)
{
    struct wr w = { out, cap, 0U, 0U };

    wr_cstr(&w, "blebridge");
    wr_cstr(&w, version);
    if (w.pos > cap) {
        return -(int)w.pos;
    }
    return (int)w.pos;
}

int bridge_info_json(const bridge_info_snapshot_t *s, uint8_t *out,
                     uint16_t cap)
{
    struct wr w = { out, cap, 0U, 0U };

    wr_cstr(&w, "{\"version\":\"blebridge");
    wr_cstr(&w, s->version);
    wr_cstr(&w, "\",\"uptime_s\":");
    wr_u32(&w, s->uptime_s);
    wr_cstr(&w, ",\"events_sent\":");
    wr_u32(&w, s->events_sent);
    wr_cstr(&w, ",\"events_dropped\":");
    wr_u32(&w, s->events_dropped);
    wr_cstr(&w, ",\"tx_timeouts\":");
    wr_u32(&w, s->tx_timeouts);
    wr_cstr(&w, ",\"rx_overflows\":");
    wr_u32(&w, s->rx_overflows);
    wr_cstr(&w, ",\"credits\":");
    wr_u32(&w, s->credits);
    wr_cstr(&w, ",\"current_log_level\":");
    wr_u32(&w, s->log_level);
    wr_cstr(&w, ",\"phy\":\"");
    wr_cstr(&w, s->phy);
    wr_cstr(&w, "\"}");
    if (w.pos > cap) {
        return -(int)w.pos;
    }
    return (int)w.pos;
}
