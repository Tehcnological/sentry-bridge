/*
 * sentry-bridge: native command-line bridge (Linux, macOS, Windows) around the C core.
 * Same flags and behaviour as bridge.py, minus --log-raw. On Windows, double-clicking the .exe
 * points to the GUI app and keeps the window open; it does not start without --dest.
 */
#if defined(_WIN32)
#ifndef _CRT_SECURE_NO_WARNINGS
#define _CRT_SECURE_NO_WARNINGS
#endif
#else
#define _POSIX_C_SOURCE 200112L
#define _DARWIN_C_SOURCE
#endif
#include "sbcore.h"
#include "license_text.h"

#include <errno.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
typedef SOCKET sock_t;
#define SOCK_BAD INVALID_SOCKET
#define closesock closesocket
static int sock_err(void) { return WSAGetLastError(); }
typedef CRITICAL_SECTION mutex_t;
static void mutex_init(mutex_t *m) { InitializeCriticalSection(m); }
static void mutex_lock(mutex_t *m) { EnterCriticalSection(m); }
static void mutex_unlock(mutex_t *m) { LeaveCriticalSection(m); }
static void sleep_ms(int ms) { Sleep((DWORD)ms); }
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <pthread.h>
#include <unistd.h>
typedef int sock_t;
#define SOCK_BAD (-1)
#define closesock close
static int sock_err(void) { return errno; }
typedef pthread_mutex_t mutex_t;
static void mutex_init(mutex_t *m) { pthread_mutex_init(m, NULL); }
static void mutex_lock(mutex_t *m) { pthread_mutex_lock(m); }
static void mutex_unlock(mutex_t *m) { pthread_mutex_unlock(m); }
static void sleep_ms(int ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}
#endif

#define ANNOUNCE_PORT 63093
#define ANNOUNCE_TTL_S 30.0
#define MAX_DESTS 16
#define MAX_APPS 32 /* announcements noted, not destinations */

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int sig) { (void)sig; g_stop = 1; }

/* ------------------------------------------------------------------ time and logging */

static double mono_now(void) {
#ifdef _WIN32
    return (double)GetTickCount64() / 1000.0;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
#endif
}

static mutex_t g_log_lock; /* the Sentry web worker logs too */

static void logm(const char *fmt, ...) {
    char stamp[16];
    time_t t = time(NULL);
    struct tm *lt;
    va_list ap;
    mutex_lock(&g_log_lock);
    lt = localtime(&t);
    strftime(stamp, sizeof stamp, "%H:%M:%S", lt);
    printf("%s ", stamp);
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n");
    fflush(stdout);
    mutex_unlock(&g_log_lock);
}

static void core_log(void *ctx, const char *msg) { (void)ctx; logm("%s", msg); }

static void ip_str(uint32_t ip, char *out, size_t cap) {
    snprintf(out, cap, "%u.%u.%u.%u", (ip >> 24) & 255, (ip >> 16) & 255, (ip >> 8) & 255, ip & 255);
}

/* ------------------------------------------------------------------ minimal HTTP client */

/* Returns the HTTP status, or -1 with err filled. Body (after headers) is copied to body. */
static int http_request(uint32_t ip, int port, const char *method, const char *path, const char *json,
                        char *body, size_t body_cap, char *err, size_t err_cap, int timeout_ms) {
    struct sockaddr_in sa;
    sock_t s;
    char req[512], ipbuf[20], resp[8192];
    const char *sp;
    size_t got = 0;
    int status = -1, rc;
    fd_set wf, ef;
    struct timeval tv;
    ip_str(ip, ipbuf, sizeof ipbuf);
    if (body && body_cap) body[0] = 0;
    s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == SOCK_BAD) { snprintf(err, err_cap, "socket error %d", sock_err()); return -1; }
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons((unsigned short)port);
    sa.sin_addr.s_addr = htonl(ip);
#ifdef _WIN32
    { u_long nb = 1; ioctlsocket(s, FIONBIO, &nb); }
#else
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
#endif
    rc = connect(s, (struct sockaddr *)&sa, sizeof sa);
    if (rc != 0) {
        int so = 0;
        socklen_t sl = sizeof so;
        FD_ZERO(&wf);
        FD_SET(s, &wf);
        /* Windows reports a failed non-blocking connect in the exception set, not as writable */
        FD_ZERO(&ef);
        FD_SET(s, &ef);
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        if (select((int)s + 1, NULL, &wf, &ef, &tv) <= 0) {
            snprintf(err, err_cap, "connection to %s:%d timed out", ipbuf, port);
            closesock(s);
            return -1;
        }
        getsockopt(s, SOL_SOCKET, SO_ERROR, (char *)&so, &sl);
        if (so || FD_ISSET(s, &ef)) {
            snprintf(err, err_cap, "connection to %s:%d refused or unreachable (%d)", ipbuf, port, so);
            closesock(s);
            return -1;
        }
    }
#ifdef _WIN32
    { u_long nb = 0; DWORD to = (DWORD)timeout_ms; ioctlsocket(s, FIONBIO, &nb);
      setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&to, sizeof to); }
#else
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) & ~O_NONBLOCK);
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof tv);
#endif
    if (json)
        snprintf(req, sizeof req,
                 "%s %s HTTP/1.0\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %u\r\n"
                 "Connection: close\r\n\r\n%s", method, path, ipbuf, (unsigned)strlen(json), json);
    else
        snprintf(req, sizeof req, "%s %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n", method, path, ipbuf);
    if (send(s, req, (int)strlen(req), 0) < 0) {
        snprintf(err, err_cap, "send failed (%d)", sock_err());
        closesock(s);
        return -1;
    }
    for (;;) {
        int n = (int)recv(s, resp + got, (int)(sizeof resp - 1 - got), 0);
        if (n <= 0) break;
        got += (size_t)n;
        if (got >= sizeof resp - 1) break;
    }
    closesock(s);
    resp[got] = 0;
    sp = strchr(resp, ' ');
    if (got < 12 || strncmp(resp, "HTTP/", 5) != 0 || !sp) {
        snprintf(err, err_cap, "no HTTP response from %s:%d", ipbuf, port);
        return -1;
    }
    status = atoi(sp + 1);
    if (body && body_cap) {
        const char *b = strstr(resp, "\r\n\r\n");
        if (b) {
            size_t bl = got - (size_t)(b + 4 - resp);
            if (bl >= body_cap) bl = body_cap - 1;
            memcpy(body, b + 4, bl);
            body[bl] = 0;
        }
    }
    return status;
}

/* ------------------------------------------------------------------ options */

typedef struct {
    uint32_t ip;
    int port;
    char text[80];
} dest;

typedef struct {
    uint32_t ip;
    int port;
    double seen;
    char app[48];
} app_dest;

typedef struct {
    int listen_port, announce_port, http_port, auto_enable, relay, has_sentry;
    uint32_t sentry_ip;
    int32_t ownship_icao;
    int has_ref;
    double ref_lat, ref_lon, stale, interval, stats_every;
    dest dests[MAX_DESTS];
    int ndest;
} options;

static const char *USAGE =
    "License: GPLv3; no warranty. Use --license for terms and notices.\n"
    "usage: sentry-bridge [--listen-port N] [--sentry-ip IP] [--dest HOST:PORT ...] [--relay]\n"
    "                     [--ownship-icao HEX] [--ref LAT,LON] [--stale S] [--interval S]\n"
    "                     [--stats-every S] [--announce-port N] [--auto-enable] [--http-port N]\n"
    "\n"
    "Turns a Sentry in Compatibility Mode into a standard GDL90 traffic source.\n"
    "\n"
    "  --listen-port N     UDP port the Sentry sends to (default 4000)\n"
    "  --sentry-ip IP      only accept data from this IP, '' = any (default 192.168.4.1)\n"
    "  --dest HOST:PORT    where to send GDL90, repeatable unicast (required; no broadcast)\n"
    "  --relay             also forward the Sentry's standard frames (for an EFB on another port)\n"
    "  --ownship-icao HEX  this aircraft's ICAO address, filtered from traffic\n"
    "  --ref LAT,LON       fallback position reference when the Sentry has no GPS fix\n"
    "  --stale S           stop sending a target S seconds after its last position (default 15)\n"
    "  --interval S        traffic report interval (default 1)\n"
    "  --stats-every S     status line interval (default 10)\n"
    "  --announce-port N   note EFB announcements, do not send to them, 0 = off (default 63093)\n"
    "  --auto-enable       re-enable Compatibility Mode when the Sentry powers up encrypted\n"
    "  --http-port N       Sentry web port (default 80)\n"
    "  --version\n"
    "\n"
    "Garmin Pilot (two devices): --dest <tablet-ipv4>:4000 --auto-enable. There is no broadcast default.\n"
    "EFB on the same device: --relay --dest 127.0.0.1:4000 shares port 4000 with it (on Linux the EFB\n"
    "must allow port sharing); or give the EFB another port, e.g. --relay --dest 127.0.0.1:4001\n";

static const char *BANNER =
    "Sentry bridge (console)\n"
    "-----------------------\n"
    "Double-clicking does not start the bridge: it needs the EFB's address.\n"
    "Use sentry-bridge-gui.exe, or run from a terminal or shortcut, for example:\n"
    "  sentry-bridge.exe --dest 192.168.4.20:4000 --auto-enable\n"
    "Run sentry-bridge.exe --help for all options.\n";

static int resolve_ipv4(const char *host, uint32_t *out) {
    struct addrinfo hints, *res = NULL;
    if (sb_parse_ipv4(host, out)) return 1;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    if (getaddrinfo(host, NULL, &hints, &res) != 0 || !res) return 0;
    *out = ntohl(((struct sockaddr_in *)res->ai_addr)->sin_addr.s_addr);
    freeaddrinfo(res);
    return 1;
}

static int parse_dest(const char *s, dest *d) {
    char host[64];
    const char *colon = strrchr(s, ':');
    size_t hl = colon ? (size_t)(colon - s) : strlen(s);
    if (hl == 0 || hl >= sizeof host) return 0;
    memcpy(host, s, hl);
    host[hl] = 0;
    d->port = colon ? atoi(colon + 1) : 4000;
    if (d->port <= 0 || d->port > 65535) return 0;
    if (!resolve_ipv4(host, &d->ip)) return 0;
    /* 224/4 multicast, 0.0.0.0, limited broadcast, and the Sentry subnet broadcast. */
    if ((d->ip & 0xF0000000u) == 0xE0000000u || d->ip == 0 || d->ip == 0xFFFFFFFFu || d->ip == 0xC0A804FFu)
        return -1;
    snprintf(d->text, sizeof d->text, "%s:%d", host, d->port);
    return 1;
}

static int need(int i, int argc, const char *opt) {
    if (i + 1 >= argc) { fprintf(stderr, "%s needs a value\n%s", opt, USAGE); return 0; }
    return 1;
}

/* returns 0 ok, 1 exit 0 (help/version), 2 error */
static int parse_args(int argc, char **argv, options *o) {
    int i;
    memset(o, 0, sizeof *o);
    o->listen_port = 4000;
    o->announce_port = ANNOUNCE_PORT;
    o->http_port = 80;
    o->has_sentry = 1;
    o->sentry_ip = 0xC0A80401u;
    o->ownship_icao = -1;
    o->stale = 15.0;
    o->interval = 1.0;
    o->stats_every = 10.0;
    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "-h") || !strcmp(a, "--help")) { printf("%s", USAGE); return 1; }
        else if (!strcmp(a, "--version")) { printf("sentry-bridge %s\n", SB_VERSION); return 1; }
        else if (!strcmp(a, "--relay")) o->relay = 1;
        else if (!strcmp(a, "--auto-enable")) o->auto_enable = 1;
        else if (!strcmp(a, "--listen-port")) { if (!need(i, argc, a)) return 2; o->listen_port = atoi(argv[++i]); }
        else if (!strcmp(a, "--announce-port")) { if (!need(i, argc, a)) return 2; o->announce_port = atoi(argv[++i]); }
        else if (!strcmp(a, "--http-port")) { if (!need(i, argc, a)) return 2; o->http_port = atoi(argv[++i]); }
        else if (!strcmp(a, "--stale")) { if (!need(i, argc, a)) return 2; o->stale = atof(argv[++i]); }
        else if (!strcmp(a, "--interval")) { if (!need(i, argc, a)) return 2; o->interval = atof(argv[++i]); }
        else if (!strcmp(a, "--stats-every")) { if (!need(i, argc, a)) return 2; o->stats_every = atof(argv[++i]); }
        else if (!strcmp(a, "--ownship-icao")) {
            char *end;
            if (!need(i, argc, a)) return 2;
            o->ownship_icao = (int32_t)strtol(argv[++i], &end, 16);
            if (*end || o->ownship_icao < 0 || o->ownship_icao > 0xFFFFFF) { fprintf(stderr, "bad --ownship-icao\n"); return 2; }
        } else if (!strcmp(a, "--ref")) {
            if (!need(i, argc, a)) return 2;
            if (sscanf(argv[++i], "%lf,%lf", &o->ref_lat, &o->ref_lon) != 2) { fprintf(stderr, "bad --ref, want LAT,LON\n"); return 2; }
            o->has_ref = 1;
        } else if (!strcmp(a, "--sentry-ip")) {
            if (!need(i, argc, a)) return 2;
            i++;
            if (!argv[i][0]) o->has_sentry = 0;
            else if (!sb_parse_ipv4(argv[i], &o->sentry_ip)) { fprintf(stderr, "bad --sentry-ip\n"); return 2; }
        } else if (!strcmp(a, "--dest")) {
            int parsed;
            if (!need(i, argc, a)) return 2;
            i++;
            parsed = o->ndest >= MAX_DESTS ? 0 : parse_dest(argv[i], &o->dests[o->ndest]);
            if (parsed == 0) { fprintf(stderr, "bad --dest %s, want HOST:PORT\n", argv[i]); return 2; }
            if (parsed < 0) { fprintf(stderr, "use a unicast address, not %s\n", argv[i]); return 2; }
            o->ndest++;
        } else { fprintf(stderr, "unknown option %s\n%s", a, USAGE); return 2; }
    }
    if (o->listen_port <= 0 || o->listen_port > 65535) { fprintf(stderr, "bad --listen-port\n"); return 2; }
    if (o->http_port <= 0 || o->http_port > 65535) { fprintf(stderr, "bad --http-port\n"); return 2; }
    if (o->announce_port < 0 || o->announce_port > 65535) { fprintf(stderr, "bad --announce-port\n"); return 2; }
    /* A zero or negative interval would resend traffic in a tight loop */
    if (!(o->interval > 0) || !(o->stale > 0) || !(o->stats_every > 0)) {
        fprintf(stderr, "--interval, --stale and --stats-every must be greater than 0\n");
        return 2;
    }
    if (o->ndest == 0) {
        fprintf(stderr, "pass --dest HOST:PORT (unicast). Broadcast is not used.\n%s", USAGE);
        return 2;
    }
    return 0;
}

/* ------------------------------------------------------------------ runner */

typedef struct {
    sock_t tx;
    options *o;
} sender;

static void send_to(sock_t tx, uint32_t ip, int port, const uint8_t *f, size_t n, const char *label) {
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons((unsigned short)port);
    sa.sin_addr.s_addr = htonl(ip);
    if (sendto(tx, (const char *)f, (int)n, 0, (struct sockaddr *)&sa, sizeof sa) < 0) {
        static double last_err = -100;
        double m = mono_now();
        if (m - last_err > 10) { logm("send to %s failed (%d)", label, sock_err()); last_err = m; }
    }
}

static void emit_all(void *ctx, const uint8_t *f, size_t n) {
    sender *s = (sender *)ctx;
    int i;
    for (i = 0; i < s->o->ndest; i++) send_to(s->tx, s->o->dests[i].ip, s->o->dests[i].port, f, n, s->o->dests[i].text);
}

/* bind_ip 0 = every address. A specific address (port sharing) still uses SO_EXCLUSIVEADDRUSE on
   Windows, which allows an EFB's 0.0.0.0 listener beside it but stops another socket taking it. */
static sock_t udp_bind(uint32_t bind_ip, int port, int exclusive, char *err, size_t cap) {
    struct sockaddr_in sa;
    int one = 1;
    sock_t s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == SOCK_BAD) { snprintf(err, cap, "socket error %d", sock_err()); return SOCK_BAD; }
#ifdef _WIN32
    if (exclusive) setsockopt(s, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char *)&one, sizeof one);
    else setsockopt(s, SOL_SOCKET, SO_REUSEADDR, (const char *)&one, sizeof one);
#else
    /* Reuse for discovery, and for one shared address beside an EFB. A 0.0.0.0 listen stays exclusive. */
    if (!exclusive || bind_ip != 0)
        setsockopt(s, SOL_SOCKET, SO_REUSEADDR, (const char *)&one, sizeof one);
#endif
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons((unsigned short)port);
    sa.sin_addr.s_addr = htonl(bind_ip);
    if (bind(s, (struct sockaddr *)&sa, sizeof sa) != 0) {
        snprintf(err, cap, "error %d", sock_err());
        closesock(s);
        return SOCK_BAD;
    }
    return s;
}

/* This machine's address the OS uses to reach ip, or 0. Sends nothing (a UDP connect only selects
   a route). */
static uint32_t local_route(uint32_t ip) {
    struct sockaddr_in sa, local;
    socklen_t ll = sizeof local;
    uint32_t found = 0;
    sock_t s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == SOCK_BAD) return 0;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons(9);
    sa.sin_addr.s_addr = htonl(ip);
    if (connect(s, (struct sockaddr *)&sa, sizeof sa) == 0 &&
        getsockname(s, (struct sockaddr *)&local, &ll) == 0)
        found = ntohl(local.sin_addr.s_addr);
    closesock(s);
    return found;
}

/* Loopback or one of this machine's addresses: the OS picks it as the source to reach itself. */
static int is_this_device(uint32_t ip) {
    return (ip >> 24) == 127 || local_route(ip) == ip;
}

static void check_power_savings(const options *o) {
    char body[4096], err[160];
    int st = http_request(o->sentry_ip, o->http_port, "GET", sb_settings_path, NULL, body, sizeof body, err, sizeof err, 3000);
    if (st < 0) { logm("could not read Sentry settings to check Power Savings: %s", err); return; }
    if (sb_power_savings_on(body, strlen(body)) == 1)
        logm("WARNING: Sentry Power Savings is ON. It shuts the Sentry off after 30 min without ForeFlight "
             "connected. Disable it in Sentry's web settings; ForeFlight is optional.");
}

static void auto_enable(const options *o) {
    char err[160];
    int st = http_request(o->sentry_ip, o->http_port, "POST", sb_enable_path, sb_enable_body, NULL, 0, err, sizeof err, 5000);
    if (st == 200) logm("auto-enable: compatibility mode requested (HTTP 200)");
    else if (st > 0) logm("auto-enable: compatibility mode FAILED (HTTP %d)", st);
    else logm("auto-enable: compatibility mode FAILED (%s)", err);
}

/* A Sentry Plus stopped streaming to a receive-only bridge after ~16 min until its status page was
   loaded, so read it at start, every 30 s while receiving and every 5 s while silent (3 s gap), as
   sentry_ctl.keepalive_due. Read-only; logs only when the result changes. */
#define KEEPALIVE_S 30.0
#define SILENT_CONTACT_S 5.0
#define SILENT_AFTER_S 3.0
static void keepalive(const options *o) {
    static int last_ok = -1;
    char err[160];
    int st = http_request(o->sentry_ip, o->http_port, "GET", "/?action=get", NULL, NULL, 0, err, sizeof err, 3000);
    int ok = st >= 200 && st < 300;
    if (ok == last_ok) return;
    last_ok = ok;
    if (st > 0) logm("keepalive: receiver status %s (HTTP %d)", ok ? "OK" : "FAILED", st);
    else logm("keepalive: receiver status FAILED (%s)", err);
}

/* Sentry web requests run on worker threads, so a slow or absent web server never pauses traffic
   forwarding. The main loop only queues them. */
static struct {
    mutex_t lock;
    int check, enable, contact; /* contact: set by the main loop, cleared by the keepalive thread */
    const options *o;
} g_web;

static void web_queue(int check, int enable) {
    mutex_lock(&g_web.lock);
    g_web.check |= check;
    g_web.enable |= enable;
    mutex_unlock(&g_web.lock);
}

#ifdef _WIN32
static DWORD WINAPI web_worker(LPVOID arg)
#else
static void *web_worker(void *arg)
#endif
{
    (void)arg;
    for (;;) {
        int check, enable;
        mutex_lock(&g_web.lock);
        check = g_web.check;
        enable = g_web.enable;
        g_web.check = g_web.enable = 0;
        mutex_unlock(&g_web.lock);
        if (check) check_power_savings(g_web.o);
        if (enable) auto_enable(g_web.o);
        if (!check && !enable) sleep_ms(50);
    }
    return 0; /* not reached */
}

/* Each keepalive runs on its own short-lived thread, so a slow status page never delays the checks
   above and nothing polls between requests. */
#ifdef _WIN32
static DWORD WINAPI contact_thread(LPVOID arg)
#else
static void *contact_thread(void *arg)
#endif
{
    (void)arg;
    keepalive(g_web.o);
    mutex_lock(&g_web.lock);
    g_web.contact = 0;
    mutex_unlock(&g_web.lock);
    return 0;
}

#ifdef _WIN32
static int start_thread(LPTHREAD_START_ROUTINE fn) {
    HANDLE h = CreateThread(NULL, 0, fn, NULL, 0, NULL);
    if (!h) return 0;
    CloseHandle(h);
    return 1;
}
#else
static int start_thread(void *(*fn)(void *)) {
    pthread_t t;
    if (pthread_create(&t, NULL, fn, NULL) != 0) return 0;
    pthread_detach(t);
    return 1;
}
#endif

/* One keepalive at a time: none starts while the previous one is still running. */
static int web_contact(void) {
    int started = 0;
    mutex_lock(&g_web.lock);
    if (!g_web.contact) g_web.contact = started = 1;
    mutex_unlock(&g_web.lock);
    if (started && !start_thread(contact_thread)) {
        mutex_lock(&g_web.lock);
        g_web.contact = started = 0;
        mutex_unlock(&g_web.lock);
    }
    return started;
}

/* The web worker runs until the process exits. */
static int web_start(const options *o) {
    g_web.o = o;
    mutex_init(&g_web.lock);
    return start_thread(web_worker);
}

static int run(options *o) {
    static sb_bridge b; /* large: keep it off the stack */
    sb_config cfg;
    sock_t rx, ann = SOCK_BAD, tx;
    char err[128], sentry[20], dl[512];
    app_dest apps[MAX_APPS];
    int napps = 0, i;
    uint32_t checked_starts = 0;
    double next_tick, next_stat, last_rx = -1, last_contact = -1, next_route;
    int shared = 0;
    uint32_t bind_ip = 0;
    sender snd;
    uint8_t buf[65536];

    sb_config_defaults(&cfg);
    cfg.sentry_ip = o->has_sentry ? o->sentry_ip : 0;
    cfg.ownship_icao = o->ownship_icao;
    cfg.has_ref = o->has_ref;
    cfg.ref_lat = o->ref_lat;
    cfg.ref_lon = o->ref_lon;
    cfg.stale_s = o->stale;
    cfg.relay = o->relay;
    cfg.log = core_log;
    sb_init(&b, &cfg);

    /* An EFB on this device on the input port shares it, as in the GUIs: bind only the address that
       reaches the Sentry, so its unicast reaches the bridge and loopback output reaches the EFB. */
    for (i = 0; i < o->ndest; i++)
        if ((o->dests[i].ip >> 24) == 127 && o->dests[i].port == o->listen_port) shared = 1;
    if (shared) {
        bind_ip = o->has_sentry ? local_route(o->sentry_ip) : 0;
        if (!bind_ip || (bind_ip >> 24) == 127) {
            logm("ERROR: sharing UDP %d with an EFB on this device needs --sentry-ip of a Sentry on another "
                 "device, reachable from this one (join its WiFi first).", o->listen_port);
            return 2;
        }
    }
    rx = udp_bind(bind_ip, o->listen_port, 1, err, sizeof err);
    if (rx == SOCK_BAD && shared) {
        logm("ERROR: cannot share UDP port %d (%s): the EFB holding it does not allow port sharing. Close it, "
             "or give the EFB another port and use --dest 127.0.0.1:<that port>.", o->listen_port, err);
        return 2;
    }
    if (rx == SOCK_BAD) {
        logm("ERROR: cannot listen on UDP port %d (%s). Another program is probably using it: an EFB app "
             "or a second copy of this bridge. Close it and try again.", o->listen_port, err);
        return 2;
    }
    if (shared) {
        char local[20];
        ip_str(bind_ip, local, sizeof local);
        logm("sharing UDP %d with the EFB on this device (bound to %s)", o->listen_port, local);
    }
    tx = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (o->announce_port) {
        ann = udp_bind(0, o->announce_port, 0, err, sizeof err);
        if (ann == SOCK_BAD) logm("note: EFB discovery off, port %d unavailable (%s)", o->announce_port, err);
    }
    if (o->has_sentry && !web_start(o)) {
        logm("ERROR: could not start the Sentry web worker");
        return 2;
    }
    if (o->has_sentry) ip_str(o->sentry_ip, sentry, sizeof sentry);
    else snprintf(sentry, sizeof sentry, "any");
    dl[0] = 0;
    for (i = 0; i < o->ndest; i++) {
        size_t l = strlen(dl);
        snprintf(dl + l, sizeof dl - l, "%s%s", i ? ", " : "", o->dests[i].text);
    }
    logm("listening :%d from %s; sending to [%s]; relay=%s", o->listen_port, sentry, dl, o->relay ? "True" : "False");

    snd.tx = tx;
    snd.o = o;
    next_tick = mono_now() + o->interval;
    next_stat = mono_now() + o->stats_every;
    next_route = mono_now() + 5.0;
    while (!g_stop) {
        fd_set rf;
        struct timeval tv;
        double wait = next_tick - mono_now(), now, m;
        int maxfd = (int)rx, r;
        if (wait < 0) wait = 0;
        if (wait > 0.5) wait = 0.5;
        tv.tv_sec = (long)wait;
        tv.tv_usec = (long)((wait - (double)tv.tv_sec) * 1e6);
        FD_ZERO(&rf);
        FD_SET(rx, &rf);
        if (ann != SOCK_BAD) { FD_SET(ann, &rf); if ((int)ann > maxfd) maxfd = (int)ann; }
        r = select(maxfd + 1, &rf, NULL, NULL, &tv);
        if (r < 0) {
#ifndef _WIN32
            if (errno == EINTR) continue;
#endif
            logm("select failed (%d)", sock_err());
            break;
        }
        now = mono_now();
        if (r > 0 && FD_ISSET(rx, &rf)) {
            struct sockaddr_in from;
            socklen_t fl = sizeof from;
            int n = (int)recvfrom(rx, (char *)buf, sizeof buf, 0, (struct sockaddr *)&from, &fl);
            if (n > 0) {
                if (!o->has_sentry || ntohl(from.sin_addr.s_addr) == o->sentry_ip) last_rx = now;
                sb_feed(&b, buf, (size_t)n, ntohl(from.sin_addr.s_addr), now, emit_all, &snd);
            }
        }
        if (r > 0 && ann != SOCK_BAD && FD_ISSET(ann, &rf)) {
            struct sockaddr_in from;
            socklen_t fl = sizeof from;
            char app[48];
            int n = (int)recvfrom(ann, (char *)buf, 4096, 0, (struct sockaddr *)&from, &fl);
            if (n > 0) {
                int port = sb_parse_announce((const char *)buf, (size_t)n, app, sizeof app);
                uint32_t src = ntohl(from.sin_addr.s_addr);
                /* Note only. Skip this device's own input port so the log does not offer a loop. */
                if (port > 0 && !(o->has_sentry && src == o->sentry_ip) &&
                    !(port == o->listen_port && is_this_device(src))) {
                    int k, found = -1;
                    for (k = 0; k < napps; k++)
                        if (apps[k].ip == src && apps[k].port == port) found = k;
                    if (found < 0 && napps < MAX_APPS) {
                        char s[20];
                        ip_str(src, s, sizeof s);
                        logm("app '%s' announced at %s:%d; not sent (pass --dest)", app, s, port);
                        found = napps++;
                        apps[found].ip = src;
                        apps[found].port = port;
                        snprintf(apps[found].app, sizeof apps[found].app, "%s", app);
                    }
                    if (found >= 0) apps[found].seen = mono_now();
                }
            }
        }
        if (o->has_sentry && sb_stream_starts(&b) != checked_starts) {
            checked_starts = sb_stream_starts(&b);
            web_queue(1, 0);
        }
        if (o->auto_enable && o->has_sentry && sb_wants_enable(&b, now)) web_queue(0, 1);
        if (o->has_sentry) {
            int silent = last_rx < 0 || now - last_rx >= SILENT_AFTER_S;
            if ((last_contact < 0 || now - last_contact >= (silent ? SILENT_CONTACT_S : KEEPALIVE_S)) && web_contact())
                last_contact = now;
        }
        /* A shared input is bound to the address that reached the Sentry at start. While silent, follow a
           new one (this computer rejoined the Sentry's WiFi), as the GUIs do. */
        if (shared && now >= next_route && (last_rx < 0 || now - last_rx >= 5.0)) {
            uint32_t local = local_route(o->sentry_ip);
            next_route = now + 5.0;
            if (local && (local >> 24) != 127 && local != bind_ip) {
                char ip[20];
                sock_t replacement = udp_bind(local, o->listen_port, 1, err, sizeof err);
                ip_str(local, ip, sizeof ip);
                if (replacement == SOCK_BAD) {
                    logm("the route to the Sentry changed to %s, but UDP %d could not be bound there (%s)", ip, o->listen_port, err);
                } else {
                    closesock(rx);
                    rx = replacement;
                    bind_ip = local;
                    logm("the route to the Sentry changed: listening on %s:%d", ip, o->listen_port);
                }
            }
        }
        m = mono_now();
        if (m >= next_tick) {
            int k = 0;
            for (i = 0; i < napps; i++) {
                if (m - apps[i].seen > ANNOUNCE_TTL_S) {
                    char s[20];
                    ip_str(apps[i].ip, s, sizeof s);
                    logm("app at %s:%d stopped announcing, removed", s, apps[i].port);
                } else
                    apps[k++] = apps[i];
            }
            napps = k;
            sb_tick(&b, now, emit_all, &snd);
            next_tick = m + o->interval;
        }
        if (m >= next_stat) {
            char line[1400];
            sb_summary(&b, line, sizeof line);
            logm("%s noted=%d", line, napps);
            next_stat = m + o->stats_every;
        }
    }
    logm("stopped");
    closesock(rx);
    closesock(tx);
    if (ann != SOCK_BAD) closesock(ann);
    return 0;
}

#ifdef _WIN32
static BOOL WINAPI on_console(DWORD ev) { (void)ev; g_stop = 1; return TRUE; }
static int double_clicked(int argc) {
    DWORD ids[4];
    return argc == 1 && GetConsoleProcessList(ids, 4) == 1; /* console created just for us */
}
#endif

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--license")) {
        const char *const *line;
        for (line = SB_LICENSE_LINES; *line; ++line) fputs(*line, stdout);
        return 0;
    }
    options o;
    int rc, dc = 0;
    mutex_init(&g_log_lock);
#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) { fprintf(stderr, "WSAStartup failed\n"); return 2; }
    SetConsoleCtrlHandler(on_console, TRUE);
    dc = double_clicked(argc);
#endif
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    if (dc) {
        printf("%s\n", BANNER);
        rc = 0;
    } else {
        rc = parse_args(argc, argv, &o);
        if (rc == 1) return 0;
        if (rc == 0) rc = run(&o);
    }
#ifdef _WIN32
    if (dc) {
        printf("Press Enter to close this window.");
        fflush(stdout);
        getchar();
    }
    WSACleanup();
#endif
    return rc;
}
