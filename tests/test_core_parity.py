"""The C core must behave exactly like bridge.py: same traffic reports byte for byte, same relayed
frames, same counters, same mode decisions. Runs on simulated traffic (including a busy, lossy
scenario and UAT frames with injected byte errors), structured fuzz and random garbage.
Skips if no C compiler is available."""

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge  # noqa: E402
import gdl90  # noqa: E402
import sim  # noqa: E402
import uat  # noqa: E402
import sbcore_ffi  # noqa: E402
from tools import discovery  # noqa: E402

LIB = sbcore_ffi.load()
SENTRY = "192.168.4.1"
REF = sim.ORIGIN   # fallback CPR reference near the simulated traffic
STAT_FIELDS = ["datagrams", "crc_bad", "modes_msgs", "modes_other", "pos_decoded", "pos_rejected", "sent",
               "uat_msgs", "uat_fixed", "uat_bad", "uat_skipped"]


def busy_traffic():
    """Many 1090 targets plus UAT, no ownship (global CPR only), dropped datagrams, irregular timing."""
    return sim.busy_datagrams(seconds=150)[0]


def uat_datagrams():
    s = sim.SimSentry(tracks=sim.uat_tracks(), seed=3, send_ownship=False)
    return [(1_700_000_000.0 + t, d) for t in range(180) for d in s.datagrams(float(t))], s


def describe(frames):
    rows = []
    for fr in sorted(frames):
        r = gdl90.decode_report(gdl90.parse(fr)[0].msg)
        rows.append(f"{r.address:06X} {r.callsign!r} {r.lat} {r.lon} alt={r.alt_ft} gs={r.gs_kt} "
                    f"trk={r.track_deg} vs={r.vs_fpm} nic={r.nic} nacp={r.nacp} air={r.airborne} "
                    f"tt={r.track_type} em={r.emitter} at={r.addr_type}")
    return "\n    ".join(rows)


@unittest.skipIf(LIB is None, "no C compiler available to build core/sbcore.c")
class TestParity(unittest.TestCase):
    def pair(self, **kw):
        py = bridge.Bridge(sentry_ip=kw.get("sentry_ip", SENTRY), ownship_icao=kw.get("ownship_icao"),
                           ref=kw.get("ref"), stale_s=kw.get("stale_s", 15.0), relay=kw.get("relay", False),
                           log=lambda m: None)
        c = sbcore_ffi.CBridge(LIB, **kw)
        return py, c

    def replay(self, datagrams, src=SENTRY, tick_every=1.0, **kw):
        """Feed (t, datagram) pairs to both; tick both on the same schedule; compare everything."""
        py, c = self.pair(**kw)
        next_tick = datagrams[0][0] + tick_every
        ticks = reports = 0
        for t, d in datagrams:
            while t >= next_tick:
                a, b = sorted(py.tick(next_tick)), sorted(c.tick(next_tick))
                if a != b:
                    self.fail(f"tick at +{next_tick - datagrams[0][0]:.0f} s differs\n  python:\n    "
                              f"{describe(a)}\n  c:\n    {describe(b)}")
                ticks += 1
                reports += len(a)
                next_tick += tick_every
            ra, rb = py.feed(d, src, t), c.feed(d, src, t)
            self.assertEqual(ra, rb, "relayed frames differ")
            self.assertEqual(py.wants_enable(t), c.wants_enable(t))
            self.assertEqual(py.mode, c.mode)
        self.assertEqual(py.stream_starts, c.stream_starts)
        cs = c.stats
        for f in STAT_FIELDS:
            self.assertEqual(getattr(py.stats, f), getattr(cs, f), f)
        for k, v in py.stats.frames.items():
            self.assertEqual(v, cs.frames[k], f"frame count 0x{k:02X}")
        return ticks, reports, py

    def test_sim_with_fix_and_relay(self):
        s = sim.SimSentry(gps_fix=True, fisb=True, opstatus=True)
        s.tracks[1].adsb_version = 0  # no NACp field: stays unknown
        dg = [(1_700_000_000.0 + t, d) for t in range(120) for d in s.datagrams(t)]
        ticks, reports, py = self.replay(dg, relay=True)
        self.assertGreater(reports, 500)
        self.assertEqual(py.stats.frames[gdl90.MSG_UPLINK], 120)
        self.assertEqual(sorted(t.nacp for t in py.targets.values()), [0, 9, 9, 9, 9])

    def test_sim_no_fix(self):
        s = sim.SimSentry(gps_fix=False)
        dg = [(1_700_000_000.0 + t, d) for t in range(60) for d in s.datagrams(t)]
        self.replay(dg)

    def test_busy_traffic_global_decode(self):
        ticks, reports, py = self.replay(busy_traffic())
        self.assertGreater(py.stats.pos_decoded, 1000)
        self.assertGreater(reports, 2000)

    def test_busy_traffic_with_fixed_reference(self):
        self.replay(busy_traffic(), ref=REF)

    def test_busy_traffic_ownship_filter_and_short_stale(self):
        self.replay(busy_traffic(), ownship_icao=0xA10000, stale_s=5.0)

    def test_uat_with_byte_errors(self):
        dg, s = uat_datagrams()
        ticks, reports, py = self.replay(dg)
        self.assertEqual(py.stats.uat_msgs, s.uat_sent["clean"] + s.uat_sent["correctable"])
        self.assertEqual(py.stats.uat_bad, s.uat_sent["uncorrectable"])
        self.assertGreater(reports, 200)

    def test_source_filter_and_any_source(self):
        dg = busy_traffic()[:200]
        self.replay(dg, src="192.168.4.7")                    # wrong source: everything ignored
        self.replay(dg, src="192.168.4.7", sentry_ip="")      # any source accepted

    def test_mode_watchdog(self):
        hb = gdl90.encode_heartbeat(True, 100)
        enc = gdl90.frame(bytes((0x26, 0x00)) + bytes(18))
        seq = []
        t = 1_700_000_000.0
        for frame, n, gap in ((enc, 70, 0), (hb, 30, 0), (enc, 30, 0), (hb, 10, 40), (enc, 50, 30), (hb, 5, 0)):
            t += gap
            for _ in range(n):
                seq.append((t, frame))
                t += 1.0
        self.replay(seq)

    def test_structured_fuzz_every_message_type(self):
        """DF17/DF18 messages of every typecode for a few aircraft, so state builds up and every
        decode path (heading/airspeed velocity, surface, Gillham altitude, CPR, emitter) is compared."""
        rng = random.Random(77)
        icaos = [0xA00001, 0xA12345, 0xAB761C, 0xC0FFEE, 0x0D0FF3]
        tcs = list(range(1, 23)) + [19, 19, 19, 11, 11, 12, 20, 5, 6]
        for ref in (None, REF):
            seq = []
            t = 1_700_000_000.0
            for i in range(6000):
                if i % 40 == 0:   # ownship now and then, near the reference
                    own = gdl90.Report(address=0, lat=REF[0] + rng.uniform(-0.2, 0.2), lon=REF[1] + rng.uniform(-0.2, 0.2),
                                       alt_ft=rng.choice([None, 1300, 4500]), nic=rng.choice([0, 8, 9]))
                    seq.append((t, gdl90.encode_report(own, gdl90.MSG_OWNSHIP)))
                tc = rng.choice(tcs)
                me0 = (tc << 3) | rng.randrange(8)
                if tc == 19 and rng.random() < 0.5:
                    me0 = (19 << 3) | rng.choice([3, 4])          # airspeed + heading subtypes
                df = rng.choice([0x8D, 0x8D, 0x8D, 0x90, 0x91, 0x92, 0x95])
                body = bytes([df]) + rng.choice(icaos).to_bytes(3, "big") + bytes([me0]) + \
                    bytes(rng.randrange(256) for _ in range(6)) + bytes(rng.randrange(256) for _ in range(3))
                seq.append((t, gdl90.frame(b"\x21\xff\xff\xff" + body)))
                t += rng.choice([0.05, 0.2, 0.5, 1.0, 3.0])
            ticks, reports, py = self.replay(seq, ref=ref)
            self.assertGreater(py.stats.pos_decoded, 50)
            self.assertGreater(reports, 50)

    def test_garbage(self):
        rng = random.Random(1234)
        seq = []
        t = 1_700_000_000.0
        for _ in range(4000):
            kind = rng.random()
            if kind < 0.4:      # random bytes, mostly bad CRC
                d = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 80)))
            else:               # CRC-valid frames with interesting IDs and random bodies
                mid = rng.choice([0x00, 0x0A, 0x21, 0x21, 0x24, 0x26, 0x14, 0x7E])
                body = bytes(rng.randrange(256) for _ in range(rng.choice([1, 3, 6, 10, 14, 17, 18, 27, 34, 52, 60])))
                if mid == 0x21 and rng.random() < 0.5:
                    body = b"\xff\xff\xff" + bytes([rng.choice([0x8D, 0x8E, 0x90, 0x95])]) + body[:13]
                d = gdl90.frame(bytes([mid]) + body)
            seq.append((t, d))
            t += rng.choice([0.01, 0.1, 0.5, 1.0, 12.0])
        self.replay(seq, relay=True, ref=REF)

    def test_oversized_frames_are_counted_as_bad(self):
        """Frames longer than the C core's 1024-byte buffer: both count them as bad and relay nothing
        of them, and a frame of exactly 1024 bytes (CRC included) is still processed."""
        t = 1_700_000_000.0
        seq = []
        for n in (1021, 1022, 1023, 1200):   # message bytes, ID included; the frame adds 2 CRC bytes
            uplink = gdl90.frame(bytes([0x07]) + bytes((i * 7) % 251 for i in range(n - 1)))
            seq.append((t, uplink + gdl90.encode_heartbeat(True, 0)))
            t += 1
        ticks, reports, py = self.replay(seq, relay=True)
        self.assertEqual(py.stats.crc_bad, 2)
        self.assertEqual(py.stats.frames[gdl90.MSG_UPLINK], 2)

    def test_target_table_limit(self):
        """More aircraft than the C table holds (256): both keep and replace the same targets, for
        1090 and UAT, with distinct and identical arrival times, refreshes and slot reuse."""
        def position(i, odd, base=0xA00000):
            lat, lon = REF[0] + (i % 20) * 0.05, REF[1] + (i // 20) * 0.05
            if i % 10 == 9:   # every tenth target is UAT
                payload = sim.uat_payload(base + 0x10000 + i, lat, lon, 3000, 100, 90, 0)
                return sim.wrap_0x24(uat.rs_encode(payload, uat.LONG_ROOTS))
            me = sim.me_airborne_position(lat, lon, 3000 + 25 * i, odd)
            return gdl90.frame(b"\x21\xff\xff\xff" + sim.df17(base + i, me))
        for step in (0.001, 0.0):
            with self.subTest(step=step):
                t = 1_700_000_000.0
                seq = []
                for i in range(300):
                    seq.append((t, position(i, i % 2)))
                    t += step
                for i in range(50):   # the first aircraft again: replaced ones return, kept ones refresh
                    seq.append((t, position(i, 1 - i % 2)))
                    t += step
                t += 61               # everything expires; new aircraft reuse the freed slots
                seq += [(t, position(i, 0, base=0xB00000)) for i in range(20)]
                seq.append((t + 2, gdl90.encode_heartbeat(True, 0)))
                ticks, reports, py = self.replay(seq, ref=REF)
                self.assertEqual(len(py.targets), 20)
                self.assertGreater(reports, 256)

    def test_helpers(self):
        for data in (b'{"App":"ForeFlight","GDL90":{"port":4000}}', b"i-want-to-play-ffm-udp",
                     b'{"GDL90":{"port":"x"}}', b'{"GDL90":{"port":4001}}', b'{"App":"X","GDL90":{"port":0}}',
                     b'{"GDL90":{"port":true}}', b'{"GDL90":{"port":false}}',
                     # Ends right after a key: nothing may be read past the input
                     b'{"GDL90":', b'{"GDL90":{"port":',
                     b'{"Version":"1","GDL90":{"port":4000},"App":"ForeFlight","DeviceName":"ipad"}',
                     # Announced names become printable ASCII labels on every platform
                     '{"App":"Flüg EFB ✈","GDL90":{"port":4000}}'.encode(),
                     b'{"App":"A\\\\B","GDL90":{"port":4000}}',
                     b'{"App":"' + b"N" * 100 + b'","GDL90":{"port":4000}}'):
            app_py, port_py = bridge.parse_announce(data)
            # The GUIs' copy (tools/discovery.py, kept free of the decoder's imports) must agree too.
            self.assertEqual(discovery.parse_announce(data), (app_py, port_py), data)
            app = sbcore_ffi.ctypes.create_string_buffer(64)
            port = LIB.sb_parse_announce(data, len(data), app, 64)
            self.assertEqual(port_py, None if port < 0 else port, data)
            if port_py:
                self.assertEqual(app_py, app.value.decode(), data)
        # C reads the port without a full JSON parse, so a cut-off App value leaves the "?" label.
        data = b'{"GDL90":{"port":4000},"App":  '
        app = sbcore_ffi.ctypes.create_string_buffer(64)
        self.assertEqual(LIB.sb_parse_announce(data, len(data), app, 64), 4000)
        self.assertEqual(app.value, b"?")
        for js in ('{"Power":{"autoOnEnabled":false,"powerSavings":true}}', '{"Power":{"powerSavings":false}}',
                   '{"Power":', '{"Power":{"powerSavings":',
                   "null pagebuffer", '{"Power":{}}', '{"client":{"powerSavings":true}}'):
            py = bridge.power_savings_on(js)
            c = LIB.sb_power_savings_on(js.encode(), len(js))
            self.assertEqual(py, {1: True, 0: False, -1: None}[c], js)

    def test_rs_matches_python(self):
        rng = random.Random(5)
        good = uat.rs_encode(bytes(rng.randrange(256) for _ in range(34)), 14)
        out = sbcore_ffi.ctypes.create_string_buffer(48)
        nerr = sbcore_ffi.ctypes.c_int()
        for _ in range(2000):
            cw = bytearray(good)
            for p in rng.sample(range(48), rng.randint(0, 10)):
                cw[p] ^= rng.randint(1, 255)
            py = uat.rs_correct(bytes(cw), 14)
            ok = LIB.sb_uat_correct(bytes(cw), 48, out, sbcore_ffi.ctypes.byref(nerr))
            self.assertEqual(py, (out.raw[:34], nerr.value) if ok else None)


if __name__ == "__main__":
    unittest.main()
