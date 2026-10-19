/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (c) 2026 Sentry Bridge contributors. See NOTICE.txt.
 */
#include "fw_bridge.h"

#include <stdio.h>
#include <string.h>

/* Same contact rhythm as sentry_ctl.keepalive_due: the Sentry stops streaming to a client that
   only receives, so read its status every 30 s while data arrives and every 5 s while silent. */
#define KEEPALIVE_S 30.0
#define SILENT_CONTACT_S 5.0
#define SILENT_AFTER_S 3.0
#define SUMMARY_S 60.0

const char fw_keepalive_path[] = "/?action=get";

static void core_log(void *ctx, const char *msg) {
    fw_bridge *b = (fw_bridge *)ctx;
    if (b->hooks.log) b->hooks.log(b->hooks.ctx, msg);
}

static void emit(void *ctx, const uint8_t *frame, size_t len) {
    fw_bridge *b = (fw_bridge *)ctx;
    b->hooks.send(b->hooks.ctx, frame, len);
}

void fw_init(fw_bridge *b, const fw_hooks *hooks) {
    sb_config cfg;
    memset(b, 0, sizeof *b);
    b->hooks = *hooks;
    sb_config_defaults(&cfg);
    /* EFBs already receive the receiver's own heartbeat, GPS and ownship frames: send traffic only. */
    cfg.relay = 0;
    cfg.log = core_log;
    cfg.log_ctx = b;
    sb_init(&b->core, &cfg);
}

void fw_datagram(fw_bridge *b, const uint8_t *data, size_t len, uint32_t src_ip, double now) {
    sb_feed(&b->core, data, len, src_ip, now, emit, b);
}

int fw_receiving(const fw_bridge *b, double now) {
    return b->core.has_last_rx && now - b->core.last_rx < SILENT_AFTER_S;
}

void fw_poll(fw_bridge *b, double now) {
    if (now >= b->next_tick) {
        b->next_tick = now + 1.0;
        sb_tick(&b->core, now, emit, b);
    }
    if (sb_wants_enable(&b->core, now)) b->enable_pending = 1;
    if (b->enable_pending) {
        if (b->hooks.http(b->hooks.ctx, FW_HTTP_ENABLE)) b->enable_pending = 0;
    } else {
        double every = fw_receiving(b, now) ? KEEPALIVE_S : SILENT_CONTACT_S;
        if ((!b->contacted || now - b->last_contact >= every) && b->hooks.http(b->hooks.ctx, FW_HTTP_KEEPALIVE)) {
            b->contacted = 1;
            b->last_contact = now;
        }
    }
    if (b->hooks.log && now >= b->next_summary) {
        static char line[1400]; /* off the stack: the Pico's default stack is 2 KB */
        b->next_summary = now + SUMMARY_S;
        sb_summary(&b->core, line, sizeof line);
        b->hooks.log(b->hooks.ctx, line);
    }
}

void fw_http_done(fw_bridge *b, fw_http request, int ok) {
    if (b->hooks.log && request == FW_HTTP_ENABLE)
        b->hooks.log(b->hooks.ctx, ok ? "Compatibility Mode requested" : "Compatibility Mode request failed");
}
