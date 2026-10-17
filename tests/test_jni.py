"""The Android app's JNI layer (android/app/src/main/cpp/sb_jni.c) must give the JVM exactly what
bridge.py produces: same traffic reports, relayed frames, enable decisions and counters.

Builds sb_jni.c + core/sbcore.c as a desktop shared library, runs tests/jni/JniReplay.java against
it on simulated traffic, and compares with bridge.py. Skips without a JDK (17+) and a C compiler.
"""

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge  # noqa: E402
import gdl90  # noqa: E402
import sim  # noqa: E402

JNI_SRC = ROOT / "android" / "app" / "src" / "main" / "cpp" / "sb_jni.c"
MODE_NUM = {"?": 0, "PUBLIC": 1, "ENCRYPTED": 2}


def _jdk():
    javac = shutil.which("javac")
    java = shutil.which("java")
    native_lib = os.environ.get("SENTRY_JNI_LIB")
    if platform.system() == "Windows" and not native_lib:
        return None
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not (javac and java) or not (cc or native_lib):
        return None
    try:
        r = subprocess.run([javac, "-version"], capture_output=True, text=True)
        m = re.search(r"javac (\d+)", r.stdout + r.stderr)
        major = int(m.group(1)) if m else 0
    except OSError:
        return None
    if major < 17:
        return None
    home = Path(os.environ.get("JAVA_HOME") or Path(os.path.realpath(javac)).parent.parent)
    if not (home / "include" / "jni.h").exists():
        return None
    if native_lib and not Path(native_lib).is_file():
        return None
    return java, javac, cc, home, native_lib


def _build(tmp: Path):
    java, javac, cc, home, native_lib = JDK
    osdir = {"Windows": "win32", "Darwin": "darwin"}.get(platform.system(), "linux")
    if native_lib:
        lib = tmp / {"win32": "sbjni.dll", "darwin": "libsbjni.dylib", "linux": "libsbjni.so"}[osdir]
        shutil.copyfile(native_lib, lib)
    else:
        _compile(tmp, cc, home, osdir)
    jni = Path(__file__).resolve().parent / "jni"
    subprocess.run([javac, "-d", str(tmp), str(jni / "io/github/tehcnological/sentrybridge/SbCore.java"),
                    str(jni / "JniReplay.java")], check=True)
    return java


def _compile(tmp, cc, home, osdir):
    lib = tmp / ("libsbjni.dylib" if osdir == "darwin" else "libsbjni.so")
    flags = ["-dynamiclib"] if osdir == "darwin" else ["-shared", "-fPIC"]
    subprocess.run([cc, *flags, "-O2", "-Wall", "-I", str(ROOT / "core"), "-I", str(home / "include"),
                    "-I", str(home / "include" / osdir), str(JNI_SRC), str(ROOT / "core" / "sbcore.c"),
                    "-o", str(lib), "-lm"], check=True)


JDK = _jdk()


def sim_traffic():
    """Busy simulated 1090 traffic, then UAT targets with injected byte errors, plus FIS-B uplinks."""
    out = sim.busy_datagrams(seconds=120, uat_targets=False, fisb=True)[0]
    s = sim.SimSentry(tracks=sim.default_tracks()[:2] + sim.uat_tracks(), seed=3, send_ownship=False, fisb=True,
                      opstatus=True)
    out += [(1_700_010_000.0 + t, d) for t in range(120) for d in s.datagrams(float(t))]
    return out


def watchdog_sequence():
    hb = gdl90.encode_heartbeat(True, 100)
    enc = gdl90.frame(bytes((0x26, 0x00)) + bytes(18))
    seq, t = [], 1_700_000_000.0
    for frame, n, gap in ((enc, 70, 0), (hb, 30, 0), (enc, 30, 0), (hb, 10, 40), (enc, 50, 30), (hb, 5, 0)):
        t += gap
        for _ in range(n):
            seq.append((t, frame))
            t += 1.0
    return seq


def python_replay(datagrams, relay):
    b = bridge.Bridge(sentry_ip="192.168.4.1", relay=relay, log=lambda m: None)
    lines, nxt, tick_no = [], None, 0
    for t, d in datagrams:
        if nxt is None:
            nxt = t + 1.0
        while t >= nxt:
            for fr in sorted(f.hex() for f in b.tick(nxt)):
                lines.append(f"T {tick_no} {fr}")
            tick_no += 1
            nxt += 1.0
        for fr in b.feed(d, "192.168.4.1", t):
            lines.append(f"R {fr.hex()}")
        lines.append(f"W {1 if b.wants_enable(t) else 0}")
    s = b.stats
    targets = list(b.targets.values())
    stats = [MODE_NUM[b.mode], 1 if b.gps_valid else 0, s.datagrams, s.crc_bad, s.modes_msgs, s.uat_msgs,
             s.uat_bad, s.sent, len(targets), sum(t.lat is not None for t in targets),
             sum(t.lat is not None and t.uat for t in targets), s.pos_rejected]
    lines.append("S " + " ".join(map(str, stats)))
    lines.append(f"N {b.stream_starts}")
    return lines


@unittest.skipIf(JDK is None, "needs JDK 17+ and a C compiler or SENTRY_JNI_LIB")
class TestJni(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.java = _build(cls.tmp)

    def run_java(self, datagrams, relay):
        inp = self.tmp / "in.txt"
        inp.write_text("".join(f"{t:.4f} {d.hex()}\n" for t, d in datagrams))
        r = subprocess.run([self.java, f"-Djava.library.path={self.tmp}", "-cp", str(self.tmp), "JniReplay",
                            str(inp), "1" if relay else "0"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def compare(self, datagrams, relay):
        out = self.run_java(datagrams, relay)
        java = [l for l in out if l[:1] in "TRWSN"]
        py = python_replay(datagrams, relay)
        self.assertEqual(len(java), len(py))
        for i, (a, b) in enumerate(zip(java, py)):
            self.assertEqual(a, b, f"line {i}")
        return out

    def test_sim_traffic(self):
        out = self.compare(sim_traffic(), relay=False)
        self.assertTrue(any(l.startswith("T ") for l in out))
        summary = next(l for l in out if l.startswith("U "))
        self.assertIn("mode=PUBLIC", summary)

    def test_sim_traffic_relay(self):
        out = self.compare(sim_traffic(), relay=True)
        self.assertTrue(any(l.startswith("R ") for l in out))
        # FIS-B uplinks (~440 bytes framed) must come through whole, not be dropped for size
        uplinks = [l for l in out if l.startswith("R 7e07")]
        self.assertGreater(len(uplinks), 200)
        self.assertTrue(all(len(l) // 2 > 436 for l in uplinks))
        self.assertFalse(any("dropped" in l for l in out if l.startswith("L ")))

    def test_message_id_counters_for_gui_include_only_crc_valid_frames(self):
        good = gdl90.encode_heartbeat(True, 1)
        weather = gdl90.frame(bytes([7]) + bytes(435))
        corrupt = weather[:-2] + bytes([weather[-2] ^ 1]) + weather[-1:]
        out = self.run_java([(0, good), (1, weather), (2, corrupt)], relay=True)
        counts = list(map(int, next(line for line in out if line.startswith("C ")).split()[1:]))
        self.assertEqual(len(counts), 256)
        self.assertEqual(counts[0], 1)
        self.assertEqual(counts[7], 1)
        self.assertEqual(sum(counts), 2)

    def test_watchdog_and_helpers(self):
        out = self.compare(watchdog_sequence(), relay=False)
        self.assertIn("W 1", out)
        self.assertTrue(any("came up in ForeFlight" in l for l in out if l.startswith("L ")))
        self.assertIn("P 1 0 -1", out)
        self.assertIn("A 4000\tForeFlight", out)
        self.assertIn("B null", out)
        self.assertTrue(any(l.startswith("V ") and len(l) > 2 for l in out))

    def test_altitude_freshness_and_address_namespaces(self):
        from test_traffic_semantics import position, modes
        own = gdl90.encode_report(gdl90.Report(address=1, lat=40.0, lon=-100.0,
                                              alt_ft=1000), gdl90.MSG_OWNSHIP)
        seq = [(0, own), (0, position()), (1, position(20, 1200, 1)),
               (2, modes(sim.me_identification("ANON"), df=0x91)),
               (2, position(altitude=3500, df=0x91)),
               (14, position(21, 1300)), (16, position(20, 1300, 1)),
               (17, gdl90.encode_heartbeat(True, 17))]
        out = self.compare(seq, relay=False)
        reps = [(int(l.split()[1]), gdl90.decode_report(gdl90.parse(bytes.fromhex(l.split()[2]))[0].msg))
                for l in out if l.startswith("T ")]
        early = {r.addr_type: r for tick, r in reps if tick == 2}  # tick at t=3
        self.assertEqual(set(early), {0, 1})
        self.assertEqual(early[0].alt_ft, 2500)
        self.assertEqual(early[1].alt_ft, 3500)
        self.assertEqual(early[0].nacp, 0)
        late = [r for tick, r in reps if tick == 15 and r.addr_type == 0]  # t=16
        self.assertEqual(len(late), 1)
        self.assertIsNone(late[0].alt_ft)


if __name__ == "__main__":
    unittest.main()
