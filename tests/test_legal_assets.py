"""Offline distribution texts stay synchronized with the reviewed source inputs."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import legal_assets


class TestLegalAssets(unittest.TestCase):
    def test_packaged_pages_and_native_notice_are_current(self):
        for name, content in legal_assets.generated().items():
            with self.subTest(path=name):
                self.assertTrue(legal_assets.current(ROOT / name, content))

    def test_notices_are_in_corresponding_source_inventory(self):
        import release_bundle
        inventory = release_bundle.source_snapshot(ROOT)
        self.assertTrue(set(legal_assets.NOTICE_FILES).issubset(inventory))
        self.assertTrue({"web/legal.html", "web/privacy.html", "docs/Privacy.md",
                         "core/license_text.h"}.issubset(inventory))
