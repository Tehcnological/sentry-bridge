#ifndef BRIDGE_IO_H
#define BRIDGE_IO_H
#include <stdint.h>
#include <stddef.h>
typedef struct sbio sbio;
typedef struct {
    uint64_t received, forwarded, send_errors, rejected_sources;
    uint32_t traffic, weather, heartbeat, crc_bad;
    int gps_valid, mode;
    double age, elapsed, max_gap;
    char detail[256];
} sbio_status;
/* Serialized worker API. Strict exclusive bind; IPv4 unicast only. */
sbio *sbio_open(const char *source_ip, uint16_t input_port,
                const char *destination_ip, uint16_t output_port,
                char *error, size_t error_cap);
/* share_input: opt-in same-device mode for an EFB that listens on the receiver port. Binds input to
   the local address that routes to the receiver (SO_REUSEADDR); destination must be loopback. */
sbio *sbio_open_shared(const char *source_ip, uint16_t input_port,
                       const char *destination_ip, uint16_t output_port, int share_input,
                       char *error, size_t error_cap);
/* Loopback-only sample input; no output socket, decoded frames are never sent. */
sbio *sbio_open_demo(uint16_t input_port, char *error, size_t error_cap);
int sbio_step(sbio *io, int timeout_ms);
/* Owning worker only, shared input: when the address routed to the receiver changed (new DHCP
   lease) and is on its network, rebind in place, keeping counters and decoder state.
   0 unchanged or not shared, 1 rebound, -1 rebind failed (old socket kept). */
int sbio_refresh_shared_input(sbio *io);
void sbio_snapshot(sbio *io, sbio_status *status);
void sbio_close(sbio *io);
double sbio_now(void);
typedef void (*sbio_packet_observer)(void *, const uint8_t *, size_t, uint32_t);
/* Called on the owning worker, before receiver-source filtering. */
void sbio_observe(sbio *io, sbio_packet_observer observer, void *context);
int sbio_efb_candidate(const char *address, uint16_t port, const char *receiver, uint16_t input_port);
/* Owning worker only: true when the core schedules a Compatibility Mode request (the Sentry
   came up encrypted after a gap; at most 3 per episode, 20 s apart). */
int sbio_wants_enable(sbio *io);
/* Cancellation for receiver HTTP requests: create per worker, set on Stop from any thread,
   destroy only after every request using it has returned. */
typedef struct sbio_cancel sbio_cancel;
sbio_cancel *sbio_cancel_create(void);
void sbio_cancel_set(sbio_cancel *cancel);
void sbio_cancel_destroy(sbio_cancel *cancel);
#define SBIO_HTTP_CANCELLED (-2)
/* Any thread; touches no sbio state. POSTs the core's publicGDL90 request to the receiver's
   HTTP server without proxies or redirects. timeout_ms bounds the whole request (connect,
   write and status line); a set cancel stops it within about 50 ms, or before it connects.
   Returns the HTTP status, -1 with error set, or SBIO_HTTP_CANCELLED. cancel may be NULL. */
int sbio_request_compatibility(const char *receiver_ip, uint16_t http_port, int timeout_ms,
                               sbio_cancel *cancel, char *error, size_t error_cap);
/* Read-only GET of the receiver status page; same contract. A Sentry stops streaming to a WiFi
   client that sends it nothing for a while (observed after ~16 min in a background session);
   this request keeps the client active and restores the stream. */
int sbio_request_status(const char *receiver_ip, uint16_t http_port, int timeout_ms,
                        sbio_cancel *cancel, char *error, size_t error_cap);
/* Owning worker only: send to another EFB address from now on (following a moved EFB), keeping the
   output port, counters and decoder state. 0 changed, -1 refused (address kept): not a valid
   unicast destination, or this route shares its input with an EFB on this device. */
int sbio_set_destination(sbio *io, const char *destination_ip);
/* Any thread. The local IPv4 address the system routes to receiver_ip (no datagram is sent), as a
   dotted string. 0 found, -1 no route. */
int sbio_route_local(const char *receiver_ip, uint16_t port, char *out, size_t cap);
/* Any thread. One TCP connection attempt to port per address, up to 128 at once, each bounded by
   timeout_ms; a connection that is accepted or refused means a device answered (results[i] = 1),
   anything else (a timeout, no route, Local Network access denied) means none (0). Nothing is sent
   on an accepted connection; it is closed at once. Returns 0, SBIO_HTTP_CANCELLED when cancel was
   set (within about 50 ms; results then incomplete), or -1 when polling failed. */
int sbio_probe(const char *const *addresses, size_t count, uint16_t port, int timeout_ms,
               sbio_cancel *cancel, uint8_t *results);
/* Read-only WebSocket client for the receiver's statistics stream (ws://receiver_ip:port/path), the
   same as the receiver's own statistics page: the handshake, pong replies and a close. Any one
   thread per connection. */
typedef struct sbio_ws sbio_ws;
/* Connect within connect_ms, then the handshake within 10 s. NULL with error set, or with error
   "Cancelled" when cancel was set (checked at least every 50 ms). */
sbio_ws *sbio_ws_open(const char *receiver_ip, uint16_t port, const char *path, int connect_ms,
                      sbio_cancel *cancel, char *error, size_t error_cap);
/* The next complete text or binary message: 1 with *data and *length set (valid until the next
   call), 0 when the receiver closed the stream, -1 with error set (including no message for
   silence_s, counted from the handshake or the previous message: pings and partial frames do not
   count), or SBIO_HTTP_CANCELLED. Messages are limited to 1 MiB, fragments included. */
int sbio_ws_message(sbio_ws *ws, double silence_s, sbio_cancel *cancel, const uint8_t **data,
                    size_t *length, char *error, size_t error_cap);
/* send_close: one non-blocking attempt to send a normal close frame first. */
void sbio_ws_close(sbio_ws *ws, int send_close);
#endif
