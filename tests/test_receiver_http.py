"""Receiver HTTP from sentry_ctl.py and bridge.py ignores proxies and never follows redirects."""

import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bridge  # noqa: E402
import sentry_ctl  # noqa: E402


def serve(routes, hits):
    """A local HTTP server: path -> (status, headers, body). Every request path is recorded."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self):
            hits.append(self.path)
            if self.headers.get("Content-Length"):
                self.rfile.read(int(self.headers["Content-Length"]))
            status, headers, body = routes.get(self.path, (404, {}, b"nope"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TestReceiverHttp(unittest.TestCase):
    def setUp(self):
        self.elsewhere_hits, self.receiver_hits, self.proxy_hits = [], [], []
        self.elsewhere = serve({"/settings/?action=set": (200, {}, b"OK"),
                                "/settings/?action=get": (200, {}, b'{"Power":{"powerSavings":true}}'),
                                "/data/log.csv": (200, {}, b"a,b\n")}, self.elsewhere_hits)
        away = f"http://127.0.0.1:{self.elsewhere.server_port}"
        moved = {path: (302, {"Location": away + path}, b"")
                 for path in ("/settings/?action=set", "/settings/?action=get", "/data/log.csv")}
        self.receiver = serve({**moved, "/?action=get": (200, {}, b'{"device":"fake"}')}, self.receiver_hits)
        self.proxy = serve({}, self.proxy_hits)
        self.host = f"127.0.0.1:{self.receiver.server_port}"
        for server in (self.elsewhere, self.receiver, self.proxy):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def test_redirects_are_not_followed(self):
        self.assertEqual(sentry_ctl._http(self.host, "/settings/?action=set", {"publicGDL90": True})[0], 302)
        self.assertEqual(bridge.enable_public(self.host), (False, "HTTP 302"))
        self.assertIsNone(bridge.check_power_savings(self.host, lambda message: None))
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(urllib.error.HTTPError):
            sentry_ctl.fetch_log(self.host, "log.csv", Path(folder) / "log.csv", 1000)
        self.assertEqual(self.elsewhere_hits, [])
        self.assertEqual(len(self.receiver_hits), 4)

    def test_proxy_settings_are_ignored(self):
        proxy = f"http://127.0.0.1:{self.proxy.server_port}"
        environment = {"http_proxy": proxy, "HTTP_PROXY": proxy, "no_proxy": "", "NO_PROXY": ""}
        # A fresh default opener would read these settings and send every request to the proxy.
        with patch.dict(os.environ, environment), patch.object(urllib.request, "_opener", None):
            self.assertEqual(sentry_ctl._http(self.host, "/?action=get"), (200, '{"device":"fake"}'))
            self.assertEqual(bridge.enable_public(self.host), (False, "HTTP 302"))
        self.assertEqual(self.proxy_hits, [])
        self.assertEqual(len(self.receiver_hits), 2)


if __name__ == "__main__":
    unittest.main()
