"""GDL90 meanings checked independently for Python and C, beyond byte-for-byte parity.

All positions and identities are synthetic. GNSS height is encoded directly in metres,
because sim.me_airborne_position encodes the barometric format only.
"""

import math
import sys
import unittest
from pathlib import Path

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
    body = bytearray(sim.df17(addr, me))
    body[0] = df
    return gdl90.frame(b"\x21\xff\xff\xff" + body)


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


class TestPythonTraffic(TrafficSemantics, unittest.TestCase):
    def make_bridge(self, **kw):
        return bridge.Bridge(log=lambda m: None, **kw)


@unittest.skipIf(LIB is None, "no C compiler available")
class TestCTraffic(TrafficSemantics, unittest.TestCase):
    def make_bridge(self, **kw):
        return sbcore_ffi.CBridge(LIB, **kw)


if __name__ == "__main__":
    unittest.main()
