"""The iOS app source for SideStore, AltStore and Feather: altsource.json at the repository root, read
from https://raw.githubusercontent.com/Tehcnological/sentry-bridge/master/altsource.json (AltStore
source format, with the older single-version fields that some readers still use).

After a release is published, add its IPA (size and SHA-256 from the published bundle's manifest):

    python tools/altsource.py update dist/release/v0.3.0 v0.3.0

and commit altsource.json to master. The privacy text is copied from the app's Info.plist, because
AltStore refuses to install an app whose permissions differ from its source.
"""
import argparse
import datetime
import hashlib
import json
import plistlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "altsource.json"
REPOSITORY = "https://github.com/Tehcnological/sentry-bridge"
RAW = "https://raw.githubusercontent.com/Tehcnological/sentry-bridge/master"
BUNDLE_ID = "io.github.tehcnological.sentrybridge"
DESCRIPTION = ("Shows traffic from a uAvionix Sentry or Sentry Plus in EFB apps other than ForeFlight. The Sentry's "
               "Compatibility Mode sends traffic in a raw form; Sentry Bridge decodes it and sends standard GDL90 "
               "traffic reports to the EFB, on this device or another one. Advisory information only.")


def app_permissions(root=ROOT):
    """The usage descriptions the app declares; it requests no special entitlements."""
    info = plistlib.loads((root / "ios/SentryBridge/Info.plist").read_bytes())
    return {"entitlements": [], "privacy": {key: value for key, value in sorted(info.items())
                                            if key.startswith("NS") and key.endswith("UsageDescription")}}


def min_os(root=ROOT):
    return re.search(r'IPHONEOS_DEPLOYMENT_TARGET = "([^"]+)"',
                     (root / "ios/SentryBridge.xcodeproj/project.pbxproj").read_text())[1]


def version_key(version):
    """Semantic version order: 0.3.0 above 0.3.0-beta.10 above 0.3.0-beta.2 above 0.2.9. Numbers compare
    as numbers; in the pre-release part a number sorts below a word, and more parts above fewer."""
    main, _, suffix = version.partition("-")
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in suffix.split(".")) if suffix else ()
    return tuple(int(part) for part in main.split(".")), not suffix, parts


def download_url(tag):
    return f"{REPOSITORY}/releases/download/{tag}/sentry-bridge.ipa"


def version_entry(bundle, tag, date):
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    ipa = bundle / "sentry-bridge.ipa"
    if manifest.get("tag") != tag or manifest["assets"].get("sentry-bridge.ipa") is None:
        raise ValueError("Use the published bundle of this tag, with its IPA")
    if hashlib.sha256(ipa.read_bytes()).hexdigest() != manifest["assets"]["sentry-bridge.ipa"]:
        raise ValueError("IPA differs from the release manifest")
    return {"version": tag[1:], "buildVersion": "1", "date": date, "minOSVersion": min_os(),
            "localizedDescription": f"Release notes: {REPOSITORY}/releases/tag/{tag}",
            "downloadURL": download_url(tag), "size": ipa.stat().st_size,
            "sha256": manifest["assets"]["sentry-bridge.ipa"]}


def base():
    icon = f"{RAW}/fastlane/metadata/android/en-US/images/icon.png"
    return {"name": "Sentry Bridge", "identifier": f"{BUNDLE_ID}.source", "subtitle": "Sentry traffic in any EFB",
            "description": DESCRIPTION, "iconURL": icon, "website": REPOSITORY, "tintColor": "#2563EB",
            "apps": [{"name": "Sentry Bridge", "bundleIdentifier": BUNDLE_ID, "developerName": "Sentry Bridge contributors",
                      "subtitle": "Sentry traffic in any EFB", "localizedDescription": DESCRIPTION, "iconURL": icon,
                      "tintColor": "#2563EB", "category": "utilities", "versions": []}],
            "news": []}


def with_legacy(app):
    """Readers of the older format take the newest version from these fields."""
    newest = app["versions"][0]
    app.update(version=newest["version"], versionDate=newest["date"], versionDescription=newest["localizedDescription"],
               downloadURL=newest["downloadURL"], size=newest["size"])
    return app


def update(bundle, tag, date=None, path=SOURCE):
    source = json.loads(path.read_text(encoding="utf-8")) if path.exists() else base()
    app = source["apps"][0]
    entry = version_entry(bundle, tag, date or datetime.date.today().isoformat())
    # Newest first by version number: rebuilding an older release must not make it the latest.
    app["versions"] = sorted([entry] + [v for v in app["versions"] if v["version"] != entry["version"]],
                             key=lambda v: version_key(v["version"]), reverse=True)
    app["appPermissions"] = app_permissions()
    with_legacy(app)
    check(source)
    path.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    return source


def check(source, root=ROOT):
    """The source describes this app, its permissions match Info.plist, and each version points at its release."""
    app = source["apps"][0]
    if app["bundleIdentifier"] != BUNDLE_ID or app["appPermissions"] != app_permissions(root):
        raise ValueError("altsource.json does not match the app's bundle ID or Info.plist permissions")
    if not app["versions"]:
        raise ValueError("altsource.json lists no version")
    for version in app["versions"]:
        if version["downloadURL"] != download_url("v" + version["version"]) or not re.fullmatch(r"[0-9a-f]{64}", version["sha256"]):
            raise ValueError(f"Version {version['version']} does not point at its release IPA")
    keys = [version_key(v["version"]) for v in app["versions"]]
    if keys != sorted(keys, reverse=True) or len(set(keys)) != len(keys):
        raise ValueError("Versions must be listed once each, newest first")
    newest = app["versions"][0]
    if (app.get("version"), app.get("downloadURL"), app.get("size")) != (newest["version"], newest["downloadURL"], newest["size"]):
        raise ValueError("The single-version fields do not name the newest version")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("update")
    add.add_argument("bundle", type=Path)
    add.add_argument("tag")
    commands.add_parser("check")
    args = parser.parse_args()
    try:
        if args.command == "update":
            update(args.bundle, args.tag)
            print(f"{SOURCE.name}: {args.tag} added")
        else:
            check(json.loads(SOURCE.read_text(encoding="utf-8")))
            print(f"{SOURCE.name} matches the app")
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
