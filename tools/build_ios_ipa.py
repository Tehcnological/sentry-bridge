"""Build the unsigned iOS release app (SideStore, AltStore, Feather or a paid developer account sign it
when it is installed). macOS with Xcode only. Output: dist/sentry-bridge.ipa and
dist/ios-build-report.json, which release_bundle.py checks before it adds the IPA to a release.

No restricted entitlements are requested. Installation and background behavior after re-signing still
require physical-device validation.
"""
from pathlib import Path
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.release_bundle import ipa_sources, sha256, source_snapshot

def main():
    build = ROOT / "build/ios-ipa"
    shutil.rmtree(build / "Build/Products", ignore_errors=True)  # no app left over from another build
    # The sources the IPA is built from, read before the build so a later edit cannot slip in unrecorded.
    sources = ipa_sources(source_snapshot(ROOT))
    subprocess.run(["xcodebuild", "-project", str(ROOT / "ios/SentryBridge.xcodeproj"), "-scheme", "SentryBridge",
                    "-configuration", "Release", "-destination", "generic/platform=iOS", "-derivedDataPath", str(build),
                    "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=", "build"],
                   check=True, stdout=subprocess.DEVNULL)
    if ipa_sources(source_snapshot(ROOT)) != sources:
        sys.exit("iOS or core sources changed during the build; build again.")
    app = build / "Build/Products/Release-iphoneos/SentryBridge.app"
    version = plistlib.loads((app / "Info.plist").read_bytes())["CFBundleShortVersionString"]
    target = ROOT / "dist/sentry-bridge.ipa"
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        payload = Path(temporary) / "Payload"
        payload.mkdir()
        shutil.copytree(app, payload / app.name, symlinks=True)
        target.unlink(missing_ok=True)
        # The archive root must be Payload/; no macOS resource forks or extended attributes.
        subprocess.run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--noqtn", "--noacl", "--keepParent", str(payload), str(target)], check=True)
    report = {"versionName": version, "ipaSha256": sha256(target), "sources": sources}
    (ROOT / "dist/ios-build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"{target.relative_to(ROOT)} ({target.stat().st_size // 1024} KB), version {version}")

if __name__ == "__main__":
    main()
