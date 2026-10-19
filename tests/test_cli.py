"""Program-level behaviour, run against both bridge.py and the native C build (cli/sentry_bridge.c):
auto-enable after an encrypted power-up, the Power Savings warning, a clean error when the port is
taken, and EFB discovery. A fake Sentry serves HTTP on localhost."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gdl90  # noqa: E402

EXE = ".exe" if sys.platform == "win32" else ""


def native_binary():
    env = os.environ.get("SENTRY_BRIDGE_BIN")
    if env:
        return env
    for p in (ROOT / "build-c" / f"sentry-bridge{EXE}", ROOT / "build" / f"sentry-bridge{EXE}"):
        if p.exists() and p.stat().st_mtime >= max((ROOT / n).stat().st_mtime for n in
                                                    ("cli/sentry_bridge.c", "core/sbcore.c", "core/sbdecode.c")):
            return str(p)
    cc = next((c for c in (os.environ.get("CC"), "cc", "gcc", "clang") if c and shutil.which(c)), None)
    if not cc or sys.platform == "win32":
        return None
    out = ROOT / "build" / "sentry-bridge"
    out.parent.mkdir(exist_ok=True)
    try:
        subprocess.run([cc, "-std=c99", "-O2", "-Icore", "-o", str(out), "cli/sentry_bridge.c", "core/sbcore.c", "-lm", "-pthread"],
                       cwd=ROOT, check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None
    return str(out)


NATIVE = native_binary()
IMPLS = {"python": [sys.executable, str(ROOT / "bridge.py")]}
if NATIVE:
    IMPLS["native"] = [NATIVE]


class FakeSentryHTTP:
    def __init__(self, power_savings=True, delay=0.0):
        self.posts, self.gets = [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = body.encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the bridge gave up waiting (slow-server test)

            def do_GET(self):
                fake.gets.append(self.path)
                time.sleep(delay)
                if self.path == "/settings/?action=get":
                    return self._send(200, json.dumps({"Power": {"autoOnEnabled": False, "powerSavings": power_savings}}))
                if self.path == "/?action=get":
                    return self._send(200, json.dumps({"device": "fake"}))
                self._send(404, "nope")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.posts.append((self.path, body))
                self._send(200, "Update successful")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def free_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Running:
    """Start a bridge, collect its output lines."""

    def __init__(self, cmd, args):
        self.p = subprocess.Popen([*cmd, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  env=dict(os.environ, PYTHONUNBUFFERED="1"))
        self.lines = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for raw in self.p.stdout:
            self.lines.append(raw.decode(errors="replace").rstrip())

    def wait_for(self, text, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if any(text in l for l in self.lines):
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=10)
        time.sleep(0.2)
        self.p.stdout.close()

    @property
    def log(self):
        return "\n".join(self.lines)


class TestCli(unittest.TestCase):
    def each(self):
        for name, cmd in IMPLS.items():
            with self.subTest(impl=name):
                yield name, cmd

    def test_auto_enable_and_power_savings_warning(self):
        enc = gdl90.frame(bytes((0x26, 0x00)) + bytes(18))
        for name, cmd in self.each():
            http = FakeSentryHTTP(power_savings=True)
            port = free_udp()
            run = Running(cmd, ["--listen-port", str(port), "--sentry-ip", "127.0.0.1", "--http-port", str(http.port),
                                "--dest", f"127.0.0.1:{free_udp()}", "--announce-port", "0", "--auto-enable",
                                "--stats-every", "2"])
            try:
                self.assertTrue(run.wait_for("listening"), run.log)
                tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                for _ in range(6):
                    tx.sendto(enc, ("127.0.0.1", port))
                    time.sleep(0.3)
                tx.close()
                self.assertTrue(run.wait_for("auto-enable: compatibility mode requested"), run.log)
                self.assertTrue(run.wait_for("Power Savings is ON"), run.log)
            finally:
                run.stop()
                http.close()
            self.assertEqual(len(http.posts), 1, http.posts)
            path, body = http.posts[0]
            self.assertEqual(path, "/settings/?action=set")
            self.assertEqual(json.loads(body), {"publicGDL90": True})
            self.assertIn("/settings/?action=get", http.gets)
            self.assertIn("came up in ForeFlight", run.log)
            # Keepalive: a read-only status request at start, logged once while it keeps succeeding.
            self.assertIn("/?action=get", http.gets)
            self.assertEqual(run.log.count("keepalive: receiver status OK"), 1, run.log)

    def test_efb_on_this_device_shares_the_input_port(self):
        # This host's own address stands in for the Sentry: datagrams from it pass the --sentry-ip filter.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # selects a route; sends nothing
            local = probe.getsockname()[0]
        if local in ("0.0.0.0", "127.0.0.1"):
            self.skipTest("no non-loopback address on this machine")
        heartbeat = gdl90.encode_heartbeat(True, 100)
        for name, cmd in self.each():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as efb:
                if not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    efb.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # an EFB that allows sharing
                efb.bind(("0.0.0.0", 0))
                efb.settimeout(0.3)
                port = efb.getsockname()[1]
                run = Running(cmd, ["--listen-port", str(port), "--sentry-ip", local, "--http-port", str(free_udp()),
                                    "--relay", "--dest", f"127.0.0.1:{port}", "--announce-port", "0"])
                relayed = 0
                try:
                    self.assertTrue(run.wait_for("sharing UDP"), run.log)
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sentry:
                        end = time.time() + 5
                        while time.time() < end and not relayed:
                            sentry.sendto(heartbeat, (local, port))  # reaches the bridge's specific bind
                            try:
                                relayed += sum(1 for f in gdl90.parse(efb.recv(4096)) if f.crc_ok and f.msg_id == 0)
                            except socket.timeout:
                                pass
                finally:
                    run.stop()
            self.assertGreater(relayed, 0, f"{name}: the EFB got no loopback output\n{run.log}")

    def test_keepalive_repeats_while_receiver_is_silent(self):
        for name, cmd in self.each():
            http = FakeSentryHTTP(power_savings=False)
            run = Running(cmd, ["--listen-port", str(free_udp()), "--sentry-ip", "127.0.0.1", "--http-port", str(http.port),
                                "--dest", f"127.0.0.1:{free_udp()}", "--announce-port", "0"])
            try:
                self.assertTrue(run.wait_for("listening"), run.log)
                end = time.time() + 12
                while http.gets.count("/?action=get") < 2 and time.time() < end:
                    time.sleep(0.1)
            finally:
                run.stop()
                http.close()
            # Silent: every 5 s, not 30 s; and never a settings change.
            self.assertGreaterEqual(http.gets.count("/?action=get"), 2, f"{name}\n{run.log}")
            self.assertEqual(http.posts, [])

    def test_no_auto_enable_without_flag_and_no_warning_when_off(self):
        enc = gdl90.frame(bytes((0x26, 0x00)) + bytes(18))
        for name, cmd in self.each():
            http = FakeSentryHTTP(power_savings=False)
            port = free_udp()
            run = Running(cmd, ["--listen-port", str(port), "--sentry-ip", "127.0.0.1", "--http-port", str(http.port),
                                "--dest", f"127.0.0.1:{free_udp()}", "--announce-port", "0"])
            try:
                self.assertTrue(run.wait_for("listening"), run.log)
                tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                for _ in range(4):
                    tx.sendto(enc, ("127.0.0.1", port))
                    time.sleep(0.3)
                tx.close()
                self.assertTrue(run.wait_for("encrypted) mode"), run.log)
                time.sleep(1.0)
            finally:
                run.stop()
                http.close()
            self.assertEqual(http.posts, [])
            self.assertNotIn("Power Savings is ON", run.log)

    def test_port_in_use(self):
        for name, cmd in self.each():
            blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            blocker.bind(("0.0.0.0", 0))
            port = blocker.getsockname()[1]
            try:
                p = subprocess.run([*cmd, "--listen-port", str(port), "--dest", "127.0.0.1:9",
                                    "--announce-port", "0"],
                                   capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                self.fail(f"{name}: bound a port already in use")
            finally:
                blocker.close()
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("cannot listen on UDP port", p.stdout)

    def test_slow_sentry_web_server_does_not_pause_forwarding(self):
        heartbeat = gdl90.encode_heartbeat(True, 100)
        for name, cmd in self.each():
            http = FakeSentryHTTP(power_savings=False, delay=4.0)
            port = free_udp()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as efb:
                efb.bind(("127.0.0.1", 0))
                efb.settimeout(0.2)
                run = Running(cmd, ["--listen-port", str(port), "--sentry-ip", "127.0.0.1", "--http-port", str(http.port),
                                    "--relay", "--dest", f"127.0.0.1:{efb.getsockname()[1]}", "--announce-port", "0"])
                relayed = 0
                try:
                    self.assertTrue(run.wait_for("listening"), run.log)
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                        # The first datagram starts the Power Savings check, which takes 3+ s here.
                        end = time.time() + 2.0
                        while time.time() < end:
                            tx.sendto(heartbeat, ("127.0.0.1", port))
                            try:
                                relayed += sum(1 for f in gdl90.parse(efb.recv(4096)) if f.crc_ok and f.msg_id == 0)
                            except socket.timeout:
                                pass
                            time.sleep(0.1)
                finally:
                    run.stop()
                    http.close()
            self.assertIn("/settings/?action=get", http.gets, f"{name}: no settings check\n{run.log}")
            self.assertGreaterEqual(relayed, 8, f"{name}: forwarding paused during the web request\n{run.log}")

    def test_hostname_destination_is_accepted(self):
        for name, cmd in self.each():
            port = free_udp()
            run = Running(cmd, ["--listen-port", str(port), "--sentry-ip", "",
                                "--dest", f"localhost:{free_udp()}", "--announce-port", "0"])
            try:
                self.assertTrue(run.wait_for("listening"), run.log)
            finally:
                run.stop()

    def test_broadcast_destination_is_rejected(self):
        for name, cmd in self.each():
            for args in ([], ["--dest", "192.168.4.255:4000"], ["--dest", "255.255.255.255:4000"]):
                p = subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=20)
                text = (p.stdout + p.stderr).lower()
                self.assertEqual(p.returncode, 2, f"{name} {args}: {text}")
                self.assertTrue("dest" in text or "unicast" in text, f"{name} {args}: {text}")

    def test_rejects_invalid_timing_and_ports(self):
        for name, cmd in self.each():
            for args in (["--interval", "0"], ["--interval", "-1"], ["--stale", "0"], ["--stats-every", "0"],
                         ["--http-port", "0"], ["--announce-port", "70000"], ["--listen-port", "0"]):
                p = subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=20)
                self.assertEqual(p.returncode, 2, f"{name} {args}: {p.stdout}{p.stderr}")

    def test_efb_discovery(self):
        hb = gdl90.encode_heartbeat(True, 100)
        for name, cmd in self.each():
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.bind(("127.0.0.1", 0))
            rx.settimeout(0.5)
            port, ann = free_udp(), free_udp()
            # Allow local discovery without requiring a second loopback address.
            run = Running(cmd, ["--listen-port", str(port), "--sentry-ip", "", "--relay",
                                "--dest", f"127.0.0.1:{free_udp()}", "--announce-port", str(ann), "--http-port", "9"])
            got = 0
            try:
                self.assertTrue(run.wait_for("listening"), run.log)
                a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                a.bind(("127.0.0.1", 0))
                a.sendto(json.dumps({"App": "TestEFB", "GDL90": {"port": rx.getsockname()[1]}}).encode(),
                         ("127.0.0.1", ann))
                a.close()
                self.assertTrue(run.wait_for("not sent (pass --dest)"), run.log)
                tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                end = time.time() + 5
                while time.time() < end and not got:
                    tx.sendto(hb, ("127.0.0.1", port))
                    try:
                        data, _ = rx.recvfrom(4096)
                        got = sum(1 for f in gdl90.parse(data) if f.crc_ok and f.msg_id == 0)
                    except socket.timeout:
                        pass
                tx.close()
            finally:
                run.stop()
                rx.close()
            self.assertEqual(got, 0, f"{name}: an announcement became a destination\n{run.log}")

    def test_announced_destinations_are_bounded_and_never_this_bridge(self):
        """One device announcing many ports is noted at most 32 times, and an announcement of
        this bridge's own input port from this device is ignored."""
        for name, cmd in self.each():
            port, ann = free_udp(), free_udp()
            run = Running(cmd, ["--listen-port", str(port), "--sentry-ip", "", "--relay", "--stats-every", "1",
                                "--dest", f"127.0.0.1:{free_udp()}", "--announce-port", str(ann), "--http-port", "9"])
            try:
                self.assertTrue(run.wait_for("listening"), run.log)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a:
                    a.bind(("127.0.0.1", 0))
                    # Loopback UDP can drop part of a burst on a busy machine; resending only
                    # refreshes known entries, so the counts below stay exact.
                    for _ in range(5):
                        for efb_port in [port] + list(range(30001, 30041)):
                            a.sendto(json.dumps({"App": "Many", "GDL90": {"port": efb_port}}).encode(), ("127.0.0.1", ann))
                        if run.wait_for("noted=32", timeout=3):
                            break
                self.assertTrue(run.wait_for("noted=32"), run.log)
                time.sleep(1.5)
            finally:
                run.stop()
            announced = [line for line in run.lines if "announced at" in line]
            self.assertEqual(len(announced), 32, f"{name}\n{run.log}")
            self.assertFalse(any(f"127.0.0.1:{port}" in line for line in announced), f"{name}\n{run.log}")
            self.assertFalse(any("noted=33" in line for line in run.lines), f"{name}\n{run.log}")


if __name__ == "__main__":
    unittest.main()
