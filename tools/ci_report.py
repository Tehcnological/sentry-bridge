"""Show a tools/run_tests.py report as GitHub check annotations. Fails on any skipped test."""
import json
from pathlib import Path
import sys


def main(path):
    # utf-8-sig: Windows PowerShell 5.1 writes the Windows report with a byte order mark.
    report = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for test in report.get("failed", []):
        print(f"::error title=Failed test::{test}")
    skipped = report.get("skipped", [])
    for skip in skipped:
        print(f"::error title=Skipped test::{skip['id']}: {skip['reason']}")
    print(f"{report.get('testsRun', 0)} tests, {len(report.get('failed', []))} failed, {len(skipped)} skipped")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
