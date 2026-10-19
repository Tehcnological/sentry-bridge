/* SPDX-License-Identifier: GPL-3.0-only
 * Copyright (c) 2026 Sentry Bridge contributors. See NOTICE.txt.
 *
 * Sentry Bridge for ESP32-C3 boards. Joins the receiver's open WiFi (name starting "Sentry"),
 * listens on UDP 4000 and broadcasts decoded traffic to the receiver's network on UDP 4000.
 * Status LED (GPIO 8, active low on SuperMini boards): on while receiver data arrives.
 */
#include <string.h>

#include "driver/gpio.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

#include "fw_bridge.h"

#define LED_GPIO 8
#define GOT_IP BIT0
#define RECEIVER "192.168.4.1"

static const char *TAG = "sentry-bridge";
static EventGroupHandle_t net;
static esp_netif_t *sta;
static fw_bridge bridge;  /* about 45 KB: static, not on a task stack */
static int out_sock = -1;
static struct sockaddr_in broadcast;
static QueueHandle_t http_queue;
static volatile int http_busy, http_finished, http_ok;
static volatile fw_http http_last;

static double now_s(void) { return esp_timer_get_time() / 1e6; }

static void send_frame(void *ctx, const uint8_t *frame, size_t len) {
    (void)ctx;
    if (out_sock >= 0) sendto(out_sock, frame, len, 0, (struct sockaddr *)&broadcast, sizeof broadcast);
}

static int start_http(void *ctx, fw_http request) {
    (void)ctx;
    if (http_busy || !(xEventGroupGetBits(net) & GOT_IP)) return 0;
    http_busy = 1;
    xQueueSend(http_queue, &request, 0);
    return 1;
}

static void log_line(void *ctx, const char *line) {
    (void)ctx;
    ESP_LOGI(TAG, "%s", line);
}

/* Blocking HTTP runs here so the bridge loop never waits on the receiver's web server. */
static void http_task(void *arg) {
    fw_http request;
    (void)arg;
    for (;;) {
        if (xQueueReceive(http_queue, &request, portMAX_DELAY) != pdTRUE) continue;
        char url[64];
        int enable = request == FW_HTTP_ENABLE;
        snprintf(url, sizeof url, "http://" RECEIVER "%s", enable ? sb_enable_path : fw_keepalive_path);
        esp_http_client_config_t config = {
            .url = url, .timeout_ms = 5000, .method = enable ? HTTP_METHOD_POST : HTTP_METHOD_GET,
        };
        esp_http_client_handle_t client = esp_http_client_init(&config);
        if (enable) {
            esp_http_client_set_header(client, "Content-Type", "application/json");
            esp_http_client_set_post_field(client, sb_enable_body, (int)strlen(sb_enable_body));
        }
        esp_err_t err = esp_http_client_perform(client);
        int status = esp_http_client_get_status_code(client);
        esp_http_client_cleanup(client);
        http_last = request;
        http_ok = err == ESP_OK && status >= 200 && status < 300;
        http_finished = 1;
        http_busy = 0;
    }
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) xEventGroupClearBits(net, GOT_IP);
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *got = (ip_event_got_ip_t *)data;
        uint32_t ip = got->ip_info.ip.addr, mask = got->ip_info.netmask.addr;
        memset(&broadcast, 0, sizeof broadcast);
        broadcast.sin_family = AF_INET;
        broadcast.sin_port = htons(FW_RECEIVER_PORT);
        broadcast.sin_addr.s_addr = (ip & mask) | ~mask;
        xEventGroupSetBits(net, GOT_IP);
    }
}

/* Join the strongest open network whose name starts with "Sentry". Returns once connected. */
static void join_receiver(void) {
    for (;;) {
        wifi_scan_config_t scan = {0};
        uint16_t count = 20;
        wifi_ap_record_t aps[20];
        int best = -1;
        esp_wifi_disconnect();
        if (esp_wifi_scan_start(&scan, true) == ESP_OK && esp_wifi_scan_get_ap_records(&count, aps) == ESP_OK) {
            for (int i = 0; i < count; i++)
                if (aps[i].authmode == WIFI_AUTH_OPEN &&
                    strncmp((const char *)aps[i].ssid, FW_SSID_PREFIX, strlen(FW_SSID_PREFIX)) == 0 &&
                    (best < 0 || aps[i].rssi > aps[best].rssi))
                    best = i;
        }
        if (best >= 0) {
            wifi_config_t config = {0};
            memcpy(config.sta.ssid, aps[best].ssid, sizeof config.sta.ssid);
            config.sta.threshold.authmode = WIFI_AUTH_OPEN;
            ESP_LOGI(TAG, "joining %s", (const char *)config.sta.ssid);
            esp_wifi_set_config(WIFI_IF_STA, &config);
            esp_wifi_connect();
            if (xEventGroupWaitBits(net, GOT_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(15000)) & GOT_IP) return;
        } else {
            ESP_LOGI(TAG, "no open network named %s* yet", FW_SSID_PREFIX);
        }
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
}

static int udp_input(void) {
    int s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in any = {.sin_family = AF_INET, .sin_port = htons(FW_RECEIVER_PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (s >= 0 && bind(s, (struct sockaddr *)&any, sizeof any) != 0) {
        close(s);
        s = -1;
    }
    return s;
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();  /* WiFi driver calibration data */
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(LED_GPIO, 1);

    net = xEventGroupCreate();
    http_queue = xQueueCreate(1, sizeof(fw_http));
    xTaskCreate(http_task, "receiver-http", 6144, NULL, 4, NULL);
    esp_netif_init();
    esp_event_loop_create_default();
    sta = esp_netif_create_default_wifi_sta();
    wifi_init_config_t wifi = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&wifi);
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL);
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_start();

    fw_hooks hooks = {.send = send_frame, .http = start_http, .log = log_line, .ctx = NULL};
    fw_init(&bridge, &hooks);
    ESP_LOGI(TAG, "Sentry Bridge %s for ESP32-C3", SB_VERSION);

    static uint8_t buf[1600];  /* off the stack: sb_feed already uses about 3 KB of it */
    for (;;) {
        join_receiver();
        int in_sock = udp_input();
        out_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        int on = 1;
        setsockopt(out_sock, SOL_SOCKET, SO_BROADCAST, &on, sizeof on);
        ESP_LOGI(TAG, "listening on UDP %d, broadcasting traffic", FW_RECEIVER_PORT);
        while (in_sock >= 0 && (xEventGroupGetBits(net) & GOT_IP)) {
            fd_set read;
            struct timeval wait = {.tv_sec = 0, .tv_usec = 100000};
            FD_ZERO(&read);
            FD_SET(in_sock, &read);
            if (select(in_sock + 1, &read, NULL, NULL, &wait) > 0) {
                struct sockaddr_in from;
                socklen_t fromlen = sizeof from;
                int n = recvfrom(in_sock, buf, sizeof buf, 0, (struct sockaddr *)&from, &fromlen);
                if (n > 0) fw_datagram(&bridge, buf, (size_t)n, ntohl(from.sin_addr.s_addr), now_s());
            }
            if (http_finished) {
                http_finished = 0;
                fw_http_done(&bridge, http_last, http_ok);
            }
            double now = now_s();
            fw_poll(&bridge, now);
            gpio_set_level(LED_GPIO, fw_receiving(&bridge, now) ? 0 : 1);
        }
        gpio_set_level(LED_GPIO, 1);
        if (in_sock >= 0) close(in_sock);
        close(out_sock);
        out_sock = -1;
        ESP_LOGI(TAG, "receiver WiFi lost; rejoining");
    }
}
