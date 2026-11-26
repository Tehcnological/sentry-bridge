"""GDL90 meanings checked independently for Python and C, beyond byte-for-byte parity.

Positions and identities are synthetic, except the published worked examples in
test_public_extended_squitter_vectors and the sanitized recording (tests/data/compat-sanitized.txt.gz,
real traffic with synthetic addresses, callsigns and location). GNSS height is encoded directly in
metres, because sim.me_airborne_position encodes the barometric format only.
"""

import gzip
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge
import gdl90
import sbcore_ffi
import sim
import uat

SENTRY = "192.168.4.1"
ADDR = 0xA12345
LIB = sbcore_ffi.load()


def modes(me, addr=ADDR, df=0x8D):
    """0x21 frame; df is the whole first byte (DF and CA/CF), with parity computed for it."""
    return gdl90.frame(b"\x21\xff\xff\xff" + sim.df17(addr, me, ca=df & 7, df=df >> 3))


def surface_me(lat, lon, odd=0, tc=5):
    """Surface CPR (90-degree zones). Movement and track bits stay zero."""
    dlat = 90.0 / (60 - odd)
    yz = int(math.floor((1 << 17) * sim._mod(lat, dlat) / dlat + 0.5)) & 0x1FFFF
    rlat = dlat * (yz / (1 << 17) + math.floor(lat / dlat))
    dlon = 90.0 / max(sim.cpr_nl(rlat) - odd, 1)
    xz = int(math.floor((1 << 17) * sim._mod(lon, dlon) / dlon + 0.5)) & 0x1FFFF
    return (tc << 51) | (odd << 34) | (yz << 17) | xz


def position(tc=11, altitude=2500, odd=0, df=0x8D):
    yz, xz = sim.cpr_encode(*sim.ORIGIN, odd)
    if tc >= 20:
        me = tc << 51 | altitude << 36 | odd << 34 | yz << 17 | xz
    else:
        me = sim.me_airborne_position(*sim.ORIGIN, altitude, odd, tc)
    return modes(me, df=df)


def uat_frame(qualifier=0, ptype=1, alt=2500, geo_primary=False, callsign="UAT", nacp=10):
    data = bytearray(sim.uat_payload(ADDR, *sim.ORIGIN, alt, 100, 90, 0, ptype=ptype,
                                   qualifier=qualifier, callsign=callsign, nacp=nacp,
                                   geo_alt_ft=3000))
    if geo_primary:
        data[9] |= 1  # primary height is geometric; the auxiliary height is pressure
    return sim.wrap_0x24(uat.rs_encode(bytes(data), uat.LONG_ROOTS))


class TrafficSemantics:
    def make_bridge(self, **kw):
        raise NotImplementedError

    def reports(self, b, now):
        return [gdl90.decode_report(gdl90.parse(fr)[0].msg) for fr in b.tick(now)]

    FAR = (sim.ORIGIN[0] + 100 / 60, sim.ORIGIN[1])  # 100 NM north of the established track

    def at(self, position, odd):
        return modes(sim.me_airborne_position(*position, 2500, odd))

    def established(self):
        """A track at the origin (last accepted at t=12) whose odd message is >10 s old by t=13."""
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(self.at(sim.ORIGIN, 1), SENTRY, 0.0)
        for t in range(1, 13):
            b.feed(self.at(sim.ORIGIN, 0), SENTRY, float(t))
        self.assertEqual(len(self.reports(b, 12.5)), 1)
        return b

    def assertNothingFar(self, reports):
        for report in reports:
            self.assertLess(bridge.dist_nm(report.lat, report.lon, *sim.ORIGIN), 1, report)

    def test_repeated_jumps_cannot_return_through_fallback_decodes(self):
        b = self.established()
        # Reference fallback: the local decode rejects each jump; repeats must not pass either.
        for t in (13.0, 14.0, 15.0):
            b.feed(self.at(self.FAR, 0), SENTRY, t)
            self.assertNothingFar(self.reports(b, t + 0.5))
        # Global fallback: a consistent far even/odd pair is still a jump from the last accepted fix.
        for t, odd in ((16.0, 1), (17.0, 0), (18.0, 1)):
            b.feed(self.at(self.FAR, odd), SENTRY, t)
            self.assertNothingFar(self.reports(b, t + 0.5))
        self.assertEqual(b.summary().split("positioned=")[1].split()[0], "0")

    def test_track_resumes_at_the_last_fix_or_reacquires_after_30_s(self):
        b = self.established()
        b.feed(self.at(self.FAR, 0), SENTRY, 13.0)
        self.assertEqual(self.reports(b, 13.5), [])  # lost: nothing reported
        b.feed(self.at(sim.ORIGIN, 0), SENTRY, 14.0)  # plausible again: reported at once
        self.assertEqual(len(self.reports(b, 14.5)), 1)
        # Once the last accepted fix (t=14) is over 30 s old, a persistent new position is accepted.
        for t in (20.0, 45.0):
            b.feed(self.at(self.FAR, 0), SENTRY, t)
        reports = self.reports(b, 45.5)
        self.assertEqual(len(reports), 1)
        self.assertLess(bridge.dist_nm(reports[0].lat, reports[0].lon, *self.FAR), 1)

    def test_closely_spaced_messages_keep_the_track(self):
        # CPR rounding between messages a few milliseconds apart is not a speed violation.
        b = self.make_bridge(ref=sim.ORIGIN)
        lat, lon = sim.ORIGIN
        for i in range(20):
            b.feed(modes(sim.me_airborne_position(lat + i * 0.00005, lon, 2500, i % 2)), SENTRY, i * 0.01)
        self.assertEqual(b.stats.pos_rejected, 0)
        self.assertEqual(len(self.reports(b, 0.5)), 1)

    def test_gnss_only_has_position_and_unknown_pressure_altitude(self):
        for tc in (20, 21, 22):
            with self.subTest(tc=tc):
                b = self.make_bridge(ref=sim.ORIGIN)
                b.feed(position(tc, 1200), SENTRY, 0)
                r, = self.reports(b, 1)
                # TC22 carries NIC=0; decode_report hides such coordinates by design.
                if tc != 22:
                    self.assertAlmostEqual(r.lat, sim.ORIGIN[0], places=4)
                else:
                    self.assertEqual(r.nic, 0)
                self.assertIsNone(r.alt_ft)
                self.assertEqual(r.nacp, 0)

    def test_operational_status_sets_nacp_only_for_versions_with_the_field(self):
        for version, expected in ((0, 0), (1, 8), (2, 10)):
            with self.subTest(version=version):
                b = self.make_bridge(ref=sim.ORIGIN)
                b.feed(position(), SENTRY, 0)
                self.assertEqual(self.reports(b, 1)[0].nacp, 0)  # unknown until status arrives
                b.feed(modes(sim.me_operational_status(expected or 9, version)), SENTRY, 1)
                b.feed(position(), SENTRY, 1)
                self.assertEqual(self.reports(b, 2)[0].nacp, expected)

    def test_surface_operational_status_and_reserved_subtypes(self):
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(position(), SENTRY, 0)
        b.feed(modes(sim.me_operational_status(7, 2, subtype=2)), SENTRY, 0)  # reserved: ignored
        self.assertEqual(self.reports(b, 1)[0].nacp, 0)
        b.feed(modes(sim.me_operational_status(7, 2, subtype=1)), SENTRY, 1)  # surface status
        b.feed(position(), SENTRY, 1)
        self.assertEqual(self.reports(b, 2)[0].nacp, 7)

    def test_alternating_altitude_sources_and_pressure_freshness(self):
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(position(), SENTRY, 0)
        b.feed(position(20, 1200, 1), SENTRY, 1)  # ~3937 ft geometric, not 2500 ft pressure
        r, = self.reports(b, 2)
        self.assertEqual(r.alt_ft, 2500)
        b.feed(position(21, 1300), SENTRY, 14)
        self.assertEqual(self.reports(b, 15)[0].alt_ft, 2500)
        r, = self.reports(b, 16)
        self.assertIsNone(r.alt_ft)  # continued positions don't keep old pressure altitude alive
        b.feed(position(11, 2700, 1), SENTRY, 17)
        self.assertEqual(self.reports(b, 18)[0].alt_ft, 2700)

    def test_unavailable_baro_altitude_clears_previous_value(self):
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(position(), SENTRY, 0)
        me = sim.me_airborne_position(*sim.ORIGIN, 2500, 1) & ~(0xFFF << 36)
        b.feed(modes(me), SENTRY, 1)
        self.assertIsNone(self.reports(b, 2)[0].alt_ft)

    def test_uat_geometric_primary_uses_pressure_auxiliary(self):
        b = self.make_bridge()
        b.feed(uat_frame(geo_primary=True), SENTRY, 0)
        self.assertEqual(self.reports(b, 1)[0].alt_ft, 3000)
        b.feed(uat_frame(ptype=3, alt=4000, geo_primary=True), SENTRY, 14)
        self.assertEqual(self.reports(b, 15)[0].alt_ft, 3000)
        self.assertIsNone(self.reports(b, 16)[0].alt_ft)
        fresh = self.make_bridge()
        fresh.feed(uat_frame(ptype=3, geo_primary=True), SENTRY, 0)
        self.assertIsNone(self.reports(fresh, 1)[0].alt_ft)

    def test_address_namespaces_keep_distinct_callsigns_and_altitudes(self):
        b = self.make_bridge(ref=sim.ORIGIN)
        for at, df in ((0, 0x8D), (1, 0x91), (2, 0x92), (3, 0x95)):
            b.feed(modes(sim.me_identification(f"TYPE{at}"), df=df), SENTRY, 0)
            b.feed(position(altitude=2500 + 100 * at, df=df), SENTRY, 0)
        b.feed(uat_frame(qualifier=4, callsign="GROUND", alt=1000), SENTRY, 0)
        reps = {r.addr_type: r for r in self.reports(b, 1)}
        self.assertEqual(set(reps), {0, 1, 2, 3, 4})
        for at in range(4):
            self.assertEqual(reps[at].callsign, f"TYPE{at}")
            self.assertEqual(reps[at].alt_ft, 2500 + 100 * at)
            self.assertEqual(reps[at].nacp, 0)
        self.assertEqual(reps[4].callsign, "GROUND")

    def test_same_icao_can_fuse_across_bands(self):
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(position(), SENTRY, 0)
        b.feed(uat_frame(alt=2600), SENTRY, 1)
        r, = self.reports(b, 2)
        self.assertEqual((r.addr_type, r.address, r.alt_ft, r.nacp), (0, ADDR, 2600, 10))

    def test_ownship_filter_preserves_non_icao_namespaces(self):
        b = self.make_bridge(ref=sim.ORIGIN, ownship_icao=ADDR)
        for df in (0x8D, 0x91, 0x92, 0x95):
            b.feed(position(df=df), SENTRY, 0)
        for q in (0, 1, 2, 3, 4, 6):
            b.feed(uat_frame(qualifier=q), SENTRY, 0)
        self.assertEqual({r.addr_type for r in self.reports(b, 1)}, {1, 3, 4})

    def test_uat_accuracy_unknown_until_mode_status(self):
        b = self.make_bridge()
        b.feed(uat_frame(ptype=2), SENTRY, 0)
        self.assertEqual(self.reports(b, 1)[0].nacp, 0)
        b.feed(uat_frame(nacp=9), SENTRY, 2)
        self.assertEqual(self.reports(b, 3)[0].nacp, 9)

    def test_global_fix_beyond_local_cpr_radius(self):
        far = (sim.ORIGIN[0] + 200 / 60.0, sim.ORIGIN[1])
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(modes(sim.me_airborne_position(*far, 2500, 1)), SENTRY, 0)
        b.feed(modes(sim.me_airborne_position(*far, 2500, 0)), SENTRY, 1)
        reports = self.reports(b, 2)
        self.assertEqual(len(reports), 1)
        self.assertLess(bridge.dist_nm(reports[0].lat, reports[0].lon, *far), 1)
        # Beyond 1090 radio range from ownship, a first global fix is bad data.
        too_far = (sim.ORIGIN[0] + 400 / 60.0, sim.ORIGIN[1])
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(modes(sim.me_airborne_position(*too_far, 2500, 1)), SENTRY, 0)
        b.feed(modes(sim.me_airborne_position(*too_far, 2500, 0)), SENTRY, 1)
        self.assertEqual(self.reports(b, 2), [])

    def test_local_cpr_folds_longitude(self):
        there = (0.0, -179.9)
        b = self.make_bridge(ref=(0.0, 179.8))
        b.feed(modes(sim.me_airborne_position(*there, 2500, 0)), SENTRY, 0)
        reports = self.reports(b, 1)
        self.assertEqual(len(reports), 1)
        self.assertLess(abs(reports[0].lon - there[1]), 0.2)
        self.assertGreaterEqual(reports[0].lon, -180)
        self.assertLessEqual(reports[0].lon, 180)

    def test_surface_jump_is_rejected(self):
        b = self.make_bridge(ref=sim.ORIGIN)
        b.feed(self.at(sim.ORIGIN, 1), SENTRY, 0)
        b.feed(self.at(sim.ORIGIN, 0), SENTRY, 1)
        jumped = (sim.ORIGIN[0] + 30 / 60.0, sim.ORIGIN[1])
        b.feed(modes(surface_me(*jumped)), SENTRY, 1.1)
        reports = self.reports(b, 1.6)
        self.assertEqual(len(reports), 1)
        self.assertLess(bridge.dist_nm(reports[0].lat, reports[0].lon, *sim.ORIGIN), 1)

    def test_monotonic_ages_at_boot_and_after_long_uptime(self):
        for start in (0, 1234567):
            with self.subTest(start=start):
                b = self.make_bridge(ref=sim.ORIGIN)
                b.feed(position(), SENTRY, start)
                self.assertEqual(len(self.reports(b, start + 15)), 1)
                self.assertEqual(self.reports(b, start + 16), [])


class FrameRules:
    """Framing, parity, integrity and freshness rules, each checked on Python and C."""

    @staticmethod
    def raw(hexmsg, tor=b"\xff\xff\xff"):
        return gdl90.frame(b"\x21" + tor + bytes.fromhex(hexmsg))

    def test_public_extended_squitter_vectors(self):
        """Worked examples published in J. Sun, "The 1090 Megahertz Riddle" (2nd ed.), not produced
        by this project's encoder: identification, an even/odd airborne pair and a velocity."""
        b = self.make_bridge()   # no reference: the pair must decode globally
        b.feed(self.raw("8D40621D58C386435CC412692AD6"), SENTRY, 0)   # odd
        b.feed(self.raw("8D40621D58C382D690C8AC2863A7"), SENTRY, 10)  # even, newest
        b.feed(self.raw("8D4840D6202CC371C32CE0576098"), SENTRY, 10)  # KLM1023
        b.feed(self.raw("8D485020994409940838175B284F"), SENTRY, 10)  # 159.20 kt, 182.88 deg, -832 fpm
        for addr, lat in ((0x4840D6, 52.3), (0x485020, 52.2)):        # synthetic positions for those two
            for odd in (0, 1):
                b.feed(modes(sim.me_airborne_position(lat, 3.9, 20000, odd), addr=addr), SENTRY, 10)
        reps = {r.address: r for r in self.reports(b, 11)}
        self.assertEqual(set(reps), {0x40621D, 0x4840D6, 0x485020})
        pos = reps[0x40621D]
        self.assertAlmostEqual(pos.lat, 52.25720, places=4)
        self.assertAlmostEqual(pos.lon, 3.91937, places=4)
        self.assertEqual(pos.alt_ft, 38000)
        self.assertEqual(reps[0x4840D6].callsign, "KLM1023")
        vel = reps[0x485020]
        self.assertEqual((vel.gs_kt, vel.vs_fpm), (159, -832))
        self.assertAlmostEqual(vel.track_deg, 182.88, delta=360 / 256)
        self.assertEqual(b.stats.modes_bad_parity, 0)

    def test_every_single_bit_error_is_rejected(self):
        good = bytes.fromhex("8D40621D58C382D690C8AC2863A7")
        b = self.make_bridge(ref=(52.25, 3.92))
        for i in range(112):
            bad = bytearray(good)
            bad[i // 8] ^= 0x80 >> (i % 8)
            b.feed(gdl90.frame(b"\x21\xff\xff\xff" + bad), SENTRY, 0)
        self.assertEqual(self.reports(b, 1), [])
        self.assertEqual(b.stats.modes_msgs, 0)
        self.assertEqual(b.stats.modes_other, 5)          # a flipped DF bit is no longer DF17
        self.assertEqual(b.stats.modes_bad_parity, 107)

    def test_df18_variants_need_their_own_parity(self):
        b = self.make_bridge(ref=sim.ORIGIN)
        for cf in (0, 1, 2, 5, 6):   # ADS-B non-transponder, non-ICAO, TIS-B fine, TIS-B other, ADS-R
            b.feed(position(df=0x90 | cf), SENTRY, 0)
        self.assertEqual((b.stats.modes_msgs, b.stats.modes_bad_parity), (5, 0))
        wrong = bytearray(sim.df17(ADDR, sim.me_airborne_position(*sim.ORIGIN, 2500, 0)))
        wrong[0] = 0x90                                    # DF17 parity under a DF18 header
        b.feed(gdl90.frame(b"\x21\xff\xff\xff" + wrong), SENTRY, 0)
        self.assertEqual((b.stats.modes_msgs, b.stats.modes_bad_parity), (5, 1))

    def test_time_of_reception_value_does_not_matter(self):
        for tor in (b"\xff\xff\xff", b"\x12\x34\x56", b"\x00\x00\x00"):
            b = self.make_bridge(ref=sim.ORIGIN)
            b.feed(gdl90.frame(b"\x21" + tor + sim.df17(ADDR, sim.me_airborne_position(*sim.ORIGIN, 2500, 0))),
                   SENTRY, 0)
            u = bytearray(gdl90.parse(uat_frame(qualifier=1))[0].msg)
            u[1:4] = tor
            b.feed(gdl90.frame(bytes(u)), SENTRY, 0)
            self.assertEqual({r.addr_type for r in self.reports(b, 1)}, {0, 1}, tor.hex())

    def test_nic_follows_integrity_supplements(self):
        # (typecode, op-status version or None, supplement-A, supplement-B) -> NIC
        cases = {(11, None, 1, 1): 8, (11, 2, 1, 1): 9, (11, 2, 1, 0): 8, (11, 2, 0, 1): 8, (11, 1, 1, 0): 9,
                 (11, 0, 1, 1): 8, (16, None, 0, 0): 2, (16, 2, 1, 1): 3, (16, 2, 0, 0): 2, (16, 1, 1, 0): 3,
                 (12, 2, 1, 1): 7, (10, None, 0, 0): 10}
        for (tc, version, sup_a, sup_b), nic in cases.items():
            b = self.make_bridge(ref=sim.ORIGIN)
            if version is not None:
                b.feed(modes(sim.me_operational_status(9, version, nic_sup_a=sup_a)), SENTRY, 0)
            b.feed(modes(sim.me_airborne_position(*sim.ORIGIN, 2500, 0, tc, nic_sup_b=sup_b)), SENTRY, 0)
            self.assertEqual(self.reports(b, 1)[0].nic, nic, (tc, version, sup_a, sup_b))

    def test_uat_payload_type_must_match_length(self):
        payload = lambda ptype: sim.uat_payload(ADDR, *sim.ORIGIN, 2500, 100, 90, 0, ptype=ptype)
        frame = lambda block: gdl90.frame(b"\x24\xff\xff\xff" + block)
        b = self.make_bridge()
        b.feed(frame(uat.rs_encode(payload(0), uat.LONG_ROOTS)), SENTRY, 0)          # long, type 0
        b.feed(frame(uat.rs_encode(payload(1)[:18], uat.BASIC_ROOTS)), SENTRY, 0)    # basic, type 1
        self.assertEqual((b.stats.uat_bad, b.stats.uat_msgs), (2, 0))
        self.assertEqual(self.reports(b, 1), [])
        b.feed(frame(uat.rs_encode(payload(0)[:18], uat.BASIC_ROOTS)), SENTRY, 0)    # basic, type 0
        self.assertEqual(b.stats.uat_msgs, 1)
        self.assertEqual(len(self.reports(b, 1)), 1)

    def test_velocity_and_track_age_on_their_own(self):
        b = self.make_bridge(ref=sim.ORIGIN, stale_s=15.0)
        b.feed(modes(sim.me_velocity(250, 90, 1000)), SENTRY, 0)
        for t in range(21):   # positions keep arriving, velocity does not
            b.feed(position(odd=t % 2), SENTRY, t)
            r, = self.reports(b, t)
            if t <= 15:
                self.assertEqual((r.gs_kt, r.track_deg, r.vs_fpm), (250, 90.0, 1024), t)
            else:
                self.assertEqual((r.gs_kt, r.track_deg, r.vs_fpm), (None, None, None), t)
            self.assertEqual(r.alt_ft, 2500)

    def test_sanitized_real_recording_is_not_rejected(self):
        """Traffic recorded from a real Sentry in Compatibility Mode, sanitized (see the file header):
        every Mode S frame must pass the parity check and every kept UAT frame must correct."""
        b = self.make_bridge(ref=None)
        for t, d in recording():
            b.feed(d, SENTRY, t)
        s = b.stats
        self.assertEqual((s.modes_bad_parity, s.modes_other, s.crc_bad, s.uat_bad), (0, 0, 0, 0))
        self.assertEqual(s.modes_msgs, 8266)
        self.assertEqual(s.uat_msgs, 18)
        self.assertGreater(s.pos_decoded, 3000)


    N = bridge.MAX_TARGETS
    STEP = 128.0 / bridge.MAX_TARGETS   # NM between neighbouring targets of fill_table

    def fill_table(self, b, with_ownship=True):
        """Every slot: positioned targets up to 128 NM south of the origin (address n at n * STEP NM). The
        nearest is heard first, so least-recently-heard and farthest-first pick different victims."""
        if with_ownship:
            b.feed(gdl90.encode_report(gdl90.Report(address=0, lat=sim.ORIGIN[0], lon=sim.ORIGIN[1], alt_ft=1000,
                                                    nic=9), gdl90.MSG_OWNSHIP), SENTRY, 0)
        for nm in range(1, bridge.MAX_TARGETS + 1):
            lat = sim.ORIGIN[0] - nm * self.STEP / 60.0
            t = 0.256 * nm / self.N
            for odd in (0, 1):
                b.feed(modes(sim.me_airborne_position(lat, sim.ORIGIN[1], 5000, odd), addr=nm), SENTRY, t)

    def test_full_table_evicts_the_farthest_positioned_target(self):
        b = self.make_bridge()
        self.fill_table(b)
        b.feed(modes(sim.me_identification("NEW")), SENTRY, 1)
        b.feed(gdl90.encode_report(gdl90.Report(address=0, lat=sim.ORIGIN[0], lon=sim.ORIGIN[1], alt_ft=1000,
                                                nic=9), gdl90.MSG_OWNSHIP), SENTRY, 1)
        addresses = {r.address for r in self.reports(b, 1)}
        self.assertNotIn(self.N, addresses)         # farthest, though not the least recently heard
        self.assertIn(1, addresses)                 # nearest, though heard first
        self.assertEqual(len(addresses), bridge.MAX_TARGETS - 1)

    def test_full_table_ranks_distances_from_the_current_reference(self):
        """Ownship jumps between two full-table scans less than a second apart: the second scan must rank
        every target from the new position, not reuse distances measured from the old one."""
        b = self.make_bridge()
        self.fill_table(b)
        b.feed(modes(sim.me_identification("NEW1"), addr=0xABCDE0), SENTRY, 1)       # evicts N, the farthest
        b.feed(gdl90.encode_report(gdl90.Report(address=0, lat=sim.ORIGIN[0] - 200 / 60.0, lon=sim.ORIGIN[1],
                                                alt_ft=1000, nic=9), gdl90.MSG_OWNSHIP), SENTRY, 1.1)
        b.feed(modes(sim.me_identification("NEW2"), addr=0xABCDE1), SENTRY, 1.2)     # now 1 is the farthest
        addresses = {r.address for r in self.reports(b, 1.3)}
        self.assertNotIn(1, addresses)
        self.assertIn(self.N - 1, addresses)

    def test_full_table_ranks_a_moved_1090_target_by_its_new_position(self):
        """A target's cached distance must not outlive its position: N-1 moves 2 NM in between two scans,
        so N-2 is now the farthest."""
        b = self.make_bridge(ref=sim.ORIGIN)
        self.fill_table(b, with_ownship=False)
        b.feed(modes(sim.me_identification("NEW1"), addr=0xABCDE0), SENTRY, 1)       # evicts N, caches all
        moved = ((self.N - 1) * self.STEP - 2) / 60
        for odd in (0, 1):
            b.feed(modes(sim.me_airborne_position(sim.ORIGIN[0] - moved, sim.ORIGIN[1], 5000, odd), addr=self.N - 1),
                   SENTRY, 10)
        b.feed(modes(sim.me_identification("NEW2"), addr=0xABCDE1), SENTRY, 10.1)
        addresses = {r.address for r in self.reports(b, 10.2)}
        self.assertIn(self.N - 1, addresses)
        self.assertNotIn(self.N - 2, addresses)

    def test_full_table_ranks_a_moved_uat_target_by_its_new_position(self):
        """The same for a UAT target, whose positions take another path: it moves out 2 NM and becomes
        the farthest."""
        b = self.make_bridge(ref=sim.ORIGIN)
        self.fill_table(b, with_ownship=False)
        uat_at = lambda nm: sim.wrap_0x24(uat.rs_encode(
            sim.uat_payload(0xABC000, sim.ORIGIN[0] - nm / 60, sim.ORIGIN[1], 5000, 100, 180, 0), uat.LONG_ROOTS))
        near = (self.N - 2) * self.STEP - 0.2   # nearer than N-2, so N-1 is the next farthest
        b.feed(uat_at(near), SENTRY, 0.5)                                            # evicts N
        b.feed(modes(sim.me_identification("NEW1"), addr=0xABCDE0), SENTRY, 1)       # evicts N-1, caches all
        b.feed(uat_at(near + 2), SENTRY, 10)
        b.feed(modes(sim.me_identification("NEW2"), addr=0xABCDE1), SENTRY, 10.1)
        addresses = {r.address for r in self.reports(b, 10.2)}
        self.assertNotIn(0xABC000, addresses)
        self.assertIn(self.N - 2, addresses)

    def test_full_table_evicts_an_unreported_target_first(self):
        b = self.make_bridge()
        self.fill_table(b)
        b.feed(modes(sim.me_identification("NOPOS"), addr=0xABCDE0), SENTRY, 0.5)   # evicts N
        b.feed(modes(sim.me_identification("NEW"), addr=0xABCDE1), SENTRY, 0.6)     # NOPOS is new: N-1 goes
        self.assertEqual(len(self.reports(b, 1)), bridge.MAX_TARGETS - 2)
        late = bridge.ACQUIRE_S + 0.6                # NOPOS never got a position: now it goes first
        b.feed(modes(sim.me_identification("LATE"), addr=0xABCDE2), SENTRY, late)
        addresses = {r.address for r in self.reports(b, late)}
        self.assertEqual(len(addresses), bridge.MAX_TARGETS - 2)
        self.assertIn(self.N - 2, addresses)

    def test_aircraft_beyond_half_a_zone_is_not_placed_at_its_alias(self):
        """An aircraft 200 NM north of ownship: its first message decoded against ownship alone lands
        about 160 NM south. It must not be shown there, and its even/odd pair must place it correctly."""
        b = self.make_bridge(ref=sim.ORIGIN)
        lat = sim.ORIGIN[0] + 200 / 60.0
        b.feed(modes(sim.me_airborne_position(lat, sim.ORIGIN[1], 30000, 0)), SENTRY, 0)
        self.assertEqual(self.reports(b, 0.2), [])
        b.feed(modes(sim.me_airborne_position(lat, sim.ORIGIN[1], 30000, 1)), SENTRY, 0.5)
        r, = self.reports(b, 1)
        self.assertLess(bridge.dist_nm(r.lat, r.lon, lat, sim.ORIGIN[1]), 0.1)
        near = self.make_bridge(ref=sim.ORIGIN)   # close traffic still decodes from one message
        near.feed(modes(sim.me_airborne_position(sim.ORIGIN[0] + 50 / 60.0, sim.ORIGIN[1], 30000, 0)), SENTRY, 0)
        self.assertEqual(len(self.reports(near, 0.2)), 1)

    def test_interleaved_newcomers_get_their_pair_in_a_full_table(self):
        """Two new aircraft whose even and odd messages interleave must not evict each other before
        either has a pair (no reference, so only a global decode can place them)."""
        b = self.make_bridge()
        self.fill_table(b, with_ownship=False)
        new = (0xAAAA01, 0xAAAA02)
        for sec in range(1, 6):
            for addr in range(1, self.N - 55):   # these stay fresh; the last 56 become the least recently heard
                b.feed(modes(sim.me_airborne_position(sim.ORIGIN[0] - addr * self.STEP / 60, sim.ORIGIN[1], 5000,
                                                      sec % 2),
                             addr=addr), SENTRY, sec)
            for odd in (0, 1):
                for addr in new:
                    b.feed(modes(sim.me_airborne_position(sim.ORIGIN[0] + 0.1, sim.ORIGIN[1], 3000, odd), addr=addr),
                           SENTRY, sec + 0.5)
        addresses = {r.address for r in self.reports(b, 5.6)}
        self.assertTrue(set(new) <= addresses)

    def crowd_with_newcomers(self, b, t):
        """A full table plus ACQUIRE_SLOTS + 1 new targets without a position (identifications), first heard
        at t, so it is crowded: the next victim would be one of them."""
        for n in range(bridge.ACQUIRE_SLOTS + 1):
            b.feed(modes(sim.me_identification("NEW"), addr=0xB00000 + n), SENTRY, t)

    def test_crowded_table_keeps_new_targets_long_enough_to_pair(self):
        """A crowded full table drops a newcomer's message rather than evict a new target that has had less
        than MIN_RESIDENCE_S, unless that one message places the newcomer near the reference."""
        b = self.make_bridge()
        self.fill_table(b)
        self.crowd_with_newcomers(b, 1)
        b.feed(modes(sim.me_identification("WAIT"), addr=0xC00000), SENTRY, 1.5)        # turned away
        far = sim.ORIGIN[0] + 100 / 60.0                                                # beyond a one-message decode
        b.feed(modes(sim.me_airborne_position(far, sim.ORIGIN[1], 3000, 0), addr=0xC00002), SENTRY, 1.5)
        b.feed(modes(sim.me_airborne_position(far, sim.ORIGIN[1], 3000, 1), addr=0xC00002), SENTRY, 1.6)
        b.feed(modes(sim.me_airborne_position(sim.ORIGIN[0] + 0.1, sim.ORIGIN[1], 3000, 0), addr=0xC00001),
               SENTRY, 1.7)                                                             # placed at once: admitted
        late = 1 + bridge.MIN_RESIDENCE_S + 0.1
        b.feed(modes(sim.me_airborne_position(far, sim.ORIGIN[1], 3000, 0), addr=0xC00003), SENTRY, late)
        b.feed(modes(sim.me_airborne_position(far, sim.ORIGIN[1], 3000, 1), addr=0xC00003), SENTRY, late + 0.1)
        addresses = {r.address for r in self.reports(b, late + 0.2)}
        self.assertIn(0xC00001, addresses)
        self.assertNotIn(0xC00002, addresses)    # turned away twice: no pair
        self.assertIn(0xC00003, addresses)        # the oldest new target had its time: replaced

    def test_more_aircraft_than_slots_are_still_shown(self):
        """A few more aircraft than slots, all heard at full rate, no reference: before MIN_RESIDENCE_S,
        newcomers evicted each other before any could pair and no aircraft was shown at all."""
        b = self.make_bridge()
        n = self.N + 8
        for sec in range(12):
            for odd in (0, 1):
                for i in range(n):
                    lat = sim.ORIGIN[0] + (i % 40) * 0.05
                    lon = sim.ORIGIN[1] + (i // 40) * 0.05
                    b.feed(modes(sim.me_airborne_position(lat, lon, 5000, odd), addr=0xD00000 + i), SENTRY,
                           sec + odd * 0.5 + i * 0.4 / n)
        self.assertGreater(len(self.reports(b, 12)), self.N - 2 * bridge.ACQUIRE_SLOTS)

    def test_full_table_without_reference_evicts_least_recently_heard(self):
        b = self.make_bridge()
        self.fill_table(b, with_ownship=False)
        b.feed(modes(sim.me_identification("NEW"), addr=0xABCDE0), SENTRY, 1)
        addresses = {r.address for r in self.reports(b, 1)}
        self.assertNotIn(1, addresses)              # heard first; distance means nothing without a reference
        self.assertIn(self.N, addresses)
        self.assertEqual(len(addresses), bridge.MAX_TARGETS - 1)


def recording():
    """(seconds, datagram) pairs of tests/data/compat-sanitized.txt.gz."""
    path = Path(__file__).resolve().parent / "data" / "compat-sanitized.txt.gz"
    with gzip.open(path, "rt") as fh:
        return [(float(t), bytes.fromhex(h)) for t, h in (line.split() for line in fh if not line.startswith("#"))]


class TestPythonFrameRules(FrameRules, unittest.TestCase):
    def make_bridge(self, **kw):
        return bridge.Bridge(log=lambda m: None, **kw)

    reports = TrafficSemantics.reports

    def test_full_table_scan_reuses_distances_until_the_reference_moves(self):
        """Every newcomer in a full table scans all targets. On a board without an FPU, a distance per
        target per newcomer was more than one core at 400 aircraft, so each distance is kept until the
        reference or that target's position changes."""
        b = self.make_bridge()
        self.fill_table(b)
        with mock.patch.object(bridge, "dist_nm", wraps=bridge.dist_nm) as dist:
            for n in range(100):
                b.feed(modes(sim.me_identification("NEW"), addr=0xB00000 + n), SENTRY, 1 + n / 200)
            self.assertLessEqual(dist.call_count, bridge.MAX_TARGETS)
            b.feed(gdl90.encode_report(gdl90.Report(address=0, lat=sim.ORIGIN[0] + 0.01, lon=sim.ORIGIN[1],
                                                    alt_ft=1000, nic=9), gdl90.MSG_OWNSHIP), SENTRY, 1.6)
            b.feed(modes(sim.me_identification("NEW"), addr=0xB10000), SENTRY, 1.7)
            self.assertGreater(dist.call_count, bridge.MAX_TARGETS)   # ownship moved: measured again


@unittest.skipIf(LIB is None, "no C compiler available")
class TestCFrameRules(FrameRules, unittest.TestCase):
    def make_bridge(self, **kw):
        return sbcore_ffi.CBridge(LIB, **kw)

    reports = TrafficSemantics.reports


class TestPythonTraffic(TrafficSemantics, unittest.TestCase):
    def make_bridge(self, **kw):
        return bridge.Bridge(log=lambda m: None, **kw)


@unittest.skipIf(LIB is None, "no C compiler available")
class TestCTraffic(TrafficSemantics, unittest.TestCase):
    def make_bridge(self, **kw):
        return sbcore_ffi.CBridge(LIB, **kw)


if __name__ == "__main__":
    unittest.main()
