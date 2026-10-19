"""ctypes binding to the C core (core/sbcore.c), used by the parity tests.

Finds the shared library via $SBCORE_LIB, or compiles core/sbcore.c with the first C compiler
on PATH into build/ (cached). Returns None when neither works, and the tests skip.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "core" / "sbcore.c"

EMIT_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t)
LOG_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)


class Config(ctypes.Structure):
    _fields_ = [
        ("sentry_ip", ctypes.c_uint32),
        ("ownship_icao", ctypes.c_int32),
        ("has_ref", ctypes.c_int),
        ("ref_lat", ctypes.c_double),
        ("ref_lon", ctypes.c_double),
        ("stale_s", ctypes.c_double),
        ("relay", ctypes.c_int),
        ("log", LOG_FN),
        ("log_ctx", ctypes.c_void_p),
    ]


class Stats(ctypes.Structure):
    _fields_ = [
        ("datagrams", ctypes.c_uint32),
        ("frames", ctypes.c_uint32 * 256),
        ("crc_bad", ctypes.c_uint32),
        ("modes_msgs", ctypes.c_uint32),
        ("modes_other", ctypes.c_uint32),
        ("pos_decoded", ctypes.c_uint32),
        ("pos_rejected", ctypes.c_uint32),
        ("sent", ctypes.c_uint32),
        ("uat_msgs", ctypes.c_uint32),
        ("uat_fixed", ctypes.c_uint32),
        ("uat_bad", ctypes.c_uint32),
        ("uat_skipped", ctypes.c_uint32),
    ]


def _lib_name() -> str:
    if sys.platform == "win32":
        return "sbcore.dll"
    if sys.platform == "darwin":
        return "libsbcore.dylib"
    return "libsbcore.so"


def _build() -> Path | None:
    out = ROOT / "build" / _lib_name()
    sources = [ROOT / "core" / n for n in ("sbcore.c", "sbcore.h", "sbdecode.c")] + [ROOT / "firmware/common" / n for n in ("fw_bridge.c", "fw_bridge.h")]
    if out.exists() and out.stat().st_mtime >= max(p.stat().st_mtime for p in sources):
        return out
    cc = next((c for c in (os.environ.get("CC"), "cc", "gcc", "clang") if c and shutil.which(c)), None)
    if not cc:
        return None
    out.parent.mkdir(exist_ok=True)
    cmd = [cc, "-std=c99", "-O2", "-shared", "-o", str(out), "-I" + str(ROOT / "core"), str(SRC), str(ROOT / "firmware/common/fw_bridge.c")]
    if sys.platform != "win32":
        cmd[3:3] = ["-fPIC"]
        cmd.append("-lm")
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return out


def load():
    path = os.environ.get("SBCORE_LIB") or _build()
    if not path:
        return None
    lib = ctypes.CDLL(str(path))
    lib.sb_bridge_size.restype = ctypes.c_size_t
    lib.sb_config_defaults.argtypes = [ctypes.POINTER(Config)]
    lib.sb_init.argtypes = [ctypes.c_void_p, ctypes.POINTER(Config)]
    lib.sb_feed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32,
                            ctypes.c_double, EMIT_FN, ctypes.c_void_p]
    lib.sb_feed.restype = ctypes.c_int
    lib.sb_tick.argtypes = [ctypes.c_void_p, ctypes.c_double, EMIT_FN, ctypes.c_void_p]
    lib.sb_tick.restype = ctypes.c_int
    lib.sb_wants_enable.argtypes = [ctypes.c_void_p, ctypes.c_double]
    lib.sb_wants_enable.restype = ctypes.c_int
    lib.sb_get_mode.argtypes = [ctypes.c_void_p]
    lib.sb_get_mode.restype = ctypes.c_int
    lib.sb_stream_starts.argtypes = [ctypes.c_void_p]
    lib.sb_stream_starts.restype = ctypes.c_uint32
    lib.sb_get_stats.argtypes = [ctypes.c_void_p]
    lib.sb_get_stats.restype = ctypes.POINTER(Stats)
    lib.sb_summary.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.sb_parse_announce.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    lib.sb_parse_announce.restype = ctypes.c_int
    lib.sb_power_savings_on.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.sb_power_savings_on.restype = ctypes.c_int
    lib.sb_parse_ipv4.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)]
    lib.sb_parse_ipv4.restype = ctypes.c_int
    lib.sb_uat_correct.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
    lib.sb_uat_correct.restype = ctypes.c_int
    return lib


def ip(s: str) -> int:
    a, b, c, d = (int(x) for x in s.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


class CBridge:
    """Same calling shape as bridge.Bridge: feed() and tick() return lists of framed messages."""

    MODES = {0: "?", 1: "PUBLIC", 2: "ENCRYPTED"}

    def __init__(self, lib, sentry_ip="192.168.4.1", ownship_icao=None, ref=None, stale_s=15.0, relay=False):
        self.lib = lib
        self.logs: list[str] = []
        self._log_cb = LOG_FN(lambda ctx, msg: self.logs.append(msg.decode()))
        cfg = Config()
        lib.sb_config_defaults(ctypes.byref(cfg))
        cfg.sentry_ip = ip(sentry_ip) if sentry_ip else 0
        cfg.ownship_icao = -1 if ownship_icao is None else ownship_icao
        if ref:
            cfg.has_ref, cfg.ref_lat, cfg.ref_lon = 1, ref[0], ref[1]
        cfg.stale_s = stale_s
        cfg.relay = 1 if relay else 0
        cfg.log = self._log_cb
        self._cfg = cfg
        self.buf = ctypes.create_string_buffer(lib.sb_bridge_size())
        lib.sb_init(self.buf, ctypes.byref(cfg))
        self._out: list[bytes] = []
        self._emit = EMIT_FN(lambda ctx, p, n: self._out.append(ctypes.string_at(p, n)))

    def feed(self, datagram: bytes, src: str, now: float) -> list[bytes]:
        self._out = []
        self.lib.sb_feed(self.buf, datagram, len(datagram), ip(src), now, self._emit, None)
        return self._out

    def tick(self, now: float) -> list[bytes]:
        self._out = []
        self.lib.sb_tick(self.buf, now, self._emit, None)
        return self._out

    def wants_enable(self, now: float) -> bool:
        return bool(self.lib.sb_wants_enable(self.buf, now))

    @property
    def mode(self) -> str:
        return self.MODES[self.lib.sb_get_mode(self.buf)]

    @property
    def stream_starts(self) -> int:
        return self.lib.sb_stream_starts(self.buf)

    @property
    def stats(self) -> Stats:
        return self.lib.sb_get_stats(self.buf).contents

    def summary(self) -> str:
        out = ctypes.create_string_buffer(1024)
        self.lib.sb_summary(self.buf, out, len(out))
        return out.value.decode()
