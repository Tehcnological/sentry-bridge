/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (c) 2026 Sentry Bridge contributors. See NOTICE.txt.
 *
 * Sentry Bridge for Raspberry Pi Pico W and Pico 2 W. Joins the receiver's open WiFi (name starting
 * "Sentry"), listens on UDP 4000 and broadcasts decoded traffic to the receiver's network on UDP
 * 4000. The onboard LED is on while receiver data arrives. lwIP runs in poll mode: everything here
 * happens on the one core, in the main loop.
 */
#include <stdio.h>
#include <string.h>

#include "lwip/ip4_addr.h"
#include "lwip/netif.h"
#include "lwip/pbuf.h"
#include "lwip/tcp.h"
#include "lwip/udp.h"
#include "pico/cyw43_arch.h"
#include "pico/stdlib.h"

#include "fw_bridge.h"

/* sb_feed keeps about 3 KB of frame buffers on the stack: see PICO_STACK_SIZE in CMakeLists.txt. */
_Static_assert(PICO_STACK_SIZE >= 0x2000, "the bridge needs an 8 KB stack");

#define RECEIVER_HTTP_PORT 80
#define HTTP_TIMEOUT_S 5.0

static fw_bridge bridge;  /* about 45 KB: static, not on the 8 KB stack */
static struct udp_pcb *rx, *tx;
static ip_addr_t broadcast, receiver;
static uint8_t datagram[1600];

static struct {
    struct tcp_pcb *pcb;
    fw_http request;
    int busy, finished, ok, saw_2xx;
    double deadline;
} http;

static double now_s(void) { return to_us_since_boot(get_absolute_time()) / 1e6; }

static void log_line(void *ctx, const char *line) {
    (void)ctx;
    printf("%s\n", line);
}

static void send_frame(void *ctx, const uint8_t *frame, size_t len) {
    (void)ctx;
    struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)len, PBUF_RAM);
    if (!p) return;
    memcpy(p->payload, frame, len);
    udp_sendto(tx, p, &broadcast, FW_RECEIVER_PORT);
    pbuf_free(p);
}

static void on_datagram(void *arg, struct udp_pcb *pcb, struct pbuf *p, const ip_addr_t *addr, u16_t port) {
    (void)arg; (void)pcb; (void)port;
    u16_t n = pbuf_copy_partial(p, datagram, sizeof datagram, 0);
    fw_datagram(&bridge, datagram, n, lwip_ntohl(ip4_addr_get_u32(ip_2_ip4(addr))), now_s());
    pbuf_free(p);
}

/* ------------------------------------------------- minimal HTTP/1.0 client for the receiver */

static void http_finish(int ok) {
    if (http.pcb) {
        tcp_arg(http.pcb, NULL);
        tcp_recv(http.pcb, NULL);
        tcp_err(http.pcb, NULL);
        if (tcp_close(http.pcb) != ERR_OK) tcp_abort(http.pcb);
        http.pcb = NULL;
    }
    http.ok = ok;
    http.finished = 1;
    http.busy = 0;
}

static void http_error(void *arg, err_t err) {
    (void)arg; (void)err;
    http.pcb = NULL;  /* lwIP already freed it */
    http.ok = 0;
    http.finished = 1;
    http.busy = 0;
}

static err_t http_received(void *arg, struct tcp_pcb *pcb, struct pbuf *p, err_t err) {
    (void)arg; (void)err;
    if (!p) {  /* server closed: the response is complete */
        http_finish(http.saw_2xx);
        return ERR_OK;
    }
    if (!http.saw_2xx && p->tot_len >= 12) {
        char head[13] = {0};
        pbuf_copy_partial(p, head, 12, 0);
        http.saw_2xx = strncmp(head, "HTTP/1.", 7) == 0 && head[9] == '2';
    }
    tcp_recved(pcb, p->tot_len);
    pbuf_free(p);
    return ERR_OK;
}

static err_t http_connected(void *arg, struct tcp_pcb *pcb, err_t err) {
    char request[256];
    int n;
    (void)arg;
    if (err != ERR_OK) {
        http_finish(0);
        return ERR_OK;
    }
    if (http.request == FW_HTTP_ENABLE)
        n = snprintf(request, sizeof request,
                     "POST %s HTTP/1.0\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %u\r\n\r\n%s",
                     sb_enable_path, ipaddr_ntoa(&receiver), (unsigned)strlen(sb_enable_body), sb_enable_body);
    else
        n = snprintf(request, sizeof request, "GET %s HTTP/1.0\r\nHost: %s\r\n\r\n", fw_keepalive_path, ipaddr_ntoa(&receiver));
    if (tcp_write(pcb, request, (u16_t)n, TCP_WRITE_FLAG_COPY) != ERR_OK) http_finish(0);
    else tcp_output(pcb);
    return ERR_OK;
}

static int start_http(void *ctx, fw_http request) {
    (void)ctx;
    if (http.busy || !netif_is_link_up(netif_default)) return 0;
    http.pcb = tcp_new_ip_type(IPADDR_TYPE_V4);
    if (!http.pcb) return 0;
    http.busy = 1;
    http.request = request;
    http.saw_2xx = 0;
    http.deadline = now_s() + HTTP_TIMEOUT_S;
    tcp_arg(http.pcb, NULL);
    tcp_err(http.pcb, http_error);
    tcp_recv(http.pcb, http_received);
    if (tcp_connect(http.pcb, &receiver, RECEIVER_HTTP_PORT, http_connected) != ERR_OK) http_finish(0);
    return 1;
}

/* ------------------------------------------------- WiFi */

static char best_ssid[33];
static int16_t best_rssi;

static int on_scan(void *env, const cyw43_ev_scan_result_t *r) {
    (void)env;
    size_t prefix = strlen(FW_SSID_PREFIX);
    if (r && r->auth_mode == 0 && r->ssid_len >= prefix && memcmp(r->ssid, FW_SSID_PREFIX, prefix) == 0 &&
        (!best_ssid[0] || r->rssi > best_rssi)) {
        memcpy(best_ssid, r->ssid, r->ssid_len);
        best_ssid[r->ssid_len] = 0;
        best_rssi = r->rssi;
    }
    return 0;
}

/* Join the strongest open network whose name starts with "Sentry". Returns once it has an address. */
static void join_receiver(void) {
    for (;;) {
        cyw43_wifi_scan_options_t options = {0};
        best_ssid[0] = 0;
        if (cyw43_wifi_scan(&cyw43_state, &options, NULL, on_scan) == 0) {
            absolute_time_t until = make_timeout_time_ms(10000);
            while (cyw43_wifi_scan_active(&cyw43_state) && absolute_time_diff_us(get_absolute_time(), until) > 0) {
                cyw43_arch_poll();
                cyw43_arch_wait_for_work_until(make_timeout_time_ms(10));
            }
        }
        if (best_ssid[0]) {
            printf("joining %s\n", best_ssid);
            if (cyw43_arch_wifi_connect_timeout_ms(best_ssid, NULL, CYW43_AUTH_OPEN, 15000) == 0) {
                const ip4_addr_t *ip = netif_ip4_addr(netif_default), *mask = netif_ip4_netmask(netif_default);
                ip_addr_set_ip4_u32(&broadcast, (ip4_addr_get_u32(ip) & ip4_addr_get_u32(mask)) | ~ip4_addr_get_u32(mask));
                return;
            }
        } else {
            printf("no open network named %s* yet\n", FW_SSID_PREFIX);
        }
        sleep_ms(3000);
    }
}

int main(void) {
    stdio_init_all();
    if (cyw43_arch_init()) {
        printf("WiFi chip failed to start\n");
        return 1;
    }
    cyw43_arch_enable_sta_mode();
    ipaddr_aton("192.168.4.1", &receiver);

    fw_hooks hooks = {.send = send_frame, .http = start_http, .log = log_line, .ctx = NULL};
    fw_init(&bridge, &hooks);
    printf("Sentry Bridge %s for Pico W / Pico 2 W\n", SB_VERSION);

    rx = udp_new_ip_type(IPADDR_TYPE_V4);
    tx = udp_new_ip_type(IPADDR_TYPE_V4);
    udp_bind(rx, IP_ANY_TYPE, FW_RECEIVER_PORT);
    udp_recv(rx, on_datagram, NULL);
    ip_set_option(tx, SOF_BROADCAST);

    for (;;) {
        join_receiver();
        printf("listening on UDP %d, broadcasting traffic\n", FW_RECEIVER_PORT);
        while (cyw43_tcpip_link_status(&cyw43_state, CYW43_ITF_STA) == CYW43_LINK_UP) {
            cyw43_arch_poll();
            double now = now_s();
            if (http.busy && now > http.deadline) http_finish(0);
            if (http.finished) {
                http.finished = 0;
                fw_http_done(&bridge, http.request, http.ok);
            }
            fw_poll(&bridge, now);
            cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, fw_receiving(&bridge, now));
            cyw43_arch_wait_for_work_until(make_timeout_time_ms(20));
        }
        cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, 0);
        if (http.busy) http_finish(0);
        printf("receiver WiFi lost; rejoining\n");
    }
}
