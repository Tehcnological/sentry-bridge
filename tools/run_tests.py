"""Run the full suite with a machine-readable report; local release gates allow no skips."""
import argparse
import json
import platform
import sys
import time
import unittest
from pathlib import Path
from release_bundle import source_snapshot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-skips", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    sources = source_snapshot()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and (args.allow_skips or not result.skipped) and sources == source_snapshot()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "passed": passed, "testsRun": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "failed": [t.id() for t, _ in result.failures + result.errors],
        "skipped": [{"test": str(t), "id": t.id(), "reason": reason} for t, reason in result.skipped],
        "seconds": round(time.monotonic() - started, 3),
        "python": platform.python_version(), "platform": platform.system(),
        "sources": sources,
    }, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
