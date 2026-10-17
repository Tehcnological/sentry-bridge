/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (c) 2026 Sentry Bridge contributors.
 * See NOTICE.txt.
 */
/*
 * sbcore implementation. See sbcore.h. Produces the same output as bridge.py, gdl90.py and
 * uat.py, which stay the reference; the parity tests compare outputs. The 1090ES and UAT field
 * decoders are in sbdecode.c, written from docs/Decoder-Spec.md.
 */
#include "sbcore.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

const char sb_enable_body[] = "{\"publicGDL90\":true}";
const char sb_enable_path[] = "/settings/?action=set";
const char sb_settings_path[] = "/settings/?action=get";

#define NM_PER_RAD 3440.065
#define MAX_LOCAL_REF_NM 170.0
#define MAX_GLOBAL_REF_NM 300.0 /* beyond 1090 radio range */
#define MAX_TARGET_KT 900.0
#define JUMP_SLACK_NM 0.1
#define GLOBAL_PAIR_WINDOW_S 10.0
#define DROP_AFTER_S 60.0
#define STREAM_GAP_S 10.0
#define ENABLE_RETRY_S 20.0
#define ENABLE_MAX_TRIES 3

/* ------------------------------------------------------------------ small helpers */

static void logf_(sb_bridge *b, const char *fmt, ...) {
    char buf[256];
    va_list ap;
    if (!b->cfg.log) return;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    b->cfg.log(b->cfg.log_ctx, buf);
}

/* Python floor-mod for ints and floats */
static long pymod_l(long a, long n) {
    long r = a % n;
    return (r != 0 && ((r < 0) != (n < 0))) ? r + n : r;
}
static double pymod_d(double a, double n) {
    double r = fmod(a, n);
    if (r != 0.0 && ((r < 0) != (n < 0))) r += n;
    else if (r == 0.0) r = copysign(0.0, n);
    return r;
}
/* Python round() on a float: round half to even */
static long pyround(double x) {
    double f = floor(x), d = x - f;
    if (d > 0.5) return (long)f + 1;
    if (d < 0.5) return (long)f;
    return ((long)f % 2 == 0) ? (long)f : (long)f + 1;
}

static const double DEG_TO_RAD = M_PI / 180.0;

static double dist_nm(double lat1, double lon1, double lat2, double lon2) {
    double p1 = lat1 * DEG_TO_RAD, p2 = lat2 * DEG_TO_RAD;
    double dp = p2 - p1, dl = (lon2 - lon1) * DEG_TO_RAD;
    double a = sin(dp / 2) * sin(dp / 2) + cos(p1) * cos(p2) * sin(dl / 2) * sin(dl / 2);
    double s = sqrt(a);
    return 2 * NM_PER_RAD * asin(s < 1.0 ? s : 1.0);
}

/* ------------------------------------------------------------------ GDL90 */

static uint16_t crc_table[256];
static int crc_ready = 0;

static void crc_init(void) {
    int i, k;
    for (i = 0; i < 256; i++) {
        uint32_t c = (uint32_t)i << 8;
        for (k = 0; k < 8; k++) c = (c & 0x8000) ? ((c << 1) ^ 0x1021) : (c << 1);
        crc_table[i] = (uint16_t)(c & 0xFFFF);
    }
    crc_ready = 1;
}

uint16_t sb_crc16(const uint8_t *d, size_t n) {
    uint16_t crc = 0;
    size_t i;
    if (!crc_ready) crc_init();
    for (i = 0; i < n; i++) crc = (uint16_t)(crc_table[crc >> 8] ^ (uint16_t)(crc << 8) ^ d[i]);
    return crc;
}

static size_t stuff_byte(uint8_t b, uint8_t *out, size_t pos, size_t cap) {
    if (b == 0x7E || b == 0x7D) {
        if (pos + 2 > cap) return (size_t)-1;
        out[pos] = 0x7D;
        out[pos + 1] = (uint8_t)(b ^ 0x20);
        return pos + 2;
    }
    if (pos + 1 > cap) return (size_t)-1;
    out[pos] = b;
    return pos + 1;
}

size_t sb_frame(const uint8_t *msg, size_t len, uint8_t *out, size_t cap) {
    size_t pos = 0, i;
    uint16_t crc = sb_crc16(msg, len);
    if (cap < 2) return 0;
    out[pos++] = 0x7E;
    for (i = 0; i < len; i++)
        if ((pos = stuff_byte(msg[i], out, pos, cap)) == (size_t)-1) return 0;
    if ((pos = stuff_byte((uint8_t)(crc & 0xFF), out, pos, cap)) == (size_t)-1) return 0;
    if ((pos = stuff_byte((uint8_t)(crc >> 8), out, pos, cap)) == (size_t)-1) return 0;
    if (pos + 1 > cap) return 0;
    out[pos++] = 0x7E;
    return pos;
}

typedef struct {
    uint32_t address;
    int has_pos;
    double lat, lon;
    int has_alt;
    int32_t alt_ft;
    int nic;
} own_report;

static long s24(uint32_t v) { return (v & 0x800000) ? (long)v - (1L << 24) : (long)v; }

static void decode_ownship(const uint8_t *m, own_report *r) {
    uint32_t lat24 = ((uint32_t)m[5] << 16) | ((uint32_t)m[6] << 8) | m[7];
    uint32_t lon24 = ((uint32_t)m[8] << 16) | ((uint32_t)m[9] << 8) | m[10];
    int alt = (m[11] << 4) | (m[12] >> 4);
    double res = 180.0 / (1 << 23);
    r->nic = m[13] >> 4;
    r->has_pos = r->nic != 0;
    r->lat = (double)s24(lat24) * res;
    r->lon = (double)s24(lon24) * res;
    r->has_alt = alt != 0xFFF;
    r->alt_ft = alt * 25 - 1000;
}

/* Fields of a Traffic Report, as bridge.py's tick() fills gdl90.Report */
typedef struct {
    uint32_t address;
    double lat, lon;
    int has_alt;
    int32_t alt_ft;
    int airborne, nic, nacp;
    int has_gs;
    double gs_kt;
    int has_vs;
    int32_t vs_fpm;
    int has_trk;
    double track_deg;
    int track_type, emitter, addr_type, emergency;
    const char *callsign;
} traffic;

static size_t encode_traffic(const traffic *r, uint8_t *out, size_t cap) {
    uint8_t b[28];
    double res = 180.0 / (1 << 23);
    uint32_t lat24 = (uint32_t)(pyround(r->lat / res) & 0xFFFFFF);
    uint32_t lon24 = (uint32_t)(pyround(r->lon / res) & 0xFFFFFF);
    long alt, hv, vv;
    int tt, i, n = 0;
    char cs[9];
    memset(b, 0, sizeof b);
    b[0] = 0x14;
    b[1] = (uint8_t)(((0 & 0xF) << 4) | (r->addr_type & 0xF));
    b[2] = (uint8_t)(r->address >> 16);
    b[3] = (uint8_t)(r->address >> 8);
    b[4] = (uint8_t)r->address;
    b[5] = (uint8_t)(lat24 >> 16); b[6] = (uint8_t)(lat24 >> 8); b[7] = (uint8_t)lat24;
    b[8] = (uint8_t)(lon24 >> 16); b[9] = (uint8_t)(lon24 >> 8); b[10] = (uint8_t)lon24;
    if (!r->has_alt) alt = 0xFFF;
    else {
        alt = pyround((r->alt_ft + 1000) / 25.0);
        if (alt > 0xFFE) alt = 0xFFE;
        if (alt < 0) alt = 0;
    }
    tt = r->has_trk ? r->track_type : 0;
    b[11] = (uint8_t)(alt >> 4);
    b[12] = (uint8_t)(((alt & 0xF) << 4) | (r->airborne ? 0x8 : 0) | (tt & 0x3));
    b[13] = (uint8_t)(((r->nic & 0xF) << 4) | (r->nacp & 0xF));
    if (!r->has_gs) hv = 0xFFF;
    else {
        hv = pyround(r->gs_kt);
        if (hv > 0xFFE) hv = 0xFFE;
        if (hv < 0) hv = 0;
    }
    if (!r->has_vs) vv = 0x800;
    else {
        vv = pyround(r->vs_fpm / 64.0);
        if (vv > 510) vv = 510;
        if (vv < -510) vv = -510;
        vv &= 0xFFF;
    }
    b[14] = (uint8_t)(hv >> 4);
    b[15] = (uint8_t)(((hv & 0xF) << 4) | (vv >> 8));
    b[16] = (uint8_t)(vv & 0xFF);
    b[17] = r->has_trk ? (uint8_t)(pyround(pymod_d(r->track_deg, 360.0) / (360.0 / 256)) & 0xFF) : 0;
    b[18] = (uint8_t)r->emitter;
    for (i = 0; r->callsign && r->callsign[i] && n < 8; i++) {
        char c = r->callsign[i];
        if (c >= 'a' && c <= 'z') c = (char)(c - 32);
        if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) cs[n++] = c;
    }
    while (n < 8) cs[n++] = ' ';
    memcpy(b + 19, cs, 8);
    b[27] = (uint8_t)((r->emergency & 0xF) << 4);
    return sb_frame(b, sizeof b, out, cap);
}

/* ------------------------------------------------------------------ Mode S */

static uint32_t bits(const uint8_t *m, int start, int n) {
    uint32_t v = 0;
    int i;
    for (i = 0; i < n; i++) {
        int p = start + i;
        v = (v << 1) | ((m[p >> 3] >> (7 - (p & 7))) & 1);
    }
    return v;
}
#define ME(m, s, n) bits((m), 32 + (s), (n))

#include "sbdecode.c" /* 1090ES and UAT field decoding, written from docs/Decoder-Spec.md */

/* GDL90 emitter categories from ADS-B (typecode, CA), as bridge.py _EMITTER */
static int emitter_for(int tc, int ca) {
    if (tc == 4 && ca >= 1 && ca <= 7) return ca;
    if (tc == 3) {
        static const int m3[8] = {0, 9, 10, 11, 12, 0, 14, 15};
        return m3[ca & 7];
    }
    if (tc == 2) {
        static const int m2[8] = {0, 17, 0, 18, 19, 19, 19, 19};
        return m2[ca & 7];
    }
    return 0;
}

static int nic_for_tc(int tc) {
    switch (tc) {
    case 9: return 11; case 10: return 10; case 11: return 8; case 12: return 7;
    case 13: return 6; case 14: return 5; case 15: return 4; case 16: return 3;
    case 17: return 1; case 18: return 0; case 20: return 11; case 21: return 10;
    case 22: return 0; case 5: return 11; case 6: return 10; case 7: return 8; case 8: return 0;
    default: return 8;
    }
}

/* ------------------------------------------------------------------ UAT (DO-282B) Reed-Solomon */

static uint8_t gf_exp[512], gf_log[256];
static int gf_ready = 0;

static void gf_init(void) {
    int i, x = 1;
    for (i = 0; i < 255; i++) {
        gf_exp[i] = (uint8_t)x;
        gf_log[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= 0x187;
    }
    for (i = 255; i < 512; i++) gf_exp[i] = gf_exp[i - 255];
    gf_ready = 1;
}
static uint8_t gmul(uint8_t a, uint8_t b) { return (a && b) ? gf_exp[gf_log[a] + gf_log[b]] : 0; }
static uint8_t gdiv(uint8_t a, uint8_t b) {
    return a ? gf_exp[(gf_log[a] + 255 - gf_log[b]) % 255] : 0;
}
static uint8_t apow(long e) { return gf_exp[pymod_l(e, 255)]; }
#define FCR 120

static uint8_t poly_eval_hi(const uint8_t *p, size_t n, uint8_t x) { /* p[0] highest degree */
    uint8_t y = 0;
    size_t i;
    for (i = 0; i < n; i++) y = (uint8_t)(gmul(y, x) ^ p[i]);
    return y;
}

int sb_uat_correct(const uint8_t *block, size_t n, uint8_t *out, int *nerr_out) {
    uint8_t cw[48], synd[14], lam[16], prev[16], tmp[32], omega[14];
    int nroots, j, k, L = 0, m = 1, lam_len = 1, prev_len = 1, nerr, npos = 0, pos[16];
    uint8_t bcoef = 1;
    if (!gf_ready) gf_init();
    if (n == 48) nroots = 14;
    else if (n == 30) nroots = 12;
    else return 0;
    memcpy(cw, block, n);
    {
        int any = 0;
        for (j = 0; j < nroots; j++) {
            synd[j] = poly_eval_hi(cw, n, apow(FCR + j));
            any |= synd[j];
        }
        if (!any) {
            memcpy(out, cw, n - nroots);
            *nerr_out = 0;
            return 1;
        }
    }
    memset(lam, 0, sizeof lam);
    memset(prev, 0, sizeof prev);
    lam[0] = 1;
    prev[0] = 1;
    for (k = 0; k < nroots; k++) {
        uint8_t d = synd[k], coef;
        int i, slen, newlen;
        for (i = 1; i <= L; i++)
            if (i < lam_len) d ^= gmul(lam[i], synd[k - i]);
        if (d == 0) { m++; continue; }
        coef = gdiv(d, bcoef);
        slen = m + prev_len;
        newlen = lam_len > slen ? lam_len : slen;
        if (newlen > 16) return 0;
        memset(tmp, 0, sizeof tmp);
        memcpy(tmp, lam, (size_t)lam_len);
        for (i = 0; i < prev_len; i++) tmp[m + i] ^= gmul(coef, prev[i]);
        if (2 * L <= k) {
            memcpy(prev, lam, sizeof lam);
            prev_len = lam_len;
            L = k + 1 - L;
            bcoef = d;
            m = 1;
        } else m++;
        memcpy(lam, tmp, 16);
        lam_len = newlen;
    }
    while (lam_len > 1 && lam[lam_len - 1] == 0) lam_len--;
    nerr = lam_len - 1;
    if (nerr == 0 || nerr > nroots / 2) return 0;
    for (j = 0; j < (int)n; j++) { /* Chien search over the shortened block */
        long deg = (long)n - 1 - j;
        uint8_t xinv = apow(-deg), y = 0;
        int i;
        for (i = lam_len - 1; i >= 0; i--) y = (uint8_t)(gmul(y, xinv) ^ lam[i]);
        if (y == 0) {
            if (npos >= 16) return 0;
            pos[npos++] = j;
        }
    }
    if (npos != nerr) return 0;
    memset(omega, 0, sizeof omega);
    for (j = 0; j < nroots; j++)
        for (k = 0; k < lam_len; k++)
            if (j + k < nroots) omega[j + k] ^= gmul(synd[j], lam[k]);
    for (j = 0; j < npos; j++) { /* Forney */
        long deg = (long)n - 1 - pos[j];
        uint8_t xinv = apow(-deg), om = 0, dl = 0, mag;
        int i;
        for (i = nroots - 1; i >= 0; i--) om = (uint8_t)(gmul(om, xinv) ^ omega[i]);
        for (i = 1; i < lam_len; i += 2) dl ^= gmul(lam[i], apow(-deg * (i - 1)));
        if (dl == 0) return 0;
        mag = gmul(apow(deg * (1 - FCR)), gdiv(om, dl));
        cw[pos[j]] ^= mag;
    }
    for (j = 0; j < nroots; j++)
        if (poly_eval_hi(cw, n, apow(FCR + j))) return 0;
    memcpy(out, cw, n - nroots);
    *nerr_out = nerr;
    return 1;
}

static int uat_decode_frame(const uint8_t *msg, size_t len, sbd_uat *r) {
    const uint8_t *body;
    size_t blen;
    uint8_t data[48];
    int nerr;
    if (len < 4 || msg[0] != 0x24) return 0;
    if (msg[1] == 0xFF && msg[2] == 0xFF && msg[3] == 0xFF) { body = msg + 4; blen = len - 4; }
    else { body = msg + 1; blen = len - 1; }
    if (blen != 48 && blen != 30) return 0;
    if (!sb_uat_correct(body, blen, data, &nerr)) return 0;
    if (!sbd_uat_mdb(data, blen == 48 ? 34 : 18, r)) return 0;
    r->fec_errors = nerr;
    return 1;
}

/* ------------------------------------------------------------------ bridge */

void sb_config_defaults(sb_config *c) {
    memset(c, 0, sizeof *c);
    c->sentry_ip = 0xC0A80401u;
    c->ownship_icao = -1;
    c->stale_s = 15.0;
}

size_t sb_bridge_size(void) { return sizeof(sb_bridge); }

void sb_init(sb_bridge *b, const sb_config *cfg) {
    memset(b, 0, sizeof *b);
    b->cfg = *cfg;
    b->fresh = 1;
    b->enable_last = -1e9;
    if (!crc_ready) crc_init();
    if (!gf_ready) gf_init();
}

static sb_target *find_target(sb_bridge *b, uint32_t icao, int addr_type) {
    int i;
    for (i = 0; i < SB_MAX_TARGETS; i++)
        if (b->targets[i].used && b->targets[i].icao == icao &&
            b->targets[i].addr_type == addr_type) return &b->targets[i];
    return NULL;
}

static sb_target *new_target(sb_bridge *b, uint32_t icao, int addr_type) {
    int i, victim = -1;
    sb_target *t;
    for (i = 0; i < SB_MAX_TARGETS; i++)
        if (!b->targets[i].used) { victim = i; break; }
    if (victim < 0) { /* full: evict the target heard least recently */
        victim = 0;
        for (i = 1; i < SB_MAX_TARGETS; i++)
            if (b->targets[i].last_seen < b->targets[victim].last_seen) victim = i;
    }
    t = &b->targets[victim];
    memset(t, 0, sizeof *t);
    t->used = 1;
    t->icao = icao;
    t->addr_type = (uint8_t)addr_type;
    t->nic = 8;
    t->nacp = 0; /* unknown until a decoded mode-status message supplies it */
    t->airborne = 1;
    return t;
}

static int ref_position(sb_bridge *b, double now, double *lat, double *lon) {
    if (b->has_own && now - b->own_t < 10) { *lat = b->own_lat; *lon = b->own_lon; return 1; }
    if (b->cfg.has_ref) { *lat = b->cfg.ref_lat; *lon = b->cfg.ref_lon; return 1; }
    return 0;
}

static int accept_pos(sb_target *t, double lat, double lon, double now, int has_ref, double rlat, double rlon,
                      double limit_nm) {
    if (!(lat >= -90 && lat <= 90) || !(lon >= -180 && lon <= 180)) return 0;
    if (has_ref && dist_nm(rlat, rlon, lat, lon) > limit_nm) return 0;
    /* Speed gate, with slack for CPR rounding and arrival jitter between closely spaced messages */
    if (t->has_pos && now >= t->pos_t && now - t->pos_t < 30 &&
        dist_nm(t->lat, t->lon, lat, lon) > MAX_TARGET_KT * (now - t->pos_t) / 3600.0 + JUMP_SLACK_NM)
        return 0;
    return 1;
}

static void set_pos(sb_bridge *b, sb_target *t, double lat, double lon, int tc, double now) {
    t->has_pos = 1;
    t->lost = 0;
    t->lat = lat;
    t->lon = lon;
    t->pos_t = now;
    t->nic = (uint8_t)nic_for_tc(tc);
    b->stats.pos_decoded++;
}

static int is_air_tc(int tc) { return (tc >= 9 && tc <= 18) || (tc >= 20 && tc <= 22); }

/* returns 0, or -1 when the reference rejects the CPR pair (counted as modes_other by the caller) */
static int do_airborne(sb_bridge *b, sb_target *t, const uint8_t *m, int tc, double now) {
    double lat = 0, lon = 0, rlat = 0, rlon = 0;
    int32_t alt;
    t->airborne = 1;
    if (tc < 19) { /* 20-22 carry GNSS height, which a traffic report has no field for */
        t->has_alt = (uint8_t)sbd_altitude(m, tc, &alt);
        if (t->has_alt) { t->alt_ft = alt; t->alt_t = now; }
    }
    if (sbd_cpr_odd(m)) { memcpy(t->odd, m, 14); t->odd_t = now; t->has_odd = 1; }
    else { memcpy(t->even, m, 14); t->even_t = now; t->has_even = 1; }
    /* 1) local decode against the target's own recent position */
    if (t->has_pos && now - t->pos_t < 60) {
        sbd_cpr_local(m, 360.0, t->lat, t->lon, &lat, &lon);
        if (accept_pos(t, lat, lon, now, 1, t->lat, t->lon, MAX_LOCAL_REF_NM)) { set_pos(b, t, lat, lon, tc, now); return 0; }
        /* Lost track: stop reporting, but keep the last accepted position as the speed-check anchor
           so neither this jump nor a repeat of it can return through the decodes below. */
        t->lost = 1;
    }
    /* 2) global decode from an even/odd pair */
    if (t->has_even && t->has_odd && fabs(t->even_t - t->odd_t) <= GLOBAL_PAIR_WINDOW_S) {
        int tc0 = sbd_typecode(t->even), tc1 = sbd_typecode(t->odd);
        int ok = (tc0 >= 9 && tc0 <= 18 && tc1 >= 9 && tc1 <= 18) || (tc0 >= 20 && tc0 <= 22 && tc1 >= 20 && tc1 <= 22);
        if (!ok) return -1;
        if (sbd_cpr_global(t->even, t->odd, t->even_t, t->odd_t, &lat, &lon)) {
            /* 170 NM is the local-CPR ambiguity radius; a global fix only has to be in radio range. */
            int hr = ref_position(b, now, &rlat, &rlon);
            if (accept_pos(t, lat, lon, now, hr, rlat, rlon, MAX_GLOBAL_REF_NM)) { set_pos(b, t, lat, lon, tc, now); return 0; }
            b->stats.pos_rejected++;
            return 0;
        }
    }
    /* 3) local decode against ownship or the fixed reference */
    if (ref_position(b, now, &rlat, &rlon)) {
        sbd_cpr_local(m, 360.0, rlat, rlon, &lat, &lon);
        if (accept_pos(t, lat, lon, now, 1, rlat, rlon, MAX_LOCAL_REF_NM)) { set_pos(b, t, lat, lon, tc, now); return 0; }
        b->stats.pos_rejected++;
    }
    return 0;
}

static void do_surface(sb_bridge *b, sb_target *t, const uint8_t *m, int tc, double now) {
    double rlat = 0, rlon = 0, lat = 0, lon = 0;
    sbd_velocity v;
    t->airborne = 0;
    if (t->has_pos && now - t->pos_t < 60) { rlat = t->lat; rlon = t->lon; }
    else if (!ref_position(b, now, &rlat, &rlon)) return;
    sbd_cpr_local(m, 90.0, rlat, rlon, &lat, &lon);
    if (accept_pos(t, lat, lon, now, 0, 0, 0, 0) && dist_nm(rlat, rlon, lat, lon) < 40)
        set_pos(b, t, lat, lon, tc, now);
    sbd_surface_velocity(m, &v);
    if (v.has_spd) { t->has_gs = 1; t->gs_kt = v.spd; }
    if (v.has_trk) { t->has_trk = 1; t->track_deg = v.trk; t->track_type = 1; }
    t->has_vs = 1;
    t->vs_fpm = 0;
}

static void raw_modes(sb_bridge *b, const uint8_t *msg, size_t len, double now) {
    const uint8_t *body;
    size_t blen;
    uint8_t m[14];
    int df, tc, addr_type = 0;
    uint32_t icao;
    sb_target *t;
    if (len >= 4 && msg[1] == 0xFF && msg[2] == 0xFF && msg[3] == 0xFF) { body = msg + 4; blen = len - 4; }
    else { body = msg + 1; blen = len - 1; }
    if (blen < 11) { b->stats.modes_other++; return; }
    df = body[0] >> 3;
    if (df != 17 && df != 18) { b->stats.modes_other++; return; }
    if (df == 18) {
        int cf = body[0] & 7;
        if (cf == 3 || cf == 4 || cf == 7) { b->stats.modes_other++; return; }
        addr_type = (cf == 1) ? 1 : (cf == 2) ? 2 : (cf == 5) ? 3 : 0;
    }
    icao = ((uint32_t)body[1] << 16) | ((uint32_t)body[2] << 8) | body[3];
    if ((addr_type == 0 || addr_type == 2) && b->cfg.ownship_icao >= 0 &&
        icao == (uint32_t)b->cfg.ownship_icao) return;
    b->stats.modes_msgs++;
    memset(m, 0, sizeof m);
    memcpy(m, body, blen < 14 ? blen : 14);
    t = find_target(b, icao, addr_type);
    if (!t) t = new_target(b, icao, addr_type);
    t->last_seen = now;
    t->msgs++;
    tc = body[4] >> 3;
    if (tc >= 1 && tc <= 4) {
        sbd_callsign(m, t->callsign);
        t->emitter = (uint8_t)emitter_for(tc, body[4] & 7);
    } else if (tc >= 5 && tc <= 8) {
        do_surface(b, t, m, tc, now);
    } else if (is_air_tc(tc)) {
        if (do_airborne(b, t, m, tc, now) < 0) {
            b->stats.modes_other++;
            logf_(b, "decode error %06X tc=%d: Incorrect or inconsistent message types", icao, tc);
        }
    } else if (tc == 19) {
        sbd_velocity v;
        sbd_airborne_velocity(m, &v);
        if (v.has_spd) { t->has_gs = 1; t->gs_kt = v.spd; }
        if (v.has_trk) { t->has_trk = 1; t->track_deg = v.trk; t->track_type = (uint8_t)(v.gs_type ? 1 : 2); }
        t->has_vs = (uint8_t)v.has_vs;
        t->vs_fpm = v.vs;
    } else if (tc == 31 && ME(m, 5, 3) <= 1 && ME(m, 40, 3) >= 1) {
        /* Aircraft operational status, DO-260A/B (version 1 or 2): NACp in ME bits 45-48.
           Version 0 has no NACp field, so those targets stay at 0 (unknown). */
        t->nacp = (uint8_t)ME(m, 44, 4);
    }
}

static void raw_uat(sb_bridge *b, const uint8_t *msg, size_t len, double now) {
    sbd_uat r;
    int addr_type;
    sb_target *t;
    static const int qmap[8] = {0, 1, 2, 3, 4, -1, 0, -1};
    if (!uat_decode_frame(msg, len, &r)) { b->stats.uat_bad++; return; }
    b->stats.uat_msgs++;
    if (r.fec_errors) b->stats.uat_fixed++;
    addr_type = qmap[r.qualifier];
    if (addr_type < 0 || !r.has_pos) { b->stats.uat_skipped++; return; }
    if ((r.qualifier == 0 || r.qualifier == 2 || r.qualifier == 6) && b->cfg.ownship_icao >= 0 &&
        r.address == (uint32_t)b->cfg.ownship_icao)
        return;
    t = find_target(b, r.address, addr_type);
    if (!t) t = new_target(b, r.address, addr_type);
    t->last_seen = now;
    t->msgs++;
    t->uat = 1;
    if (r.has_ms) {
        t->emitter = (uint8_t)r.emitter;
        t->nacp = (uint8_t)r.nacp;
        t->emergency = (uint8_t)r.emergency;
        if (r.callsign_is_flight_id && r.callsign[0]) { /* the field alternates with the squawk */
            memcpy(t->callsign, r.callsign, sizeof t->callsign);
        }
    }
    if (r.has_alt && !r.alt_geo) { t->has_alt = 1; t->alt_ft = r.alt_ft; t->alt_t = now; }
    else if (r.has_alt2 && r.alt_geo) { t->has_alt = 1; t->alt_ft = r.alt2_ft; t->alt_t = now; }
    t->airborne = (uint8_t)(r.airground != 2);
    t->has_gs = (uint8_t)r.has_gs;
    t->gs_kt = r.gs_kt;
    if (r.has_trk) { t->has_trk = 1; t->track_deg = r.track_deg; t->track_type = (uint8_t)r.track_type; }
    if (t->airborne) { t->has_vs = (uint8_t)r.has_vs; t->vs_fpm = r.vs_fpm; }
    else { t->has_vs = 1; t->vs_fpm = 0; }
    if (accept_pos(t, r.lat, r.lon, now, 0, 0, 0, 0)) {
        t->has_pos = 1;
        t->lost = 0;
        t->lat = r.lat;
        t->lon = r.lon;
        t->pos_t = now;
        t->nic = (uint8_t)r.nic;
        b->stats.pos_decoded++;
    } else
        b->stats.pos_rejected++;
}

static void set_mode(sb_bridge *b, sb_mode mode) {
    sb_mode prev;
    if (b->fresh) {
        b->fresh = 0;
        b->encrypted_after_gap = mode == SB_MODE_ENCRYPTED;
    } else if (mode == SB_MODE_ENCRYPTED && b->mode != SB_MODE_ENCRYPTED)
        b->encrypted_after_gap = 0; /* switched mid-stream: deliberate */
    if (mode == SB_MODE_PUBLIC) b->enable_tries = 0;
    if (mode == b->mode) return;
    prev = b->mode;
    b->mode = mode;
    if (mode == SB_MODE_ENCRYPTED)
        logf_(b, "WARNING: Sentry %s ForeFlight (encrypted) mode. No traffic can be bridged. "
                 "Turn Compatibility Mode back on (sentry-ctl enable).",
              b->encrypted_after_gap ? "came up in" : "switched to");
    else if (prev == SB_MODE_ENCRYPTED)
        logf_(b, "Sentry is back in compatibility (public) mode");
}

typedef struct {
    sb_emit_fn emit;
    void *ctx;
    int n;
} emit_state;

int sb_feed(sb_bridge *b, const uint8_t *d, size_t n, uint32_t src_ip, double now, sb_emit_fn emit, void *ctx) {
    size_t i = 0;
    int relayed = 0;
    uint8_t raw[1024], out[2 * 1024 + 4];
    if (b->cfg.sentry_ip && src_ip != b->cfg.sentry_ip) return 0;
    b->stats.datagrams++;
    if (!b->has_last_rx || now - b->last_rx > STREAM_GAP_S) {
        b->fresh = 1;
        b->stream_starts++;
    }
    b->has_last_rx = 1;
    b->last_rx = now;
    while (i <= n) {
        /* one chunk between 0x7E flags (Python: datagram.split(b"\x7e")) */
        size_t j = i, rl = 0;
        int esc = 0, overflow = 0;
        while (j < n && d[j] != 0x7E) {
            uint8_t c = d[j++];
            if (esc) { c ^= 0x20; esc = 0; }
            else if (c == 0x7D) { esc = 1; continue; }
            if (rl < sizeof raw) raw[rl++] = c;
            else overflow = 1;
        }
        i = j + 1;
        if (overflow) { b->stats.crc_bad++; continue; } /* longer than any GDL90 frame: bad, as in bridge.py */
        if (rl < 3) continue;
        {
            size_t ml = rl - 2;
            uint16_t rx = (uint16_t)(raw[rl - 2] | (raw[rl - 1] << 8));
            uint8_t mid = raw[0];
            if (sb_crc16(raw, ml) != rx) { b->stats.crc_bad++; continue; }
            b->stats.frames[mid]++;
            if (mid == 0x00 && ml >= 7) {
                b->gps_valid = (raw[1] & 0x80) != 0;
                set_mode(b, SB_MODE_PUBLIC);
            } else if (mid == 0x26) {
                set_mode(b, SB_MODE_ENCRYPTED);
            } else if (mid == 0x0A && ml >= 28) {
                own_report r;
                decode_ownship(raw, &r);
                if (r.has_pos && !(fabs(r.lat) < 1e-6 && fabs(r.lon) < 1e-6)) {
                    b->has_own = 1;
                    b->own_lat = r.lat;
                    b->own_lon = r.lon;
                    b->own_t = now;
                    b->has_own_alt = r.has_alt;
                    b->own_alt = r.alt_ft;
                }
            } else if (mid == 0x21) {
                raw_modes(b, raw, ml, now);
            } else if (mid == 0x24) {
                raw_uat(b, raw, ml, now);
            }
            if (b->cfg.relay && mid != 0x21 && mid != 0x24 && mid != 0x26 && mid != 0x25 && mid != 0x28) {
                size_t fl = sb_frame(raw, ml, out, sizeof out);
                if (fl && emit) emit(ctx, out, fl);
                relayed++;
            }
        }
    }
    return relayed;
}

static void ghost_check(sb_bridge *b, sb_target *t) {
    int close;
    if ((t->addr_type != 0 && t->addr_type != 2) || b->cfg.ownship_icao >= 0 ||
        !b->has_own || !t->has_alt || !b->has_own_alt) return;
    if (t->ghost_reported) return;
    close = dist_nm(b->own_lat, b->own_lon, t->lat, t->lon) < 0.2 && abs(t->alt_ft - b->own_alt) < 300;
    t->ghost_hits = close ? (uint16_t)(t->ghost_hits + 1) : 0;
    if (t->ghost_hits >= 10) {
        t->ghost_reported = 1;
        logf_(b, "HINT: target %06X has tracked ownship for 10 s. If that is this aircraft, "
                 "set it as the ownship ICAO (--ownship-icao %06X)", t->icao, t->icao);
    }
}

int sb_tick(sb_bridge *b, double now, sb_emit_fn emit, void *ctx) {
    int i, n = 0;
    uint8_t out[SB_MAX_FRAME];
    for (i = 0; i < SB_MAX_TARGETS; i++)
        if (b->targets[i].used && now - b->targets[i].last_seen > DROP_AFTER_S) b->targets[i].used = 0;
    for (i = 0; i < SB_MAX_TARGETS; i++) {
        sb_target *t = &b->targets[i];
        traffic r;
        size_t fl;
        if (t->has_alt && now - t->alt_t > b->cfg.stale_s) t->has_alt = 0;
        if (!t->used || !t->has_pos || t->lost || now - t->pos_t > b->cfg.stale_s) continue;
        ghost_check(b, t);
        memset(&r, 0, sizeof r);
        r.address = t->icao;
        r.lat = t->lat;
        r.lon = t->lon;
        r.has_alt = t->has_alt;
        r.alt_ft = t->alt_ft;
        r.airborne = t->airborne;
        r.nic = t->nic;
        r.nacp = t->nacp;
        r.has_gs = t->has_gs;
        r.gs_kt = t->gs_kt;
        r.has_vs = t->has_vs;
        r.vs_fpm = t->vs_fpm;
        r.has_trk = t->has_trk;
        r.track_deg = t->track_deg;
        r.track_type = t->has_trk ? t->track_type : 0;
        r.emitter = t->emitter;
        r.callsign = t->callsign;
        r.addr_type = t->addr_type;
        r.emergency = t->emergency;
        fl = encode_traffic(&r, out, sizeof out);
        if (fl && emit) emit(ctx, out, fl);
        n++;
    }
    b->stats.sent += (uint32_t)n;
    return n;
}

int sb_wants_enable(sb_bridge *b, double now) {
    if (b->mode == SB_MODE_ENCRYPTED && b->encrypted_after_gap && b->enable_tries < ENABLE_MAX_TRIES &&
        now - b->enable_last >= ENABLE_RETRY_S) {
        b->enable_tries++;
        b->enable_last = now;
        return 1;
    }
    return 0;
}

sb_mode sb_get_mode(const sb_bridge *b) { return b->mode; }
uint32_t sb_stream_starts(const sb_bridge *b) { return b->stream_starts; }
const sb_stats *sb_get_stats(const sb_bridge *b) { return &b->stats; }

int sb_summary(const sb_bridge *b, char *buf, size_t cap) {
    const sb_stats *s = &b->stats;
    int i, live = 0, live_uat = 0, targets = 0;
    size_t pos;
    static const char *names[] = {"?", "PUBLIC", "ENCRYPTED"};
    for (i = 0; i < SB_MAX_TARGETS; i++) {
        const sb_target *t = &b->targets[i];
        if (!t->used) continue;
        targets++;
        if (t->has_pos && !t->lost) { live++; if (t->uat) live_uat++; }
    }
    pos = (size_t)snprintf(buf, cap, "mode=%s dgrams=%u frames[", names[b->mode], (unsigned)s->datagrams);
    for (i = 0; i < 256 && pos < cap; i++)
        if (s->frames[i]) pos += (size_t)snprintf(buf + pos, cap - pos, "%s%02X:%u", buf[pos - 1] == '[' ? "" : " ", i, (unsigned)s->frames[i]);
    if (pos < cap)
        pos += (size_t)snprintf(buf + pos, cap - pos,
                                "] modes=%u other=%u uat=%u(fixed %u, bad %u) targets=%d positioned=%d (uat %d) "
                                "sent=%u rejected=%u crc_bad=%u gps=%s ref=%s",
                                (unsigned)s->modes_msgs, (unsigned)s->modes_other, (unsigned)s->uat_msgs,
                                (unsigned)s->uat_fixed, (unsigned)s->uat_bad, targets, live, live_uat,
                                (unsigned)s->sent, (unsigned)s->pos_rejected, (unsigned)s->crc_bad,
                                b->gps_valid ? "ok" : "no",
                                b->has_own ? "ownship" : (b->cfg.has_ref ? "fixed" : "none"));
    return (int)pos;
}

/* ------------------------------------------------------------------ tiny JSON helpers */

static const char *skip_ws(const char *p, const char *e) {
    while (p < e && (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')) p++;
    return p;
}

/* finds "key" followed by ':' and returns a pointer to the value, or NULL. A non-NULL result is
   always before e, so callers may read one byte; data need not be NUL-terminated. */
static const char *find_key(const char *p, const char *e, const char *key) {
    size_t kl = strlen(key);
    while (p < e) {
        const char *q = memchr(p, '"', (size_t)(e - p));
        if (!q || (size_t)(e - q) < kl + 2) return NULL;
        if (memcmp(q + 1, key, kl) == 0 && q[kl + 1] == '"') {
            const char *v = skip_ws(q + kl + 2, e);
            if (v < e && *v == ':') {
                v = skip_ws(v + 1, e);
                return v < e ? v : NULL; /* "key": with no value before the end */
            }
        }
        p = q + 1;
    }
    return NULL;
}

int sb_parse_announce(const char *data, size_t len, char *app, size_t app_cap) {
    const char *e = data + len, *p, *v;
    long port = 0;
    int digits = 0, quoted = 0;
    if (app && app_cap) app[0] = 0;
    p = skip_ws(data, e);
    if (p >= e || *p != '{') return -1;
    v = find_key(p, e, "GDL90");
    if (!v || *v != '{') return -1;
    v = find_key(v, e, "port");
    if (!v) return -1;
    if (*v == '"') { quoted = 1; v++; }
    while (v < e && *v >= '0' && *v <= '9' && digits < 6) { port = port * 10 + (*v - '0'); v++; digits++; }
    if (!digits || (quoted && (v >= e || *v != '"'))) return -1;
    if (port <= 0 || port >= 65536) return -1;
    if (app && app_cap) {
        const char *a = find_key(p, e, "App");
        if (a && *a == '"') {
            size_t n = 0;
            a++;
            /* Display label only: keep printable ASCII so logs and JNI strings stay valid text */
            for (; a < e && *a != '"' && n + 1 < app_cap; a++)
                if (*a >= 0x20 && *a <= 0x7E && *a != '\\') app[n++] = *a;
            app[n] = 0;
        } else
            snprintf(app, app_cap, "?");
    }
    return (int)port;
}

int sb_power_savings_on(const char *json, size_t len) {
    const char *e = json + len;
    const char *p = find_key(json, e, "Power");
    const char *v;
    if (!p || *p != '{') return -1;
    v = find_key(p, e, "powerSavings");
    if (!v) return -1;
    if (e - v >= 4 && memcmp(v, "true", 4) == 0) return 1;
    if (e - v >= 5 && memcmp(v, "false", 5) == 0) return 0;
    return -1;
}

int sb_parse_ipv4(const char *s, uint32_t *out) {
    uint32_t v = 0;
    int part, i;
    for (i = 0; i < 4; i++) {
        int digits = 0;
        part = 0;
        while (*s >= '0' && *s <= '9' && digits < 3) { part = part * 10 + (*s - '0'); s++; digits++; }
        if (!digits || part > 255) return 0;
        v = (v << 8) | (uint32_t)part;
        if (i < 3) {
            if (*s != '.') return 0;
            s++;
        }
    }
    if (*s) return 0;
    *out = v;
    return 1;
}
