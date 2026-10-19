"""The standalone diagnostic worker of the packaged desktop app."""
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
from tools import gui_server

gui_server.RELEASES = "http://127.0.0.1:9/"  # update checks in tests never reach GitHub


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
