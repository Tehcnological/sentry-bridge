"""Build an unsigned iOS .ipa for sideloading (SideStore or AltStore re-sign it with the user's free
Apple ID). macOS with Xcode only. Output: dist/SentryBridge-<version>-unsigned.ipa.

No restricted entitlements are requested. Installation and background behavior after free-account
re-signing still require physical-device validation.
"""
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]

def main():
    build = ROOT / "build/ios-ipa"
    subprocess.run(["xcodebuild", "-project", str(ROOT / "ios/SentryBridge.xcodeproj"), "-scheme", "SentryBridge",
                    "-configuration", "Release", "-destination", "generic/platform=iOS", "-derivedDataPath", str(build),
                    "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=", "build"],
                   check=True, stdout=subprocess.DEVNULL)
    app = build / "Build/Products/Release-iphoneos/SentryBridge.app"
    version = plistlib.loads((app / "Info.plist").read_bytes())["CFBundleShortVersionString"]
    target = ROOT / f"dist/SentryBridge-{version}-unsigned.ipa"
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        payload = Path(temporary) / "Payload"
        payload.mkdir()
        shutil.copytree(app, payload / app.name, symlinks=True)
        target.unlink(missing_ok=True)
        # The archive root must be Payload/; no macOS resource forks or extended attributes.
        subprocess.run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--noqtn", "--noacl", "--keepParent", str(payload), str(target)], check=True)
    print(f"{target.relative_to(ROOT)} ({target.stat().st_size // 1024} KB)")

if __name__ == "__main__":
    main()
