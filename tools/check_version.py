#!/usr/bin/env python3
"""Release check: the tag (e.g. v0.2.0) must match the version built into the apps.

    python3 tools/check_version.py v0.2.0

Bump all three before tagging: SB_VERSION in core/sbcore.h, versionName in android/app/build.gradle.kts
and CFBundleShortVersionString in ios/SentryBridge/Info.plist.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    "core/sbcore.h (SB_VERSION)": r'#define SB_VERSION "([^"]+)"',
    "android/app/build.gradle.kts (versionName)": r'versionName = "([^"]+)"',
    "ios/SentryBridge/Info.plist (CFBundleShortVersionString)": r"<key>CFBundleShortVersionString</key><string>([^<]+)</string>",
}


def main(tag: str) -> int:
    want = tag.removeprefix("refs/tags/").removeprefix("v")
    bad = 0
    for label, pattern in SOURCES.items():
        path = ROOT / label.split(" ")[0]
        m = re.search(pattern, path.read_text())
        have = m.group(1) if m else None
        if have != want:
            print(f"::error title=Version mismatch::{label} is {have!r}, tag is {tag!r}")
            bad += 1
    if not bad:
        print(f"version {want} matches")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
