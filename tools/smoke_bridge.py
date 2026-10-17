#!/usr/bin/env python3
"""End-to-end smoke test of a bridge build (the .exe, or bridge.py itself).

Starts the bridge, feeds it 6 s of simulated Sentry traffic over real UDP, and checks
that standard GDL90 traffic reports for all 5 simulated aircraft come out.

    python tools/smoke_bridge.py dist/sentry-bridge.exe
    python tools/smoke_bridge.py python bridge.py
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gdl90  # noqa: E402


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main(cmd: list[str]) -> int:
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(0.5)
    in_port = free_udp_port()
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    br = subprocess.Popen([*cmd, "--listen-port", str(in_port), "--sentry-ip", "127.0.0.1",
                           "--dest", f"127.0.0.1:{rx.getsockname()[1]}", "--announce-port", "0",
                           "--stats-every", "2"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    lines: list[str] = []

    def pump():
        for raw in br.stdout:
            lines.append(raw.decode(errors="replace").rstrip())

    threading.Thread(target=pump, daemon=True).start()
    # generous: a cold Windows runner can be slow to start a process the first time
    deadline = time.time() + 60
    while time.time() < deadline and not any("listening" in l for l in lines):
        if br.poll() is not None:
            break
        time.sleep(0.2)
    if not any("listening" in l for l in lines):
        time.sleep(0.5)
        return fail("bridge never reported it was listening", lines, br)
    sim = subprocess.Popen([sys.executable, str(ROOT / "sim.py"), "--dest", f"127.0.0.1:{in_port}",
                            "--seconds", "6"], stdout=subprocess.DEVNULL, env=env)
    seen = {}
    end = time.time() + 8
    try:
        while time.time() < end:
            try:
                data, _ = rx.recvfrom(4096)
            except socket.timeout:
                continue
            for f in gdl90.parse(data):
                if f.crc_ok and f.msg_id == gdl90.MSG_TRAFFIC:
                    r = gdl90.decode_report(f.msg)
                    seen[r.address] = r
    finally:
        sim.wait(timeout=15)
        br.terminate()
        try:
            br.wait(timeout=15)
        except subprocess.TimeoutExpired:
            br.kill()
        rx.close()
    time.sleep(0.3)
    print("\n".join(lines))
    for a, r in sorted(seen.items()):
        print(f"  {a:06X} {r.callsign:8} {r.lat:.4f},{r.lon:.4f} {r.alt_ft} ft")
    if len(seen) != 5:
        return fail(f"expected 5 aircraft, got {len(seen)}", lines, None)
    print("PASS: 5 aircraft bridged")
    return 0


def fail(msg: str, lines: list[str], proc) -> int:
    if proc is not None and proc.poll() is None:
        proc.kill()
    print("\n".join(lines))
    print(f"FAIL: {msg}")
    if os.environ.get("GITHUB_ACTIONS"):
        # surfaced through the checks API, so the reason is readable without downloading logs
        body = (msg + "\n" + "\n".join(lines[-40:])).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::error title=smoke test::{body[:6000]}")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
