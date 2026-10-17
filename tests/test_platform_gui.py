"""Network control boundaries and the standalone diagnostic worker."""
import base64
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from tools.desktop_app import tool_worker
from tools.pi_server import PiController, PiHandler
from tools import gui_server

gui_server.RELEASES = "http://127.0.0.1:9/"  # update checks in tests never reach GitHub


class PiGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = PiController(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PiHandler)
        self.server.controller = self.controller
        self.server.credentials = "bridge:local-test-password-only"
        self.server.allowed_hosts = {f"sentrybridge.local:{self.server.server_port}"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = next(iter(self.server.allowed_hosts))
        self.auth = "Basic " + base64.b64encode(self.server.credentials.encode()).decode()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.controller.close(); self.temp.cleanup()

    def request(self, method, path, body=None, *, auth=True, host=None, origin=None):
        headers = {"Host": host or self.host, "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = self.auth
        if origin:
            headers["Origin"] = origin
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        status, content = response.status, response.read()
        connection.close()
        return status, json.loads(content) if content else None

    def test_password_protects_assets_status_and_writes(self):
        for method, path, body in [("GET", "/", None), ("GET", "/api/status", None), ("POST", "/api/actions/stop", {})]:
            self.assertEqual(self.request(method, path, body, auth=False)[0], 401)

    def test_password_refusal_survives_a_late_request_body(self):
        # The body arrives after the 401 decision; the client must still receive the 401.
        port = self.server.server_port
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall((f"POST /api/actions/stop HTTP/1.1\r\nHost: {self.host}\r\n"
                            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n").encode())
            time.sleep(.3)
            client.sendall(b"{}")
            time.sleep(.2)
            self.assertIn(b" 401 ", client.recv(4096).split(b"\r\n", 1)[0])

    def test_non_ascii_authorization_is_refused_not_dropped(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("GET", "/api/status", skip_host=True)
        connection.putheader("Host", self.host)
        connection.putheader("Authorization", "Basic \u00e9t\u00e9")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        connection.close()

    def test_pi_status_has_actual_host_and_saved_settings(self):
        code, status = self.request("GET", "/api/status")
        self.assertEqual(code, 200); self.assertEqual(status["host"], "pi")
        self.assertFalse(status["running"])

    def test_foreign_host_and_origin_cannot_change_settings(self):
        self.assertEqual(self.request("POST", "/api/actions/settings", {}, host="evil.local")[0], 403)
        self.assertEqual(self.request("POST", "/api/actions/settings", {}, origin="http://evil.local")[0], 403)
        self.assertEqual(self.request("POST", "/api/validate", {}, origin="http://evil.local")[0], 403)

    def test_same_origin_can_save_settings(self):
        code, result = self.request("POST", "/api/actions/settings", {"ownshipHex": "ABC123"}, origin="http://" + self.host)
        self.assertEqual(code, 200); self.assertTrue(result["saved"])
        self.assertEqual(self.controller.runtime.settings["ownshipHex"], "ABC123")

    def test_files_require_password(self):
        token, path = self.controller.artifact("capture.jsonl", "capture")
        path.write_text("flight-private-data")
        self.assertEqual(self.request("GET", "/api/files/" + token, auth=False)[0], 401)

    def test_web_service_cannot_be_quit_or_feed_itself(self):
        self.assertEqual(self.request("POST", "/api/actions/quit", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/actions/start", {"topology": "same"})[0], 400)

    def test_boot_preference_survives_service_restart(self):
        code, result = self.request("POST", "/api/actions/autostart", {"enabled": False})
        self.assertEqual(code, 200); self.assertTrue(result["saved"])
        other = PiController(self.temp.name)
        try:
            self.assertFalse(other.auto_start)
            self.assertFalse(other.status()["running"])
        finally:
            other.close()

    def test_boot_starts_saved_network_routes_without_a_browser(self):
        from unittest.mock import patch
        from tools.gui_runtime import Runtime
        self.controller.routes_request = dict(host="pi", topology="network", address="192.168.4.2")
        self.controller.save()
        with patch.object(Runtime, "start") as start:
            other = PiController(self.temp.name)
            try:
                self.assertEqual(start.call_args.args[0][0]["address"], "192.168.4.2")
            finally:
                other.close()


class FrozenToolTests(unittest.TestCase):
    def test_discovery_retains_reference_protocol_behavior(self):
        from tools.discovery import parse_announce
        import bridge
        for value in (b'{"App":"Avare","GDL90":{"port":4001}}', b'{"GDL90":{"port":65536}}', b'not-json', b'[]', b'\xff',
                      b'{"GDL90":{"port":true}}'):
            self.assertEqual(parse_announce(value), bridge.parse_announce(value))
        self.assertEqual(parse_announce(b'{"GDL90":{"port":true}}'), ("", None))

    def test_worker_runs_capture_without_python_script_files(self):
        with tempfile.TemporaryDirectory() as folder:
            result = Path(folder) / "result.json"
            self.assertEqual(tool_worker("capture.py", result, ["--help"]), 0)
            record = json.loads(result.read_text())
            self.assertIn("analyze", record["text"])

    def test_worker_rejects_arbitrary_executables(self):
        with self.assertRaises(ValueError):
            tool_worker("untrusted.py", "unused", [])


if __name__ == "__main__":
    unittest.main()
