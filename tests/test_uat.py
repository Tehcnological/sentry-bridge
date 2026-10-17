"""UAT decoding tests on simulated frames (sim.uat_payload + uat.rs_encode)."""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sim  # noqa: E402
import uat  # noqa: E402

try:
    import reedsolo  # independent Reed-Solomon implementation, optional
except ImportError:
    reedsolo = None

PAYLOAD = sim.uat_payload(0xA0CA70, 40.05, -99.92, 1800, 69, 54, 512, ptype=1, callsign="N42UA",
                          flight_id=True, emitter=1, nacp=10, nic=9)
BLOCK = uat.rs_encode(PAYLOAD, uat.LONG_ROOTS)
HEADER = b"\x24\xff\xff\xff"
CLEAN_MS = HEADER + BLOCK


def _corrupt_including_first_byte(block, nerr, seed):
    """nerr byte errors, one of them on the header byte (payload type / address qualifier)."""
    rng = random.Random(seed)
    b = bytearray(block)
    b[0] ^= 0x80
    for pos in rng.sample(range(1, len(b)), nerr - 1):
        b[pos] ^= rng.randrange(1, 256)
    return bytes(b)


FIXED7 = HEADER + _corrupt_including_first_byte(BLOCK, 7, seed=1)   # the most RS(48,34) can fix
BAD = HEADER + _corrupt_including_first_byte(BLOCK, 12, seed=2)     # beyond the code's reach


class TestReedSolomon(unittest.TestCase):
    def test_clean(self):
        data, nerr = uat.rs_correct(CLEAN_MS[4:], 14)
        self.assertEqual(nerr, 0)
        self.assertEqual(data, PAYLOAD)

    def test_seven_errors_fixed(self):
        self.assertNotEqual(FIXED7[4], CLEAN_MS[4])
        data, nerr = uat.rs_correct(FIXED7[4:], 14)
        self.assertEqual(nerr, 7)
        self.assertEqual(data, PAYLOAD)

    def test_uncorrectable_rejected(self):
        self.assertIsNone(uat.rs_correct(BAD[4:], 14))
        self.assertIsNone(uat.decode_frame(BAD))

    def test_random_corruption(self):
        rng = random.Random(7)
        for _ in range(500):
            k = rng.randint(1, 7)
            self.assertEqual(uat.rs_correct(sim.corrupt(BLOCK, k, rng), 14), (PAYLOAD, k))

    def test_seeded_twelve_error_blocks_are_rejected(self):
        # A regression corpus, not a guarantee beyond the RS correction radius:
        # sufficiently many errors can transform one valid codeword into another.
        rng = random.Random(11)
        for _ in range(500):
            self.assertIsNone(uat.rs_correct(sim.corrupt(BLOCK, 12, rng), 14))

    def test_encode_roundtrip_both_lengths(self):
        rng = random.Random(5)
        for nroots, n in ((14, 34), (12, 18)):
            for _ in range(200):
                data = bytes(rng.randrange(256) for _ in range(n))
                self.assertEqual(uat.rs_correct(uat.rs_encode(data, nroots), nroots), (data, 0))

    @unittest.skipIf(reedsolo is None, "reedsolo not installed")
    def test_parity_matches_independent_implementation(self):
        rng = random.Random(6)
        for nroots, n in ((14, 34), (12, 18)):
            ref = reedsolo.RSCodec(nroots, fcr=120, prim=0x187, generator=2, c_exp=8)
            for _ in range(200):
                data = bytes(rng.randrange(256) for _ in range(n))
                encoded = bytes(ref.encode(data))
                self.assertEqual(uat.rs_encode(data, nroots), encoded)
                k = rng.randrange(nroots // 2 + 1)
                damaged = sim.corrupt(encoded, k, rng)
                self.assertEqual(bytes(ref.decode(damaged)[0]), data)
                self.assertEqual(uat.rs_correct(damaged, nroots), (data, k))


class TestDecode(unittest.TestCase):
    def test_state_vector_and_mode_status(self):
        r = uat.decode_frame(CLEAN_MS)
        self.assertEqual(r.address, 0xA0CA70)
        self.assertEqual(r.qualifier, 0)
        self.assertEqual(r.payload_type, 1)
        self.assertAlmostEqual(r.lat, 40.05, places=4)
        self.assertAlmostEqual(r.lon, -99.92, places=4)
        self.assertEqual(r.alt_ft, 1800)
        self.assertFalse(r.alt_geo)
        self.assertEqual(r.baro_alt_ft, 1800)
        self.assertEqual(r.alt2_ft, 1650)
        self.assertTrue(r.airborne)
        self.assertAlmostEqual(r.gs_kt, 69, delta=1)
        self.assertAlmostEqual(r.track_deg, 54, delta=1)
        self.assertEqual(r.vs_fpm, 512)
        self.assertTrue(r.has_ms)
        self.assertEqual(r.callsign, "N42UA")
        self.assertTrue(r.callsign_is_flight_id)
        self.assertEqual(r.emitter, 1)
        self.assertEqual(r.nacp, 10)
        self.assertEqual(r.nic, 9)

    def test_southern_and_western_hemispheres(self):
        p = sim.uat_payload(0xC0FFEE, -33.95, -70.66, 9500, 240, 210, -1024, ptype=2)
        r = uat.decode_mdb(p)
        self.assertAlmostEqual(r.lat, -33.95, places=4)
        self.assertAlmostEqual(r.lon, -70.66, places=4)
        self.assertAlmostEqual(r.track_deg, 210, delta=1)
        self.assertEqual(r.vs_fpm, -1024)
        self.assertFalse(r.has_ms)

    def test_squawk_field_is_not_a_flight_id(self):
        p = sim.uat_payload(0xA0CA70, 40, -100, 3000, 90, 0, 0, ptype=1, callsign="1200", flight_id=False)
        r = uat.decode_mdb(p)
        self.assertEqual(r.callsign, "1200")
        self.assertFalse(r.callsign_is_flight_id)

    def test_corrected_frame_decodes(self):
        r = uat.decode_frame(FIXED7)
        self.assertEqual(r.address, 0xA0CA70)
        self.assertEqual(r.fec_errors, 7)
        self.assertAlmostEqual(r.lon, -99.92, places=4)

    def test_rejects_other_ids_and_lengths(self):
        self.assertIsNone(uat.decode_frame(b"\x21\xff\xff\xff" + bytes(14)))
        self.assertIsNone(uat.decode_frame(b"\x24\xff\xff\xff" + bytes(20)))


if __name__ == "__main__":
    unittest.main()
