"""ws_probe against a minimal fake Sentry WebSocket server (stdlib only)."""

import base64
import hashlib
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ws_probe  # noqa: E402


def _frame(op, payload, fin=True):
    n = len(payload)
    b0 = (0x80 if fin else 0) | op
    if n < 126:
        return struct.pack("!BB", b0, n) + payload
    return struct.pack("!BBH", b0, 126, n) + payload


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ValueError("closed")
        buf += chunk
    return buf


def _read_client_frame(conn):
    b0, b1 = _recv_exact(conn, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", _recv_exact(conn, 2))[0]
    mask = _recv_exact(conn, 4)
    data = _recv_exact(conn, n) if n else b""
    assert b1 & 0x80, "client frames must be masked"
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


class FakeSentry(threading.Thread):
    def __init__(self, refuse=False, slow=False):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.refuse = refuse
        self.slow = slow
        self.client_frames = []

    def run(self):
        conn, _ = self.srv.accept()
        self.srv.close()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(1024)
        if self.refuse:
            conn.sendall(b'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Digest qop="auth"\r\n\r\n')
            conn.close()
            return
        key = [l.split(":", 1)[1].strip() for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1((key + ws_probe.GUID).encode()).digest()).decode()
        status = {"status": {"gpsFix": 3, "gpsSatellites": 11, "latitude": 400000000, "longitude": -1000000000,
                             "baroAltitude": 3953, "coPpm": 0, "battery": 87, "pitch": 1.5,
                             "pad": "x" * 200}}
        msg1 = json.dumps(status).encode()          # > 125 bytes: exercises 16-bit length
        status["status"]["pitch"] = 12.0
        msg2 = json.dumps(status).encode()
        out = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               b"Sec-WebSocket-Accept: " + acc.encode() + b"\r\n\r\n")
        out += _frame(0x1, msg1)                    # sent in the same packet as the handshake
        out += _frame(0x9, b"hi")                   # ping
        out += _frame(0x1, msg2[:50], fin=False) + _frame(0x0, msg2[50:])   # fragmented
        out += _frame(0x2, b"\x01\x02\x03")         # binary
        out += _frame(0x8, struct.pack("!H", 1000))
        if self.slow:  # split mid-frame across the reader's 1 s timeout
            cut = out.index(b"\r\n\r\n") + 4 + 3
            conn.sendall(out[:cut])
            import time
            time.sleep(1.6)
            conn.sendall(out[cut:])
        else:
            conn.sendall(out)
        try:
            while True:
                op, data = _read_client_frame(conn)
                self.client_frames.append((op, data))
                if op == 0x8:
                    break
        except (OSError, ValueError):
            pass
        conn.close()


class TestProbe(unittest.TestCase):
    def test_collects_all_fields(self):
        srv = FakeSentry()
        srv.start()
        summ = ws_probe.collect("127.0.0.1", srv.port, "/", seconds=5)
        srv.join(timeout=5)
        self.assertEqual(summ.messages, 2)
        self.assertEqual(summ.non_json, 1)
        f = summ.fields
        self.assertIn("status.battery", f)
        self.assertEqual((f["status.pitch"]["min"], f["status.pitch"]["max"]), (1.5, 12.0))
        self.assertEqual(len(f["status.gpsFix"]["values"]), 1)
        self.assertIn((0xA, b"hi"), srv.client_frames)          # answered the ping
        self.assertEqual(srv.client_frames[-1][0], 0x8)         # closed cleanly
        rep = summ.report()
        self.assertIn("status.battery", rep)
        self.assertIn("<==", rep)

    def test_timeout_mid_frame_loses_nothing(self):
        srv = FakeSentry(slow=True)
        srv.start()
        summ = ws_probe.collect("127.0.0.1", srv.port, "/", seconds=6)
        srv.join(timeout=5)
        self.assertEqual(summ.messages, 2)
        self.assertEqual(summ.fields["status.gpsSatellites"]["last"], 11)

    def test_message_and_recording_limits(self):
        # A message made of fragments that together pass 1 MiB is refused, as on Android.
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        client.settimeout(5)   # fail instead of waiting for more fragments forever
        chunk = b"x" * 60000
        frames = _frame(0x1, chunk, fin=False) + b"".join(_frame(0x0, chunk, fin=False) for _ in range(17))
        sender = threading.Thread(target=lambda: server.sendall(frames), daemon=True)
        sender.start()
        with self.assertRaisesRegex(ws_probe.WSError, "exceeds 1 MiB"):
            ws_probe.Reader(client).message()
        # The recording stops with an error at its size limit instead of growing without bound.
        srv = FakeSentry()
        srv.start()
        with tempfile.TemporaryDirectory() as folder, patch.object(ws_probe, "RECORDING_LIMIT", 300):
            out = Path(folder) / "ws.jsonl"
            with self.assertRaisesRegex(ws_probe.WSError, "recording reached"):
                ws_probe.collect("127.0.0.1", srv.port, "/", seconds=5, out_path=str(out))
            self.assertLessEqual(out.stat().st_size, 300)
        srv.join(timeout=5)

    def test_sensor_values_match_shared_cases(self):
        # The same cases run on Android and iOS, so every platform shows the same readings.
        cases = json.loads((Path(__file__).parent / "data" / "stats-cases.json").read_text(encoding="utf-8"))
        for case in cases:
            self.assertEqual(ws_probe.sensor_values(case["message"]), case["expected"], case["message"])

    def test_refused_handshake_reports_headers(self):
        srv = FakeSentry(refuse=True)
        srv.start()
        with self.assertRaises(ws_probe.WSError) as cm:
            ws_probe.collect("127.0.0.1", srv.port, "/", seconds=2)
        self.assertIn("401", str(cm.exception))
        self.assertIn("Digest", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
