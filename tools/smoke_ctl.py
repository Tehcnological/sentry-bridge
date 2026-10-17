"""Run the packaged control tool against a local simulated Sentry."""
import json
import subprocess
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_ctl import FakeSentry


def main():
    binary = str(Path(sys.argv[1]).resolve())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    fake = FakeSentry(port)
    try:
        with tempfile.TemporaryDirectory() as outdir:
            base = [binary, "--host", f"127.0.0.1:{fake.http_port}", "--outdir", outdir]
            status = subprocess.run([*base, "status"], capture_output=True, text=True, timeout=30)
            status_header, _, status_json = status.stdout.partition("\n")
            if status.returncode or status_header.strip() != "HTTP 200":
                raise RuntimeError(f"Packaged status failed: {status.stdout} {status.stderr}")
            if json.loads(status_json).get("version") != "1.0.32":
                raise RuntimeError(f"Packaged status has unexpected firmware: {status_json}")
            backup = subprocess.run([*base, "backup"], capture_output=True, text=True, timeout=30)
            if backup.returncode or not list(Path(outdir).glob("sentry-settings-*.json")):
                raise RuntimeError(f"Packaged backup failed: {backup.stderr}")
            for mode, expected in (("enable", True), ("disable", False)):
                result = subprocess.run([*base, mode, "--no-verify"], capture_output=True, text=True, timeout=30)
                if result.returncode or fake.public != expected:
                    raise RuntimeError(f"Packaged {mode} failed: {result.stderr}")
        print("PASS: packaged control-tool status, backup, enable, disable")
        return 0
    finally:
        fake.close()


if __name__ == "__main__":
    sys.exit(main())
