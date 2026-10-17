#!/usr/bin/env python3
"""Simulated Sentry in public mode, for testing the bridge without hardware.

Generates moving targets around a fixed origin, encodes real DF17 squitters (identification,
airborne position with CPR, velocity), wraps them the way the Sentry does in
public mode (0x21 + ff ff ff + DF17 with a non-standard 3-byte trailer), and adds
Heartbeat, Ownship and a static 0x25 frame. UAT targets are sent as 0x24 frames
(ff ff ff + 48-byte UAT long message with Reed-Solomon parity) with byte errors injected,
like the Sentry passes them on: some clean, some correctable, some not. With --fisb it also sends
a FIS-B uplink (0x07, 432-byte block) each second, as its own datagram; the content is filler,
since the bridge only relays uplinks and never decodes them.

    python3 sim.py --dest 127.0.0.1:4000          # stream to a local bridge
    python3 sim.py --dest 127.0.0.1:4000 --no-fix # bench case: no GPS fix
    python3 sim.py --dest 127.0.0.1:4000 --uat    # add UAT targets
    python3 sim.py --dest 127.0.0.1:4000 --fisb   # add FIS-B uplinks

Nothing here transmits RF. It only sends UDP to the address you give it.
"""

from __future__ import annotations

import argparse
import math
import random
import socket
import sys
import time
from dataclasses import dataclass

import gdl90
import uat

# ------------------------------------------------------------------ Mode S encoding

_CS_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"
_GEN = 0x1FFF409
NZ = 15


def modes_parity(first88: int) -> int:
    data = first88 << 24
    for i in range(111, 23, -1):
        if data & (1 << i):
            data ^= _GEN << (i - 24)
    return data & 0xFFFFFF


def df17(icao: int, me: int, ca: int = 5) -> bytes:
    first88 = (17 << 83) | (ca << 80) | (icao << 56) | me
    return ((first88 << 24) | modes_parity(first88)).to_bytes(14, "big")


def cpr_nl(lat: float) -> int:
    if lat == 0:
        return 59
    if abs(lat) == 87:
        return 2
    if abs(lat) > 87:
        return 1
    a = 1 - math.cos(math.pi / (2 * NZ))
    b = math.cos(math.pi / 180.0 * abs(lat)) ** 2
    return int(math.floor(2 * math.pi / math.acos(1 - a / b)))


def _mod(x, y):
    return x - y * math.floor(x / y)


def cpr_encode(lat: float, lon: float, odd: int) -> tuple[int, int]:
    dlat = 360.0 / (4 * NZ - odd)
    yz = math.floor(2 ** 17 * _mod(lat, dlat) / dlat + 0.5)
    rlat = dlat * (yz / 2 ** 17 + math.floor(lat / dlat))
    nl = cpr_nl(rlat) - odd
    dlon = 360.0 / max(nl, 1)
    xz = math.floor(2 ** 17 * _mod(lon, dlon) / dlon + 0.5)
    return yz & 0x1FFFF, xz & 0x1FFFF


def me_identification(callsign: str, tc: int = 4, ca: int = 1) -> int:
    me = (tc << 51) | (ca << 48)
    cs = callsign.upper().ljust(8)[:8]
    for i, ch in enumerate(cs):
        idx = _CS_CHARS.index(ch) if ch in _CS_CHARS and ch != "#" else 32
        me |= idx << (42 - 6 * i)
    return me


def me_airborne_position(lat, lon, alt_ft, odd, tc=11) -> int:
    n = int(round((alt_ft + 1000) / 25.0))
    alt12 = ((n & 0x7F0) << 1) | 0x10 | (n & 0x0F)
    yz, xz = cpr_encode(lat, lon, odd)
    return (tc << 51) | (alt12 << 36) | (odd << 34) | (yz << 17) | xz


def me_velocity(gs_kt: float, track_deg: float, vs_fpm: float) -> int:
    vx = gs_kt * math.sin(math.radians(track_deg))
    vy = gs_kt * math.cos(math.radians(track_deg))
    s_ew, v_ew = (1 if vx < 0 else 0), min(1022, int(round(abs(vx)))) + 1
    s_ns, v_ns = (1 if vy < 0 else 0), min(1022, int(round(abs(vy)))) + 1
    s_vr, vr = (1 if vs_fpm < 0 else 0), min(510, int(round(abs(vs_fpm) / 64.0))) + 1
    me = 19 << 51 | 1 << 48
    me |= s_ew << 42 | v_ew << 32 | s_ns << 31 | v_ns << 21
    me |= 0 << 20 | s_vr << 19 | vr << 10
    return me


def wrap_0x21(modes: bytes, rng: random.Random) -> bytes:
    """Sentry public-mode wrapper: 21 ff ff ff + DF17 (parity replaced by a trailer)."""
    body = modes[:11] + bytes(rng.randrange(256) for _ in range(3))
    return gdl90.frame(bytes((0x21, 0xFF, 0xFF, 0xFF)) + body)


# ------------------------------------------------------------------ UAT encoding

_BASE40 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ  .."


def _b40(ch: str) -> int:
    return _BASE40.index(ch) if ch in _BASE40[:37] else 36


def uat_payload(icao: int, lat: float, lon: float, alt_ft: float, gs_kt: float, track_deg: float,
                vs_fpm: float, ptype: int = 1, qualifier: int = 0, callsign: str = "", flight_id: bool = True,
                emitter: int = 1, nic: int = 9, nacp: int = 10, geo_alt_ft: float | None = None) -> bytes:
    """34-byte UAT long ADS-B payload (HDR, SV, and MS/AUXSV by payload type), per DO-282B."""
    m = bytearray(34)
    m[0] = (ptype << 3) | qualifier
    m[1:4] = icao.to_bytes(3, "big")
    raw_lat = int(round((lat if lat >= 0 else lat + 180.0) * 16777216 / 360.0)) & 0x7FFFFF
    raw_lon = int(round((lon if lon >= 0 else lon + 360.0) * 16777216 / 360.0)) & 0xFFFFFF
    raw_alt = max(1, min(0xFFF, int(round((alt_ft + 1000) / 25.0)) + 1))
    m[4] = (raw_lat >> 15) & 0xFF
    m[5] = (raw_lat >> 7) & 0xFF
    m[6] = ((raw_lat & 0x7F) << 1) | ((raw_lon >> 23) & 1)
    m[7] = (raw_lon >> 15) & 0xFF
    m[8] = (raw_lon >> 7) & 0xFF
    m[9] = (raw_lon & 0x7F) << 1          # bit 0 clear: primary altitude is barometric
    m[10] = raw_alt >> 4
    m[11] = ((raw_alt & 0xF) << 4) | (nic & 0xF)
    vn = gs_kt * math.cos(math.radians(track_deg))
    ve = gs_kt * math.sin(math.radians(track_deg))
    raw_ns = (0x400 if vn < 0 else 0) | (min(1022, int(round(abs(vn)))) + 1)
    raw_ew = (0x400 if ve < 0 else 0) | (min(1022, int(round(abs(ve)))) + 1)
    raw_vv = 0x400 | (0x200 if vs_fpm < 0 else 0) | (min(510, int(round(abs(vs_fpm) / 64.0))) + 1)
    m[12] = (0 << 6) | ((raw_ns >> 6) & 0x1F)   # airborne, subsonic
    m[13] = ((raw_ns & 0x3F) << 2) | ((raw_ew >> 9) & 0x3)
    m[14] = (raw_ew >> 1) & 0xFF
    m[15] = ((raw_ew & 1) << 7) | ((raw_vv >> 4) & 0x7F)
    m[16] = (raw_vv & 0xF) << 4
    if ptype in (1, 3):
        cs = (callsign.upper() + " " * 8)[:8]
        v1 = emitter * 1600 + _b40(cs[0]) * 40 + _b40(cs[1])
        v2 = _b40(cs[2]) * 1600 + _b40(cs[3]) * 40 + _b40(cs[4])
        v3 = _b40(cs[5]) * 1600 + _b40(cs[6]) * 40 + _b40(cs[7])
        m[17:19] = v1.to_bytes(2, "big")
        m[19:21] = v2.to_bytes(2, "big")
        m[21:23] = v3.to_bytes(2, "big")
        m[23] = (2 << 2) | 3                   # no emergency, UAT version 2, SIL 3
        m[25] = (nacp & 0xF) << 4
        m[26] = 0x02 if flight_id else 0x00    # the field carries a flight ID, or a squawk code
    if ptype in (1, 2, 5, 6):
        g = alt_ft - 150 if geo_alt_ft is None else geo_alt_ft
        raw2 = max(1, min(0xFFF, int(round((g + 1000) / 25.0)) + 1))
        m[29] = raw2 >> 4
        m[30] = (raw2 & 0xF) << 4
    return bytes(m)


def corrupt(block: bytes, nerr: int, rng: random.Random) -> bytes:
    """Flip nerr distinct bytes, anywhere in the block (data or parity)."""
    b = bytearray(block)
    for pos in rng.sample(range(len(b)), nerr):
        b[pos] ^= rng.randrange(1, 256)
    return bytes(b)


def uplink_0x07(rng: random.Random) -> bytes:
    """A framed GDL90 Uplink Data message: time of reception unknown (0xFFFFFF) + a 432-byte
    ground uplink block of filler, salted with 0x7E/0x7D so byte stuffing is exercised."""
    block = bytearray(rng.randrange(256) for _ in range(432))
    for i in rng.sample(range(432), 12):
        block[i] = rng.choice((0x7E, 0x7D))
    return gdl90.frame(bytes((gdl90.MSG_UPLINK, 0xFF, 0xFF, 0xFF)) + bytes(block))


def me_operational_status(nacp: int, version: int = 2, subtype: int = 0) -> int:
    """TC 31 aircraft operational status (DO-260A/B layout): version in ME bits 41-43,
    NACp in ME bits 45-48. Capability, mode and integrity fields are left zero."""
    return (31 << 51) | (subtype << 48) | ((version & 7) << 13) | ((nacp & 0xF) << 8)


def wrap_0x24(block48: bytes) -> bytes:
    return gdl90.frame(bytes((0x24, 0xFF, 0xFF, 0xFF)) + block48)


# ------------------------------------------------------------------ scenario

ORIGIN = (40.0, -100.0)   # arbitrary point for simulated traffic


@dataclass
class Track:
    icao: int
    callsign: str
    lat: float
    lon: float
    alt: float
    gs: float
    trk: float
    vs: float
    emitter_ca: int = 1
    link: str = "1090"   # "1090" (0x21 frames) or "uat" (0x24 frames)
    nacp: int = 9        # sent in operational status (1090, with opstatus=True)
    adsb_version: int = 2

    def at(self, t: float):
        d_nm = self.gs * t / 3600.0
        lat = self.lat + d_nm / 60.0 * math.cos(math.radians(self.trk))
        lon = self.lon + d_nm / 60.0 * math.sin(math.radians(self.trk)) / math.cos(math.radians(self.lat))
        return lat, lon, self.alt + self.vs * t / 60.0


def uat_tracks():
    la, lo = ORIGIN
    return [
        Track(0xA0CA70, "N42UA", la + 0.05, lo + 0.08, 2000, 70, 40, 500, 1, link="uat"),
        Track(0xA0CA71, "N77UB", la - 0.12, lo + 0.02, 3500, 95, 350, 0, 1, link="uat"),
    ]


def busy_tracks(n: int = 24, seed: int = 7):
    """Many 1090 targets spread over ~60 NM, at varied speeds, altitudes and tracks."""
    rng = random.Random(seed)
    la, lo = ORIGIN
    out = []
    for i in range(n):
        out.append(Track(0xA10000 + i * 0x111, f"TST{i:03d}", la + rng.uniform(-0.9, 0.9), lo + rng.uniform(-1.1, 1.1),
                         rng.choice([1500, 3500, 6500, 11000, 24000, 37000]), rng.uniform(70, 480),
                         rng.uniform(0, 360), rng.choice([-1500, -500, 0, 0, 0, 700, 2000]), rng.choice([1, 2, 3, 5])))
    return out


def default_tracks():
    la, lo = ORIGIN
    return [
        Track(0xA12345, "N123AB", la + 0.10, lo - 0.05, 4500, 110, 90, 0, 1),
        Track(0xAB761C, "ASA1322", la - 0.40, lo + 0.30, 38000, 460, 300, 0, 3),
        Track(0xA40B41, "N36PJ", la + 0.25, lo + 0.20, 9900, 220, 200, -1200, 2),
        Track(0xA495BF, "DAL2167", la - 0.05, lo - 0.35, 6000, 210, 45, 1800, 3),
        Track(0xA00001, "N1SKY", la - 0.30, lo - 0.30, 2500, 75, 135, 300, 1),
    ]


class SimSentry:
    def __init__(self, tracks=None, ownship=None, gps_fix=True, seed=1, send_ownship=True, fisb=False,
                 opstatus=False):
        self.tracks = tracks if tracks is not None else default_tracks()
        self.send_ownship = send_ownship
        self.fisb = fisb
        self.fisb_rng = random.Random(seed + 1000)  # separate, so --fisb leaves the other frames unchanged
        self.opstatus = opstatus
        self.ops_rng = random.Random(seed + 2000)   # likewise for operational status
        # counts of UAT messages sent, by what the bridge should make of them
        self.uat_sent = {"clean": 0, "correctable": 0, "uncorrectable": 0}
        self.ownship = ownship or Track(0xADF00D, "N12345", ORIGIN[0], ORIGIN[1], 5500, 100, 0, 0)
        self.gps_fix = gps_fix
        self.rng = random.Random(seed)
        self.status = gdl90.frame(bytes.fromhex("25" + "53454e5452590001" + "00" * 12))

    def truth(self, t):
        out = {}
        for tr in self.tracks:
            lat, lon, alt = tr.at(t)
            out[tr.icao] = dict(lat=lat, lon=lon, alt=alt, gs=tr.gs, trk=tr.trk, vs=tr.vs, cs=tr.callsign)
        return out

    def datagrams(self, t: float):
        """Datagrams the Sentry would send during second t (each one is a separate UDP payload)."""
        dg = [gdl90.encode_heartbeat(self.gps_fix, int(t)) + self.status]
        if not self.send_ownship:
            pass
        elif self.gps_fix:
            la, lo, alt = self.ownship.at(t)
            own = gdl90.Report(address=self.ownship.icao, lat=la, lon=lo, alt_ft=int(alt), gs_kt=self.ownship.gs,
                               track_deg=self.ownship.trk, vs_fpm=0, callsign=self.ownship.callsign, nic=9, nacp=10)
        else:
            own = gdl90.Report(address=self.ownship.icao, lat=None, lon=None, alt_ft=None)
        if self.send_ownship:
            dg.append(gdl90.encode_report(own, gdl90.MSG_OWNSHIP))
        sec = int(t)
        for tr in self.tracks:
            lat, lon, alt = tr.at(t)
            if tr.link == "uat":
                dg.append(self._uat_frame(tr, lat, lon, alt, sec))
                continue
            odd = sec % 2
            dg.append(wrap_0x21(df17(tr.icao, me_airborne_position(lat, lon, alt, odd)), self.rng))
            lat2, lon2, alt2 = tr.at(t + 0.5)
            dg.append(wrap_0x21(df17(tr.icao, me_airborne_position(lat2, lon2, alt2, 1 - odd)), self.rng))
            dg.append(wrap_0x21(df17(tr.icao, me_velocity(tr.gs, tr.trk, tr.vs)), self.rng))
            if sec % 5 == 0:
                dg.append(wrap_0x21(df17(tr.icao, me_identification(tr.callsign, 4, tr.emitter_ca)), self.rng))
            if self.opstatus and sec % 3 == 1:
                me = me_operational_status(tr.nacp, tr.adsb_version)
                dg.append(wrap_0x21(df17(tr.icao, me), self.ops_rng))
        if self.fisb:
            dg.append(uplink_0x07(self.fisb_rng))
        return dg

    def _uat_frame(self, tr: Track, lat: float, lon: float, alt: float, sec: int) -> bytes:
        # cycle like a real UAT transmitter: MS with flight ID, AUXSV, MS with squawk, AUXSV
        kind = sec % 4
        ptype = 1 if kind in (0, 2) else 2
        payload = uat_payload(tr.icao, lat, lon, alt, tr.gs, tr.trk, tr.vs, ptype=ptype,
                              callsign=tr.callsign if kind == 0 else "1200", flight_id=(kind == 0),
                              emitter=tr.emitter_ca)
        block = uat.rs_encode(payload, uat.LONG_ROOTS)
        r = self.rng.random()
        if r < 0.4:
            self.uat_sent["clean"] += 1
        elif r < 0.75:
            block = corrupt(block, self.rng.randint(1, 7), self.rng)
            self.uat_sent["correctable"] += 1
        else:
            block = corrupt(block, self.rng.randint(10, 16), self.rng)
            self.uat_sent["uncorrectable"] += 1
        return wrap_0x24(block)


def busy_datagrams(seconds: int = 300, seed: int = 11, t0: float = 1_700_000_000.0, uat_targets: bool = True,
                   fisb: bool = False):
    """A busy, imperfect stream for replay tests: many 1090 targets plus UAT targets, no ownship
    (so positions need global CPR), about 8% of datagrams dropped and irregular timing.
    fisb adds a FIS-B uplink datagram each second.
    Returns (datagrams as [(t, bytes)], the SimSentry for its truth and UAT counts)."""
    tracks = busy_tracks() + (uat_tracks() if uat_targets else [])
    s = SimSentry(tracks=tracks, seed=seed, send_ownship=False, fisb=fisb)
    rng = random.Random(seed)
    out = []
    for sec in range(seconds):
        dgs = s.datagrams(float(sec))
        for i, d in enumerate(dgs):
            if i and rng.random() < 0.08:
                continue
            out.append((t0 + sec + min(0.95, i * 0.01 + rng.uniform(0, 0.03)), d))
    out.sort(key=lambda x: x[0])
    return out, s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default="127.0.0.1:4000")
    ap.add_argument("--seconds", type=float, default=0, help="0 = forever")
    ap.add_argument("--no-fix", action="store_true", help="simulate no GPS fix (bench)")
    ap.add_argument("--uat", action="store_true", help="add two UAT targets (0x24 frames with byte errors)")
    ap.add_argument("--fisb", action="store_true", help="add a FIS-B uplink (0x07) each second")
    a = ap.parse_args(argv)
    host, _, port = a.dest.rpartition(":")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sim = SimSentry(tracks=default_tracks() + (uat_tracks() if a.uat else []), gps_fix=not a.no_fix, fisb=a.fisb,
                    opstatus=True)
    start = time.monotonic()
    print(f"Simulated Sentry -> {a.dest} ({len(sim.tracks)} targets, fix={'no' if a.no_fix else 'yes'})")
    try:
        while a.seconds <= 0 or time.monotonic() - start < a.seconds:
            t = time.monotonic() - start
            for d in sim.datagrams(t):
                s.sendto(d, (host, int(port)))
            time.sleep(max(0.0, 1.0 - (time.monotonic() - start - t)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
