"""The POSIX gate must reject missing coverage and stale success reports."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import test_posix as runner


def report(skipped):
    return dict(passed=True, testsRun=119, failures=0, errors=0, skipped=skipped)


NAMES = ("test_publishes_only_after_complete_verified_upload", "test_commit_not_on_master_prevents_upload")


class TestPosixRunner(unittest.TestCase):
    def test_publication_skips_without_pwsh_are_allowed(self):
        skips = [dict(test=f"{name} (test_publish_release.TestPublishRelease.{name})",
                      reason=runner.PWSH_SKIP_REASON) for name in NAMES]
        runner.validate_test_report(report(skips))

    def test_skip_reason_matches_the_publication_tests(self):
        source = (Path(__file__).resolve().parent / "test_publish_release.py").read_text()
        self.assertIn(repr(runner.PWSH_SKIP_REASON), source)

    def test_other_skips_with_the_same_reason_are_rejected(self):
        skips = [dict(test="test_sim_traffic (test_jni.TestJni.test_sim_traffic)", reason=runner.PWSH_SKIP_REASON)]
        with self.assertRaises(RuntimeError):
            runner.validate_test_report(report(skips))

    def test_jni_skip_or_failure_cannot_pass_the_gate(self):
        cases = [report([dict(test="test_sim_traffic (test_jni.TestJni.test_sim_traffic)",
                              reason="needs JDK 17+")]), report([])]
        cases[1]["failures"] = 1
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                runner.validate_test_report(case)

    def test_python_310_skip_names_and_stable_ids(self):
        for stable in (False, True):
            skips = [dict(test=f"{name} (test_publish_release.TestPublishRelease)",
                          reason=runner.PWSH_SKIP_REASON,
                          **({"id": f"test_publish_release.TestPublishRelease.{name}"} if stable else {}))
                     for name in NAMES]
            runner.validate_test_report(report(skips))

    def test_preflight_failure_invalidates_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(dict(passed=True, posixValidationPassed=True)))
            with patch.object(runner.platform, "system", return_value="Linux"), \
                    patch.object(runner.shutil, "which", return_value=None), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(["--report", str(path)]), 1)
            evidence = json.loads(path.read_text())
            self.assertFalse(evidence["passed"])
            self.assertFalse(evidence["posixValidationPassed"])
