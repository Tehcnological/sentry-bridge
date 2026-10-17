"""Tests: run with  python3 -m unittest discover -s tests -v  (from the project folder)."""

import io
import json
from collections import Counter
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402
import capture  # noqa: E402
import gdl90  # noqa: E402
import sim  # noqa: E402

SENTRY = "192.168.4.1"


def run_sim(b, s, seconds, start=0, collect=None):
    """Feed `seconds` of simulated data, return {t: [Report,...]} of bridge output."""
    out = {}
    for t in range(start, start + seconds):
        for d in s.datagrams(t):
            b.feed(d, SENTRY, float(t))
        frames = b.tick(float(t))
        reps = []
        for fr in frames:
            for f in gdl90.parse(fr):
                assert f.crc_ok
                reps.append(gdl90.decode_report(f.msg))
        out[t] = reps
    return out


def malformed_http_server():
    """A receiver whose web server answers every request with a line that is not HTTP: http.client
    raises HTTPException (BadStatusLine), not OSError. Returns (host:port, close)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    conn.recv(4096)
                    conn.sendall(b"NOT HTTP\r\n\r\n")
                except OSError:
                    pass
    threading.Thread(target=serve, daemon=True).start()
    return f"127.0.0.1:{server.getsockname()[1]}", server.close


class TestGDL90(unittest.TestCase):
    def test_report_roundtrip(self):
        r = gdl90.Report(address=0xAB761C, lat=-33.8688, lon=151.2093, alt_ft=38000, gs_kt=460,
                         vs_fpm=-1216, track_deg=300.9, callsign="ASA1322", emitter=3, nic=8, nacp=9)
        f = gdl90.parse(gdl90.encode_report(r))[0]
        self.assertTrue(f.crc_ok)
        d = gdl90.decode_report(f.msg)
        self.assertEqual(d.address, 0xAB761C)
        self.assertAlmostEqual(d.lat, -33.8688, places=4)
        self.assertAlmostEqual(d.lon, 151.2093, places=4)
        self.assertEqual(d.alt_ft, 38000)
        self.assertEqual(d.gs_kt, 460)
        self.assertEqual(d.vs_fpm, -1216)
        self.assertLess(abs(d.track_deg - 300.9), 1.41)
        self.assertEqual(d.callsign, "ASA1322")
        self.assertEqual((d.emitter, d.nic, d.nacp), (3, 8, 9))

    def test_invalid_fields(self):
        d = gdl90.decode_report(gdl90.parse(gdl90.encode_report(gdl90.Report(address=1)))[0].msg)
        self.assertIsNone(d.lat)
        self.assertIsNone(d.alt_ft)
        self.assertIsNone(d.gs_kt)
        self.assertIsNone(d.vs_fpm)
        self.assertIsNone(d.track_deg)

    def test_byte_stuffing_and_multiframe(self):
        msg = bytes((0x25, 0x7E, 0x7D, 0x00, 0x7E))
        dg = gdl90.frame(msg) + gdl90.encode_heartbeat(True, 1234)
        frames = gdl90.parse(dg)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f.crc_ok for f in frames))
        self.assertEqual(frames[0].msg, msg)
        hb = gdl90.decode_heartbeat(frames[1].msg)
        self.assertTrue(hb["gps_pos_valid"])
        self.assertEqual(hb["timestamp_s"], 1234)

    def test_known_crc(self):
        # GDL90 heartbeat test vector: 7E 00 81 41 DB D0 08 02 B3 8B 7E
        dg = bytes.fromhex("7E008141DBD00802B38B7E")
        f = gdl90.parse(dg)[0]
        self.assertTrue(f.crc_ok)
        self.assertEqual(gdl90.frame(f.msg), dg)


class TestBridgeAccuracy(unittest.TestCase):
    def _check(self, out, s, t_from):
        for t in range(t_from, max(out) + 1):
            reps = {r.address: r for r in out[t]}
            truth = s.truth(t + 0.5)  # last position message of each second is at t+0.5
            self.assertEqual(set(reps), set(truth), f"t={t}")
            for icao, tr in truth.items():
                r = reps[icao]
                err = bridge.dist_nm(r.lat, r.lon, tr["lat"], tr["lon"])
                self.assertLess(err, 0.02, f"{icao:06X} pos err {err:.4f} NM at t={t}")
                self.assertLessEqual(abs(r.alt_ft - tr["alt"]), 25, f"{icao:06X} alt")
                self.assertLessEqual(abs(r.gs_kt - tr["gs"]), 2)
                self.assertLess(abs((r.track_deg - tr["trk"] + 180) % 360 - 180), 2.0)
                self.assertLessEqual(abs(r.vs_fpm - tr["vs"]), 128)
                self.assertEqual(r.callsign, tr["cs"])
                self.assertTrue(r.airborne)
                self.assertEqual(r.track_type, 1)

    def test_with_gps_fix(self):
        s = sim.SimSentry(gps_fix=True)
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        out = run_sim(b, s, 120)
        self._check(out, s, 1)
        self.assertIsNotNone(b.own_lat)
        self.assertEqual(b.stats.pos_rejected, 0)

    def test_bench_no_fix_no_ref(self):
        s = sim.SimSentry(gps_fix=False)
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        out = run_sim(b, s, 60)
        self.assertIsNone(b.own_lat)
        self._check(out, s, 1)  # global even/odd decode needs no reference

    def test_emitter_category(self):
        s = sim.SimSentry()
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        out = run_sim(b, s, 3)
        cats = {r.address: r.emitter for r in out[2]}
        self.assertEqual(cats[0xAB761C], 3)  # large
        self.assertEqual(cats[0xA12345], 1)  # light


class TestBridgeBehaviour(unittest.TestCase):
    def test_changed_route_reports_only_a_new_usable_address(self):
        # The shared input follows this, so it must ignore no route, loopback and the bound address.
        from unittest.mock import patch
        with patch.object(bridge, "local_route", return_value="192.0.2.7"):
            self.assertEqual(bridge.changed_route("192.0.2.1", "192.0.2.5"), "192.0.2.7")
            self.assertIsNone(bridge.changed_route("192.0.2.1", "192.0.2.7"))
        for local in (None, "127.0.0.1"):
            with patch.object(bridge, "local_route", return_value=local):
                self.assertIsNone(bridge.changed_route("192.0.2.1", "192.0.2.5"))

    def test_malformed_receiver_http_is_reported_not_raised(self):
        # These run on the web worker thread: an escaping exception would end auto-enable for the session.
        host, close = malformed_http_server()
        try:
            self.assertFalse(bridge.enable_public(host, timeout=2)[0])
            lines = []
            self.assertIsNone(bridge.check_power_savings(host, lines.append, timeout=2))
            self.assertIn("could not read Sentry settings", lines[0])
        finally:
            close()

    def test_ownship_filter_and_ghost_hint(self):
        s = sim.SimSentry()
        own = s.ownship
        s.tracks.append(sim.Track(0xADF00D, "N12345", own.lat, own.lon, own.alt, own.gs, own.trk, 0))
        logs = []
        b = bridge.Bridge(sentry_ip=SENTRY, log=logs.append)
        out = run_sim(b, s, 20)
        self.assertIn(0xADF00D, {r.address for r in out[19]})
        self.assertTrue(any("--ownship-icao ADF00D" in m for m in logs), logs)
        b2 = bridge.Bridge(sentry_ip=SENTRY, ownship_icao=0xADF00D, log=lambda m: None)
        out2 = run_sim(b2, s, 10)
        self.assertNotIn(0xADF00D, {r.address for r in out2[9]})

    def test_stale_and_drop(self):
        s = sim.SimSentry()
        b = bridge.Bridge(sentry_ip=SENTRY, stale_s=15, log=lambda m: None)
        run_sim(b, s, 5)
        self.assertEqual(len(b.tick(10.0)), 5)
        self.assertEqual(len(b.tick(20.0)), 0)   # stale: no longer sent
        self.assertEqual(len(b.targets), 5)
        b.tick(70.0)
        self.assertEqual(len(b.targets), 0)      # dropped after 60 s

    def test_source_filter(self):
        s = sim.SimSentry()
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        for d in s.datagrams(0):
            b.feed(d, "192.168.4.50", 0.0)
        self.assertEqual(b.stats.datagrams, 0)

    def test_relay(self):
        s = sim.SimSentry()
        b = bridge.Bridge(sentry_ip=SENTRY, relay=True, log=lambda m: None)
        relayed = []
        for d in s.datagrams(0):
            relayed += b.feed(d, SENTRY, 0.0)
        ids = {gdl90.parse(fr)[0].msg_id for fr in relayed}
        self.assertEqual(ids, {0x00, 0x0A})

    def test_relay_fisb_uplink_intact(self):
        s = sim.SimSentry(fisb=True)
        b = bridge.Bridge(sentry_ip=SENTRY, relay=True, log=lambda m: None)
        sent = [d for d in s.datagrams(0) if gdl90.parse(d)[0].msg_id == gdl90.MSG_UPLINK]
        self.assertEqual(len(sent), 1)
        relayed = b.feed(sent[0], SENTRY, 0.0)
        self.assertEqual(relayed, sent)  # byte for byte, stuffing included
        self.assertEqual(len(gdl90.parse(relayed[0])[0].msg), 436)

    def test_garbage_does_not_crash(self):
        rng = random.Random(7)
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        for _ in range(3000):
            n = rng.randrange(1, 40)
            body = bytes((0x21,)) + bytes(rng.randrange(256) for _ in range(n))
            b.feed(gdl90.frame(body), SENTRY, 0.0)
            b.feed(bytes(rng.randrange(256) for _ in range(n)), SENTRY, 0.0)
        b.tick(0.0)

    def test_trailer_shorter_than_parity(self):
        s = sim.SimSentry(tracks=[sim.default_tracks()[0]])
        b = bridge.Bridge(sentry_ip=SENTRY, log=lambda m: None)
        for t in range(4):
            for d in s.datagrams(t):
                f = gdl90.parse(d)[0]
                if f.msg_id == 0x21:
                    d = gdl90.frame(f.msg[:-2])  # drop 2 trailer bytes
                b.feed(d, SENTRY, float(t))
        self.assertEqual(len(b.tick(3.0)), 1)

    def test_keepalive_schedule(self):
        due = bridge.keepalive_due
        self.assertTrue(due(100.0, None, None))                        # at start
        self.assertFalse(due(129.0, 100.0, 128.5))                     # receiving: every 30 s
        self.assertTrue(due(130.0, 100.0, 129.5))
        self.assertFalse(due(104.0, 100.0, None))                      # silent: every 5 s
        self.assertTrue(due(105.0, 100.0, None))
        self.assertTrue(due(105.0, 100.0, 102.0))                      # 3 s without data is silent
        self.assertFalse(due(105.0, 100.0, 102.5))


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.s = sim.SimSentry()

    def _dgrams(self, seconds=10):
        for t in range(seconds):
            for d in self.s.datagrams(t):
                yield 1_700_000_000 + t, d

    def test_callsign_matches_pymodes_without_importing_it(self):
        import pyModeS
        self.assertEqual(capture.modes_callsign(bytes.fromhex("8D4840D6202CC371C32CE0576098")), "KLM1023")
        rng = random.Random(23)
        for _ in range(500):
            body = bytes([0x8D]) + rng.randbytes(3) + bytes([rng.randint(1, 4) << 3 | rng.randint(0, 7)]) + rng.randbytes(9)
            self.assertEqual(capture.modes_callsign(body), pyModeS.adsb.callsign(body.hex()).strip("_ "), body.hex())
        self.assertFalse(hasattr(capture, "pms"))  # packaged diagnostics must not need pyModeS

    def test_analyze_jsonl(self):
        p = self.tmp / "c.jsonl"
        with p.open("w") as f:
            for t, d in self._dgrams():
                f.write(json.dumps({"t": t, "src": SENTRY, "sport": 4000, "hex": d.hex()}) + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            capture.main(["analyze", str(p), "--report", str(self.tmp / "r.txt")])
        text = (self.tmp / "r.txt").read_text()
        self.assertIn("Mode: PUBLIC", text)
        self.assertIn("unique addresses: 5", text)
        self.assertIn("ASA1322", text)
        self.assertIn("0x25 len", text)
        # The shareable report leaves out this aircraft's position and identity unless asked.
        own = self.s.ownship
        self.assertIn("Last ownship: nic=", text)
        self.assertNotIn(f"{own.icao:06X}", text)
        self.assertNotIn("lat=", text.split("Last ownship")[1].splitlines()[0])
        with redirect_stdout(io.StringIO()):
            capture.main(["analyze", str(p), "--show-ownship", "--report", str(self.tmp / "r.txt")])
        self.assertIn(f"addr={own.icao:06X} lat=", (self.tmp / "r.txt").read_text())

    def test_analyze_pcap_and_pcapng(self):
        pkts = []
        for t, d in self._dgrams(4):
            udp = struct.pack("!HHHH", 4000, 4000, 8 + len(d), 0) + d
            ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                             socket.inet_aton(SENTRY), socket.inet_aton("192.168.4.100")) + udp
            eth = b"\xff" * 6 + b"\x02" * 6 + b"\x08\x00" + ip
            pkts.append((t, eth))
        pcap = self.tmp / "c.pcap"
        with pcap.open("wb") as f:
            f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
            for t, e in pkts:
                f.write(struct.pack("<IIII", t, 0, len(e), len(e)) + e)
        ng = self.tmp / "c.pcapng"
        with ng.open("wb") as f:
            shb = struct.pack("<IIIHHq", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1) + struct.pack("<I", 28)
            idb = struct.pack("<IIHHI", 1, 20, 1, 0, 65535) + struct.pack("<I", 20)
            f.write(shb + idb)
            for t, e in pkts:
                pad = (-len(e)) % 4
                ts = t * 1_000_000
                blen = 32 + len(e) + pad
                f.write(struct.pack("<IIIIIII", 6, blen, 0, ts >> 32, ts & 0xFFFFFFFF, len(e), len(e))
                        + e + b"\0" * pad + struct.pack("<I", blen))
        # The same packets with nanosecond timestamps (if_tsresol = 9) must give the same times.
        ns = self.tmp / "ns.pcapng"
        with ns.open("wb") as f:
            idb = struct.pack("<IIHHI", 1, 32, 1, 0, 65535) + struct.pack("<HHB3x", 9, 1, 9) + struct.pack("<HH", 0, 0) + struct.pack("<I", 32)
            f.write(shb + idb)
            for t, e in pkts:
                pad = (-len(e)) % 4
                ts = t * 1_000_000_000
                blen = 32 + len(e) + pad
                f.write(struct.pack("<IIIIIII", 6, blen, 0, ts >> 32, ts & 0xFFFFFFFF, len(e), len(e))
                        + e + b"\0" * pad + struct.pack("<I", blen))
        times = [[round(r[0], 6) for r in capture.read_pcap(p, 4000, Counter())] for p in (pcap, ng, ns)]
        self.assertEqual(times[0], times[1])
        self.assertEqual(times[0], times[2])
        for p in (pcap, ng):
            buf = io.StringIO()
            with redirect_stdout(buf):
                capture.main(["analyze", str(p)])
            out = buf.getvalue()
            self.assertIn("Mode: PUBLIC", out, p.name)
            self.assertIn("192.168.4.1 -> 192.168.4.100", out, p.name)


def _free_port():
    p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    p.bind(("127.0.0.1", 0))
    port = p.getsockname()[1]
    p.close()
    return port


class TestSockets(unittest.TestCase):
    """End to end over real UDP sockets: sim -> bridge -> listener."""

    def _run(self, extra_args, announce_to=None, seconds=5):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("0.0.0.0", 0))
        out_port = rx.getsockname()[1]
        rx.settimeout(0.5)
        in_port = _free_port()
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        args = [a.replace("{out}", str(out_port)) for a in extra_args]
        br = subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "--listen-port", str(in_port),
                               "--sentry-ip", "" if announce_to else "127.0.0.1", "--stats-every", "2", *args],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(iter(br.stdout.readline, b"")), daemon=True)
        reader.start()

        def wait_for_log(text, action=None, timeout=10):
            end = time.time() + timeout
            while time.time() < end:
                if any(text in line.decode(errors="replace") for line in list(lines)):
                    return
                if action:
                    action()
                time.sleep(.1)
            br.kill()
            self.fail(f"bridge never logged {text!r}:\n" + b"".join(lines).decode(errors="replace"))

        wait_for_log("listening")  # logged after the input socket is bound
        if announce_to:
            # The discovery socket binds after "listening", so repeat the announcement until the
            # bridge registers it, as real EFBs do. Any-source mode avoids needing a loopback alias.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a:
                a.bind(("127.0.0.1", 0))
                announcement = json.dumps({"App": "TestEFB", "GDL90": {"port": out_port}}).encode()
                wait_for_log("app 'TestEFB' announced", lambda: a.sendto(announcement, ("127.0.0.1", announce_to)))
        sm = subprocess.Popen([sys.executable, str(ROOT / "sim.py"), "--dest", f"127.0.0.1:{in_port}",
                               "--seconds", str(seconds)], stdout=subprocess.DEVNULL, env=env)
        seen = set()
        end = time.time() + seconds + 1
        try:
            while time.time() < end:
                try:
                    data, _ = rx.recvfrom(4096)
                except socket.timeout:
                    continue
                for f in gdl90.parse(data):
                    if f.crc_ok and f.msg_id == gdl90.MSG_TRAFFIC:
                        seen.add(gdl90.decode_report(f.msg).address)
        finally:
            sm.wait(timeout=10)
            br.terminate()
            br.wait(timeout=5)
            reader.join(timeout=5)
            log = b"".join(lines).decode(errors="replace")
            rx.close()
        return seen, log

    def test_udp_static_dest(self):
        seen, log = self._run(["--dest", "127.0.0.1:{out}", "--announce-port", "0"])
        self.assertEqual(len(seen), 5, log)

    def test_udp_announced_app(self):
        ann = _free_port()
        seen, log = self._run(["--dest", f"127.0.0.1:{_free_port()}", "--announce-port", str(ann)], announce_to=ann)
        self.assertEqual(seen, set(), log)
        self.assertIn("not sent (pass --dest)", log)

    def test_parse_announce(self):
        self.assertEqual(bridge.parse_announce(b'{"App":"ForeFlight","GDL90":{"port":4000}}'), ("ForeFlight", 4000))
        self.assertEqual(bridge.parse_announce(b"i-want-to-play-ffm-udp"), ("", None))
        self.assertEqual(bridge.parse_announce(b'{"GDL90":{"port":"x"}}'), ("", None))
        self.assertEqual(bridge.parse_announce('{"App":"Äir\\tEFB","GDL90":{"port":4001}}'.encode()), ("irEFB", 4001))


if __name__ == "__main__":
    unittest.main()
