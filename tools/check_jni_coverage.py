"""Check unittest JNI coverage with either LF or CRLF log lines."""
from pathlib import Path
import re
import sys


def check(text):
    lines = [line for line in text.splitlines() if "TestJni" in line]
    if any(re.search(r"\.\.\. skipped", line) for line in lines):
        raise ValueError("JNI parity must run on every platform")
    count = sum(bool(re.search(r"\.\.\. ok$", line)) for line in lines)
    if not count:
        raise ValueError("No passing JNI parity tests found")
    return count


if __name__ == "__main__":
    try:
        print(f"JNI parity: {check(Path(sys.argv[1]).read_text(encoding='utf-8'))} tests passed")
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
