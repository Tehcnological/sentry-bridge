/* SPDX-License-Identifier: GPL-3.0-only */
/*
 * 1090ES and UAT field decoders.
 * Written from docs/Decoder-Spec.md only (clean room); see that file.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

typedef struct {
    int has_spd;  double spd;   /* knots */
    int has_trk;  double trk;   /* degrees */
    int has_vs;   int32_t vs;   /* feet per minute */
    int gs_type;                /* 1 = ground speed / true track, 0 = airspeed / heading */
} sbd_velocity;

typedef struct {
    int payload_type, qualifier, fec_errors;  /* fec_errors: left 0, the caller sets it */
    uint32_t address;
    int has_pos;  double lat, lon;
    int has_alt, alt_geo, has_alt2;  int32_t alt_ft, alt2_ft;
    int nic, airground;
    int has_gs, has_trk, has_vs, track_type;
    double gs_kt, track_deg;  int32_t vs_fpm;
    int has_ms, emitter, emergency, nacp, callsign_is_flight_id;
    char callsign[9];
} sbd_uat;

#define SBD_DEG (180.0 / M_PI)

/* Bits first..last (inclusive, bit 0 = MSB of byte 0) as an unsigned integer. */
static uint32_t sbd_field(const uint8_t *p, int first, int last)
{
    uint32_t v = 0;
    int i;
    for (i = first; i <= last; i++)
        v = (v << 1) | (uint32_t)((p[i >> 3] >> (7 - (i & 7))) & 1u);
    return v;
}

/* ME field of a 1090ES message: ME bit k is message bit 32 + k. */
static uint32_t sbd_me(const uint8_t *msg, int first, int last)
{
    return sbd_field(msg, 32 + first, 32 + last);
}

static long sbd_fmod_l(long a, long n)
{
    long r = a % n;
    return r < 0 ? r + n : r;
}

static double sbd_fmod360(double x)
{
    double r = fmod(x, 360.0);
    if (r != 0 && r < 0)
        r += 360.0;
    return r;
}

static int sbd_nl(double lat)
{
    double a, b;
    if (fabs(lat) <= 1e-08)
        return 59;
    if (fabs(fabs(lat) - 87) <= 1e-08 + 1e-05 * 87)
        return 2;
    if (lat > 87 || lat < -87)
        return 1;
    a = 1 - cos(M_PI / (2 * 15));
    b = pow(cos(M_PI / 180 * fabs(lat)), 2);
    return (int)floor(2 * M_PI / acos(1 - a / b));
}

static int sbd_typecode(const uint8_t *msg)
{
    return (int)sbd_me(msg, 0, 4);
}

static int sbd_cpr_odd(const uint8_t *msg)
{
    return (int)sbd_me(msg, 21, 21);
}

static void sbd_callsign(const uint8_t *msg, char out[9])
{
    int i, n = 0;
    for (i = 0; i < 8; i++) {
        unsigned c = (unsigned)sbd_me(msg, 8 + 6 * i, 13 + 6 * i);
        if (c >= 1 && c <= 26)
            out[n++] = (char)('A' + c - 1);
        else if (c >= 48 && c <= 57)
            out[n++] = (char)('0' + c - 48);
        /* spaces and invalid values are left out */
    }
    out[n] = '\0';
}

static unsigned sbd_gray(unsigned g)
{
    unsigned n = g;
    while (g >>= 1)
        n ^= g;
    return n;
}

static int sbd_altitude(const uint8_t *msg, int tc, int32_t *feet)
{
    uint32_t raw = sbd_me(msg, 8, 19);
    if (tc >= 20 && tc <= 22) {
        *feet = (int32_t)(raw * 3.28084);
        return 1;
    }
    if (tc < 9 || tc > 18 || raw == 0)
        return 0;
    if (raw & 0x10u) {
        /* Q set: 11-bit count without M and Q */
        uint32_t n = ((raw >> 5) << 4) | (raw & 0xFu);
        *feet = (int32_t)n * 25 - 1000;
        return 1;
    } else {
        /* raw bit order (MSB first): C1 A1 C2 A2 C4 A4 B1 Q B2 D2 B4 D4 */
#define SBD_RB(i) ((raw >> (11 - (i))) & 1u)
        unsigned g500 = (SBD_RB(9) << 7) | (SBD_RB(11) << 6) | (SBD_RB(1) << 5) |
                        (SBD_RB(3) << 4) | (SBD_RB(5) << 3) | (SBD_RB(6) << 2) |
                        (SBD_RB(8) << 1) | SBD_RB(10);
        unsigned g100 = (SBD_RB(0) << 2) | (SBD_RB(2) << 1) | SBD_RB(4);
#undef SBD_RB
        int n500 = (int)sbd_gray(g500);
        int n100 = (int)sbd_gray(g100);
        if (n100 == 0 || n100 == 5 || n100 == 6)
            return 0;
        if (n100 == 7)
            n100 = 5;
        if (n500 & 1)
            n100 = 6 - n100;
        *feet = n500 * 500 + n100 * 100 - 1300;
        return 1;
    }
}

static double sbd_yz(const uint8_t *msg) { return sbd_me(msg, 22, 38) / 131072.0; }
static double sbd_xz(const uint8_t *msg) { return sbd_me(msg, 39, 55) / 131072.0; }

static int sbd_cpr_global(const uint8_t *even, const uint8_t *odd, double t_even, double t_odd,
                          double *lat, double *lon)
{
    double yz0 = sbd_yz(even), xz0 = sbd_xz(even);
    double yz1 = sbd_yz(odd), xz1 = sbd_xz(odd);
    double lat0, lat1, la, xz, lo;
    long j, m;
    int nl, ni;

    j = (long)floor(59 * yz0 - 60 * yz1 + 0.5);
    lat0 = (360.0 / 60) * (sbd_fmod_l(j, 60) + yz0);
    lat1 = (360.0 / 59) * (sbd_fmod_l(j, 59) + yz1);
    if (lat0 >= 270) lat0 -= 360;
    if (lat1 >= 270) lat1 -= 360;
    if (sbd_nl(lat0) != sbd_nl(lat1))
        return 0;
    if (t_even > t_odd) {
        la = lat0;
        nl = sbd_nl(la);
        ni = nl > 1 ? nl : 1;
        xz = xz0;
    } else {
        la = lat1;
        nl = sbd_nl(la);
        ni = nl - 1 > 1 ? nl - 1 : 1;
        xz = xz1;
    }
    m = (long)floor(xz0 * (nl - 1) - xz1 * nl + 0.5);
    lo = (360.0 / ni) * (sbd_fmod_l(m, ni) + xz);
    if (lo > 180) lo -= 360;
    *lat = la;
    *lon = lo;
    return 1;
}

static void sbd_cpr_local(const uint8_t *msg, double span, double lat_ref, double lon_ref,
                          double *lat, double *lon)
{
    int f = sbd_cpr_odd(msg);
    double yz = sbd_yz(msg), xz = sbd_xz(msg);
    double dlat = f ? span / 59 : span / 60;
    long j = (long)floor(0.5 + lat_ref / dlat - yz);
    double la = dlat * (j + yz);
    int ni = sbd_nl(la) - f;
    double dlon = ni > 0 ? span / ni : span;
    long m = (long)floor(0.5 + lon_ref / dlon - xz);
    double lo = dlon * (m + xz);
    if (lo > 180) lo -= 360;
    else if (lo < -180) lo += 360;
    *lat = la;
    *lon = lo;
}

static void sbd_airborne_velocity(const uint8_t *msg, sbd_velocity *v)
{
    int sub = (int)sbd_me(msg, 5, 7);
    uint32_t vr;
    memset(v, 0, sizeof *v);
    if (sub == 1 || sub == 2) {
        long we = (long)sbd_me(msg, 14, 23), sn = (long)sbd_me(msg, 25, 34);
        v->gs_type = 1;
        if (we != 0 && sn != 0) {
            double t;
            we -= 1;
            sn -= 1;
            if (sub == 2) { we *= 4; sn *= 4; }
            if (sbd_me(msg, 13, 13)) we = -we;
            if (sbd_me(msg, 24, 24)) sn = -sn;
            v->spd = (double)(long)sqrt((double)(sn * sn + we * we));
            v->has_spd = 1;
            t = atan2((double)we, (double)sn) * SBD_DEG;
            v->trk = t >= 0 ? t : t + 360;
            v->has_trk = 1;
        }
    } else {
        uint32_t as = sbd_me(msg, 25, 34);
        if (sbd_me(msg, 13, 13)) {
            v->trk = sbd_me(msg, 14, 23) / 1024.0 * 360.0;
            v->has_trk = 1;
        }
        if (as != 0) {
            long s = (long)as - 1;
            if (sub == 4) s *= 4;
            v->spd = (double)s;
            v->has_spd = 1;
        }
    }
    vr = sbd_me(msg, 37, 45);
    if (vr != 0) {
        v->vs = (sbd_me(msg, 36, 36) ? -1 : 1) * ((int32_t)vr - 1) * 64;
        v->has_vs = 1;
    }
}

static void sbd_surface_velocity(const uint8_t *msg, sbd_velocity *v)
{
    static const int ranges[6][2] = { {2, 8}, {9, 12}, {13, 38}, {39, 93}, {94, 108}, {109, 123} };
    static const double base[6] = { 0.125, 1, 2, 15, 70, 100 };
    static const double step[6] = { 0.125, 0.25, 0.5, 1, 2, 5 };
    int mov = (int)sbd_me(msg, 5, 11);
    int i;
    memset(v, 0, sizeof *v);
    if (sbd_me(msg, 12, 12)) {
        v->trk = sbd_me(msg, 13, 19) * 360.0 / 128;
        v->has_trk = 1;
    }
    if (mov == 0 || mov > 124)
        return;
    v->has_spd = 1;
    if (mov == 1) { v->spd = 0.0; return; }
    if (mov == 124) { v->spd = 175.0; return; }
    for (i = 0; i < 6; i++) {
        if (mov >= ranges[i][0] && mov <= ranges[i][1]) {
            v->spd = base[i] + (mov - ranges[i][0]) * step[i];
            return;
        }
    }
}

static int sbd_uat_alt(uint32_t raw, int32_t *ft)
{
    if (raw == 0)
        return 0;
    *ft = ((int32_t)raw - 1) * 25 - 1000;
    return 1;
}

static int sbd_uat_mdb(const uint8_t *mdb, size_t len, sbd_uat *r)
{
    static const char b40[] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ  ..";
    uint32_t rlat, rlon;
    memset(r, 0, sizeof *r);
    if (len != 18 && len != 34)
        return 0;
    r->payload_type = (int)sbd_field(mdb, 0, 4);
    if (r->payload_type > 10 || (len == 18 && r->payload_type != 0))
        return 0;
    r->qualifier = (int)sbd_field(mdb, 5, 7);
    r->address = sbd_field(mdb, 8, 31);

    rlat = sbd_field(mdb, 32, 54);
    rlon = sbd_field(mdb, 55, 78);
    if (rlat != 0 || rlon != 0) {
        r->has_pos = 1;
        r->lat = rlat * 360.0 / 16777216.0;
        if (r->lat > 90.0) r->lat -= 180;
        r->lon = rlon * 360.0 / 16777216.0;
        if (r->lon > 180.0) r->lon -= 360;
    }
    r->alt_geo = (int)sbd_field(mdb, 79, 79);
    r->has_alt = sbd_uat_alt(sbd_field(mdb, 80, 91), &r->alt_ft);
    r->nic = (int)sbd_field(mdb, 92, 95);
    r->airground = (int)sbd_field(mdb, 96, 97);

    if (r->airground <= 1) {
        uint32_t nsf = sbd_field(mdb, 99, 109), ewf = sbd_field(mdb, 110, 120);
        uint32_t mns = nsf & 0x3FFu, mew = ewf & 0x3FFu, vmag;
        if (mns != 0 && mew != 0) {
            long mult = r->airground == 1 ? 4 : 1;
            long ns = ((long)mns - 1) * mult * ((nsf & 0x400u) ? -1 : 1);
            long ew = ((long)mew - 1) * mult * ((ewf & 0x400u) ? -1 : 1);
            r->gs_kt = hypot((double)ns, (double)ew);
            r->has_gs = 1;
            if (ns != 0 || ew != 0) {
                r->track_deg = sbd_fmod360(atan2((double)ew, (double)ns) * SBD_DEG);
                r->has_trk = 1;
                r->track_type = 1;
            }
        }
        vmag = sbd_field(mdb, 123, 131);
        if (vmag != 0) {
            r->vs_fpm = ((int32_t)vmag - 1) * 64 * (sbd_field(mdb, 122, 122) ? -1 : 1);
            r->has_vs = 1;
        }
    } else if (r->airground == 2) {
        uint32_t gs = sbd_field(mdb, 100, 109);
        uint32_t tt = sbd_field(mdb, 110, 111);
        if (gs != 0) {
            r->gs_kt = (double)(gs - 1);
            r->has_gs = 1;
        }
        if (tt != 0) {
            r->track_deg = sbd_field(mdb, 112, 120) * 360.0 / 512.0;
            r->has_trk = 1;
            r->track_type = (int)tt;
        }
    }

    if (len == 34) {
        int pt = r->payload_type;
        if (pt == 1 || pt == 3) {
            uint32_t w[3];
            int i, n = 0;
            w[0] = sbd_field(mdb, 136, 151);
            w[1] = sbd_field(mdb, 152, 167);
            w[2] = sbd_field(mdb, 168, 183);
            r->has_ms = 1;
            r->emitter = (int)((w[0] / 1600) % 40);
            r->callsign[n++] = b40[(w[0] / 40) % 40];
            r->callsign[n++] = b40[w[0] % 40];
            for (i = 1; i < 3; i++) {
                r->callsign[n++] = b40[(w[i] / 1600) % 40];
                r->callsign[n++] = b40[(w[i] / 40) % 40];
                r->callsign[n++] = b40[w[i] % 40];
            }
            while (n > 0 && r->callsign[n - 1] == ' ')
                n--;
            r->callsign[n] = '\0';
            r->emergency = (int)sbd_field(mdb, 184, 186);
            r->nacp = (int)sbd_field(mdb, 200, 203);
            r->callsign_is_flight_id = (int)sbd_field(mdb, 214, 214);
        }
        if (pt == 1 || pt == 2 || pt == 5 || pt == 6)
            r->has_alt2 = sbd_uat_alt(sbd_field(mdb, 232, 243), &r->alt2_ft);
    }
    return 1;
}

#undef SBD_DEG
