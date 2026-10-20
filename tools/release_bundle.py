"""Prepare and verify a complete local release; never talks to GitHub or signs files."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Firmware images (tools/build_firmware.py) are not release assets until they are tested on boards.
ASSETS = ("sentry-bridge.exe", "sentry-ctl.exe", "sentry-bridge-gui.exe", "sentry-bridge.apk")
CERTIFICATE = "1afa17aec207256b2352089f9d4e0a1417d09c859e11fa349aef8d45d0a019c5"
DISTRIBUTION_ASSETS = ASSETS + ("sentry-bridge-source.zip", "LICENSE", "NOTICE.txt")
# Sources Git records as 100755. Listed because a release export (git archive) has no Git
# metadata and Windows drops the bits; verify() checks them against the GitHub tree.
EXECUTABLES = ("capture.py", "sentry_ctl.py", "sim.py")
SOURCE_DIRS = ("notices", "core", "cli", "firmware", "android", "tests", "tools", "docs", "assets")
ROOT_FILES = (".gitignore", "Run-Gui.cmd", "CMakeLists.txt", "LICENSE", "NOTICE.txt", "README.md", "bridge.py", "capture.py",
              "gdl90.py", "sentry_ctl.py", "sim.py", "uat.py", "ws_probe.py",
              "requirements.txt", "requirements-dev.txt", "requirements-build.txt")
PUBLIC_ASSETS = ("config/android-simulator.json", "tests/data/stats-cases.json", "web/app.html", "web/app.css", "web/app.js",
                 "web/favicon.svg", "web/privacy.html", "web/legal.html", "web/legal.js",
                 # Build inputs listed by name: a suffix rule would also pick up untracked local files
                 # such as android/local.properties.
                 "android/gradle.properties", "android/gradle/wrapper/gradle-wrapper.properties",
                 # F-Droid store text and icon, read from the public source
                 "fastlane/metadata/android/en-US/title.txt", "fastlane/metadata/android/en-US/short_description.txt",
                 "fastlane/metadata/android/en-US/full_description.txt", "fastlane/metadata/android/en-US/images/icon.png")
# Select reviewed iOS paths explicitly, rather than exporting every plist, entitlement or Swift file.
IOS_PUBLIC_FILES = (
    "ios/SentryBridge/BridgeIO.c", "ios/SentryBridge/BridgeIO.h",
    "ios/SentryBridge/Bridge-Bridging-Header.h", "ios/SentryBridge/BridgeModel.swift",
    "ios/SentryBridge/BridgeView.swift", "ios/SentryBridge/BridgeWorker.swift",
    "ios/SentryBridge/EFBDiscovery.swift", "ios/SentryBridge/BridgeIntents.swift",
    "ios/SentryBridge/SentryBridgeApp.swift", "ios/SentryBridge/Info.plist",
    "ios/SentryBridge/PrivacyInfo.xcprivacy", "ios/SentryBridge/DemoReplayer.swift", "ios/SentryBridge/Licenses.swift", "ios/SentryBridge/DemoReceiver.bin", "ios/SentryBridge/AppIcon.icon/icon.json", "ios/SentryBridge/AppIcon.icon/Assets/2-rings.svg",
    "ios/SentryBridge/AppIcon.icon/Assets/3-signal.svg", "ios/SentryBridge/AppIcon.icon/Assets/4-aircraft.svg",
    "ios/tests/BridgeUITests.swift", "ios/tests/WorkerSmoke.swift", "ios/tests/test_ios_adapter.py",
    "ios/SentryBridge.xcodeproj/project.pbxproj",
    "ios/SentryBridge.xcodeproj/xcshareddata/xcschemes/SentryBridge.xcscheme",
    "ios/Multicast.entitlements.example",
)
EXCLUDED_DIRS = {"build", "build-c", ".gradle", ".cxx", "__pycache__", ".git",
                 "DerivedData", "xcuserdata", ".build", ".swiftpm", ".cache", ".venv",
                 ".local-tools", "local-signing", "signing-check", "captures", "logs", "sentry-logs"}
SOURCE_SUFFIXES = {".c", ".h", ".py", ".ps1", ".sh", ".java", ".kt", ".kts", ".xml",
                   ".md", ".txt", ".yml", ".yaml", ".service", ".conf", ".rc", ".ico", ".svg", ".defaults"}  # sdkconfig.defaults


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_public_source(name):
    """One allowlist for local exports and the remote release-source gate."""
    path = Path(name)
    if EXCLUDED_DIRS.intersection(path.parts):
        return False
    return name in ROOT_FILES + PUBLIC_ASSETS + IOS_PUBLIC_FILES or (
        path.parts[0] in SOURCE_DIRS and path.suffix in SOURCE_SUFFIXES)


def source_snapshot(root=ROOT):
    """Hash only explicitly selected public sources; signing/captures/cache are excluded."""
    files = [root / name for name in ROOT_FILES + PUBLIC_ASSETS + IOS_PUBLIC_FILES if (root / name).is_file()]
    for directory in SOURCE_DIRS:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if path.is_file() and is_public_source(relative.as_posix()):
                files.append(path)
    return {p.relative_to(root).as_posix(): hashlib.sha1(
        f"blob {p.stat().st_size}\0".encode() + p.read_bytes()).hexdigest() for p in sorted(files)}


def converted_sources(root=ROOT):
    """Sources Git reports unchanged whose bytes still differ from their committed blobs: a checkout
    that converts line endings (core.autocrlf). Their hashes can never match the GitHub commit, and
    the converted text would be built into the APK and executables. Empty outside a Git checkout,
    such as an exported source snapshot."""
    def git(*arguments):
        return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True,
                              check=True).stdout
    try:
        if Path(git("rev-parse", "--show-toplevel").strip()).resolve() != Path(root).resolve():
            return []  # inside another checkout's folder, e.g. an export under build/local
        index = {line.split("\t", 1)[1]: line.split()[1] for line in git("ls-files", "-s").splitlines()}
        changed = set(git("diff", "--name-only", "HEAD").splitlines())
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted(path for path, blob in source_snapshot(root).items()
                  if path in index and path not in changed and index[path] != blob)


def check_checkout(root=ROOT):
    converted = converted_sources(root)
    if converted:
        raise ValueError(f"{len(converted)} source files differ from Git only by line endings (for example "
                         f"{converted[0]}). Prepare from an LF export (git -c core.autocrlf=false archive)")


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def app_version(root=ROOT):
    match = re.search(r'#define SB_VERSION "([^"]+)"', (root / "core/sbcore.h").read_text())
    return match[1] if match else None


def validate_reports(root, dist, tag):
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", tag):
        raise ValueError("Use a version tag such as v0.1.3 or v0.1.3-beta.1")
    native = re.search(r'#define SB_VERSION "([^"]+)"', (root / "core/sbcore.h").read_text())
    android = re.search(r'versionName\s*=\s*"([^"]+)"', (root / "android/app/build.gradle.kts").read_text())
    if not native or not android or native[1] != tag[1:] or android[1] != tag[1:]:
        raise ValueError("Release tag must match both application versions")
    tests = load(dist / "test-report.json")
    if not tests.get("passed") or tests.get("testsRun", 0) == 0 or tests.get("skipped") or tests.get("failures") or tests.get("errors"):
        raise ValueError("A complete passing test suite with no skips is required")
    if (not tests.get("smokePassed") or not tests.get("controlSmokePassed") or not tests.get("guiSmokePassed")
            or tests.get("fuzzIterations", 0) < 100000 or not tests.get("sanitizer")):
        raise ValueError("UDP, control-tool and GUI smoke checks and sanitizer checks are required")
    sources = source_snapshot(root)
    if tests.get("sources") != sources:
        raise ValueError("Sources changed since the tests; run Test-Local.ps1 again")
    apk = load(dist / "android-build-report.json")
    if apk.get("signerSha256") != CERTIFICATE or set(apk.get("checkedAbis", [])) != {"arm64-v8a", "armeabi-v7a", "x86_64"}:
        raise ValueError("APK must have the original certificate and all three verified ABIs")
    if apk.get("versionName") != tag[1:] or apk.get("versionCode") != int((root / "android/version-code.txt").read_text()):
        raise ValueError("APK version does not match the tracked application version")
    if apk.get("sources") != sources:
        raise ValueError("Sources changed since the APK build; build it again")
    hashes = {}
    for name in ASSETS:
        path = dist / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing release asset: {name}")
        hashes[name] = sha256(path)
    if hashes["sentry-bridge.apk"] != apk.get("apkSha256"):
        raise ValueError("APK changed after signature verification")
    if (hashes["sentry-bridge.exe"] != tests.get("nativeSha256") or hashes["sentry-ctl.exe"] != tests.get("controlSha256")
            or hashes["sentry-bridge-gui.exe"] != tests.get("guiSha256")):
        raise ValueError("Windows executables changed since their tests")
    return sources, hashes, tests, apk


def prepare(root, tag):
    check_checkout(root)
    dist = root / "dist"
    sources, hashes, tests, apk = validate_reports(root, dist, tag)
    bundle = dist / "release" / tag
    bundle.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        shutil.copyfile(dist / name, bundle / name)
    for name in ("LICENSE", "NOTICE.txt"):
        shutil.copyfile(root / name, bundle / name)
        hashes[name] = sha256(bundle / name)
    with zipfile.ZipFile(bundle / "sentry-bridge-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(sources):
            # Preserve exact source bytes and executable script modes without paths/timestamps
            # from the workstation. The manifest pins their Git blob hashes.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so unzip applies the mode bits
            info.external_attr = (0o100755 if name in EXECUTABLES else 0o100644) << 16
            archive.writestr(info, (root / name).read_bytes())
    hashes["sentry-bridge-source.zip"] = sha256(bundle / "sentry-bridge-source.zip")
    (bundle / "SHA256SUMS").write_text("".join(f"{hashes[name]}  {name}\n" for name in DISTRIBUTION_ASSETS), encoding="ascii")
    manifest = {"tag": tag, "versionCode": apk["versionCode"], "certificateSha256": CERTIFICATE,
                "assets": hashes, "sources": sources, "validation": tests,
                "hardwareValidated": False}
    (bundle / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return bundle


def verify(bundle, tree=None, tag=None):
    manifest = load(bundle / "release-manifest.json")
    if tag is not None and manifest.get("tag") != tag:
        raise ValueError("Release manifest tag does not match the requested tag")
    validation = manifest.get("validation", {})
    if (not validation.get("passed") or validation.get("testsRun", 0) == 0
            or validation.get("failures") or validation.get("errors") or validation.get("skipped")
            or not validation.get("smokePassed") or not validation.get("controlSmokePassed")
            or not validation.get("guiSmokePassed") or validation.get("fuzzIterations", 0) < 100000 or not validation.get("sanitizer")):
        raise ValueError("Release manifest does not contain complete passing validation")
    if manifest.get("certificateSha256") != CERTIFICATE or validation.get("sources") != manifest.get("sources"):
        raise ValueError("Release manifest certificate or tested sources do not match")
    if set(manifest["assets"]) != set(DISTRIBUTION_ASSETS):
        raise ValueError("Release manifest has an incomplete asset set")
    for name, expected in manifest["assets"].items():
        if sha256(bundle / name) != expected:
            raise ValueError(f"Release asset changed: {name}")
    checksums = "".join(f"{manifest['assets'][name]}  {name}\n" for name in DISTRIBUTION_ASSETS)
    if (bundle / "SHA256SUMS").read_text() != checksums:
        raise ValueError("SHA256SUMS does not match the release assets")
    try:
        with zipfile.ZipFile(bundle / "sentry-bridge-source.zip") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(manifest["sources"]):
                raise ValueError("Source archive file list differs from validated sources")
            for info in archive.infolist():
                name, data = info.filename, archive.read(info.filename)
                if info.create_system != 3 or info.external_attr >> 16 != (
                        0o100755 if name in EXECUTABLES else 0o100644):
                    raise ValueError("Source archive file mode differs: " + name)
                blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
                if blob != manifest["sources"][name]:
                    raise ValueError("Source archive differs from validated sources: " + name)
    except zipfile.BadZipFile as error:
        raise ValueError("Invalid source archive") from error
    if tree is not None:
        if tree.get("truncated"):
            raise ValueError("GitHub returned an incomplete source tree")
        remote = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}
        # Both directions prevent publishing an older/incomplete local source copy.
        remote_sources = {name: sha for name, sha in remote.items() if is_public_source(name)}
        if remote_sources != manifest["sources"]:
            raise ValueError("Built sources do not match the target GitHub commit")
        modes = {e["path"] for e in tree["tree"] if e["type"] == "blob" and e.get("mode") == "100755"}
        if modes & remote_sources.keys() != set(EXECUTABLES) & remote_sources.keys():
            raise ValueError("EXECUTABLES does not match the GitHub commit's executable sources")
    return manifest


def main():
    parser = argparse.ArgumentParser(__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("tag")
    commands.add_parser("check-checkout", help="fail if line-ending conversion changed any source file")
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    check.add_argument("--tree-json", type=Path)
    check.add_argument("--tag")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(prepare(ROOT, args.tag))
        elif args.command == "check-checkout":
            check_checkout(ROOT)
            print("Sources match Git byte for byte")
        else:
            verify(args.bundle, load(args.tree_json) if args.tree_json else None, args.tag)
            print("Release bundle verified")
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(f"Release check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
