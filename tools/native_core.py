"""Small runtime binding to the existing C decoder. Never compiles on app startup."""
import ctypes as C
import ipaddress
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parent.parent
EMIT = C.CFUNCTYPE(None, C.c_void_p, C.POINTER(C.c_uint8), C.c_size_t)
LOG = C.CFUNCTYPE(None, C.c_void_p, C.c_char_p)


class Config(C.Structure):
    _fields_ = [("sentry_ip", C.c_uint32), ("ownship_icao", C.c_int32),
                ("has_ref", C.c_int), ("ref_lat", C.c_double), ("ref_lon", C.c_double),
                ("stale_s", C.c_double), ("relay", C.c_int), ("log", LOG), ("log_ctx", C.c_void_p)]


class Stats(C.Structure):
    _fields_ = [("datagrams", C.c_uint32), ("frames", C.c_uint32 * 256)] + [
        (name, C.c_uint32) for name in ("crc_bad", "modes_msgs", "modes_other", "pos_decoded",
        "pos_rejected", "sent", "uat_msgs", "uat_fixed", "uat_bad", "uat_skipped")]


def library():
    name = "sbcore.dll" if sys.platform == "win32" else "libsbcore.dylib" if sys.platform == "darwin" else "libsbcore.so"
    candidates = [Path(os.environ["SBCORE_LIB"])] if os.environ.get("SBCORE_LIB") else [
        ROOT / name, ROOT / "build" / "windows" / name, ROOT / "build" / name,
        ROOT / "build" / "posix" / name,
        ROOT / "build" / f"{platform.system().lower()}-{platform.machine()}" / name]
    path = next((path for path in candidates if path.is_file()), None)
    if path is None:
        return None
    lib = C.CDLL(str(path))
    lib.sb_bridge_size.restype = C.c_size_t
    lib.sb_config_defaults.argtypes = [C.POINTER(Config)]
    lib.sb_init.argtypes = [C.c_void_p, C.POINTER(Config)]
    lib.sb_feed.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t, C.c_uint32, C.c_double, EMIT, C.c_void_p]
    lib.sb_tick.argtypes = [C.c_void_p, C.c_double, EMIT, C.c_void_p]
    lib.sb_wants_enable.argtypes = [C.c_void_p, C.c_double]
    lib.sb_get_mode.argtypes = [C.c_void_p]
    lib.sb_stream_starts.argtypes = [C.c_void_p]
    lib.sb_stream_starts.restype = C.c_uint32
    lib.sb_get_stats.argtypes = [C.c_void_p]
    lib.sb_get_stats.restype = C.POINTER(Stats)
    return lib


class Decoder:
    def __init__(self, lib, settings, log):
        self.lib = lib
        self._log = LOG(lambda ctx, message: log(message.decode("utf-8", "replace")))
        cfg = Config()
        lib.sb_config_defaults(C.byref(cfg))
        cfg.sentry_ip = int(ipaddress.IPv4Address(settings["sentryIp"])) if settings["sentryIp"] else 0
        cfg.ownship_icao = int(settings["ownshipHex"], 16) if settings["ownshipHex"] else -1
        if settings["reference"]:
            cfg.has_ref = 1
            cfg.ref_lat, cfg.ref_lon = settings["reference"]
        cfg.stale_s, cfg.relay, cfg.log = settings["staleSeconds"], settings["relay"], self._log
        self.buf = C.create_string_buffer(lib.sb_bridge_size())
        lib.sb_init(self.buf, C.byref(cfg))
        self._out = []
        self._emit = EMIT(lambda ctx, data, size: self._out.append(C.string_at(data, size)))

    def feed(self, data, source, now):
        self._out = []
        self.lib.sb_feed(self.buf, data, len(data), int(ipaddress.IPv4Address(source)), now, self._emit, None)
        return self._out

    def tick(self, now):
        self._out = []
        self.lib.sb_tick(self.buf, now, self._emit, None)
        return self._out

    def wants_enable(self, now):
        return bool(self.lib.sb_wants_enable(self.buf, now))

    @property
    def mode(self):
        return {0: "UNKNOWN", 1: "PUBLIC", 2: "ENCRYPTED"}[self.lib.sb_get_mode(self.buf)]

    @property
    def stream_starts(self):
        return self.lib.sb_stream_starts(self.buf)

    @property
    def stats(self):
        return self.lib.sb_get_stats(self.buf).contents


def decoder(settings, log):
    lib = library()
    if lib is not None:
        return Decoder(lib, settings, log), "C core"
    if getattr(sys, "frozen", False):
        # Packaged builds ship only the C core; the Python reference decoder needs pyModeS.
        raise ValueError("This build is missing its native decoder. Reinstall Sentry Bridge.")
    import bridge
    return bridge.Bridge(sentry_ip=settings["sentryIp"], ownship_icao=int(settings["ownshipHex"], 16)
                         if settings["ownshipHex"] else None, ref=settings["reference"],
                         stale_s=settings["staleSeconds"], relay=settings["relay"], log=log), "Python reference"
