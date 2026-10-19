"""The microcontroller firmware's board-independent loop (firmware/common/fw_bridge.c), driven through
ctypes with simulated receiver datagrams: what it broadcasts and when it contacts the receiver."""
import ctypes
import re
import socket
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gdl90  # noqa: E402
import sbcore_ffi  # noqa: E402
import sim  # noqa: E402

LIB = sbcore_ffi.load()
SENTRY = struct.unpack(">I", socket.inet_aton("192.168.4.1"))[0]
SEND = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t)
HTTP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
LOG = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)


class Hooks(ctypes.Structure):
    _fields_ = [("send", SEND), ("http", HTTP), ("log", LOG), ("ctx", ctypes.c_void_p)]


class TestFirmwareNotices(unittest.TestCase):
    def test_notice_files_named_in_the_notices_exist(self):
        notices = (ROOT / "firmware/notices/Third-Party.txt").read_text(encoding="utf-8")
        self.assertIn("firmware/notices/Third-Party.txt", (ROOT / "NOTICE.txt").read_text(encoding="utf-8"))
        named = re.findall(r"(?:firmware/)?notices/[\w.-]+\.txt", notices)
        self.assertGreaterEqual(len(named), 3)
        for name in named:
            self.assertTrue((ROOT / name).is_file(), name)


@unittest.skipIf(LIB is None, "no C compiler available to build the core")
class TestFirmwareLoop(unittest.TestCase):
    def setUp(self):
        self.sent, self.requests, self.lines, self.busy = [], [], [], False
        self.hooks = Hooks(SEND(lambda ctx, p, n: self.sent.append(bytes(p[:n]))),
                           HTTP(self.http), LOG(lambda ctx, m: self.lines.append(m.decode())), None)
        # fw_bridge holds an sb_bridge first; reserve generously for the rest of the struct.
        self.state = ctypes.create_string_buffer(LIB.sb_bridge_size() + 512)
        LIB.fw_init(self.state, ctypes.byref(self.hooks))

    def http(self, ctx, request):
        if self.busy:
            return 0
        self.requests.append(request)
        return 1

    def feed(self, datagrams, now):
        for d in datagrams:
            buf = (ctypes.c_uint8 * len(d)).from_buffer_copy(d)
            LIB.fw_datagram(self.state, buf, ctypes.c_size_t(len(d)), ctypes.c_uint32(SENTRY), ctypes.c_double(now))

    def poll(self, now):
        LIB.fw_poll(self.state, ctypes.c_double(now))

    def test_broadcasts_traffic_only_and_keeps_the_receiver_streaming(self):
        sentry = sim.SimSentry()
        for t in range(40):
            self.feed(sentry.datagrams(float(t)), float(t))
            for step in range(10):
                self.poll(t + step / 10)
        ids = {gdl90.parse(frame)[0].msg[0] for frame in self.sent}
        self.assertEqual(ids, {0x14}, "only traffic reports: EFBs get the receiver's own frames directly")
        reports = [gdl90.decode_report(gdl90.parse(f)[0].msg) for f in self.sent[-20:]]
        self.assertTrue({r.address for r in reports} & {tr.icao for tr in sentry.tracks})
        # Keepalive at start, then every 30 s while data arrives; no enable request in Compatibility Mode.
        self.assertEqual(self.requests, [0, 0])
        self.assertTrue(any("mode=PUBLIC" in line for line in self.lines), self.lines)

    def test_silent_receiver_is_contacted_every_5_s(self):
        for t in range(0, 21):
            self.poll(float(t))
        self.assertEqual(self.requests, [0] * 5)  # t = 0, 5, 10, 15, 20
        self.assertEqual(self.sent, [])

    def test_receiver_up_in_foreflight_mode_is_switched_back(self):
        encrypted = gdl90.frame(bytes((0x26, 0x00)) + bytes(18))
        for t in range(0, 70):
            self.feed([encrypted], float(t))
            self.poll(float(t))
        self.assertEqual(self.requests.count(1), 3)  # at most 3 enable attempts, 20 s apart
        self.assertTrue(any("ENCRYPTED" in line or "ForeFlight" in line for line in self.lines), self.lines)

    def test_busy_http_is_retried(self):
        encrypted = gdl90.frame(bytes((0x26, 0x00)) + bytes(18))
        self.busy = True
        self.feed([encrypted], 0.0)
        self.poll(0.0)
        self.poll(0.5)
        self.assertEqual(self.requests, [])
        self.busy = False
        self.poll(1.0)
        self.assertEqual(self.requests, [1])  # the pending enable goes first


if __name__ == "__main__":
    unittest.main()
