"""Run the packaged Windows GUI headless: simulator traffic through its C core to a local EFB port."""
import base64
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gdl90
import sim


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    binary = str(Path(sys.argv[1]).resolve())
    port = free_port()
    opener = build_opener(ProxyHandler({}))
    token = None

    def wait_idle():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = call("/api/status")["job"]
            if job["state"] != "running":
                return job
            time.sleep(.5)
        raise RuntimeError("Packaged GUI job did not finish")

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": "Bearer " + token} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read())

    with tempfile.TemporaryDirectory() as data_dir, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as efb:
        efb.bind(("127.0.0.1", 0))
        efb.settimeout(1)
        # A just-recorded update check: the smoke test never contacts GitHub.
        (Path(data_dir) / "update-check.json").write_text(json.dumps({"checked": time.time(), "update": None}), encoding="utf-8")
        process = subprocess.Popen([binary, "--no-window", "--port", str(port), "--data-dir", data_dir],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60  # first start unpacks the one-file executable
            status = None
            while time.monotonic() < deadline and status is None:
                if process.poll() is not None:
                    raise RuntimeError(f"Packaged GUI exited early: {process.stdout.read().decode(errors='replace')}")
                try:
                    token = (Path(data_dir) / "gui-session").read_text(encoding="ascii").strip()
                    status = call("/api/status")
                except OSError:
                    time.sleep(.5)
            if not status or status.get("desktop") is not True or status.get("adapter") != "local":
                raise RuntimeError(f"Packaged GUI did not serve its controller: {status}")
            try:  # other programs on this computer must not reach the controls
                opener.open(f"http://127.0.0.1:{port}/api/status", timeout=10)
                raise RuntimeError("Packaged GUI answered without its session token")
            except HTTPError as error:
                if error.code != 403:
                    raise
            # The simulator keeps the EFB port from Settings: this test's listener.
            call("/api/actions/settings", {"efbPort": efb.getsockname()[1]})
            call("/api/actions/simulator", {"enabled": True})
            call("/api/actions/start", {"host": "windows", "topology": "same", "address": "127.0.0.1"})
            # Automatic restoration switches the simulated receiver to Compatibility Mode first.
            seen = set()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not {0, 0x14}.issubset(seen):
                try:
                    seen.update(f.msg_id for f in gdl90.parse(efb.recv(65535)) if f.crc_ok)
                except socket.timeout:
                    pass
            status = call("/api/status")
            if not {0, 0x14}.issubset(seen) or status.get("engine") != "C core":
                raise RuntimeError(f"Packaged GUI forwarded {sorted(seen)} with engine {status.get('engine')}")
            call("/api/actions/stop", {})
            wait_idle()
            # Packaged diagnostic worker: analyze an imported capture, including 1090 callsigns.
            receiver = sim.SimSentry()
            rows = "".join(json.dumps({"t": t, "src": "192.168.4.1", "hex": d.hex()}) + "\n"
                           for t in range(5) for d in receiver.datagrams(t))
            capture = call("/api/actions/upload", {"name": "smoke.jsonl",
                                                    "data": base64.b64encode(rows.encode()).decode()})["capture"]
            call("/api/actions/analyze", {"capture": capture})
            job = wait_idle()
            text = (job.get("result") or {}).get("text", "")
            if job.get("state") != "done" or job["result"].get("exitCode") != 0 or "N123AB" not in text:
                raise RuntimeError(f"Packaged capture analysis failed: {job}")
            call("/api/actions/quit", {})
            process.wait(timeout=20)
        finally:
            if process.poll() is None:
                # A one-file build runs as a launcher plus a child; stop both.
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
                process.wait(timeout=10)
    print("PASS: packaged GUI forwarded simulator GPS and traffic through the C core; capture analysis worker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
