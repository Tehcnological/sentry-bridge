"""GUI-owned UDP transport. One socket owner for forwarding, mode checks and captures."""
import base64
from collections import deque
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import re
import select
import socket
import threading
import time

import gdl90
from tools.native_core import decoder
from tools.discovery import ANNOUNCE_TTL_S, parse_announce
from tools.routes import check_endpoint
from sentry_ctl import keepalive_due

DEFAULTS = dict(sentryIp="192.168.4.1", listenPort=4000, efbPort=4000, httpPort=80,
                autoRestore=True, relay=True, updateCheck=True, announcePort=63093, ownshipHex="",
                reference=None, staleSeconds=15.0, intervalSeconds=1.0, statsSeconds=10.0)
CAPTURE_LIMIT = 32 * 1024 * 1024
DISCOVERY_LIMIT = 32  # EFBs kept for Find EFB, the same bound as discovered forwarding destinations
# GDL90 heartbeats arrive at 1 Hz. Other frames (FIS-B uplinks, traffic) do not carry GPS status,
# so a fix is shown only while heartbeats are current.
HEARTBEAT_STALE_S = 3.0
SHARED_SILENCE_NOTE_S = 15.0  # a shared input that hears nothing this long gets one explanation


def number(value, lo, hi, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number.")
    if not lo <= value <= hi or (integer and not isinstance(value, int)):
        raise ValueError(f"{name} must be between {lo} and {hi}.")
    return value


def validate_settings(request):
    if not isinstance(request, dict) or set(request) - set(DEFAULTS):
        raise ValueError("Unknown receiver setting.")
    s = {**DEFAULTS, **request}
    if not isinstance(s["sentryIp"], str):
        raise ValueError("Receiver address must be IPv4, or blank to accept any source.")
    if s["sentryIp"]:
        ip = ipaddress.IPv4Address(s["sentryIp"])
        if ip.is_multicast or ip.is_unspecified or ip.is_reserved or int(ip) == 0xffffffff:
            raise ValueError("Use a receiver's unicast IPv4 address.")
    for key in ("listenPort", "efbPort", "httpPort"):
        number(s[key], 1, 65535, key, True)
    if s["announcePort"] != 0:
        number(s["announcePort"], 1, 65535, "Discovery port", True)
    elif isinstance(s["announcePort"], bool):
        raise ValueError("Discovery port must be a number.")
    if s["announcePort"] == s["listenPort"]:
        raise ValueError("Discovery and receiver ports must differ.")
    for key in ("autoRestore", "relay", "updateCheck"):
        if not isinstance(s[key], bool):
            raise ValueError(f"{key} must be on or off.")
    if not isinstance(s["ownshipHex"], str) or not re.fullmatch(r"(?:[a-fA-F0-9]{6})?", s["ownshipHex"]):
        raise ValueError("Ownship ICAO must be blank or six hex digits.")
    s["ownshipHex"] = s["ownshipHex"].upper()
    if s["reference"] is not None:
        ref = s["reference"]
        if not isinstance(ref, list) or len(ref) != 2:
            raise ValueError("Reference needs latitude and longitude, or leave both blank.")
        number(ref[0], -90, 90, "Latitude")
        number(ref[1], -180, 180, "Longitude")
    for key, lo, hi in (("staleSeconds", 1, 60), ("intervalSeconds", .1, 10), ("statsSeconds", 1, 600)):
        number(s[key], lo, hi, key)
    return s


def receiver_route(receiver, port):
    """The local address the OS uses to reach the receiver. Sends nothing."""
    if not receiver:
        raise ValueError(f"Set the Receiver IP in Settings: sharing UDP {port} with an EFB uses the address that reaches it.")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((receiver, port))
            local = probe.getsockname()[0]
        except OSError:
            local = "0.0.0.0"
    if local == "0.0.0.0":
        raise ValueError(f"Join the receiver's WiFi first: sharing UDP {port} uses the address that reaches it.")
    if ipaddress.IPv4Address(local).is_loopback:
        raise ValueError(f"A receiver on this device cannot share UDP {port} with an EFB. Set another EFB port in Settings.")
    return local


def udp_listener(port, receiver=None):
    """Bind UDP port. With receiver, bind only the local address that reaches it: an EFB on this
    device keeps its own 0.0.0.0 listener on the same port, the receiver's unicast reaches the
    bridge (the more specific bind) and loopback output reaches the EFB. Windows keeps
    SO_EXCLUSIVEADDRUSE, which still allows that pair and stops another socket taking the
    bridge's address. Linux and macOS need SO_REUSEADDR; on Linux the EFB must set it too."""
    address = "0.0.0.0" if receiver is None else receiver_route(receiver, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif receiver is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
    except OSError as error:
        sock.close()
        if receiver is not None:
            raise ValueError(f"UDP {port} is held by an app that does not share it. Close that app, or set another "
                             "EFB port in Settings and the same port in the EFB.") from error
        raise ValueError(f"UDP {port} is unavailable. Close another bridge or change the EFB input port.") from error
    return sock


class Runtime:
    def __init__(self, settings=None, on_enable=None, on_stream=None, on_contact=None):
        self.settings = validate_settings(settings or {})
        self.on_enable = on_enable or (lambda: None)
        self.on_stream = on_stream or (lambda: None)
        self.on_contact = on_contact or (lambda: None)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.rx = self.ann = None
        self.forwarding = False
        self.recording = None
        self.logs = deque(maxlen=120)
        self.discovered = {}  # (address, port) -> (app label, monotonic time last heard)
        self.view = dict(mode="UNKNOWN", datagrams=0, frames={}, lastPacketSeconds=None,
                         gps=False, traffic=0, weather=0, engine="—", error=None, apps=0)

    def log(self, text):
        with self.lock:
            self.logs.append(time.strftime("%H:%M:%S ") + text)

    def snapshot(self):
        with self.lock:
            v = deepcopy(self.view)
            age = None if self.last_rx is None else max(0, time.monotonic() - self.last_rx)
            v.update(lastPacketSeconds=round(age, 1) if age is not None else None,
                     running=self.forwarding and self.active(), monitoring=self.active(),
                     recording=self.recording is not None, logs=list(self.logs))
            # A last-known fix or mode is not current after receiver loss.
            v["state"] = ("stopped" if not v["running"] else "waiting" if age is None else
                          "lost" if age > 10 else "mode" if v["mode"] == "ENCRYPTED" else "running")
            if age is None or age > 10:
                v["gps"], v["traffic"] = False, 0
            heartbeat = getattr(self, "last_heartbeat", None)
            heartbeat_age = None if heartbeat is None else max(0, time.monotonic() - heartbeat)
            v["heartbeatSeconds"] = None if heartbeat_age is None else round(heartbeat_age, 1)
            if heartbeat_age is None or heartbeat_age > HEARTBEAT_STALE_S:
                v["gps"] = False
            return v

    last_rx = None
    share_input = False  # set by the controller from the saved route, for receiver tools
    shared = False

    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, routes=(), forwarding=True):
        with self.lock:
            if self.active():
                raise ValueError("Stop the current bridge or capture first.")
            # Receiver tools (no routes) bind like the saved route, so they work beside a sharing EFB.
            shared = any(route.get("shared") for route in routes) if routes else self.share_input
            for route in routes:
                if not route.get("shared"):
                    check_endpoint(route["address"], route["port"], self.settings["listenPort"])
            self.rx = udp_listener(self.settings["listenPort"], self.settings["sentryIp"] if shared else None)
            self.shared, self.silence_noted = shared, False
            self.started_at = self.next_route_check = time.monotonic()
            try:
                self.core, engine = decoder(self.settings, self.log)
                self.ann = udp_listener(self.settings["announcePort"]) if forwarding and self.settings["announcePort"] else None
            except Exception:
                self.rx.close()
                raise
            self.routes = list(dict.fromkeys((r["address"], r["port"]) for r in routes))
            self.discovered = {}  # announcements from an earlier run or network are stale
            self.forwarding = forwarding
            self.last_rx = None
            self.last_heartbeat = None
            self.gps = False
            self.view.update(mode="UNKNOWN", datagrams=0, frames={}, gps=False, traffic=0,
                             weather=0, engine=engine, error=None, apps=0)
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="gui-udp", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.active():
            raise ValueError("Bridge is still stopping; try again.")
        self.forwarding = False

    def begin_capture(self, path, seconds=0):
        number(seconds, 0, 3600, "Capture duration")
        if not self.active():
            self.start(forwarding=False)
        with self.lock:
            if self.recording is not None:
                raise ValueError("A capture is already running.")
            self.recording = (path.open("w", encoding="utf-8"), time.monotonic() + seconds if seconds else None)
            self.capture_only = not self.forwarding

    def end_capture(self):
        with self.lock:
            if self.recording:
                try:
                    self.recording[0].close()
                except OSError as error:
                    self.view["error"] = f"Recording could not be saved: {error}"
                    self.log(self.view["error"])
                self.recording = None
        if not self.forwarding:
            self.stop()

    def _run(self):
        next_tick = next_stat = time.monotonic()
        starts = 0
        enable_pending = False
        last_fault = None
        self.last_contact = None
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def send(frames, _now):
            if not self.forwarding:
                return
            for route in self.routes:
                for frame in frames:
                    try:
                        tx.sendto(frame, route)
                    except OSError as error:
                        self.log(f"Output {route[0]}:{route[1]} failed: {error}")
                        break

        def guarded(stage, *args):
            """Run one loop stage. A fault in one stage (a bad datagram, a decoder error) is logged
            and skipped so the other stages, including the tick that sends traffic, keep running.
            Socket errors still end the loop. Returns (ok, result)."""
            nonlocal last_fault
            try:
                return True, stage(*args)
            except OSError:
                raise
            except Exception as error:
                if last_fault is None or now - last_fault > 10:
                    last_fault = now
                    with self.lock:
                        self.view["error"] = f"Skipped a packet: {error}"
                    self.log(f"Skipped a packet that could not be processed: {error}")
                return False, None

        try:
            while not self.stop_event.is_set():
                ready, _, _ = select.select([s for s in (self.rx, self.ann) if s], [], [], .1)
                now = time.monotonic()
                guarded(self._receive, ready, now, send)
                guarded(self._announce, ready, now)
                ok, wanted = guarded(self._wants_enable, now)
                if ok and wanted:
                    enable_pending = True
                if enable_pending:
                    ok, handled = guarded(self._try_enable)
                    if ok and handled:
                        enable_pending = False
                guarded(self._contact, now)
                guarded(self._shared_input, now)
                ok, done = guarded(self._capture_done, now)
                if ok and done:
                    break
                ok, result = guarded(self._stream_check, starts)
                if ok:
                    starts = result
                ok, result = guarded(self._periodic, now, next_tick, next_stat, send)
                if ok:
                    next_tick, next_stat = result
        except Exception as error:
            with self.lock:
                self.view["error"] = str(error)
            self.log(f"Bridge stopped: {error}")
        finally:
            # Release every socket even if saving the recording fails, so Start can bind again.
            with self.lock:
                if self.recording:
                    out, self.recording = self.recording[0], None
                    try:
                        out.close()
                    except OSError as error:
                        self.view["error"] = f"Recording could not be saved: {error}"
                        self.logs.append(time.strftime("%H:%M:%S ") + self.view["error"])
            for sock in (tx, self.rx, self.ann):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self.forwarding = False

    def _stream_check(self, starts):
        if self.forwarding and self.core.stream_starts != starts:
            starts = self.core.stream_starts
            self.on_stream()
        return starts

    def _receive(self, ready, now, send):
        if self.rx not in ready:
            return
        data, (source, _) = self.rx.recvfrom(65535)
        if self.settings["sentryIp"] and source != self.settings["sentryIp"]:
            return
        self.last_rx = now
        # Only parse datagrams that can hold a heartbeat (ID 0 is never escaped, so it follows a
        # 7E flag); C does the decoding. Only a complete CRC-valid heartbeat (7 bytes, as in
        # core/sbcore.c) refreshes GPS; undersized 0x00 frames do not.
        if b"\x7e\x00" in data:
            for frame in gdl90.parse(data):
                if frame.crc_ok and frame.msg_id == 0 and len(frame.msg) >= 7:
                    self.gps = gdl90.decode_heartbeat(frame.msg)["gps_pos_valid"]
                    self.last_heartbeat = now
        with self.lock:
            if self.recording:
                out, end = self.recording
                row = json.dumps(dict(t=round(time.time(), 4), src=source, hex=data.hex())) + "\n"
                try:
                    if out.tell() + len(row) > CAPTURE_LIMIT or (end and now >= end):
                        out.close()
                        self.recording = None
                        self.log("Capture stopped at its duration or 32 MiB limit.")
                    else:
                        out.write(row)
                except OSError as error:
                    self.recording = None
                    try:
                        out.close()
                    except OSError:
                        pass
                    self.view["error"] = f"Recording stopped: {error}"
                    self.log(self.view["error"])
        send(self.core.feed(data, source, now), now)

    def _announce(self, ready, now):
        if self.ann not in ready:
            return
        data, (source, _) = self.ann.recvfrom(4096)
        app, port = parse_announce(data)
        if not port or source == self.settings["sentryIp"]:
            return
        try:
            check_endpoint(source, port, self.settings["listenPort"])
        except ValueError:
            return
        # Find EFB may offer any valid other-device destination, including a remote port 4000.
        # Announcements are not destinations. The pilot presses Use, or passes --dest.
        self.remember_efb(source, port, app, now)

    def remember_efb(self, source, port, app, now):
        """Find EFB's list: bounded like forwarding destinations and pruned as announcements expire."""
        with self.lock:
            for key in [key for key, (_, seen) in self.discovered.items() if now - seen > ANNOUNCE_TTL_S]:
                del self.discovered[key]
            if (source, port) in self.discovered or len(self.discovered) < DISCOVERY_LIMIT:
                self.discovered[(source, port)] = (app, now)

    def _wants_enable(self, now):
        return self.forwarding and self.settings["autoRestore"] and self.core.wants_enable(now)

    def _contact(self, now):
        if (self.forwarding and self.settings["sentryIp"] and keepalive_due(now, self.last_contact, self.last_rx)
                and self.on_contact() is not False):
            self.last_contact = now

    def _shared_input(self, now):
        """A shared input is bound to the address that reached the receiver at Start. While silent,
        follow a changed route (the computer joined the receiver's WiFi after Start), and say once
        why a shared input can hear nothing: the receiver must send to this address."""
        if not self.shared or (self.last_rx is not None and now - self.last_rx < 5) or now < self.next_route_check:
            return
        self.next_route_check = now + 5
        port, receiver = self.settings["listenPort"], self.settings["sentryIp"]
        try:
            local = receiver_route(receiver, port)
        except ValueError:
            return  # no usable route yet; check again later
        if local != self.rx.getsockname()[0]:
            try:
                replacement = udp_listener(port, receiver)
            except ValueError as error:
                self.log(f"The route to the receiver changed to {local}, but {error}")
                return
            old, self.rx = self.rx, replacement
            old.close()
            self.log(f"The route to the receiver changed: listening on {local}:{port}.")
        elif self.last_rx is None and not self.silence_noted and now - self.started_at >= SHARED_SILENCE_NOTE_S:
            self.silence_noted = True
            self.log(f"No receiver data on {local}:{port}. Sharing the port needs the receiver to send to this "
                     "address; if it broadcasts, set another EFB port in Settings and in the EFB.")

    def _try_enable(self):
        """The core allows a few attempts per episode. Keep one that arrives while the GUI is busy
        (on_enable returns False) instead of spending it; drop it once the receiver is public."""
        return self.core.mode != "ENCRYPTED" or self.on_enable() is not False

    def _capture_done(self, now):
        with self.lock:
            if self.recording and self.recording[1] and now >= self.recording[1]:
                try:
                    self.recording[0].close()
                except OSError as error:
                    self.view["error"] = f"Recording could not be saved: {error}"
                    self.log(self.view["error"])
                self.recording = None
            return not self.forwarding and not self.recording and getattr(self, "capture_only", False)

    def _periodic(self, now, next_tick, next_stat, send):
        if now >= next_tick:
            frames = self.core.tick(now)
            send(frames, now)
            with self.lock:
                stats = self.core.stats
                counts = dict(stats.frames) if isinstance(stats.frames, dict) else {
                    i: count for i, count in enumerate(stats.frames) if count}
                self.view.update(mode=self.core.mode if self.core.mode != "?" else "UNKNOWN",
                                 frames={str(k): v for k, v in counts.items()}, datagrams=stats.datagrams,
                                 traffic=len(frames), weather=counts.get(7, 0), apps=0,
                                 **{key: getattr(stats, key) for key in ("crc_bad", "uat_msgs", "uat_fixed", "uat_bad")})
                # Heartbeat has the GPS-valid bit; avoid exposing the native struct layout.
                self.view["gps"] = getattr(self, "gps", False)
            next_tick = now + self.settings["intervalSeconds"]
        if now >= next_stat:
            self.log(f"{self.view['mode']}; {self.view['datagrams']} packets; {self.view['traffic']} targets")
            next_stat = now + self.settings["statsSeconds"]
        return next_tick, next_stat


class Simulator:
    """Loopback-only UDP + HTTP receiver for offline GUI testing, never an EFB emulator."""
    def __init__(self, port, gps=True, uat=True, weather=True):
        import sim
        self.sim = sim.SimSentry(tracks=sim.default_tracks() + (sim.uat_tracks() if uat else []),
                                 gps_fix=gps, fisb=weather)
        self.public = False
        self.port = port
        self.stop_event = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data, ctype="application/json"):
                body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/?action=get":
                    self.reply(200, {"device": "Simulator", "firmware": "simulated"})
                elif self.path == "/settings/?action=get":
                    # Like real hardware, the settings JSON does not expose publicGDL90.
                    self.reply(200, {"Power": {"powerSavings": True}})
                elif self.path == "/data/":
                    self.reply(200, '<a href="simulator.csv">simulator.csv</a>', "text/html")
                elif self.path == "/data/simulator.csv":
                    self.reply(200, "time,battery\n0,100\n", "text/csv")
                elif self.path == "/stats" and self.headers.get("Upgrade", "").lower() == "websocket":
                    self.statistics()
                else:
                    self.reply(404, {})

            def statistics(self):
                """The receiver's statistics WebSocket, twice a second, shaped like firmware 1.0.32."""
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                 b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                self.close_connection = True
                try:
                    while not owner.stop_event.is_set():
                        body = json.dumps({"CO_SENSOR_PPM": 0, "batteryCapacity_%": 80, "CO_SENSOR_STATE": 1,
                                           "sensor": {"ats": {"temp_c": 21.5}, "baro": {"temp_c": 27.0}}}).encode()
                        self.wfile.write(bytes([0x81, 126]) + len(body).to_bytes(2, "big") + body)
                        self.wfile.flush()
                        owner.stop_event.wait(.5)
                except OSError:
                    pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if self.path == "/settings/?action=set" and isinstance(body.get("publicGDL90"), bool):
                    owner.public = body["publicGDL90"]
                    self.reply(200, "OK", "text/plain")
                else:
                    self.reply(400, {})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.http_thread.start()
        self.thread.start()

    def _run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            start = time.monotonic()
            while not self.stop_event.is_set():
                frames = self.sim.datagrams(time.monotonic() - start) if self.public else [gdl90.frame(bytes([0x26]) + bytes(19))]
                for frame in frames:
                    tx.sendto(frame, ("127.0.0.1", self.port))
                self.stop_event.wait(.5)

    def close(self):
        self.stop_event.set()
        self.thread.join(2)
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
