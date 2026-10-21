/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (c) 2026 Sentry Bridge contributors.
 * See NOTICE.txt.
 */
/*
 * sbcore: portable core of the Sentry bridge.
 *
 * Turns the datagrams a uAvionix Sentry sends in Compatibility Mode (publicGDL90) into
 * standard GDL90: 0x21 (raw 1090ES Mode S) and 0x24 (raw, uncorrected UAT) become 0x14
 * Traffic Reports. Also tracks whether the Sentry is in public or encrypted mode.
 *
 * Pure C99. No sockets, no threads, no heap allocation: the platform layer (Linux, Windows,
 * Android, ESP32) owns networking and calls sb_feed() for every datagram and sb_tick()
 * about once a second. Behaviour mirrors bridge.py, which is the reference implementation;
 * tests/test_core_parity.py checks the two produce identical output on simulated traffic.
 */
#ifndef SBCORE_H
#define SBCORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SB_VERSION "0.3.0"
#define SB_MAX_TARGETS 256
#define SB_MAX_FRAME 128 /* largest frame sb_tick() emits (a stuffed 0x14 is <= 64). Frames relayed by
                            sb_feed() are the Sentry's own and can be much larger: a FIS-B uplink is ~440 */

typedef void (*sb_emit_fn)(void *ctx, const uint8_t *frame, size_t len);
typedef void (*sb_log_fn)(void *ctx, const char *msg);

typedef struct {
    uint32_t sentry_ip;   /* IPv4 in host byte order, e.g. 0xC0A80401 for 192.168.4.1; 0 = accept any */
    int32_t ownship_icao; /* this aircraft's ICAO address, filtered from traffic; -1 = none */
    int has_ref;          /* fallback CPR reference when the Sentry has no GPS fix */
    double ref_lat, ref_lon;
    double stale_s;       /* stop sending a target this many seconds after its last position */
    int relay;            /* also return the Sentry's standard frames from sb_feed() */
    sb_log_fn log;        /* may be NULL */
    void *log_ctx;
} sb_config;

typedef struct {
    uint32_t datagrams;
    uint32_t frames[256]; /* count per GDL90 message ID */
    uint32_t crc_bad;
    uint32_t modes_msgs, modes_other;
    uint32_t pos_decoded, pos_rejected;
    uint32_t sent;
    uint32_t uat_msgs, uat_fixed, uat_bad, uat_skipped;
} sb_stats;

typedef enum { SB_MODE_UNKNOWN = 0, SB_MODE_PUBLIC = 1, SB_MODE_ENCRYPTED = 2 } sb_mode;

typedef struct {
    uint8_t used, addr_type, emitter, nic, nacp, emergency, track_type, uat, airborne;
    uint8_t has_pos, has_alt, has_gs, has_trk, has_vs, has_even, has_odd;
    /* has_pos/lat/lon/pos_t keep the last accepted position for plausibility checks. lost: a newer
       message was implausible, so nothing is reported until a fresh position passes those checks. */
    uint8_t lost;
    uint32_t icao;
    char callsign[9];
    double lat, lon, pos_t, last_seen, alt_t;
    /* alt_ft is pressure altitude only; geometric height never goes in a 0x14, so it is not kept. */
    int32_t alt_ft, vs_fpm;
    double gs_kt, track_deg;
    uint8_t even[14], odd[14];
    double even_t, odd_t;
    uint32_t msgs;
    uint16_t ghost_hits;
    uint8_t ghost_reported;
} sb_target;

typedef struct {
    sb_config cfg;
    sb_stats stats;
    sb_target targets[SB_MAX_TARGETS];
    /* ownship from the Sentry's 0x0A */
    int has_own;
    double own_lat, own_lon, own_t;
    int has_own_alt;
    int32_t own_alt;
    int gps_valid;
    /* stream mode tracking */
    sb_mode mode;
    int has_last_rx;
    double last_rx;
    int fresh;
    uint32_t stream_starts;
    int encrypted_after_gap;
    int enable_tries;
    double enable_last;
} sb_bridge;

/* Lifecycle */
void sb_config_defaults(sb_config *cfg); /* 192.168.4.1, no ownship filter, stale 15 s, no relay */
void sb_init(sb_bridge *b, const sb_config *cfg);
size_t sb_bridge_size(void);             /* sizeof(sb_bridge), for FFI callers */

/* All now arguments use seconds from the same monotonic clock, including feed, tick and
   wants_enable. Wall-clock corrections must never affect ages, CPR pairs or retries.
   Feed one UDP datagram from src_ip (host byte order). Relayed standard frames, if cfg.relay,
   go to emit. Returns the number of relayed frames. */
int sb_feed(sb_bridge *b, const uint8_t *data, size_t len, uint32_t src_ip, double now,
            sb_emit_fn emit, void *ctx);

/* Call about once a second. Emits one framed 0x14 Traffic Report per live target.
   Returns the number emitted. */
int sb_tick(sb_bridge *b, double now, sb_emit_fn emit, void *ctx);

/* True when the Sentry came up encrypted after a stream gap (power cycle or bridge start) and a
   re-enable attempt is due: at most 3 per episode, 20 s apart. Never after a mid-stream switch. */
int sb_wants_enable(sb_bridge *b, double now);

sb_mode sb_get_mode(const sb_bridge *b);
uint32_t sb_stream_starts(const sb_bridge *b); /* bumps on the first datagram and after each gap */
const sb_stats *sb_get_stats(const sb_bridge *b);
int sb_summary(const sb_bridge *b, char *buf, size_t cap); /* one-line status, like bridge.py */

/* Helpers the platform layers share */
/* Port, or -1. app gets the announced name as printable ASCII (no backslash), truncated to app_cap - 1 */
int sb_parse_announce(const char *data, size_t len, char *app, size_t app_cap);
int sb_power_savings_on(const char *json, size_t len);                          /* 1, 0, or -1 */
int sb_parse_ipv4(const char *s, uint32_t *out);                                /* 1 ok, 0 bad */
extern const char sb_enable_body[];  /* {"publicGDL90":true} */
extern const char sb_enable_path[];  /* /settings/?action=set */
extern const char sb_settings_path[];/* /settings/?action=get */

/* Lower-level pieces, exposed for tests */
uint16_t sb_crc16(const uint8_t *data, size_t len);
size_t sb_frame(const uint8_t *msg, size_t len, uint8_t *out, size_t cap);
int sb_uat_correct(const uint8_t *block, size_t len, uint8_t *out, int *nerr); /* 1 ok, 0 uncorrectable */

#ifdef __cplusplus
}
#endif
#endif
