"""tools/apk_matches.py: the reproducible-build check F-Droid makes on the published APK."""
import io
from pathlib import Path
import struct
import sys
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from apk_matches import matches  # noqa: E402


def unsigned_apk(payload=b"classes"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("classes.dex", payload)
        z.writestr("AndroidManifest.xml", b"manifest")
    return out.getvalue()


def signed(apk):
    """apk with an APK Signing Block inserted before the central directory, as apksigner does."""
    eocd = apk.rfind(b"PK")
    cd_offset = struct.unpack_from("<I", apk, eocd + 16)[0]
    pairs = struct.pack("<QI", 4 + 8, 0x7109871A) + b"signatur"
    block = struct.pack("<Q", len(pairs) + 24) + pairs + struct.pack("<Q", len(pairs) + 24) + b"APK Sig Block 42"
    out = bytearray(apk[:cd_offset] + block + apk[cd_offset:])
    struct.pack_into("<I", out, eocd + len(block) + 16, cd_offset + len(block))
    return bytes(out)


class TestApkMatches(unittest.TestCase):
    def test_changed_or_malformed_zip_envelope_is_rejected(self):
        apk = unsigned_apk()
        sealed = signed(apk)
        for original, other, unsigned_side in ((apk, sealed, True), (sealed, apk, False)):
            end = original.rfind(b"PK\x05\x06")
            for offset in (4, 6, 8, 10, 12, 16, 20):
                changed = bytearray(original)
                changed[end + offset] ^= 1
                with self.subTest(unsigned_side=unsigned_side, offset=offset):
                    self.assertFalse(matches(changed, other) if unsigned_side else matches(other, changed))
        for broken in (b"", sealed[:-1], sealed + b"trailing"):
            self.assertFalse(matches(apk, broken))
        changed = bytearray(sealed)
        cd = struct.unpack_from("<I", sealed, sealed.rfind(b"PK\x05\x06") + 16)[0]
        struct.pack_into("<Q", changed, cd - 24, len(sealed) + 100)
        self.assertFalse(matches(apk, changed))

    def test_signed_apk_matches_only_its_own_unsigned_build(self):
        apk = unsigned_apk()
        self.assertTrue(matches(apk, signed(apk)))
        self.assertFalse(matches(unsigned_apk(b"classes2"), signed(apk)))  # different content
        self.assertFalse(matches(apk, apk))  # not signed at all
        self.assertFalse(matches(signed(apk), signed(apk)))  # the "unsigned" side must be unsigned


if __name__ == "__main__":
    unittest.main()
