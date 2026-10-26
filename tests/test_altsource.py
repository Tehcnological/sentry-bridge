"""The SideStore, AltStore and Feather source lists each release's IPA and matches the app's permissions."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import altsource


class TestAltSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "altsource.json"

    def bundle(self, tag, data):
        """A published bundle as release_bundle.py writes it, reduced to the IPA and its manifest."""
        bundle = Path(self.tmp.name) / tag
        bundle.mkdir()
        (bundle / "sentry-bridge.ipa").write_bytes(data)
        manifest = {"tag": tag, "assets": {"sentry-bridge.ipa": hashlib.sha256(data).hexdigest()}}
        (bundle / "release-manifest.json").write_text(json.dumps(manifest))
        return bundle

    def test_releases_are_listed_newest_first_with_their_ipa(self):
        altsource.update(self.bundle("v0.3.0", b"first"), "v0.3.0", "2026-10-21", self.path)
        source = altsource.update(self.bundle("v0.3.1", b"second build"), "v0.3.1", "2026-11-01", self.path)
        self.assertEqual(json.loads(self.path.read_text()), source)
        app = source["apps"][0]
        self.assertEqual([v["version"] for v in app["versions"]], ["0.3.1", "0.3.0"])
        newest = app["versions"][0]
        self.assertEqual(newest["downloadURL"], "https://github.com/Tehcnological/sentry-bridge/releases/download/v0.3.1/sentry-bridge.ipa")
        self.assertEqual((newest["size"], newest["sha256"]), (12, hashlib.sha256(b"second build").hexdigest()))
        self.assertEqual(newest["minOSVersion"], "26.0")
        # Older readers take the single-version fields.
        self.assertEqual((app["version"], app["downloadURL"], app["size"]), ("0.3.1", newest["downloadURL"], 12))
        self.assertEqual(app["bundleIdentifier"], "io.github.tehcnological.sentrybridge")
        self.assertIn("NSLocalNetworkUsageDescription", app["appPermissions"]["privacy"])
        self.assertEqual(app["appPermissions"]["entitlements"], ["com.apple.developer.usernotifications.time-sensitive"])

    def test_a_rebuilt_version_replaces_its_entry(self):
        altsource.update(self.bundle("v0.3.0", b"first"), "v0.3.0", "2026-10-21", self.path)
        (Path(self.tmp.name) / "again").mkdir()
        bundle = self.bundle("again/v0.3.0", b"rebuilt")
        (bundle / "release-manifest.json").write_text(json.dumps({"tag": "v0.3.0", "assets": {
            "sentry-bridge.ipa": hashlib.sha256(b"rebuilt").hexdigest()}}))
        source = altsource.update(bundle, "v0.3.0", "2026-10-22", self.path)
        self.assertEqual([v["size"] for v in source["apps"][0]["versions"]], [7])
        # Rebuilding an older release keeps the newest version first, for the apps and the older fields.
        altsource.update(self.bundle("v0.10.0", b"newest"), "v0.10.0", "2026-11-01", self.path)
        (Path(self.tmp.name) / "older").mkdir()
        older = self.bundle("older/v0.3.0", b"rebuilt again")
        (older / "release-manifest.json").write_text(json.dumps({"tag": "v0.3.0", "assets": {
            "sentry-bridge.ipa": hashlib.sha256(b"rebuilt again").hexdigest()}}))
        app = altsource.update(older, "v0.3.0", "2026-11-02", self.path)["apps"][0]
        self.assertEqual([v["version"] for v in app["versions"]], ["0.10.0", "0.3.0"])
        self.assertEqual(app["version"], "0.10.0")
        order = ["0.10.0", "0.3.0", "0.3.0-rc.1", "0.3.0-beta.10", "0.3.0-beta.2", "0.3.0-beta", "0.3.0-alpha.1", "0.2.9"]
        self.assertEqual(sorted(reversed(order), key=altsource.version_key, reverse=True), order)

    def test_wrong_bundle_or_changed_permissions_are_rejected(self):
        bundle = self.bundle("v0.3.0", b"first")
        (bundle / "sentry-bridge.ipa").write_bytes(b"changed after publishing")
        with self.assertRaisesRegex(ValueError, "differs from the release manifest"):
            altsource.update(bundle, "v0.3.0", "2026-10-21", self.path)
        with self.assertRaisesRegex(ValueError, "this tag"):
            altsource.update(self.bundle("v0.3.1", b"x"), "v0.3.2", "2026-10-21", self.path)
        source = altsource.update(self.bundle("v0.3.3", b"ok"), "v0.3.3", "2026-10-21", self.path)
        source["apps"][0]["appPermissions"]["privacy"]["NSLocalNetworkUsageDescription"] = "older wording"
        with self.assertRaisesRegex(ValueError, "Info.plist permissions"):
            altsource.check(source)
        source = altsource.update(self.bundle("v0.3.4", b"next"), "v0.3.4", "2026-10-22", self.path)
        source["apps"][0]["versions"].reverse()
        with self.assertRaisesRegex(ValueError, "newest first"):
            altsource.check(source)

    def test_published_source_matches_the_app(self):
        if not altsource.SOURCE.exists():
            return  # added after the first release with an IPA (a skip would block release preparation)
        altsource.check(json.loads(altsource.SOURCE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
