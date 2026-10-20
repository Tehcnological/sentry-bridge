#!/usr/bin/env python3
"""Listen to the Sentry's WebSocket status stream and report every field it carries.

The Sentry's own status page (http://192.168.4.1/) opens ws://192.168.4.1 and receives
JSON once a second, but shows only GPS fix, satellites, position, altitudes, NIC and
NACp. This records everything the stream sends, to find out whether CO, battery,
temperature, baro or attitude are in there too, even in compatibility mode.

Read-only: it sends nothing but the WebSocket handshake, pong replies and a close.
That is the same as having the status page open in a browser.

    python3 ws_probe.py                      # 60 s, saves ws-status.jsonl
    python3 ws_probe.py --seconds 180 --show # also print every message

Tip: hold the Sentry still for 20 s, then pitch it, then roll it, so any attitude
fields show up as changing values in the summary.
Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import socket
import struct
import sys
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MESSAGE_LIMIT = 1024 * 1024           # per message, fragments included; the same limits as Android
RECORDING_LIMIT = 32 * 1024 * 1024    # per recording file
INTERESTING = re.compile(r"co|carbon|batt|volt|charg|temp|pitch|roll|bank|yaw|head|ahrs|att|"
                         r"press|baro|accel|gyro|mag|alarm|power|slip|turn|g_?load", re.I)


class WSError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WSError("connection closed")
        buf += chunk
    return buf


def _send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """Client frames must be masked."""
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def connect(host: str, port: int = 80, path: str = "/", timeout: float = 5.0) -> tuple[socket.socket, bytes]:
    """Open the WebSocket. Returns the socket and any frame bytes that arrived with the handshake."""
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        return sock, _handshake(sock, host, path)
    except BaseException:
        sock.close()
        raise


def _handshake(sock: socket.socket, host: str, path: str) -> bytes:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: http://{host}\r\n\r\n")
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(1024)
        if not chunk:
            raise WSError(f"closed during handshake, got: {resp[:300]!r}")
        resp += chunk
        if len(resp) > 16384:
            raise WSError("handshake response too large")
    head, _, rest = resp.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    if " 101 " not in lines[0] + " ":
        raise WSError("handshake refused:\n  " + "\n  ".join(lines))
    want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    headers = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
    if headers.get("sec-websocket-accept") != want:
        raise WSError("bad Sec-WebSocket-Accept from server")
    return rest


class Reader:
    """Frame reader. Only whole frames are consumed from the buffer, and fragment state
    lives on the object, so a socket timeout at any point loses nothing."""

    def __init__(self, sock: socket.socket, pending: bytes = b""):
        self.sock = sock
        self.buf = pending
        self.parts: list[bytes] = []
        self.first_op: int | None = None

    def _fill(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self.buf += chunk

    def _frame(self) -> tuple[bool, int, bytes]:
        self._fill(2)
        b0, b1 = self.buf[0], self.buf[1]
        n, hl = b1 & 0x7F, 2
        if n == 126:
            self._fill(4)
            n, hl = struct.unpack_from("!H", self.buf, 2)[0], 4
        elif n == 127:
            self._fill(10)
            n, hl = struct.unpack_from("!Q", self.buf, 2)[0], 10
        if n + sum(map(len, self.parts)) > MESSAGE_LIMIT:
            raise WSError(f"WebSocket message exceeds {MESSAGE_LIMIT // (1024 * 1024)} MiB")
        ml = 4 if b1 & 0x80 else 0
        self._fill(hl + ml + n)
        mask = self.buf[hl:hl + ml]
        data = self.buf[hl + ml:hl + ml + n]
        self.buf = self.buf[hl + ml + n:]
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return bool(b0 & 0x80), b0 & 0x0F, data

    def message(self) -> tuple[int, bytes] | None:
        """Next complete data message as (opcode, payload); None when the server closes."""
        while True:
            fin, op, data = self._frame()
            if op == 0x9:              # ping
                _send_frame(self.sock, 0xA, data)
                continue
            if op == 0xA:              # pong
                continue
            if op == 0x8:              # close
                try:
                    _send_frame(self.sock, 0x8, data[:2])
                except OSError:
                    pass
                return None
            if op in (0x1, 0x2):
                self.first_op, self.parts = op, [data]
            elif op == 0x0 and self.first_op is not None:
                self.parts.append(data)
            if fin and self.first_op is not None:
                out = (self.first_op, b"".join(self.parts))
                self.first_op, self.parts = None, []
                return out


def flatten(obj, prefix: str = "") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


# Sensor values the bridge shows from the Sentry's /stats WebSocket. Matched on the end of the flattened
# path, so the nesting may change; the first listed suffix with a plausible number wins. Android and iOS
# apply the same table (tests/data/stats-cases.json keeps them in step).
SENSORS = (("batteryPercent", ("batterycapacity_%",), 0, 100, 0),
           ("coPpm", ("co_sensor_ppm",), 0, 2000, 0),
           ("temperatureC", ("ats.temp_c", "baro.temp_c"), -40, 85, 1))  # the baro sensor reads the warm board


def sensor_values(obj) -> dict:
    """Plausible battery, CO and temperature readings in a /stats message; absent ones are left out."""
    flat = {key.lower(): value for key, value in flatten(obj).items()}
    out = {}
    for name, suffixes, low, high, digits in SENSORS:
        for suffix in suffixes:
            found = [v for k, v in flat.items() if (k == suffix or k.endswith("." + suffix))
                     and isinstance(v, (int, float)) and not isinstance(v, bool) and low <= v <= high]
            if found:
                scale = 10 ** digits  # half up, as Kotlin and Swift round
                value = math.floor(found[0] * scale + .5)
                out[name] = value / scale if digits else int(value)
                break
    return out


class Summary:
    def __init__(self):
        self.fields: dict[str, dict] = {}
        self.messages = 0
        self.non_json = 0

    def add(self, flat: dict) -> None:
        self.messages += 1
        for k, v in flat.items():
            f = self.fields.setdefault(k, {"n": 0, "first": v, "last": v, "min": None, "max": None,
                                           "values": set(), "type": type(v).__name__})
            f["n"] += 1
            f["last"] = v
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                f["min"] = v if f["min"] is None else min(f["min"], v)
                f["max"] = v if f["max"] is None else max(f["max"], v)
            if len(f["values"]) < 1000:
                f["values"].add(json.dumps(v))

    def report(self) -> str:
        lines = [f"messages: {self.messages}   non-JSON: {self.non_json}   distinct fields: {len(self.fields)}", ""]
        lines.append(f"{'field':40} {'type':6} {'n':>5} {'distinct':>8}  first -> last   [min .. max]")
        for k in sorted(self.fields):
            f = self.fields[k]
            rng = f"  [{f['min']} .. {f['max']}]" if f["min"] is not None else ""
            flag = "  <==" if INTERESTING.search(k) else ""
            lines.append(f"{k:40} {f['type']:6} {f['n']:5d} {len(f['values']):8d}  "
                         f"{json.dumps(f['first'])} -> {json.dumps(f['last'])}{rng}{flag}")
        lines.append("")
        lines.append("<== marks names that look like CO, battery, temperature, baro or attitude.")
        lines.append("Fields with 'distinct' > 1 changed during the run.")
        return "\n".join(lines)


def collect(host: str, port: int, path: str, seconds: float, out_path: str | None = None,
            show: bool = False) -> Summary:
    sock, pending = connect(host, port, path)
    sock.settimeout(1.0)
    reader = Reader(sock, pending)
    summ = Summary()
    out = open(out_path, "w") if out_path else None
    end = time.monotonic() + seconds
    recorded = 0
    try:
        while time.monotonic() < end:
            try:
                msg = reader.message()
            except socket.timeout:
                continue
            if msg is None:
                print("server closed the connection", file=sys.stderr)
                break
            op, data = msg
            t = time.time()
            try:
                obj = json.loads(data.decode("utf-8"))
                summ.add(flatten(obj))
                rec = {"t": round(t, 3), "json": obj}
            except (UnicodeDecodeError, ValueError):
                summ.non_json += 1
                rec = {"t": round(t, 3), "op": op, "hex": data.hex()}
            if out:
                row = json.dumps(rec) + "\n"
                recorded += len(row)
                if recorded > RECORDING_LIMIT:
                    raise WSError(f"WebSocket recording reached {RECORDING_LIMIT // (1024 * 1024)} MiB")
                out.write(row)
                out.flush()
            if show:
                print(json.dumps(rec))
    finally:
        try:
            _send_frame(sock, 0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        sock.close()
        if out:
            out.close()
    return summ


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.4.1")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--path", default="/")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("-o", "--out", default="ws-status.jsonl")
    ap.add_argument("--report", default="ws-report.txt")
    ap.add_argument("--show", action="store_true", help="print every message as it arrives")
    a = ap.parse_args(argv)
    print(f"Listening to ws://{a.host}:{a.port}{a.path} for {a.seconds:.0f} s ...", file=sys.stderr)
    try:
        summ = collect(a.host, a.port, a.path, a.seconds, a.out, a.show)
    except (OSError, WSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    rep = summ.report()
    print(rep)
    if a.report:
        with open(a.report, "w") as f:
            f.write(rep + "\n")
        print(f"\nSaved {a.out} and {a.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
