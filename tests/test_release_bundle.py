"""Release gates reject stale sources, missing downloads, and altered artifacts."""
import hashlib
import json
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import release_bundle as rb

ROOT = Path(__file__).resolve().parent.parent
IOS_EXPORT = {
    "ios/SentryBridge/BridgeIO.c", "ios/SentryBridge/BridgeIO.h",
    "ios/SentryBridge/Bridge-Bridging-Header.h", "ios/SentryBridge/BridgeModel.swift",
    "ios/SentryBridge/BridgeView.swift", "ios/SentryBridge/BridgeWorker.swift",
    "ios/SentryBridge/EFBDiscovery.swift", "ios/SentryBridge/ReceiverReadings.swift", "ios/SentryBridge/BridgeIntents.swift",
    "ios/SentryBridge/SentryBridgeApp.swift", "ios/SentryBridge/Info.plist",
    "ios/SentryBridge/PrivacyInfo.xcprivacy", "ios/SentryBridge/DemoReplayer.swift", "ios/SentryBridge/Licenses.swift", "ios/SentryBridge/DemoReceiver.bin", "ios/SentryBridge/AppIcon.icon/icon.json", "ios/SentryBridge/AppIcon.icon/Assets/2-rings.svg",
    "ios/SentryBridge/AppIcon.icon/Assets/3-signal.svg", "ios/SentryBridge/AppIcon.icon/Assets/4-aircraft.svg",
    "ios/tests/BridgeUITests.swift", "ios/tests/WorkerSmoke.swift", "ios/tests/test_ios_adapter.py",
    "ios/SentryBridge.xcodeproj/project.pbxproj",
    "ios/SentryBridge.xcodeproj/xcshareddata/xcschemes/SentryBridge.xcscheme",
    "ios/Multicast.entitlements.example", "config/android-simulator.json",
}
PRIVATE_IOS_FILES = {
    "ios/build/generated.swift", "ios/DerivedData/Build/generated.h",
    "ios/.build/generated.c", "ios/.swiftpm/cache.swift", "ios/.cache/cache.py",
    "ios/tests/__pycache__/cached.py", "ios/SentryBridge.xcodeproj/xcuserdata/private.xcscheme",
    "ios/SentryBridge.xcodeproj/xcuserdata/private.pbxproj", "ios/private.xcuserstate",
    "ios/SentryBridge/Signing.entitlements", "ios/SentryBridge/Signing.plist",
    "ios/local-signing/private.p12", "ios/private.p8", "ios/private.mobileprovision",
    "ios/private.provisionprofile", "ios/private.cer", "ios/private.password",
    "ios/captures/session.pcap", "ios/captures/session.txt", "ios/session.jsonl",
    "ios/logs/session.txt", "ios/session.log", "ios/device-report.txt",
    "config/private.json", "config/capture.json", "shared/efb-presets.json",
    # Supported source suffixes must not bypass directory exclusions on existing platforms.
    "android/app/build/generated.kt", "tools/DerivedData/private.txt",
    "tools/local-signing/private.txt", "tests/captures/private.py", "tools/logs/private.txt",
}


class TestReleaseBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "core").mkdir()
        (self.root / "android/app").mkdir(parents=True)
        (self.root / "core/sbcore.h").write_text('#define SB_VERSION "0.1.3"\n')
        (self.root / "android/app/build.gradle.kts").write_text('versionName = "0.1.3"\n')
        (self.root / "android/version-code.txt").write_text('35\n')
        for name in ("LICENSE", "NOTICE.txt"):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        self.dist = self.root / "dist"
        self.dist.mkdir()
        for name in rb.ASSETS:
            (self.dist / name).write_bytes(name.encode())
        self.write_ipa()
        self.tests = {"passed": True, "testsRun": 94, "skipped": [], "failures": 0, "errors": 0,
                      "smokePassed": True, "controlSmokePassed": True, "fuzzIterations": 100000,
                      "sanitizer": "MSVC AddressSanitizer", "sources": rb.source_snapshot(self.root),
                      "controlSha256": rb.sha256(self.dist / "sentry-ctl.exe"),
                      "guiSmokePassed": True, "guiSha256": rb.sha256(self.dist / "sentry-bridge-gui.exe")}
        self.apk = {"versionName": "0.1.3", "versionCode": 35, "signerSha256": rb.CERTIFICATE,
                    "checkedAbis": ["arm64-v8a", "armeabi-v7a", "x86_64"],
                    "sources": rb.source_snapshot(self.root), "apkSha256": rb.sha256(self.dist / "sentry-bridge.apk")}
        self.write_reports()

    def test_checkout_with_converted_line_endings_cannot_prepare(self):
        def git(*arguments):
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            *arguments], check=True, capture_output=True)
        header = self.root / "core/sbcore.h"
        header.write_bytes(b'#define SB_VERSION "0.1.3"\n')
        git("init", "-q")
        git("config", "core.autocrlf", "false")
        git("add", "core/sbcore.h")
        git("commit", "-q", "-m", "fixture")
        self.assertEqual(rb.converted_sources(self.root), [])
        rb.check_checkout(self.root)
        # A checkout that converts to CRLF: Git calls the file unchanged, but its bytes differ.
        git("config", "core.autocrlf", "true")
        header.unlink()
        git("checkout", "--", "core/sbcore.h")
        self.assertEqual(header.read_bytes(), b'#define SB_VERSION "0.1.3"\r\n')
        self.assertEqual(rb.converted_sources(self.root), ["core/sbcore.h"])
        with self.assertRaisesRegex(ValueError, "line endings"):
            rb.prepare(self.root, "v0.1.3")
        # A real edit is not mistaken for conversion; the source gates handle it.
        header.write_bytes(b'#define SB_VERSION "0.1.3" /* edited */\r\n')
        self.assertEqual(rb.converted_sources(self.root), [])

    @staticmethod
    def binary(entitlements):
        """A stand-in executable whose ad-hoc signature holds this entitlements plist (None: no blob)."""
        if entitlements is None:
            return b"\xcf\xfa\xed\xfe unsigned"
        blob = plistlib.dumps(entitlements)
        return b"\xcf\xfa\xed\xfe header" + b"\xfa\xde\x71\x71" + (len(blob) + 8).to_bytes(4, "big") + blob + b"tail"

    def write_ipa(self, version="0.1.3", extra=(), flavor="Sideload", report_flavor="Sideload", entitlements="project"):
        if entitlements == "project":
            entitlements = plistlib.loads(rb.IPA_ENTITLEMENTS.read_bytes())
        """An IPA as tools/build_ios_ipa.py writes it, with its report."""
        ipa = self.dist / "sentry-bridge.ipa"
        info = {"CFBundleShortVersionString": version, "SBBuildFlavor": flavor,
                "UIBackgroundModes": rb.FLAVOR_BACKGROUND_MODES.get(flavor, [])}
        with zipfile.ZipFile(ipa, "w") as archive:
            archive.writestr("Payload/SentryBridge.app/Info.plist", plistlib.dumps(info))
            archive.writestr("Payload/SentryBridge.app/SentryBridge", self.binary(entitlements))
            for name in extra:
                archive.writestr(name, b"")
        report = {"versionName": version, "ipaSha256": rb.sha256(ipa), "sources": rb.ipa_sources(rb.source_snapshot(self.root))}
        if report_flavor:
            report["flavor"] = report_flavor
        (self.dist / "ios-build-report.json").write_text(json.dumps(report))

    def test_ipa_must_match_its_report_sources_version_and_be_unsigned(self):
        (self.dist / "sentry-bridge.ipa").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "IPA changed"):
            rb.prepare(self.root, "v0.1.3")
        self.write_ipa()
        (self.root / "core/sbcore.h").write_text('#define SB_VERSION "0.1.3"\n/* edited after the IPA build */\n')
        self.tests["sources"] = self.apk["sources"] = rb.source_snapshot(self.root)
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "other iOS, core or license sources"):
            rb.prepare(self.root, "v0.1.3")
        # The app bundles the license texts too: a stale copy in the IPA is rejected.
        self.write_ipa()
        (self.root / "NOTICE.txt").write_text("changed after the IPA build\n")
        self.tests["sources"] = self.apk["sources"] = rb.source_snapshot(self.root)
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "other iOS, core or license sources"):
            rb.prepare(self.root, "v0.1.3")
        self.assertEqual(set(rb.ipa_sources(rb.source_snapshot(self.root))), {"core/sbcore.h", "LICENSE", "NOTICE.txt"})
        self.write_ipa(version="0.1.2")
        with self.assertRaisesRegex(ValueError, "IPA version"):
            rb.prepare(self.root, "v0.1.3")
        # Not signed for a team; the ad-hoc signature declares exactly the project's entitlements.
        self.write_ipa(extra=["Payload/SentryBridge.app/embedded.mobileprovision"])
        with self.assertRaisesRegex(ValueError, "signed for a team"):
            rb.prepare(self.root, "v0.1.3")
        for entitlements in (None, {}, {"com.apple.developer.usernotifications.time-sensitive": True, "get-task-allow": True}):
            self.write_ipa(entitlements=entitlements)
            with self.assertRaisesRegex(ValueError, "exactly the entitlements"):
                rb.prepare(self.root, "v0.1.3")
        self.write_ipa(extra=["Payload/SentryBridge.app/_CodeSignature/CodeResources"])
        rb.prepare(self.root, "v0.1.3")  # the ad-hoc signature's resource seal is fine
        # Only the Sideload build is released: not Lite, and not a report from before flavors existed.
        for flavor, report_flavor in (("Lite", "Lite"), ("Lite", "Sideload"), ("Sideload", None)):
            self.write_ipa(flavor=flavor, report_flavor=report_flavor)
            with self.assertRaisesRegex(ValueError, "Sideload build"):
                rb.prepare(self.root, "v0.1.3")
        self.write_ipa()
        bundle = rb.prepare(self.root, "v0.1.3")
        self.assertIn("sentry-bridge.ipa", (bundle / "SHA256SUMS").read_text())
        self.assertNotIn("sentry-bridge.exe", rb.DISTRIBUTION_ASSETS)  # the console bridge is not released

    def test_ipa_pins_every_file_the_xcode_project_uses_outside_ios(self):
        project = (ROOT / "ios/SentryBridge.xcodeproj/project.pbxproj").read_text(encoding="utf-8")
        outside = {path for path in re.findall(r'path = "?\.\./([^";]+)', project) if path}
        self.assertTrue(outside)
        for path in outside:
            self.assertTrue(path.startswith(rb.IPA_SOURCES) or path in rb.IPA_FILES, path)

    def write_reports(self):
        (self.dist / "test-report.json").write_text(json.dumps(self.tests))
        (self.dist / "android-build-report.json").write_text(json.dumps(self.apk))

    def test_firmware_is_not_distributed_yet(self):
        # Owner decision: firmware is source-only until it is tested on boards. Shipping it means
        # adding firmware/notices/Third-Party.txt and the newlib notices to the release assets.
        publish = (ROOT / "tools/Publish-Release.ps1").read_text(encoding="utf-8")
        for name in ("sentry-bridge-esp32c3.bin", "sentry-bridge-pico-w.uf2", "sentry-bridge-pico2-w.uf2"):
            self.assertNotIn(name, rb.DISTRIBUTION_ASSETS)
            self.assertNotIn(name, publish)

    def add_ios_export(self):
        for name in IOS_EXPORT:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exercise the reviewed repository files, including the real project and scheme.
            path.write_bytes((ROOT / name).read_bytes())
        self.tests["sources"] = rb.source_snapshot(self.root)
        self.apk["sources"] = rb.source_snapshot(self.root)
        self.write_reports()
        self.write_ipa()  # built from the same sources

    def remote_tree(self):
        return {"tree": [{"path": p, "sha": s, "type": "blob",
                          "mode": "100755" if p in rb.EXECUTABLES else "100644"}
                         for p, s in rb.source_snapshot(self.root).items()]}

    def test_reviewed_ios_files_are_in_public_snapshot(self):
        self.add_ios_export()
        sources = rb.source_snapshot(self.root)
        self.assertTrue(IOS_EXPORT <= sources.keys())
        for name in IOS_EXPORT:
            with self.subTest(path=name):
                data = (ROOT / name).read_bytes()
                self.assertEqual(sources[name], hashlib.sha1(
                    f"blob {len(data)}\0".encode() + data).hexdigest())
        bundle = rb.prepare(self.root, "v0.1.3")
        self.assertEqual(rb.verify(bundle, self.remote_tree())["sources"], sources)

    def test_private_and_generated_ios_files_are_excluded_locally_and_remotely(self):
        self.add_ios_export()
        before = rb.source_snapshot(self.root)
        for name in PRIVATE_IOS_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private fixture")
        self.assertEqual(rb.source_snapshot(self.root), before)
        bundle = rb.prepare(self.root, "v0.1.3")
        tree = self.remote_tree()
        tree["tree"].extend({"path": name, "sha": "0" * 40, "type": "blob"}
                            for name in PRIVATE_IOS_FILES)
        self.assertEqual(rb.verify(bundle, tree)["sources"], before)

    def test_ios_export_changes_invalidate_local_release_reports(self):
        self.add_ios_export()
        for name in IOS_EXPORT:
            with self.subTest(path=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b"\nchanged\n")
                with self.assertRaisesRegex(ValueError, "Sources changed since the tests"):
                    rb.prepare(self.root, "v0.1.3")
                # Android's source gate still rejects a stale build after tests are refreshed.
                self.tests["sources"] = rb.source_snapshot(self.root)
                self.write_reports()
                with self.assertRaisesRegex(ValueError, "Sources changed since the APK build"):
                    rb.prepare(self.root, "v0.1.3")
                path.write_bytes(original)
                self.tests["sources"] = rb.source_snapshot(self.root)
                self.write_reports()

    def test_remote_ios_export_must_be_complete_and_match_built_sources(self):
        self.add_ios_export()
        bundle = rb.prepare(self.root, "v0.1.3")
        for name in IOS_EXPORT:
            for mutation in ("missing", "changed"):
                with self.subTest(path=name, mutation=mutation):
                    tree = self.remote_tree()
                    if mutation == "missing":
                        tree["tree"] = [e for e in tree["tree"] if e["path"] != name]
                    else:
                        next(e for e in tree["tree"] if e["path"] == name)["sha"] = "0" * 40
                    with self.assertRaisesRegex(ValueError, "target GitHub commit"):
                        rb.verify(bundle, tree)

    def test_complete_bundle_and_matching_commit(self):
        bundle = rb.prepare(self.root, "v0.1.3")
        tree = {"tree": [{"path": p, "sha": s, "type": "blob"} for p, s in rb.source_snapshot(self.root).items()]}
        self.assertEqual(rb.verify(bundle, tree)["versionCode"], 35)
        self.assertFalse(rb.verify(bundle)["hardwareValidated"])

    def test_skip_is_a_release_failure(self):
        self.tests["skipped"] = [{"test": "JNI", "reason": "no compiler"}]
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "no skips"):
            rb.prepare(self.root, "v0.1.3")

    def test_sources_changed_since_tests(self):
        (self.root / "core/sbcore.h").write_text('#define SB_VERSION "0.1.3"\n// changed\n')
        with self.assertRaisesRegex(ValueError, "Sources changed"):
            rb.prepare(self.root, "v0.1.3")

    def test_unverified_control_tool_is_rejected(self):
        self.tests["controlSmokePassed"] = False
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "smoke"):
            rb.prepare(self.root, "v0.1.3")

    def test_unverified_or_changed_gui_is_rejected(self):
        self.tests["guiSmokePassed"] = False
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "GUI smoke"):
            rb.prepare(self.root, "v0.1.3")
        self.tests["guiSmokePassed"] = True
        self.write_reports()
        (self.dist / "sentry-bridge-gui.exe").write_bytes(b"rebuilt after testing")
        with self.assertRaisesRegex(ValueError, "changed since their tests"):
            rb.prepare(self.root, "v0.1.3")

    def test_missing_asset_is_rejected(self):
        (self.dist / "sentry-ctl.exe").unlink()
        with self.assertRaisesRegex(ValueError, "Missing release asset"):
            rb.prepare(self.root, "v0.1.3")

    def test_apk_tampering_is_rejected(self):
        (self.dist / "sentry-bridge.apk").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "APK changed"):
            rb.prepare(self.root, "v0.1.3")

    def test_bundle_tampering_and_wrong_commit_are_rejected(self):
        bundle = rb.prepare(self.root, "v0.1.3")
        with self.assertRaisesRegex(ValueError, "target GitHub commit"):
            rb.verify(bundle, {"tree": []})
        (bundle / "sentry-bridge-gui.exe").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "asset changed"):
            rb.verify(bundle)

    def test_signing_files_and_captures_are_never_in_source_manifest(self):
        (self.root / "local-signing").mkdir()
        (self.root / "local-signing/private.password").write_text("secret")
        (self.root / "tools").mkdir()
        (self.root / "tools/private.p12").write_bytes(b"secret")
        (self.root / "android/app/build").mkdir()
        (self.root / "android/app/build/generated.txt").write_text("private")
        self.assertEqual(set(rb.source_snapshot(self.root)), {"LICENSE", "NOTICE.txt", "core/sbcore.h", "android/app/build.gradle.kts", "android/version-code.txt"})

    def test_renamed_bundle_cannot_publish_as_another_version(self):
        bundle = rb.prepare(self.root, "v0.1.3")
        with self.assertRaisesRegex(ValueError, "requested tag"):
            rb.verify(bundle, tag="v0.1.4")

    def test_only_named_public_web_and_catalog_assets_are_selected(self):
        for name in rb.PUBLIC_ASSETS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public")
        (self.root / "config/private.json").write_text("secret")
        (self.root / "web/capture.json").write_text("private")
        (self.root / "web/private.password").write_text("secret")
        sources = rb.source_snapshot(self.root)
        self.assertTrue(set(rb.PUBLIC_ASSETS) <= sources.keys())
        self.assertNotIn("config/private.json", sources)
        self.assertNotIn("web/capture.json", sources)
        self.assertNotIn("web/private.password", sources)

    def test_public_assets_are_part_of_release_source_gate(self):
        path = self.root / rb.PUBLIC_ASSETS[0]
        path.parent.mkdir()
        path.write_text("public")
        self.tests["sources"] = rb.source_snapshot(self.root)
        self.apk["sources"] = rb.source_snapshot(self.root)
        self.write_reports()
        bundle = rb.prepare(self.root, "v0.1.3")
        remote = {"tree": [{"path": p, "sha": s, "type": "blob"} for p, s in rb.source_snapshot(self.root).items()]}
        self.assertEqual(rb.verify(bundle, remote)["versionCode"], 35)
        path.write_text("edited")
        with self.assertRaisesRegex(ValueError, "Sources changed"):
            rb.prepare(self.root, "v0.1.3")

    def test_manifest_requires_all_validation(self):
        bundle = rb.prepare(self.root, "v0.1.3")
        manifest = rb.load(bundle / "release-manifest.json")
        manifest["validation"]["controlSmokePassed"] = False
        (bundle / "release-manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "passing validation"):
            rb.verify(bundle)

    def test_source_archive_carries_exact_validated_bytes_and_licenses(self):
        import zipfile
        bundle = rb.prepare(self.root, "v0.1.3")
        manifest = rb.verify(bundle, self.remote_tree())
        self.assertEqual(set(manifest["assets"]), set(rb.DISTRIBUTION_ASSETS))
        with zipfile.ZipFile(bundle / "sentry-bridge-source.zip") as archive:
            self.assertEqual(set(archive.namelist()), set(manifest["sources"]))
            for info in archive.infolist():
                self.assertEqual(archive.read(info), (self.root / info.filename).read_bytes())
                self.assertEqual((info.create_system, info.external_attr >> 16),
                                 (3, 0o100755 if info.filename in rb.EXECUTABLES else 0o100644))
        tree = self.remote_tree()
        rb.verify(bundle, tree)
        next(e for e in tree["tree"] if e["path"] == "core/sbcore.h")["mode"] = "100755"
        with self.assertRaisesRegex(ValueError, "EXECUTABLES"):
            rb.verify(bundle, tree)
        for name in ("LICENSE", "NOTICE.txt"):
            self.assertEqual((bundle / name).read_bytes(), (self.root / name).read_bytes())

    def test_source_content_checked_even_when_archive_hash_is_updated(self):
        import zipfile
        bundle = rb.prepare(self.root, "v0.1.3")
        path = bundle / "sentry-bridge-source.zip"
        with zipfile.ZipFile(path) as archive:
            original = [(info, archive.read(info)) for info in archive.infolist()]
        # Changed bytes, then the same bytes written with zipfile's default (DOS, no mode) metadata.
        for changed, error in ((True, "Source archive differs"), (False, "file mode differs")):
            with self.subTest(error=error):
                with zipfile.ZipFile(path, "w") as archive:
                    for info, data in original:
                        if changed:
                            archive.writestr(info, b"changed" if info.filename == "core/sbcore.h" else data)
                        else:
                            archive.writestr(info.filename, data)
                manifest = json.loads((bundle / "release-manifest.json").read_text())
                manifest["assets"][path.name] = rb.sha256(path)
                (bundle / "release-manifest.json").write_text(json.dumps(manifest))
                (bundle / "SHA256SUMS").write_text("".join(
                    f"{manifest['assets'][name]}  {name}\n" for name in rb.DISTRIBUTION_ASSETS))
                with self.assertRaisesRegex(ValueError, error):
                    rb.verify(bundle)
