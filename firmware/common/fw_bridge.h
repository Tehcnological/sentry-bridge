/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (c) 2026 Sentry Bridge contributors. See NOTICE.txt.
 *
 * Board-independent bridge loop for the microcontroller firmware (ESP32-C3, Pico W, Pico 2 W).
 * The board code owns WiFi, sockets and time; this file decides what to send and when to contact
 * the receiver, using the shared C core.
 */
#ifndef FW_BRIDGE_H
#define FW_BRIDGE_H

#include "sbcore.h"

#define FW_RECEIVER_PORT 4000 /* the Sentry streams Compatibility Mode GDL90 here */
#define FW_SSID_PREFIX "Sentry"

typedef enum { FW_HTTP_KEEPALIVE = 0, FW_HTTP_ENABLE = 1 } fw_http;

typedef struct {
    /* Broadcast one GDL90 frame to the receiver's network on FW_RECEIVER_PORT. */
    void (*send)(void *ctx, const uint8_t *frame, size_t len);
    /* Start one HTTP request to the receiver: GET fw_keepalive_path (keepalive), or POST
       sb_enable_body to sb_enable_path. Return 0 if busy; the loop retries. Call fw_http_done()
       when it finishes. */
    int (*http)(void *ctx, fw_http request);
    void (*log)(void *ctx, const char *line);
    void *ctx;
} fw_hooks;

typedef struct {
    sb_bridge core;
    fw_hooks hooks;
    double next_tick, last_contact, next_summary;
    int contacted, enable_pending;
} fw_bridge;

void fw_init(fw_bridge *b, const fw_hooks *hooks);
/* One datagram that arrived on FW_RECEIVER_PORT from src_ip (host byte order). */
void fw_datagram(fw_bridge *b, const uint8_t *data, size_t len, uint32_t src_ip, double now);
/* Call often (every 100 ms or so) with a monotonic time in seconds. */
void fw_poll(fw_bridge *b, double now);
/* The board finished an fw_hooks.http request; ok = HTTP 2xx. */
void fw_http_done(fw_bridge *b, fw_http request, int ok);
/* 1 if a datagram arrived in the last 3 s: for a status LED. */
int fw_receiving(const fw_bridge *b, double now);

extern const char fw_keepalive_path[]; /* "/?action=get", the receiver's status read */

#endif
