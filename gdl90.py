"""GDL90 framing, CRC, and the message encoders/decoders the bridge needs.

Reference: FAA/Garmin "GDL 90 Data Interface Specification" 560-1058-00 Rev A.
CRC uses the spec's table form: crc = T[crc >> 8] ^ (crc << 8) ^ byte, LSB first on the wire.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

FLAG = 0x7E
ESC = 0x7D

MSG_HEARTBEAT = 0x00
MSG_UPLINK = 0x07
MSG_OWNSHIP = 0x0A
MSG_OWNSHIP_GEO_ALT = 0x0B
MSG_TRAFFIC = 0x14
MSG_UAV_TRAFFIC_RAW = 0x21   # uAvionix: raw Mode S DF17/18 in public mode
MSG_UAV_UAT_RAW = 0x24       # uAvionix: raw UAT long ADS-B (48 bytes incl. RS parity, not corrected)
MSG_UAV_STATUS = 0x25        # uAvionix: static device/status frame
MSG_UAV_ENCRYPTED = 0x26     # uAvionix: AES payload (ForeFlight mode)
MSG_UAV_SENSOR = 0x28        # uAvionix: 5 s sensor summary (not decoded)
MSG_FF_EXT = 0x65            # ForeFlight extension (ID / AHRS)

KNOWN_IDS = {
    0x00: "Heartbeat",
    0x02: "Initialization",
    0x07: "Uplink (FIS-B)",
    0x09: "Height Above Terrain",
    0x0A: "Ownship",
    0x0B: "Ownship Geo Alt",
    0x14: "Traffic",
    0x1E: "Basic UAT",
    0x1F: "Long UAT",
    0x21: "uAvionix raw Mode S",
    0x24: "uAvionix raw UAT",
    0x25: "uAvionix status",
    0x26: "uAvionix encrypted",
    0x28: "uAvionix sensor summary",
    0x4C: "Levil/Stratux AHRS",
    0x65: "ForeFlight ext",
}

_CRC_TABLE = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) if _c & 0x8000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFF)


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = (_CRC_TABLE[crc >> 8] ^ (crc << 8) ^ b) & 0xFFFF
    return crc


def _stuff(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (FLAG, ESC):
            out += bytes((ESC, b ^ 0x20))
        else:
            out.append(b)
    return bytes(out)


def _unstuff(data: bytes) -> bytes:
    out, esc = bytearray(), False
    for b in data:
        if esc:
            out.append(b ^ 0x20)
            esc = False
        elif b == ESC:
            esc = True
        else:
            out.append(b)
    return bytes(out)


def frame(msg: bytes) -> bytes:
    """Wrap a message (ID byte + data) in flags, CRC and byte stuffing."""
    crc = crc16(msg)
    return bytes((FLAG,)) + _stuff(msg + bytes((crc & 0xFF, crc >> 8))) + bytes((FLAG,))


@dataclass
class Frame:
    msg_id: int
    msg: bytes        # ID byte + data, CRC removed
    crc_ok: bool
    raw: bytes        # unstuffed bytes between flags, CRC included


def parse(datagram: bytes) -> list[Frame]:
    """Split a UDP payload into GDL90 frames. Tolerates several frames per datagram."""
    frames = []
    for chunk in datagram.split(bytes((FLAG,))):
        if not chunk:
            continue
        raw = _unstuff(chunk)
        if len(raw) < 3:
            continue
        msg, rx = raw[:-2], raw[-2] | (raw[-1] << 8)
        frames.append(Frame(msg[0], msg, crc16(msg) == rx, raw))
    return frames


# ---------------------------------------------------------------- traffic / ownship

def _s24(v: int) -> int:
    return v - (1 << 24) if v & 0x800000 else v


def _latlon_to_24(deg: float) -> int:
    v = int(round(deg / (180.0 / (1 << 23))))
    return v & 0xFFFFFF


EMERGENCY_NONE = 0


@dataclass
class Report:
    """Fields shared by Ownship (0x0A) and Traffic (0x14) reports."""
    address: int
    lat: float | None = None
    lon: float | None = None
    alt_ft: int | None = None          # pressure altitude
    airborne: bool = True
    nic: int = 8
    nacp: int = 8
    gs_kt: int | None = None
    vs_fpm: int | None = None
    track_deg: float | None = None
    track_type: int = 1                # 0 invalid, 1 true track, 2 mag heading, 3 true heading
    extrapolated: bool = False
    emitter: int = 0
    callsign: str = ""
    alert: int = 0
    addr_type: int = 0
    emergency: int = EMERGENCY_NONE


def encode_report(r: Report, msg_id: int = MSG_TRAFFIC) -> bytes:
    """Encode a Traffic (0x14) or Ownship (0x0A) report, returns the framed bytes."""
    b = bytearray(28)
    b[0] = msg_id
    b[1] = ((r.alert & 0xF) << 4) | (r.addr_type & 0xF)
    b[2:5] = (r.address & 0xFFFFFF).to_bytes(3, "big")
    if r.lat is None or r.lon is None:
        lat24 = lon24 = 0
        nic = 0
    else:
        lat24, lon24, nic = _latlon_to_24(r.lat), _latlon_to_24(r.lon), r.nic
    b[5:8] = lat24.to_bytes(3, "big")
    b[8:11] = lon24.to_bytes(3, "big")
    if r.alt_ft is None:
        alt = 0xFFF
    else:
        alt = max(0, min(0xFFE, int(round((r.alt_ft + 1000) / 25.0))))
    tt = r.track_type if r.track_deg is not None else 0
    misc = (0x8 if r.airborne else 0) | (0x4 if r.extrapolated else 0) | (tt & 0x3)
    b[11] = alt >> 4
    b[12] = ((alt & 0xF) << 4) | misc
    b[13] = ((nic & 0xF) << 4) | (r.nacp & 0xF)
    hv = 0xFFF if r.gs_kt is None else max(0, min(0xFFE, int(round(r.gs_kt))))
    if r.vs_fpm is None:
        vv = 0x800
    else:
        vv = max(-510, min(510, int(round(r.vs_fpm / 64.0)))) & 0xFFF
    b[14] = hv >> 4
    b[15] = ((hv & 0xF) << 4) | (vv >> 8)
    b[16] = vv & 0xFF
    b[17] = 0 if r.track_deg is None else int(round((r.track_deg % 360.0) / (360.0 / 256))) & 0xFF
    b[18] = r.emitter & 0xFF
    cs = "".join(c for c in r.callsign.upper() if c.isalnum())[:8].ljust(8)
    b[19:27] = cs.encode("ascii")
    b[27] = (r.emergency & 0xF) << 4
    return frame(bytes(b))


def decode_report(msg: bytes) -> Report:
    """Decode a Traffic or Ownship report (msg = ID byte + 27 data bytes)."""
    if len(msg) < 28:
        raise ValueError(f"report too short: {len(msg)}")
    address = int.from_bytes(msg[2:5], "big")
    lat24 = _s24(int.from_bytes(msg[5:8], "big"))
    lon24 = _s24(int.from_bytes(msg[8:11], "big"))
    alt = (msg[11] << 4) | (msg[12] >> 4)
    misc = msg[12] & 0xF
    nic, nacp = msg[13] >> 4, msg[13] & 0xF
    hv = (msg[14] << 4) | (msg[15] >> 4)
    vv = ((msg[15] & 0xF) << 8) | msg[16]
    tt = misc & 0x3
    res = 180.0 / (1 << 23)
    return Report(
        address=address,
        lat=lat24 * res if nic else None,
        lon=lon24 * res if nic else None,
        alt_ft=None if alt == 0xFFF else alt * 25 - 1000,
        airborne=bool(misc & 0x8),
        extrapolated=bool(misc & 0x4),
        nic=nic,
        nacp=nacp,
        gs_kt=None if hv == 0xFFF else hv,
        vs_fpm=None if vv == 0x800 else (vv - 0x1000 if vv & 0x800 else vv) * 64,
        track_deg=msg[17] * 360.0 / 256 if tt else None,
        track_type=tt,
        emitter=msg[18],
        callsign=msg[19:27].decode("ascii", "replace").strip(),
        alert=msg[1] >> 4,
        addr_type=msg[1] & 0xF,
        emergency=msg[27] >> 4,
    )


def encode_heartbeat(gps_valid: bool, utc_seconds: int, uat_ok: bool = True) -> bytes:
    s1 = (0x80 if gps_valid else 0) | (0x01 if uat_ok else 0)
    ts = utc_seconds % 86400
    s2 = 0x80 if ts & 0x10000 else 0
    s2 |= 0x01  # UTC OK
    return frame(bytes((MSG_HEARTBEAT, s1, s2, ts & 0xFF, (ts >> 8) & 0xFF, 0, 0)))


def decode_heartbeat(msg: bytes) -> dict:
    s1, s2 = msg[1], msg[2]
    ts = ((s2 & 0x80) << 9) | msg[3] | (msg[4] << 8)
    return {
        "gps_pos_valid": bool(s1 & 0x80),
        "maint_req": bool(s1 & 0x40),
        "gps_batt_low": bool(s1 & 0x08),
        "uat_initialized": bool(s1 & 0x01),
        "utc_ok": bool(s2 & 0x01),
        "timestamp_s": ts,
    }


def entropy(b: bytes) -> float:
    if not b:
        return 0.0
    counts = {}
    for x in b:
        counts[x] = counts.get(x, 0) + 1
    n = len(b)
    return -sum(c / n * math.log2(c / n) for c in counts.values())
