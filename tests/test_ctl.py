"""Control tool against a fake Sentry (HTTP settings API + UDP stream that changes with publicGDL90)."""

import io
import json
import os
import random
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gdl90  # noqa: E402
import sentry_ctl  # noqa: E402
import sim  # noqa: E402


class FakeSentry:
    def __init__(self, udp_port):
        self.public = False
        self.settings = {"wifi": {"ssid": "Sentry-TEST"}, "led": {"brightness": 37}, "coAlarmLevel": 50}
        self.udp_port = udp_port
        self.stop = threading.Event()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/?action=get":
                    return self._reply(200, json.dumps({"version": "1.0.32", "clientCount": 1}))
                if self.path == "/settings/?action=get":
                    return self._reply(200, json.dumps(fake.settings))
                self._reply(404, "not found")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path != "/settings/?action=set":
                    return self._reply(404, "not found")
                try:
                    d = json.loads(body)
                except ValueError:
                    return self._reply(400, "JSON parse failed")
                v = d.get("publicGDL90")
                if isinstance(v, (bool, int)):   # strings are ignored, as observed on real units
                    fake.public = bool(v)
                self._reply(200, "Update successful")

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.http_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self._udp, daemon=True).start()

    def _udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ss = sim.SimSentry()
        rng = random.Random(3)
        t = 0
        while not self.stop.is_set():
            if self.public:
                dgs = ss.datagrams(t)
            else:
                enc = bytes((0x26,)) + bytes(rng.randrange(256) for _ in range(48))
                dgs = [ss.status + gdl90.frame(enc)]
            for d in dgs:
                s.sendto(d, ("127.0.0.1", self.udp_port))
            t += 1
            time.sleep(0.25)
        s.close()

    def close(self):
        self.stop.set()
        self.http.shutdown()
        self.http.server_close()
        time.sleep(0.3)


class TestCtl(unittest.TestCase):
    def setUp(self):
        p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        p.bind(("127.0.0.1", 0))
        self.udp = p.getsockname()[1]
        p.close()
        self.fake = FakeSentry(self.udp)
        self.host = f"127.0.0.1:{self.fake.http_port}"
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.fake.close()

    def ctl(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sentry_ctl.main(["--host", self.host, "--port", str(self.udp), "--outdir", self.tmp, *args])
        return rc, buf.getvalue()

    def test_full_cycle(self):
        rc, out = self.ctl("check", "--seconds", "1.5")
        self.assertEqual(rc, 0, out)
        self.assertIn("ENCRYPTED", out)

        rc, out = self.ctl("enable", "--verify-seconds", "1.5")
        self.assertEqual(rc, 0, out)
        self.assertIn("PUBLIC", out)
        self.assertTrue(self.fake.public)
        backups = os.listdir(self.tmp)
        self.assertTrue(any(b.startswith("sentry-settings-") for b in backups), backups)

        rc, out = self.ctl("disable", "--verify-seconds", "1.5")
        self.assertEqual(rc, 0, out)
        self.assertIn("ENCRYPTED", out)
        self.assertFalse(self.fake.public)

    def test_status_and_backup(self):
        rc, out = self.ctl("status")
        self.assertEqual(rc, 0)
        self.assertIn("1.0.32", out)
        rc, out = self.ctl("backup")
        self.assertEqual(rc, 0)
        saved = json.loads(next(Path(self.tmp).glob("sentry-settings-*.json")).read_text())
        self.assertEqual(saved["led"]["brightness"], 37)


if __name__ == "__main__":
    unittest.main()
