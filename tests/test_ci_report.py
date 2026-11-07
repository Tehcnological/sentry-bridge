"""The CI step that shows test reports as check annotations."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import ci_report


class TestCiReport(unittest.TestCase):
    def run_report(self, report, encoding="utf-8"):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(report), encoding=encoding)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                return ci_report.main(path), output.getvalue()

    def test_passing_report_with_a_byte_order_mark(self):
        code, output = self.run_report({"testsRun": 3, "failed": [], "skipped": []}, "utf-8-sig")
        self.assertEqual(code, 0)
        self.assertIn("3 tests, 0 failed, 0 skipped", output)

    def test_failed_test_is_annotated(self):
        _, output = self.run_report({"testsRun": 1, "failed": ["a.B.c"], "skipped": []})
        self.assertIn("::error title=Failed test::a.B.c", output)

    def test_skipped_test_fails(self):
        code, output = self.run_report({"testsRun": 1, "skipped": [{"id": "a.B.c", "reason": "no pwsh"}]})
        self.assertEqual(code, 1)
        self.assertIn("a.B.c: no pwsh", output)

    def test_report_from_a_failed_preflight_has_no_counts(self):
        self.assertEqual(self.run_report({"passed": False})[0], 0)


if __name__ == "__main__":
    unittest.main()
