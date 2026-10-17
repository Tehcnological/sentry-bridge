#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE 1
#endif
#include "BridgeIO.h"
#include "sbcore.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
struct sbio {
    sb_bridge bridge;
    int input, output, recovering, has_heartbeat, demo;
    struct sockaddr_in destination;
    uint8_t buffer[65536];
    sbio_status status;
    double started, last_tick, last_step, last_rx, last_heartbeat;
    sbio_packet_observer observer;
    void *observer_context;
    int shared;                  /* input bound to shared_local, the address routed to source */
    uint32_t shared_source, shared_local;
    uint16_t shared_port;
};
double sbio_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}
static void log_message(void *ctx, const char *msg) {
    sbio *io = ctx;
    snprintf(io->status.detail, sizeof io->status.detail, "%s", msg);
}
static void emit(void *ctx, const uint8_t *data, size_t len) {
    sbio *io = ctx;
    /* Only the shared core calls emit with CRC-valid canonical frames. Count unescaped
       bytes to exclude short heartbeats even when escaping makes them look longer. */
    if (len >= 2 && data[0] == 0x7e && data[1] == 0x00) {
        size_t bytes = 0;
        for (size_t i = 1; i + 1 < len; i++) {
            if (data[i] == 0x7d) i++;
            bytes++;
        }
        if (bytes >= 9) { /* Seven message bytes plus the two CRC bytes. */
            io->has_heartbeat = 1; io->last_heartbeat = sbio_now();
        }
    }
    if (io->demo) return; /* Decode/count sample frames, never emit network output. */
    ssize_t n = sendto(io->output, data, len, 0,
                       (struct sockaddr *)&io->destination, sizeof io->destination);
    if (n == (ssize_t)len) io->status.forwarded++;
    else {
        io->status.send_errors++;
        snprintf(io->status.detail, sizeof io->status.detail,
                 "UDP output failed: %s. Check Local Network access and destination.", strerror(errno));
    }
}
static int unicast(uint32_t ip) {
    return ip != 0 && (ip >> 24) != 0 && (ip >> 24) < 224;
}
static int interface_broadcast(uint32_t ip) {
    struct ifaddrs *interfaces = NULL;
    if (getifaddrs(&interfaces) != 0) return 1;
    int found = 0;
    for (struct ifaddrs *a = interfaces; a; a = a->ifa_next) {
        if ((a->ifa_flags & IFF_BROADCAST) && a->ifa_broadaddr &&
            a->ifa_broadaddr->sa_family == AF_INET &&
            ntohl(((struct sockaddr_in *)a->ifa_broadaddr)->sin_addr.s_addr) == ip) found = 1;
    }
    freeifaddrs(interfaces); return found;
}
static int local_address(uint32_t ip) {
    if ((ip >> 24) == 127) return 1;
    struct ifaddrs *interfaces = NULL;
    if (getifaddrs(&interfaces) != 0) return 1; /* conservative on inspection failure */
    int found = 0;
    for (struct ifaddrs *a = interfaces; a; a = a->ifa_next) {
        if (a->ifa_addr && a->ifa_addr->sa_family == AF_INET &&
            ntohl(((struct sockaddr_in *)a->ifa_addr)->sin_addr.s_addr) == ip) found = 1;
    }
    freeifaddrs(interfaces); return found;
}
/* Local IPv4 address the system routes to dst (no datagram is sent), host order. */
static int routed_address(uint32_t dst, uint16_t port, uint32_t *local) {
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return -1;
    struct sockaddr_in route = {0};
    socklen_t length = sizeof route;
    route.sin_family = AF_INET; route.sin_port = htons(port); route.sin_addr.s_addr = htonl(dst);
    int ok = connect(s, (struct sockaddr *)&route, sizeof route) == 0 &&
             getsockname(s, (struct sockaddr *)&route, &length) == 0;
    close(s);
    if (!ok) return -1;
    *local = ntohl(route.sin_addr.s_addr); return 0;
}
/* Whether local is a usable address for receiving from dst: a non-loopback, non-tunnel interface
   address whose subnet contains dst. A receiver reached through any router (another WiFi network,
   cellular, VPN) is refused so the wrong network gets "Join its WiFi" rather than a silent bind.
   1 usable, 0 not usable, -1 interfaces unreadable. */
static int receiver_interface(uint32_t local, uint32_t dst) {
    struct ifaddrs *interfaces = NULL;
    if ((local >> 24) == 127) return 0;
    if (getifaddrs(&interfaces) != 0) return -1;
    int usable = 0;
    for (struct ifaddrs *a = interfaces; a; a = a->ifa_next) {
        if (!a->ifa_addr || a->ifa_addr->sa_family != AF_INET || (a->ifa_flags & IFF_LOOPBACK) ||
            ntohl(((struct sockaddr_in *)a->ifa_addr)->sin_addr.s_addr) != local) continue;
        uint32_t mask = a->ifa_netmask ? ntohl(((struct sockaddr_in *)a->ifa_netmask)->sin_addr.s_addr) : 0xffffffffu;
        const char *name = a->ifa_name ? a->ifa_name : "";
        int tunnel = (a->ifa_flags & IFF_POINTOPOINT) || !strncmp(name, "pdp_ip", 6) || !strncmp(name, "utun", 4) ||
                     !strncmp(name, "ipsec", 5) || !strncmp(name, "ppp", 3);
        usable = !tunnel && (local & mask) == (dst & mask);
        break;
    }
    freeifaddrs(interfaces); return usable;
}
/* SO_REUSEADDR socket bound to local:port beside an EFB's wildcard listener; never SO_REUSEPORT. */
static int shared_socket(uint32_t local, uint16_t port) {
    int s = socket(AF_INET, SOCK_DGRAM, 0), one = 1;
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET; address.sin_port = htons(port); address.sin_addr.s_addr = htonl(local);
    if (s < 0) return -1;
    if (setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one) < 0 ||
        bind(s, (struct sockaddr *)&address, sizeof address) < 0 || fcntl(s, F_SETFL, O_NONBLOCK) < 0) {
        int saved = errno; close(s); errno = saved; return -1;
    }
    return s;
}
int sbio_efb_candidate(const char *address, uint16_t port, const char *receiver, uint16_t input_port) {
    uint32_t ip, source;
    if (!sb_parse_ipv4(address, &ip) || !sb_parse_ipv4(receiver, &source) || !port ||
        !unicast(ip) || ip == source || (ip >> 24) == 127 || interface_broadcast(ip)) return 0;
    return port != input_port || !local_address(ip);
}
void sbio_observe(sbio *io, sbio_packet_observer observer, void *context) {
    io->observer = observer; io->observer_context = context;
}
sbio *sbio_open(const char *source_ip, uint16_t input_port, const char *destination_ip,
                uint16_t output_port, char *error, size_t cap) {
    return sbio_open_shared(source_ip, input_port, destination_ip, output_port, 0, error, cap);
}
static sbio *open_route(const char *source_ip, uint16_t input_port,
                       const char *destination_ip, uint16_t output_port, int share_input,
                       int demo, char *error, size_t cap);
sbio *sbio_open_demo(uint16_t input_port, char *error, size_t cap) {
    return open_route("127.0.0.1", input_port, "127.0.0.1", 14501, 0, 1, error, cap);
}
sbio *sbio_open_shared(const char *source_ip, uint16_t input_port, const char *destination_ip,
                       uint16_t output_port, int share_input, char *error, size_t cap) {
    return open_route(source_ip, input_port, destination_ip, output_port, share_input, 0, error, cap);
}
static sbio *open_route(const char *source_ip, uint16_t input_port,
                       const char *destination_ip, uint16_t output_port, int share_input,
                       int demo, char *error, size_t cap) {
    uint32_t src, dst;
    if (!sb_parse_ipv4(source_ip, &src) || !unicast(src) ||
        !sb_parse_ipv4(destination_ip, &dst) || !unicast(dst) || interface_broadcast(dst) || !input_port || !output_port) {
        snprintf(error, cap, "Enter valid unicast IPv4 addresses and ports 1–65535."); return NULL;
    }
    /* Shared input binds only the address that reaches the receiver, so loopback output on the
       same port reaches the EFB's wildcard listener instead of this bridge. */
    if (share_input && (dst >> 24) != 127) {
        snprintf(error, cap, "Port sharing sends to an EFB on this device: use destination 127.0.0.1.");
        return NULL;
    }
    /* Any local interface on the same port feeds our own wildcard listener. */
    if (!share_input && input_port == output_port && local_address(dst)) {
        snprintf(error, cap, "EFB output and receiver input ports must differ on this device.");
        return NULL;
    }
    sbio *io = calloc(1, sizeof *io);
    if (!io) { snprintf(error, cap, "Unable to allocate bridge."); return NULL; }
    io->input = io->output = -1; io->demo = demo;
    io->input = socket(AF_INET, SOCK_DGRAM, 0);
    if (io->input < 0) goto fail;
    /* No SO_REUSEADDR/SO_REUSEPORT by default: never promise duplicate app delivery. */
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET; address.sin_port = htons(input_port);
    address.sin_addr.s_addr = htonl(demo ? INADDR_LOOPBACK : INADDR_ANY);
    if (share_input) {
        /* Opt-in: bind only the local address routed to the receiver beside an EFB already
           listening on 0.0.0.0. The more specific bind receives the receiver's unicast; loopback
           datagrams still reach the EFB. No SO_REUSEPORT, so no duplicate delivery. */
        uint32_t local;
        int usable = routed_address(src, input_port, &local) < 0 ? 0 : receiver_interface(local, src);
        if (usable != 1) {
            snprintf(error, cap, usable < 0 ? "Network interfaces could not be read. Start again."
                                            : "The receiver is not on this device's WiFi network. Join its WiFi, then Start again.");
            sbio_close(io); return NULL;
        }
        close(io->input);
        if ((io->input = shared_socket(local, input_port)) < 0) goto fail;
        io->shared = 1; io->shared_source = src; io->shared_local = local; io->shared_port = input_port;
    }
    if (!share_input && (bind(io->input, (struct sockaddr *)&address, sizeof address) < 0 ||
                         fcntl(io->input, F_SETFL, O_NONBLOCK) < 0)) goto fail;
    if (!demo) {
        io->output = socket(AF_INET, SOCK_DGRAM, 0);
        if (io->output < 0 || fcntl(io->output, F_SETFL, O_NONBLOCK) < 0) goto fail;
    }
    io->destination.sin_family = AF_INET;
    io->destination.sin_port = htons(output_port);
    io->destination.sin_addr.s_addr = htonl(dst);
    sb_config config; sb_config_defaults(&config);
    config.sentry_ip = src; config.relay = 1;
    config.log = log_message; config.log_ctx = io;
    sb_init(&io->bridge, &config);
    io->started = io->last_step = io->last_tick = sbio_now();
    snprintf(io->status.detail, sizeof io->status.detail, "Waiting for receiver data");
    return io;
fail:
    if (errno == EADDRINUSE) {
        snprintf(error, cap, share_input
                 ? "Receiver UDP port %u is in use on this device's WiFi address. Stop the other bridge, then Start again."
                 : "Receiver UDP port %u is already in use. Close the EFB or other app using it, or turn on port sharing, then Start again.", input_port);
    } else if (errno == EACCES || errno == EPERM) {
        snprintf(error, cap, "UDP access denied. Enable Sentry Bridge in Settings > Privacy & Security > Local Network, then Start again.");
    } else {
        snprintf(error, cap, "Cannot open UDP input/output: %s. Check WiFi and Local Network access, then Start again.", strerror(errno));
    }
    sbio_close(io); return NULL;
}
int sbio_step(sbio *io, int timeout_ms) {
    if (!io || timeout_ms < 0 || timeout_ms > 1000) return -1;
    double now = sbio_now();
    if (now - io->last_step > 3) {
        /* A suspended worker must not turn queued old positions into fresh ones. */
        const sb_stats *stats = sb_get_stats(&io->bridge);
        io->status.traffic += stats->sent; io->status.weather += stats->frames[7];
        io->status.heartbeat += stats->frames[0]; io->status.crc_bad += stats->crc_bad;
        if (io->last_rx && now - io->last_rx > io->status.max_gap) io->status.max_gap = now - io->last_rx;
        io->recovering = 1;
        sb_config cfg = io->bridge.cfg;
        sb_init(&io->bridge, &cfg);
        io->last_rx = 0; io->has_heartbeat = 0;
        snprintf(io->status.detail, sizeof io->status.detail, "Execution gap: cleared queued input and stale targets");
    }
    io->last_step = now;
    if (io->recovering) {
        /* Bounded drain: a flooding sender must not prevent cancellation. */
        for (int i = 0; i < 64; i++) {
            if (recv(io->input, io->buffer, sizeof io->buffer, 0) < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) { io->recovering = 0; break; }
                if (errno != EINTR) return -1;
            }
        }
        return 0;
    }
    struct pollfd fd = {io->input, POLLIN, 0};
    int result = poll(&fd, 1, timeout_ms);
    if (result < 0 && errno != EINTR) return -1;
    if (fd.revents & (POLLERR | POLLNVAL | POLLHUP)) return -1;
    /* Bound each batch so stop and status still run under a saturated socket. */
    for (int i = 0; i < 64 && result > 0; i++) {
        struct sockaddr_in src; socklen_t len = sizeof src;
        ssize_t n = recvfrom(io->input, io->buffer, sizeof io->buffer, 0, (struct sockaddr *)&src, &len);
        if (n < 0) { if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) break; return -1; }
        if (io->observer) io->observer(io->observer_context, io->buffer, (size_t)n, ntohl(src.sin_addr.s_addr));
        if (ntohl(src.sin_addr.s_addr) != io->bridge.cfg.sentry_ip) {
            io->status.rejected_sources++; continue;
        }
        now = sbio_now();
        if (io->last_rx && now - io->last_rx > io->status.max_gap) io->status.max_gap = now - io->last_rx;
        io->last_rx = now; io->status.received++;
        sb_feed(&io->bridge, io->buffer, (size_t)n, ntohl(src.sin_addr.s_addr), now, emit, io);
    }
    now = sbio_now();
    if (now - io->last_tick >= 1) { sb_tick(&io->bridge, now, emit, io); io->last_tick = now; }
    return 0;
}
void sbio_snapshot(sbio *io, sbio_status *status) {
    *status = io->status;
    const sb_stats *s = sb_get_stats(&io->bridge);
    status->traffic += s->sent; status->weather += s->frames[7];
    status->heartbeat += s->frames[0]; status->crc_bad += s->crc_bad;
    double now = sbio_now();
    status->age = io->last_rx ? now - io->last_rx : -1;
    status->gps_valid = io->bridge.gps_valid && io->has_heartbeat && now - io->last_heartbeat <= 3;
    status->mode = sb_get_mode(&io->bridge);
    status->elapsed = now - io->started;
    if (status->age > status->max_gap) status->max_gap = status->age;
}
int sbio_refresh_shared_input(sbio *io) {
    uint32_t local;
    if (!io->shared || routed_address(io->shared_source, io->shared_port, &local) < 0 ||
        local == io->shared_local || receiver_interface(local, io->shared_source) != 1) return 0;
    int s = shared_socket(local, io->shared_port), failure = errno;
    char address[INET_ADDRSTRLEN];
    struct in_addr in = { .s_addr = htonl(local) };
    inet_ntop(AF_INET, &in, address, sizeof address);
    if (s < 0) {
        /* Keep the old socket; the worker retries while the receiver stays silent. */
        snprintf(io->status.detail, sizeof io->status.detail,
                 "WiFi address changed to %s but UDP %u cannot be opened there (%s). Close other apps using it, or Stop and Start.",
                 address, io->shared_port, strerror(failure));
        return -1;
    }
    close(io->input); io->input = s; io->shared_local = local;
    snprintf(io->status.detail, sizeof io->status.detail, "WiFi address changed; receiving on %s", address);
    return 1;
}
#ifdef SBIO_TESTING
/* Adapter tests only: pretend the input was bound to another address so refresh sees a change. */
void sbio_test_set_shared_local(sbio *io, uint32_t local) { io->shared_local = local; }
#endif
int sbio_wants_enable(sbio *io) { return sb_wants_enable(&io->bridge, sbio_now()); }
struct sbio_cancel { _Atomic int cancelled; };
sbio_cancel *sbio_cancel_create(void) { return calloc(1, sizeof(sbio_cancel)); }
void sbio_cancel_set(sbio_cancel *cancel) { if (cancel) atomic_store(&cancel->cancelled, 1); }
void sbio_cancel_destroy(sbio_cancel *cancel) { free(cancel); }
static int cancelled(sbio_cancel *cancel) { return cancel && atomic_load(&cancel->cancelled); }
/* Waits for readiness in slices of at most 50 ms so cancellation is noticed promptly.
   1 ready, 0 deadline passed, -2 cancelled, -1 poll error. */
static int wait_ready(int s, short events, double deadline, sbio_cancel *cancel) {
    for (;;) {
        if (cancelled(cancel)) return -2;
        double left = deadline - sbio_now();
        if (left <= 0) return 0;
        struct pollfd p = { .fd = s, .events = events };
        int r = poll(&p, 1, left > 0.05 ? 50 : (int)(left * 1000) + 1);
        if (r > 0) return 1;
        if (r < 0 && errno != EINTR) return -1;
    }
}
/* One monotonic deadline bounds connect, the whole request write and the status-line read. */
static int receiver_http(const char *receiver_ip, uint16_t http_port, int timeout_ms, const char *path,
                         const char *json, sbio_cancel *cancel, char *error, size_t error_cap) {
    if (cancelled(cancel)) { snprintf(error, error_cap, "Cancelled"); return SBIO_HTTP_CANCELLED; }
    double deadline = sbio_now() + timeout_ms / 1000.0;
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET; sa.sin_port = htons(http_port);
    if (!http_port || inet_pton(AF_INET, receiver_ip, &sa.sin_addr) != 1) {
        snprintf(error, error_cap, "Invalid receiver address"); return -1;
    }
    int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s < 0) { snprintf(error, error_cap, "Socket error %d", errno); return -1; }
    int one = 1, ready;
    setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
    if (connect(s, (struct sockaddr *)&sa, sizeof sa) != 0) {
        int so = 0; socklen_t sl = sizeof so;
        if (errno != EINPROGRESS) goto refused;
        if ((ready = wait_ready(s, POLLOUT, deadline, cancel)) != 1) goto unready;
        if (getsockopt(s, SOL_SOCKET, SO_ERROR, &so, &sl) != 0 || so) goto refused;
    }
    char request[512], response[512];
    int len = json
        ? snprintf(request, sizeof request,
                   "POST %s HTTP/1.0\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %u\r\n"
                   "Connection: close\r\n\r\n%s", path, receiver_ip, (unsigned)strlen(json), json)
        : snprintf(request, sizeof request, "GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n",
                   path, receiver_ip);
    for (int sent = 0; sent < len;) {
        if ((ready = wait_ready(s, POLLOUT, deadline, cancel)) != 1) goto unready;
        ssize_t n = send(s, request + sent, (size_t)(len - sent), 0);
        if (n > 0) sent += (int)n;
        else if (n < 0 && errno != EAGAIN && errno != EINTR) {
            snprintf(error, error_cap, "Sending to the receiver failed (%d)", errno); close(s); return -1;
        }
    }
    size_t got = 0;
    /* Only the status line is needed; a redirect status is reported, never followed. */
    while (got < sizeof response - 1 && !memchr(response, '\n', got)) {
        if ((ready = wait_ready(s, POLLIN, deadline, cancel)) != 1) goto unready;
        ssize_t n = recv(s, response + got, sizeof response - 1 - got, 0);
        if (n == 0) break;
        if (n > 0) got += (size_t)n;
        else if (errno != EAGAIN && errno != EINTR) break;
    }
    close(s);
    response[got] = 0;
    int status = 0;
    if (sscanf(response, "HTTP/%*d.%*d %d", &status) != 1 || status < 100 || status > 599) {
        snprintf(error, error_cap, "No HTTP response from the receiver"); return -1;
    }
    return status;
refused:
    close(s);
    snprintf(error, error_cap, "Receiver web server refused or unreachable at %s:%u", receiver_ip, http_port);
    return -1;
unready:
    close(s);
    if (ready == -2) { snprintf(error, error_cap, "Cancelled"); return SBIO_HTTP_CANCELLED; }
    if (ready == 0) snprintf(error, error_cap, "Receiver web server did not answer within %d ms at %s:%u", timeout_ms, receiver_ip, http_port);
    else snprintf(error, error_cap, "Receiver connection failed (%d)", errno);
    return -1;
}
int sbio_request_compatibility(const char *receiver_ip, uint16_t http_port, int timeout_ms,
                               sbio_cancel *cancel, char *error, size_t error_cap) {
    return receiver_http(receiver_ip, http_port, timeout_ms, sb_enable_path, sb_enable_body, cancel, error, error_cap);
}
int sbio_request_status(const char *receiver_ip, uint16_t http_port, int timeout_ms,
                        sbio_cancel *cancel, char *error, size_t error_cap) {
    return receiver_http(receiver_ip, http_port, timeout_ms, "/?action=get", NULL, cancel, error, error_cap);
}
void sbio_close(sbio *io) {
    if (!io) return;
    if (io->input >= 0) close(io->input);
    if (io->output >= 0) close(io->output);
    free(io);
}
