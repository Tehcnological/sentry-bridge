"""The C core and the Android app must carry the same version (tools/check_version.py enforces it
against the tag at release time; this catches drift on every push)."""

import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import check_version  # noqa: E402


def versions():
    return {label: re.search(p, (ROOT / label.split(" ")[0]).read_text()).group(1)
            for label, p in check_version.SOURCES.items()}


class TestVersion(unittest.TestCase):
    def test_versions_agree(self):
        self.assertEqual(len(set(versions().values())), 1, versions())

    def test_tag_check(self):
        v = next(iter(versions().values()))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check_version.main(f"v{v}"), 0)
            self.assertEqual(check_version.main(f"refs/tags/v{v}"), 0)
            self.assertEqual(check_version.main("v99.0.0"), 1)


if __name__ == "__main__":
    unittest.main()
