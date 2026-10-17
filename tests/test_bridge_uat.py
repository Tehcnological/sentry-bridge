"""Bridge: UAT (0x24) traffic and encrypted-mode watchdog, on simulated UAT frames with byte errors."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bridge  # noqa: E402
import gdl90  # noqa: E402
import sim  # noqa: E402

SENTRY = "192.168.4.1"
HEARTBEAT = gdl90.encode_heartbeat(True, 100)
ENCRYPTED = gdl90.frame(bytes((0x26, 0x00)) + bytes(18))
SENSOR_0x28 = gdl90.frame(bytes.fromhex("280601") + bytes(36))


def uat_scenario(seconds=180, seed=3):
    """Two UAT-only aircraft; about 40% of frames clean, 35% correctable, 25% uncorrectable."""
    s = sim.SimSentry(tracks=sim.uat_tracks(), seed=seed, send_ownship=False)
    return [(float(t), d) for t in range(seconds) for d in s.datagrams(float(t))], s


def replay(b, datagrams):
    """Feed the datagrams, tick each second, return [(tick time, report)]."""
    out, i, t = [], 0, 1.0
    end = datagrams[-1][0] + 1
    while t <= end:
        while i < len(datagrams) and datagrams[i][0] < t:
            b.feed(datagrams[i][1], SENTRY, datagrams[i][0])
            i += 1
        for fr in b.tick(t):
            out.append((t, gdl90.decode_report(gdl90.parse(fr)[0].msg)))
        t += 1.0
    return out


class TestUatTraffic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datagrams, cls.sim = uat_scenario()

    def test_counts_match_what_was_sent(self):
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        replay(b, self.datagrams)
        sent = self.sim.uat_sent
        self.assertGreater(sent["uncorrectable"], 50)
        self.assertEqual(b.stats.uat_msgs, sent["clean"] + sent["correctable"])
        self.assertEqual(b.stats.uat_fixed, sent["correctable"])
        self.assertEqual(b.stats.uat_bad, sent["uncorrectable"])

    def test_reports_track_the_truth(self):
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        reps = replay(b, self.datagrams)
        tracks = {tr.icao: tr for tr in self.sim.tracks}
        # uncorrectable frames carry plausible-looking garbage: no bogus aircraft may appear
        self.assertEqual({r.address for _, r in reps}, set(tracks))
        for t, r in reps:
            tr = tracks[r.address]
            # the report holds the last FEC-valid position, at most stale_s (15 s) old
            err = min(bridge.dist_nm(r.lat, r.lon, *tr.at(tt)[:2]) for tt in range(int(t) - 16, int(t) + 1))
            self.assertLess(err, 0.05, f"{r.address:06X} at t={t}")
            self.assertEqual((r.addr_type, r.emitter, r.nacp, r.nic), (0, 1, 10, 9))
            self.assertTrue(r.airborne)
            self.assertNotEqual(r.callsign, "1200")   # the squawk in alternate MS frames is not a callsign
        last = {r.address: r for _, r in reps}
        self.assertEqual({a: r.callsign for a, r in last.items()}, {a: tr.callsign for a, tr in tracks.items()})
        climber = last[0xA0CA70]
        self.assertAlmostEqual(climber.vs_fpm, 512, delta=64)
        self.assertAlmostEqual(climber.gs_kt, 70, delta=1)
        self.assertLess(abs((climber.track_deg - 40 + 180) % 360 - 180), 1.5)

    def test_ownship_filtered(self):
        b = bridge.Bridge(sentry_ip=SENTRY, ownship_icao=0xA0CA70, log=lambda m: None)
        self.assertEqual({r.address for _, r in replay(b, self.datagrams)}, {0xA0CA71})

    def test_relay_skips_vendor_frames(self):
        b = bridge.Bridge(sentry_ip=SENTRY, relay=True, log=lambda m: None)
        uat_frame = next(d for _, d in self.datagrams if gdl90.parse(d)[0].msg_id == 0x24)
        out = b.feed(uat_frame + SENSOR_0x28 + HEARTBEAT, SENTRY, 0.0)
        self.assertEqual([gdl90.parse(f)[0].msg_id for f in out], [0x00])


class TestModeWatchdog(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.b = bridge.Bridge(sentry_ip=SENTRY, log=self.logs.append)

    def feed(self, frame, t0, t1):
        fired = []
        t = t0
        while t <= t1:
            self.b.feed(frame, SENTRY, t)
            if self.b.wants_enable(t):
                fired.append(t)
            t += 1.0
        return fired

    def test_starts_encrypted_retries_then_gives_up(self):
        fired = self.feed(ENCRYPTED, 0, 120)
        self.assertEqual(fired, [0, 20, 40])
        self.assertEqual(self.b.mode, "ENCRYPTED")
        self.assertTrue(any("came up in ForeFlight" in m for m in self.logs))

    def test_mid_stream_disable_is_left_alone(self):
        self.feed(HEARTBEAT, 0, 30)
        fired = self.feed(ENCRYPTED, 31, 120)
        self.assertEqual(fired, [])
        self.assertTrue(any("switched to ForeFlight" in m for m in self.logs), self.logs)

    def test_power_cycle_reverts_are_fixed_every_time(self):
        self.feed(HEARTBEAT, 0, 30)
        self.assertEqual(self.feed(ENCRYPTED, 60, 65), [60])       # 30 s gap: power cycle
        self.feed(HEARTBEAT, 66, 90)                               # enable worked
        self.assertTrue(any("back in compatibility" in m for m in self.logs))
        self.assertEqual(self.feed(ENCRYPTED, 200, 205), [200])    # second power cycle

    def test_public_start_never_fires(self):
        self.assertEqual(self.feed(HEARTBEAT, 0, 60), [])
        self.assertEqual(self.b.mode, "PUBLIC")
        self.assertEqual(self.logs, [])


class TestPowerSavings(unittest.TestCase):
    SETTINGS = ('{"client":{"ssid":"x","security":true},"Power":{"autoOnEnabled":false,"powerSavings":%s}}')

    def test_parse(self):
        self.assertTrue(bridge.power_savings_on(self.SETTINGS % "true"))
        self.assertFalse(bridge.power_savings_on(self.SETTINGS % "false"))
        self.assertIsNone(bridge.power_savings_on("null pagebuffer"))
        self.assertIsNone(bridge.power_savings_on('{"Power":{}}'))

    def test_check_against_http(self):
        import http.server
        import threading
        body = {"v": self.SETTINGS % "true"}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = body["v"].encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host = f"127.0.0.1:{srv.server_address[1]}"
        logs = []
        self.assertTrue(bridge.check_power_savings(host, logs.append))
        self.assertTrue(any("Power Savings is ON" in m for m in logs))
        body["v"] = self.SETTINGS % "false"
        logs.clear()
        self.assertFalse(bridge.check_power_savings(host, logs.append))
        self.assertEqual(logs, [])
        srv.shutdown()
        srv.server_close()

    def test_unreachable_is_logged_not_raised(self):
        logs = []
        self.assertIsNone(bridge.check_power_savings("127.0.0.1:9", logs.append, timeout=1))
        self.assertTrue(any("could not read" in m for m in logs))

    def test_stream_starts_counts_gaps(self):
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        for t in (0, 1, 2, 30, 31, 100):
            b.feed(HEARTBEAT, SENTRY, float(t))
        self.assertEqual(b.stream_starts, 3)


class TestPackagedEntry(unittest.TestCase):
    def test_double_click_detection(self):
        self.assertFalse(bridge.double_clicked([]))           # not frozen
        sys.frozen = True
        try:
            self.assertTrue(bridge.double_clicked([]))
            self.assertFalse(bridge.double_clicked(["--relay"]))
        finally:
            del sys.frozen

    def test_port_in_use_is_reported_not_raised(self):
        import socket
        import subprocess
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("0.0.0.0", 0))
        port = blocker.getsockname()[1]
        try:
            p = subprocess.run([sys.executable, str(ROOT / "bridge.py"), "--listen-port", str(port),
                                "--dest", "127.0.0.1:9", "--announce-port", "0"],
                               capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("bridge bound a port that was already in use")
        finally:
            blocker.close()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("cannot listen on UDP port", p.stdout)

if __name__ == "__main__":
    unittest.main()
