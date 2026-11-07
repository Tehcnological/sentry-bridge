"""The apps must carry consistent versions (tools/check_version.py enforces an exact match with the
tag at a full release; this catches drift on every push)."""

import io
from pathlib import Path
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import check_version  # noqa: E402


def versions():
    return {label: re.search(p, (ROOT / label.split(" ")[0]).read_text()).group(1)
            for label, p in check_version.SOURCES.items()}


def parts(version):
    return tuple(int(n) for n in re.match(r"(\d+)\.(\d+)\.(\d+)", version).groups())


def consistent(core, android, ios):
    """Core and Android move together. An iOS-only patch (docs/Building.md) moves iOS alone, ahead by patch number."""
    return core == android and parts(ios)[:2] == parts(core)[:2] and parts(ios) >= parts(core)


def write_versions(root, core, android, ios):
    contents = (f'#define SB_VERSION "{core}"', f'versionName = "{android}"',
                f"<key>CFBundleShortVersionString</key><string>{ios}</string>")
    for label, content in zip(check_version.SOURCES, contents):
        path = root / label.split(" ")[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class TestVersion(unittest.TestCase):
    def test_versions_agree(self):
        self.assertTrue(consistent(*versions().values()), versions())

    def test_consistency_rule(self):
        for allowed in (("0.3.0", "0.3.0", "0.3.0"), ("0.3.0", "0.3.0", "0.3.1"), ("0.3.0", "0.3.0", "0.3.10")):
            self.assertTrue(consistent(*allowed), allowed)
        for drifted in (("0.3.0", "0.3.1", "0.3.1"), ("0.3.1", "0.3.0", "0.3.1"),  # core and Android differ
                        ("0.3.1", "0.3.1", "0.3.0"),  # iOS behind
                        ("0.3.0", "0.3.0", "0.4.0"), ("0.3.0", "0.3.0", "1.3.0")):  # iOS ahead by more than a patch
            self.assertFalse(consistent(*drifted), drifted)

    def test_tag_check(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(check_version, "ROOT", Path(folder)), \
                redirect_stdout(io.StringIO()):
            write_versions(Path(folder), "1.2.3", "1.2.3", "1.2.3")
            self.assertEqual(check_version.main("v1.2.3"), 0)
            self.assertEqual(check_version.main("refs/tags/v1.2.3"), 0)
            self.assertEqual(check_version.main("v99.0.0"), 1)

    def test_full_release_requires_every_version_to_match_the_tag(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(check_version, "ROOT", Path(folder)), \
                redirect_stdout(io.StringIO()):
            write_versions(Path(folder), "1.2.3", "1.2.3", "1.2.4")
            self.assertEqual(check_version.main("v1.2.3"), 1)
            self.assertEqual(check_version.main("v1.2.4"), 1)


if __name__ == "__main__":
    unittest.main()
