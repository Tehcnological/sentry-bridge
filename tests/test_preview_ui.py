"""The prototype only serves selected public assets and validates proposed routes."""
import gzip
import http.client
import json
from pathlib import Path
import re
import sys
from threading import Thread
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import preview_ui


class TestPreviewUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), preview_ui.Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_public_assets_and_headers(self):
        for path in preview_ui.ASSETS:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, headers, body = self.request("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertFalse(body)
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_private_files_and_traversal_are_not_served(self):
        for path in ("/local-signing/", "/.local-tools/windows-environment.json", "/.git/config",
                     "/../local-signing/private.password", "/%2e%2e/local-signing/private.password",
                     "/tools/preview_ui.py", "/config/", "/web/", "/api/start"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_dns_rebinding_host_and_cross_origin_post_rejected(self):
        self.assertEqual(self.request("/", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/validate", "POST", "{}", {"Origin": "https://attacker.example", "Content-Type": "application/json"})[0], 403)

    def test_validation_api_never_applies_changes(self):
        request = dict(host="android", topology="same")
        status, _, body = self.request("/api/validate", "POST", json.dumps(request), {"Content-Type": "application/json"})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["routes"]), 1)
        self.assertFalse(result["appliedToBridge"])
        self.assertFalse(result["displayVerified"])

    def test_malformed_and_oversized_json_rejected(self):
        for body, content_type in (("{", "application/json"), ("[]", "application/json"),
                                   (" " * 8193, "application/json"), ("{}", "text/plain")):
            with self.subTest(body=body[:20]):
                self.assertEqual(self.request("/api/validate", "POST", body, {"Content-Type": content_type})[0], 400)

    def test_old_webviews_and_icon_notice(self):
        # Android API 26 WebViews may predate :has() (Chromium 105); one unsupported selector drops the whole rule.
        css = re.sub(r"/\*.*?\*/", "", (preview_ui.ROOT / "web/app.css").read_text(encoding="utf-8"), flags=re.S)
        self.assertNotIn(":has(", css)
        # Radios are invisible, so their span needs a focus ring without :focus-visible (Chromium 86).
        self.assertIn(".segmented input:focus + span {", css)
        for rule in re.findall(r"[^{}]*:focus-visible[^{}]*\{", css):
            self.assertNotIn(",", rule, "keep :focus-visible out of shared selector lists")
        # A hover rule that sets a tab background must skip the selected tab (contrast).
        for rule in re.findall(r"[^{}]*\.tabs button:hover[^{}]*\{[^}]*background", css):
            self.assertIn(":not([aria-selected=true])", rule)
        # The inline Phosphor glyph is MIT: its copyright and permission notice must travel with every copy.
        html = (preview_ui.ROOT / "web/app.html").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2023 Phosphor Icons", html)
        self.assertIn("The above copyright notice and this permission notice shall be included", html)

    def test_compatibility_links_reach_real_sections(self):
        # app.js links docs/Compatibility.md#<section> per platform; GitHub anchors are lowercased headings
        # with punctuation removed and spaces as hyphens.
        script = (preview_ui.ROOT / "web/app.js").read_text(encoding="utf-8")
        sections = set(re.findall(r'[a-z0-9]+:"([a-z0-9-]+)"', re.search(r"LIMITS_SECTION = \{(.*?)\}", script).group(1)))
        sections.add("all-platforms")  # the fallback for an unknown host
        doc = (preview_ui.ROOT / "docs/Compatibility.md").read_text(encoding="utf-8")
        anchors = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in re.findall(r"^## (.+)$", doc, re.M)}
        self.assertLessEqual(sections, anchors)
        html = (preview_ui.ROOT / "web/app.html").read_text(encoding="utf-8")
        for element in ('id="limits-dialog"', 'id="limits-hide"', 'class="setting-row link-row limits-link"'):
            self.assertIn(element, html)
        self.assertNotIn('id="limits-hide" checked', html)  # "Don't show again" starts unchecked

    def test_assets_meet_compressed_size_target(self):
        paths = {name for name, _ in preview_ui.ASSETS.values()}
        total = sum(len(gzip.compress((preview_ui.ROOT / path).read_bytes())) for path in paths)
        self.assertLess(total, 100 * 1024)

