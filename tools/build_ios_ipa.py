"""Build the iOS release app, without a developer signature (SideStore, AltStore, Feather or a paid developer account sign it
when it is installed). macOS with Xcode only. Output: dist/sentry-bridge.ipa and
dist/ios-build-report.json, which release_bundle.py checks before it adds the IPA to a release.

The app is ad-hoc signed only to declare its entitlements (Time Sensitive notifications): no identity and no
provisioning profile. Installers re-sign it and keep what their profile allows; without the capability iOS
delivers the same notifications at the normal level.

FLAVOR picks the build (SB_FLAVOR in the Xcode project). Sideload, the release IPA, keeps the bridge running
with background audio; Lite (--flavor Lite, for a future App Store build) has no silent audio and writes
dist/sentry-bridge-lite.ipa, which release_bundle.py never accepts.
"""
from pathlib import Path
import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.release_bundle import IPA_ENTITLEMENTS, IPA_FLAVOR, flavor_problem, ipa_sources, sha256, source_snapshot

FLAVOR = IPA_FLAVOR  # the release flavor

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flavor", choices=("Sideload", "Lite"), default=FLAVOR)
    flavor = parser.parse_args().flavor
    build = ROOT / ("build/ios-ipa" if flavor == FLAVOR else "build/ios-ipa-" + flavor.lower())
    shutil.rmtree(build / "Build/Products", ignore_errors=True)  # no app left over from another build
    # The sources the IPA is built from, read before the build so a later edit cannot slip in unrecorded.
    sources = ipa_sources(source_snapshot(ROOT))
    subprocess.run(["xcodebuild", "-project", str(ROOT / "ios/SentryBridge.xcodeproj"), "-scheme", "SentryBridge",
                    "-configuration", "Release", "-destination", "generic/platform=iOS", "-derivedDataPath", str(build),
                    "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=", "SB_FLAVOR=" + flavor, "build"],
                   check=True, stdout=subprocess.DEVNULL)
    if ipa_sources(source_snapshot(ROOT)) != sources:
        sys.exit("iOS or core sources changed during the build; build again.")
    app = build / "Build/Products/Release-iphoneos/SentryBridge.app"
    info = plistlib.loads((app / "Info.plist").read_bytes())
    version = info["CFBundleShortVersionString"]
    linked = subprocess.run(["otool", "-L", str(app / "SentryBridge")], check=True, capture_output=True, text=True).stdout
    problem = flavor_problem(info, flavor, links_audio="AVFAudio.framework" in linked)
    if problem:
        sys.exit(f"{flavor} build is wrong: {problem}")
    subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", "--entitlements", str(IPA_ENTITLEMENTS), str(app)],
                   check=True, stdout=subprocess.DEVNULL)
    target = ROOT / ("dist/sentry-bridge.ipa" if flavor == FLAVOR else f"dist/sentry-bridge-{flavor.lower()}.ipa")
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        payload = Path(temporary) / "Payload"
        payload.mkdir()
        shutil.copytree(app, payload / app.name, symlinks=True)
        target.unlink(missing_ok=True)
        # The archive root must be Payload/; no macOS resource forks or extended attributes.
        subprocess.run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--noqtn", "--noacl", "--keepParent", str(payload), str(target)], check=True)
    report = {"versionName": version, "flavor": flavor, "ipaSha256": sha256(target), "sources": sources}
    report_name = "ios-build-report.json" if flavor == FLAVOR else f"ios-build-report-{flavor.lower()}.json"
    (ROOT / "dist" / report_name).write_text(json.dumps(report, indent=2) + "\n")
    print(f"{target.relative_to(ROOT)} ({target.stat().st_size // 1024} KB), version {version}, {flavor}")

if __name__ == "__main__":
    main()
