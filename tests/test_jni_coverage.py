import unittest
from tools.check_jni_coverage import check


class TestJniCoverage(unittest.TestCase):
    def test_lf_crlf_and_python_310_names(self):
        for ending in ("\n", "\r\n"):
            for label in ("test_jni.TestJni", "test_jni.TestJni.test_sim_traffic"):
                with self.subTest(ending=ending, label=label):
                    self.assertEqual(check(f"test_sim_traffic ({label}) ... ok{ending}"), 1)

    def test_skips_and_empty_coverage_fail(self):
        for text in ("Ran 100 tests\nOK", "test_sim (test_jni.TestJni.test_sim) ... skipped 'No JDK'\r\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                check(text)
