# SPDX-License-Identifier: GPL-3.0-only
"""UAT (978 MHz) ADS-B decoding for the Sentry's 0x24 frames.

In public (compatibility) mode the Sentry forwards each UAT ADS-B message it hears as
a 0x24 frame: ff ff ff + the raw 48-byte long message (34 data bytes + 14 bytes of
Reed-Solomon parity). It does NOT apply error correction first; on the ground a large
share of frames arrive with byte errors. So every frame goes through RS decoding here
and anything uncorrectable is dropped, the same rule dump978 uses. Plausible-looking
but uncorrectable frames carry garbage (wrong longitude, wrong address), so they must
never be used.

Field layout follows DO-282B as implemented in dump978 (uat_decode.c).
Standard library only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------- Reed-Solomon
# GF(2^8) with primitive polynomial 0x187, generator alpha = 2, first consecutive root 120.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x187
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
FCR = 120


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    return 0 if a == 0 else _EXP[(_LOG[a] - _LOG[b]) % 255]


def _pow_alpha(e: int) -> int:
    return _EXP[e % 255]


def _poly_eval(p: list[int], x: int) -> int:
    """p[0] is the highest-degree coefficient."""
    y = 0
    for c in p:
        y = _mul(y, x) ^ c
    return y


def rs_correct(block: bytes, nroots: int) -> tuple[bytes, int] | None:
    """Correct a shortened RS codeword (data then nroots parity bytes).

    Returns (corrected data bytes, number of byte errors fixed) or None if uncorrectable.
    """
    n = len(block)
    cw = list(block)
    synd = [_poly_eval(cw, _pow_alpha(FCR + j)) for j in range(nroots)]
    if not any(synd):
        return bytes(cw[: n - nroots]), 0

    # Berlekamp-Massey. Polynomials here are lowest-degree first.
    lam = [1]
    prev = [1]
    L = 0
    m = 1
    b = 1
    for k in range(nroots):
        d = synd[k]
        for i in range(1, L + 1):
            if i < len(lam):
                d ^= _mul(lam[i], synd[k - i])
        if d == 0:
            m += 1
            continue
        coef = _div(d, b)
        shifted = [0] * m + [_mul(coef, c) for c in prev]
        new = lam + [0] * max(0, len(shifted) - len(lam))
        for i, c in enumerate(shifted):
            new[i] ^= c
        if 2 * L <= k:
            prev, L, b, m = lam, k + 1 - L, d, 1
        else:
            m += 1
        lam = new
    while len(lam) > 1 and lam[-1] == 0:
        lam.pop()
    nerr = len(lam) - 1
    if nerr == 0 or nerr > nroots // 2:
        return None

    # Chien search over the positions that exist in the shortened block.
    pos = []
    for i in range(n):
        deg = n - 1 - i
        xinv = _pow_alpha(-deg)
        y = 0
        for c in reversed(lam):
            y = _mul(y, xinv) ^ c
        if y == 0:
            pos.append(i)
    if len(pos) != nerr:
        return None

    # Forney. omega = S(x) * lambda(x) mod x^nroots, lowest-degree first.
    omega = [0] * nroots
    for i, s in enumerate(synd):
        for j, l in enumerate(lam):
            if i + j < nroots:
                omega[i + j] ^= _mul(s, l)
    for i in pos:
        deg = n - 1 - i
        xinv = _pow_alpha(-deg)
        om = 0
        for c in reversed(omega):
            om = _mul(om, xinv) ^ c
        dl = 0  # formal derivative of lambda at xinv: odd terms only
        for j in range(1, len(lam), 2):
            dl ^= _mul(lam[j], _pow_alpha(-deg * (j - 1)))
        if dl == 0:
            return None
        mag = _mul(_pow_alpha(deg * (1 - FCR)), _div(om, dl))
        cw[i] ^= mag

    if any(_poly_eval(cw, _pow_alpha(FCR + j)) for j in range(nroots)):
        return None
    return bytes(cw[: n - nroots]), nerr


_GENERATORS: dict[int, list[int]] = {}


def _rs_generator(nroots: int) -> list[int]:
    """g(x) = product of (x - alpha^(FCR+i)), highest-degree coefficient first."""
    g = [1]
    for i in range(nroots):
        root = _pow_alpha(FCR + i)
        ng = g + [0]
        for j, c in enumerate(g):
            ng[j + 1] ^= _mul(c, root)
        g = ng
    return g


def rs_encode(data: bytes, nroots: int) -> bytes:
    """Systematic RS encode: data followed by nroots parity bytes (used by the simulator and tests)."""
    g = _GENERATORS.get(nroots)
    if g is None:
        g = _GENERATORS[nroots] = _rs_generator(nroots)
    rem = [0] * nroots
    for d in data:
        fb = d ^ rem[0]
        rem = rem[1:] + [0]
        if fb:
            for j in range(nroots):
                rem[j] ^= _mul(fb, g[j + 1])
    return bytes(data) + bytes(rem)


# ---------------------------------------------------------------- decoding
LONG_LEN, LONG_ROOTS = 48, 14
BASIC_LEN, BASIC_ROOTS = 30, 12
_BASE40 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ  .."

AG_SUBSONIC, AG_SUPERSONIC, AG_GROUND = 0, 1, 2
# address qualifier -> GDL90 traffic address type (None = do not report as traffic)
GDL90_ADDR_TYPE = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: None, 6: 0, 7: None}
QUALIFIER_NAME = {0: "ADS-B", 1: "ADS-B anon", 2: "TIS-B", 3: "TIS-B track", 4: "vehicle",
                  5: "beacon", 6: "ADS-R", 7: "reserved"}


@dataclass
class UatReport:
    payload_type: int
    qualifier: int
    address: int
    fec_errors: int
    lat: float | None = None
    lon: float | None = None
    alt_ft: int | None = None          # primary altitude
    alt_geo: bool = False              # primary altitude is geometric (not baro)
    alt2_ft: int | None = None         # secondary altitude (opposite type), from AUXSV
    nic: int = 0
    airground: int = AG_SUBSONIC
    gs_kt: float | None = None
    track_deg: float | None = None
    track_type: int = 0                # GDL90 encoding: 0 invalid, 1 true track, 2 mag hdg, 3 true hdg
    vs_fpm: int | None = None
    # mode status (payload types 1 and 3)
    has_ms: bool = False
    emitter: int = 0
    callsign: str = ""
    callsign_is_flight_id: bool = False  # False = the field is a squawk, not a callsign
    emergency: int = 0
    nacp: int = 0

    @property
    def airborne(self) -> bool:
        return self.airground != AG_GROUND

    @property
    def baro_alt_ft(self) -> int | None:
        """Pressure altitude, which is what GDL90 traffic reports carry."""
        if self.alt_ft is not None and not self.alt_geo:
            return self.alt_ft
        if self.alt2_ft is not None and self.alt_geo:
            return self.alt2_ft
        return None


def _alt(raw: int) -> int | None:
    return None if raw == 0 else (raw - 1) * 25 - 1000


def decode_mdb(m: bytes, fec_errors: int = 0) -> UatReport | None:
    """Decode a corrected UAT ADS-B payload (18 or 34 bytes)."""
    ptype = m[0] >> 3
    if ptype > 10 or (len(m) == 18 and ptype != 0):
        return None
    r = UatReport(payload_type=ptype, qualifier=m[0] & 7,
                  address=(m[1] << 16) | (m[2] << 8) | m[3], fec_errors=fec_errors)

    raw_lat = (m[4] << 15) | (m[5] << 7) | (m[6] >> 1)
    raw_lon = ((m[6] & 1) << 23) | (m[7] << 15) | (m[8] << 7) | (m[9] >> 1)
    if raw_lat or raw_lon:
        lat = raw_lat * 360.0 / 16777216.0
        lon = raw_lon * 360.0 / 16777216.0
        r.lat = lat - 180.0 if lat > 90.0 else lat
        r.lon = lon - 360.0 if lon > 180.0 else lon
    r.alt_geo = bool(m[9] & 1)
    r.alt_ft = _alt((m[10] << 4) | (m[11] >> 4))
    r.nic = m[11] & 0x0F
    r.airground = (m[12] >> 6) & 3

    if r.airground in (AG_SUBSONIC, AG_SUPERSONIC):
        raw_ns = ((m[12] & 0x1F) << 6) | ((m[13] & 0xFC) >> 2)
        raw_ew = ((m[13] & 0x03) << 9) | (m[14] << 1) | ((m[15] & 0x80) >> 7)
        if raw_ns & 0x3FF and raw_ew & 0x3FF:
            mult = 4 if r.airground == AG_SUPERSONIC else 1
            ns = ((raw_ns & 0x3FF) - 1) * mult * (-1 if raw_ns & 0x400 else 1)
            ew = ((raw_ew & 0x3FF) - 1) * mult * (-1 if raw_ew & 0x400 else 1)
            r.gs_kt = math.hypot(ns, ew)
            if ns or ew:
                r.track_deg = math.degrees(math.atan2(ew, ns)) % 360.0
                r.track_type = 1
        raw_vv = ((m[15] & 0x7F) << 4) | ((m[16] & 0xF0) >> 4)
        if raw_vv & 0x1FF:
            r.vs_fpm = ((raw_vv & 0x1FF) - 1) * 64 * (-1 if raw_vv & 0x200 else 1)
    elif r.airground == AG_GROUND:
        raw_gs = ((m[12] & 0x1F) << 6) | ((m[13] & 0xFC) >> 2)
        if raw_gs & 0x3FF:
            r.gs_kt = float((raw_gs & 0x3FF) - 1)
        raw_trk = ((m[13] & 0x03) << 9) | (m[14] << 1) | ((m[15] & 0x80) >> 7)
        tt = (raw_trk & 0x0600) >> 9  # 1 track, 2 mag heading, 3 true heading
        if tt:
            r.track_deg = (raw_trk & 0x1FF) * 360.0 / 512.0
            r.track_type = tt

    if len(m) >= 34:
        if ptype in (1, 3):
            r.has_ms = True
            v = (m[17] << 8) | m[18]
            r.emitter = (v // 1600) % 40
            cs = [_BASE40[(v // 40) % 40], _BASE40[v % 40]]
            for hi, lo in ((19, 20), (21, 22)):
                v = (m[hi] << 8) | m[lo]
                cs += [_BASE40[(v // 1600) % 40], _BASE40[(v // 40) % 40], _BASE40[v % 40]]
            r.callsign = "".join(cs).rstrip()
            r.emergency = (m[23] >> 5) & 7
            r.nacp = (m[25] >> 4) & 0x0F
            r.callsign_is_flight_id = bool(m[26] & 0x02)
        if ptype in (1, 2, 5, 6):
            r.alt2_ft = _alt((m[29] << 4) | ((m[30] & 0xF0) >> 4))
    return r


def decode_frame(frame: bytes) -> UatReport | None:
    """Decode a Sentry 0x24 frame (message ID included). None if invalid or uncorrectable."""
    if len(frame) < 4 or frame[0] != 0x24:
        return None
    body = frame[4:] if frame[1:4] == b"\xff\xff\xff" else frame[1:]
    if len(body) == LONG_LEN:
        fixed = rs_correct(body, LONG_ROOTS)
    elif len(body) == BASIC_LEN:
        fixed = rs_correct(body, BASIC_ROOTS)
    else:
        return None
    if fixed is None:
        return None
    data, nerr = fixed
    return decode_mdb(data, nerr)
