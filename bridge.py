#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Sentry bridge: turns the Sentry's public-mode raw ADS-B into standard GDL90 traffic.

In publicGDL90 ("Compatibility Mode") the Sentry already sends standard Heartbeat (0x00)
and Ownship (0x0A/0x0B) frames, but traffic arrives in two vendor wrappers that EFB apps
ignore:
  0x21  raw 1090ES Mode S DF17/18 squitters, decoded with pyModeS v2
  0x24  raw 978 MHz UAT ADS-B messages, sent WITHOUT error correction; uat.py applies
        Reed-Solomon correction and drops anything uncorrectable
Each target is tracked and sent as a standard GDL90 Traffic Report (0x14) at 1 Hz.

If the Sentry is in ForeFlight (encrypted) mode there is nothing to bridge; the bridge
logs a warning. With --auto-enable it switches compatibility mode back on, but only when
the Sentry comes up encrypted after a gap in its stream (a power cycle or the bridge
starting). A switch in the middle of a running stream is somebody pressing Disable in
ForeFlight, and is left alone.

Typical cockpit use (Pi joined to the Sentry WiFi, tablet on the same WiFi):
    python3 bridge.py --ownship-icao A1B2C3 --dest 192.168.4.20:4000

The tablet keeps receiving heartbeat/ownship straight from the Sentry and gets traffic
from the bridge. Use --relay if the tablet is NOT on the Sentry's network, so the
bridge forwards those standard frames too.

Positions are never extrapolated. A target stops being sent STALE seconds after its
last position (default 15 s) and is dropped after 60 s of silence.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import queue
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import gdl90
import uat
from sentry_ctl import RECEIVER, keepalive_due  # receiver HTTP: no proxy, no redirects

try:
    import pyModeS as pms
except ImportError:  # pragma: no cover
    sys.exit("pyModeS v2 is required:  pip install -r requirements.txt")

NM_PER_RAD = 3440.065
MAX_LOCAL_REF_NM = 170.0       # airborne local CPR decode is valid within ~180 NM of ref
MAX_GLOBAL_REF_NM = 300.0      # beyond 1090 radio range: a global fix farther from ownship is bad data
MAX_TARGET_KT = 900.0          # sanity limit between successive positions
JUMP_SLACK_NM = 0.1            # ...plus this much, for CPR rounding and arrival jitter


def fold_lon(lon):
    """Bring a local CPR longitude into ±180. Global decode already does this."""
    if lon is None:
        return None
    if lon > 180:
        return lon - 360
    if lon < -180:
        return lon + 360
    return lon
GLOBAL_PAIR_WINDOW_S = 10.0
DROP_AFTER_S = 60.0
MAX_TARGETS = 256              # SB_MAX_TARGETS: the C core's fixed target table
MAX_FRAME_BYTES = 1024         # the C core's frame buffer (sb_feed raw[])
STREAM_GAP_S = 10.0            # silence longer than this means the Sentry (or bridge) restarted
ENABLE_RETRY_S = 20.0          # auto-enable: wait between attempts
ENABLE_MAX_TRIES = 3

# GDL90 emitter categories from ADS-B (typecode, CA)
_EMITTER = {
    (4, 1): 1, (4, 2): 2, (4, 3): 3, (4, 4): 4, (4, 5): 5, (4, 6): 6, (4, 7): 7,
    (3, 1): 9, (3, 2): 10, (3, 3): 11, (3, 4): 12, (3, 6): 14, (3, 7): 15,
    (2, 1): 17, (2, 3): 18, (2, 4): 19, (2, 5): 19, (2, 6): 19, (2, 7): 19,
}
# NIC implied by airborne position typecode (approximate, assumes supplements = 0)
_NIC_BY_TC = {9: 11, 10: 10, 11: 8, 12: 7, 13: 6, 14: 5, 15: 4, 16: 3, 17: 1, 18: 0,
              20: 11, 21: 10, 22: 0, 5: 11, 6: 10, 7: 8, 8: 0}


def dist_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * NM_PER_RAD * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class Target:
    icao: int
    addr_type: int = 0
    callsign: str = ""
    emitter: int = 0
    lat: float | None = None
    lon: float | None = None
    pos_t: float = 0.0
    nic: int = 8
    alt_ft: int | None = None
    alt_t: float = 0.0          # pressure-altitude freshness, independent of position
    # Geometric height is not kept: a traffic report carries pressure altitude only.
    airborne: bool = True
    gs_kt: float | None = None
    track_deg: float | None = None
    track_type: int = 0
    vs_fpm: int | None = None
    last_seen: float = 0.0
    even: tuple | None = None   # (hex, t)
    odd: tuple | None = None
    msgs: int = 0
    nacp: int = 0              # unknown until operational status (1090) or mode status (UAT)
    emergency: int = 0
    uat: bool = False           # heard on 978 MHz
    slot: int = 0               # index in the C core's table, which decides eviction ties
    # lat/lon/pos_t keep the last accepted position for plausibility checks. lost: a newer message
    # was implausible, so nothing is reported until a fresh position passes those checks.
    lost: bool = False


@dataclass
class Stats:
    datagrams: int = 0
    frames: dict = field(default_factory=dict)
    crc_bad: int = 0
    modes_msgs: int = 0
    modes_other: int = 0
    pos_decoded: int = 0
    pos_rejected: int = 0
    sent: int = 0
    uat_msgs: int = 0           # FEC-valid UAT messages
    uat_fixed: int = 0          # ... of which needed byte corrections
    uat_bad: int = 0            # uncorrectable, dropped
    uat_skipped: int = 0        # valid but not traffic (beacons, reserved, no position)


class Bridge:
    def __init__(self, sentry_ip: str | None = "192.168.4.1", ownship_icao: int | None = None,
                 ref: tuple[float, float] | None = None, stale_s: float = 15.0, relay: bool = False,
                 log=print):
        self.sentry_ip = sentry_ip or None
        self.ownship_icao = ownship_icao
        self.fixed_ref = ref
        self.stale_s = stale_s
        self.relay = relay
        self.log = log
        self.targets: dict[tuple[int, int], Target] = {}  # (GDL90 address type, address)
        self.stats = Stats()
        self.own_lat = self.own_lon = None
        self.own_alt = None
        self.own_t = 0.0
        self.gps_valid = False
        self._ghost_hits: dict[tuple[int, int], int] = {}
        self._ghost_reported: set[tuple[int, int]] = set()
        # stream mode tracking
        self.mode = "?"                 # PUBLIC, ENCRYPTED or ? (nothing seen yet)
        self.last_rx: float | None = None
        self._fresh = True              # next mode-bearing frame is the first after a gap
        self.stream_starts = 0          # bumps on the first datagram and after every gap
        self.encrypted_after_gap = False
        self._enable_tries = 0
        self._enable_last = -1e9

    # ----------------------------------------------------------- reference position
    def ref_position(self, now: float):
        if self.own_lat is not None and now - self.own_t < 10:
            return self.own_lat, self.own_lon
        return self.fixed_ref

    # ----------------------------------------------------------- input
    def feed(self, datagram: bytes, src: str | None, now: float) -> list[bytes]:
        """Process a datagram; now uses the same monotonic seconds as tick/wants_enable.
        Returns frames to relay immediately."""
        if self.sentry_ip and src != self.sentry_ip:
            return []
        self.stats.datagrams += 1
        if self.last_rx is None or now - self.last_rx > STREAM_GAP_S:
            self._fresh = True
            self.stream_starts += 1
        self.last_rx = now
        out = []
        for f in gdl90.parse(datagram):
            # Over MAX_FRAME_BYTES (CRC included, unstuffed) is longer than any GDL90 frame; the C
            # core's buffer holds no more, so both count it as bad.
            if not f.crc_ok or len(f.raw) > MAX_FRAME_BYTES:
                self.stats.crc_bad += 1
                continue
            mid = f.msg_id
            self.stats.frames[mid] = self.stats.frames.get(mid, 0) + 1
            if mid == gdl90.MSG_HEARTBEAT and len(f.msg) >= 7:
                self.gps_valid = gdl90.decode_heartbeat(f.msg)["gps_pos_valid"]
                self._set_mode("PUBLIC")
            elif mid == gdl90.MSG_UAV_ENCRYPTED:
                self._set_mode("ENCRYPTED")
            elif mid == gdl90.MSG_OWNSHIP and len(f.msg) >= 28:
                self._ownship(gdl90.decode_report(f.msg), now)
            elif mid == gdl90.MSG_UAV_TRAFFIC_RAW:
                self._raw_modes(f.msg, now)
            elif mid == gdl90.MSG_UAV_UAT_RAW:
                self._uat(f.msg, now)
            if self.relay and mid not in (gdl90.MSG_UAV_TRAFFIC_RAW, gdl90.MSG_UAV_UAT_RAW,
                                          gdl90.MSG_UAV_ENCRYPTED, gdl90.MSG_UAV_STATUS,
                                          gdl90.MSG_UAV_SENSOR):
                out.append(gdl90.frame(f.msg))
        return out

    # ----------------------------------------------------------- mode tracking
    def _set_mode(self, mode: str):
        if self._fresh:
            self._fresh = False
            self.encrypted_after_gap = mode == "ENCRYPTED"
        elif mode == "ENCRYPTED" and self.mode != "ENCRYPTED":
            self.encrypted_after_gap = False   # switched mid-stream: deliberate
        if mode == "PUBLIC":
            self._enable_tries = 0
        if mode == self.mode:
            return
        prev, self.mode = self.mode, mode
        if mode == "ENCRYPTED":
            how = "came up in" if self.encrypted_after_gap else "switched to"
            self.log(f"WARNING: Sentry {how} ForeFlight (encrypted) mode. No traffic can be bridged. "
                     f"Run: python sentry_ctl.py enable")
        elif prev == "ENCRYPTED":
            self.log("Sentry is back in compatibility (public) mode")

    def wants_enable(self, now: float) -> bool:
        """True when the Sentry came up encrypted after a stream gap and an enable attempt is due
        (at most ENABLE_MAX_TRIES per episode, ENABLE_RETRY_S apart)."""
        if (self.mode == "ENCRYPTED" and self.encrypted_after_gap
                and self._enable_tries < ENABLE_MAX_TRIES and now - self._enable_last >= ENABLE_RETRY_S):
            self._enable_tries += 1
            self._enable_last = now
            return True
        return False

    # ----------------------------------------------------------- UAT (978 MHz)
    def _uat(self, msg: bytes, now: float):
        r = uat.decode_frame(msg)
        if r is None:
            self.stats.uat_bad += 1
            return
        self.stats.uat_msgs += 1
        if r.fec_errors:
            self.stats.uat_fixed += 1
        addr_type = uat.GDL90_ADDR_TYPE.get(r.qualifier)
        if addr_type is None or r.lat is None:
            self.stats.uat_skipped += 1
            return
        if r.qualifier in (0, 2, 6) and self.ownship_icao is not None and r.address == self.ownship_icao:
            return
        t = self._target(addr_type, r.address)
        t.last_seen = now
        t.msgs += 1
        t.uat = True
        if r.has_ms:
            t.emitter = r.emitter
            t.nacp = r.nacp
            t.emergency = r.emergency
            if r.callsign_is_flight_id and r.callsign:   # the field alternates with the squawk
                t.callsign = r.callsign
        alt = r.baro_alt_ft
        if alt is not None:
            t.alt_ft, t.alt_t = alt, now
        t.airborne = r.airborne
        t.gs_kt = r.gs_kt
        if r.track_deg is not None:
            t.track_deg, t.track_type = r.track_deg, r.track_type
        t.vs_fpm = r.vs_fpm if r.airborne else 0
        if self._accept(t, r.lat, r.lon, now, None):
            t.lat, t.lon, t.pos_t, t.nic, t.lost = r.lat, r.lon, now, r.nic, False
            self.stats.pos_decoded += 1
        else:
            self.stats.pos_rejected += 1

    def _target(self, addr_type: int, icao: int) -> Target:
        """The target for an address. A new one takes the lowest free slot; when all MAX_TARGETS
        are in use it replaces the target heard least recently (lowest slot on a tie), as in C."""
        key = (addr_type, icao)
        t = self.targets.get(key)
        if t is None:
            if len(self.targets) < MAX_TARGETS:
                used = {other.slot for other in self.targets.values()}
                slot = next(i for i in range(MAX_TARGETS) if i not in used)
            else:
                victim = min(self.targets.values(), key=lambda other: (other.last_seen, other.slot))
                self._forget((victim.addr_type, victim.icao))
                slot = victim.slot
            t = self.targets[key] = Target(icao=icao, addr_type=addr_type, slot=slot)
        return t

    def _forget(self, key):
        """Remove a target and its ownship-ghost state; C clears both with the slot."""
        del self.targets[key]
        self._ghost_hits.pop(key, None)
        self._ghost_reported.discard(key)

    def _ownship(self, r: gdl90.Report, now: float):
        if r.lat is None or r.lon is None or (abs(r.lat) < 1e-6 and abs(r.lon) < 1e-6):
            return
        self.own_lat, self.own_lon, self.own_t = r.lat, r.lon, now
        self.own_alt = r.alt_ft

    def _raw_modes(self, msg: bytes, now: float):
        body = msg[4:] if msg[1:4] == b"\xff\xff\xff" else msg[1:]
        if len(body) < 11:
            self.stats.modes_other += 1
            return
        df = body[0] >> 3
        if df not in (17, 18):
            self.stats.modes_other += 1
            return
        addr_type = 0
        if df == 18:
            cf = body[0] & 0x7
            if cf in (3, 4, 7):        # coarse TIS-B, management, reserved: not decoded
                self.stats.modes_other += 1
                return
            addr_type = {0: 0, 1: 1, 2: 2, 5: 3, 6: 0}[cf]
        icao = int.from_bytes(body[1:4], "big")
        if addr_type in (0, 2) and self.ownship_icao is not None and icao == self.ownship_icao:
            return
        self.stats.modes_msgs += 1
        hexmsg = (body[:14] + bytes(max(0, 14 - len(body)))).hex().upper()
        t = self._target(addr_type, icao)
        t.last_seen = now
        t.msgs += 1
        tc = body[4] >> 3
        try:
            if 1 <= tc <= 4:
                t.callsign = pms.adsb.callsign(hexmsg).replace("_", "").strip()
                t.emitter = _EMITTER.get((tc, body[4] & 0x7), 0)
            elif 5 <= tc <= 8:
                self._surface(t, hexmsg, tc, now)
            elif 9 <= tc <= 18 or 20 <= tc <= 22:
                self._airborne(t, hexmsg, tc, now)
            elif tc == 19:
                spd, ang, vr, kind = pms.adsb.velocity(hexmsg)
                if spd is not None:
                    t.gs_kt = spd
                if ang is not None:
                    t.track_deg = ang
                    t.track_type = 1 if kind == "GS" else 2
                t.vs_fpm = vr
            elif tc == 31:
                # Aircraft operational status, DO-260A/B (version 1 or 2): NACp in ME bits 45-48.
                # Version 0 has no NACp field, so those targets stay at 0 (unknown).
                me = int.from_bytes(bytes.fromhex(hexmsg)[4:11], "big")
                subtype, version = (me >> 48) & 0x7, (me >> 13) & 0x7
                if subtype <= 1 and version >= 1:
                    t.nacp = (me >> 8) & 0xF
        except Exception as e:  # malformed or unexpected message: count and move on
            self.stats.modes_other += 1
            self.log(f"decode error {icao:06X} tc={tc}: {e}")

    def _accept(self, t: Target, lat: float, lon: float, now: float, ref, limit=MAX_LOCAL_REF_NM) -> bool:
        if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return False
        if ref is not None and dist_nm(ref[0], ref[1], lat, lon) > limit:
            return False
        # Speed gate, with slack for CPR rounding and arrival jitter between closely spaced messages.
        if (t.lat is not None and 0 <= now - t.pos_t < 30
                and dist_nm(t.lat, t.lon, lat, lon) > MAX_TARGET_KT * (now - t.pos_t) / 3600.0 + JUMP_SLACK_NM):
            return False
        return True

    def _set_pos(self, t: Target, lat, lon, tc, now):
        t.lat, t.lon, t.pos_t, t.lost = lat, lon, now, False
        t.nic = _NIC_BY_TC.get(tc, 8)
        self.stats.pos_decoded += 1

    def _airborne(self, t: Target, hexmsg: str, tc: int, now: float):
        t.airborne = True
        alt = pms.adsb.altitude(hexmsg)   # also for 20-22, so a decode error counts as in C
        if tc < 19:   # 20-22 carry GNSS height, which a traffic report has no field for
            t.alt_ft = int(alt) if alt is not None else None
            t.alt_t = now
        if pms.adsb.oe_flag(hexmsg):
            t.odd = (hexmsg, now)
        else:
            t.even = (hexmsg, now)
        # 1) local decode against the target's own recent position
        if t.lat is not None and now - t.pos_t < 60:
            lat, lon = pms.adsb.position_with_ref(hexmsg, t.lat, t.lon)
            lon = fold_lon(lon)
            if self._accept(t, lat, lon, now, (t.lat, t.lon)):
                return self._set_pos(t, lat, lon, tc, now)
            # Lost track: stop reporting, but keep the last accepted position as the speed-check
            # anchor so neither this jump nor a repeat of it can return through the decodes below.
            t.lost = True
        self._fresh_position(t, hexmsg, tc, now)

    def _fresh_position(self, t: Target, hexmsg: str, tc: int, now: float) -> bool:
        # 2) global decode from an even/odd pair (no reference needed)
        if t.even and t.odd and abs(t.even[1] - t.odd[1]) <= GLOBAL_PAIR_WINDOW_S:
            pos = pms.adsb.position(t.even[0], t.odd[0], t.even[1], t.odd[1])
            if pos is not None:
                # 170 NM is the local-CPR ambiguity radius; a global fix only has to be in radio range.
                lat, lon = pos[0], fold_lon(pos[1])
                if self._accept(t, lat, lon, now, self.ref_position(now), MAX_GLOBAL_REF_NM):
                    self._set_pos(t, lat, lon, tc, now)
                    return True
                self.stats.pos_rejected += 1
                return False
        # 3) local decode against ownship / fixed reference
        ref = self.ref_position(now)
        if ref is not None:
            lat, lon = pms.adsb.position_with_ref(hexmsg, ref[0], ref[1])
            lon = fold_lon(lon)
            if self._accept(t, lat, lon, now, ref):
                self._set_pos(t, lat, lon, tc, now)
                return True
            self.stats.pos_rejected += 1
        return False

    def _surface(self, t: Target, hexmsg: str, tc: int, now: float):
        t.airborne = False
        ref = (t.lat, t.lon) if t.lat is not None and now - t.pos_t < 60 else self.ref_position(now)
        if ref is None:
            return
        lat, lon = pms.adsb.position_with_ref(hexmsg, ref[0], ref[1])
        lon = fold_lon(lon)
        if self._accept(t, lat, lon, now, None) and dist_nm(ref[0], ref[1], lat, lon) < 40:
            self._set_pos(t, lat, lon, tc, now)
        spd, trk, _, _ = pms.adsb.surface_velocity(hexmsg)
        if spd is not None:
            t.gs_kt = spd
        if trk is not None:
            t.track_deg, t.track_type = trk, 1
        t.vs_fpm = 0

    # ----------------------------------------------------------- output
    def tick(self, now: float) -> list[bytes]:
        """Called about once per second. Returns GDL90 Traffic Report frames."""
        frames = []
        for key in [k for k, t in self.targets.items() if now - t.last_seen > DROP_AFTER_S]:
            self._forget(key)
        for t in self.targets.values():
            if t.alt_ft is not None and now - t.alt_t > self.stale_s:
                t.alt_ft = None
            if t.lat is None or t.lost or now - t.pos_t > self.stale_s:
                continue
            self._ghost_check(t)
            r = gdl90.Report(
                address=t.icao, lat=t.lat, lon=t.lon, alt_ft=t.alt_ft, airborne=t.airborne,
                nic=t.nic, nacp=t.nacp, gs_kt=t.gs_kt, vs_fpm=t.vs_fpm, track_deg=t.track_deg,
                track_type=t.track_type if t.track_deg is not None else 0,
                extrapolated=False, emitter=t.emitter, callsign=t.callsign, addr_type=t.addr_type,
                emergency=t.emergency,
            )
            frames.append(gdl90.encode_report(r))
        self.stats.sent += len(frames)
        return frames

    def _ghost_check(self, t: Target):
        """Hint at the aircraft's own ICAO when a target sits on top of ownship."""
        if (t.addr_type not in (0, 2) or self.ownship_icao is not None or self.own_lat is None
                or t.alt_ft is None or self.own_alt is None):
            return
        key = (t.addr_type, t.icao)
        if key in self._ghost_reported:
            return
        close = dist_nm(self.own_lat, self.own_lon, t.lat, t.lon) < 0.2 and abs(t.alt_ft - self.own_alt) < 300
        self._ghost_hits[key] = self._ghost_hits.get(key, 0) + 1 if close else 0
        if self._ghost_hits[key] >= 10:
            self._ghost_reported.add(key)
            self.log(f"HINT: target {t.icao:06X} has tracked ownship for 10 s. "
                     f"If that is this aircraft, restart with --ownship-icao {t.icao:06X}")

    def summary(self) -> str:
        s = self.stats
        live = sum(1 for t in self.targets.values() if t.lat is not None and not t.lost)
        live_uat = sum(1 for t in self.targets.values() if t.lat is not None and not t.lost and t.uat)
        ids = " ".join(f"{k:02X}:{v}" for k, v in sorted(s.frames.items()))
        ref = "ownship" if self.own_lat is not None else ("fixed" if self.fixed_ref else "none")
        return (f"mode={self.mode} dgrams={s.datagrams} frames[{ids}] modes={s.modes_msgs} other={s.modes_other} "
                f"uat={s.uat_msgs}(fixed {s.uat_fixed}, bad {s.uat_bad}) "
                f"targets={len(self.targets)} positioned={live} (uat {live_uat}) sent={s.sent} "
                f"rejected={s.pos_rejected} crc_bad={s.crc_bad} gps={'ok' if self.gps_valid else 'no'} ref={ref}")


# ------------------------------------------------------------------ socket runner

def _parse_dest(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    return (host or s), int(port or 4000)


ANNOUNCE_PORT = 63093
ANNOUNCE_TTL_S = 30.0
MAX_APPS = 32                  # announcements noted, not destinations


def local_route(address: str) -> str | None:
    """This machine's address the OS uses to reach address, or None. Sends nothing (a UDP connect
    only selects a route)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((address, 9))
            local = probe.getsockname()[0]
        except OSError:
            return None
    return None if local == "0.0.0.0" else local


def changed_route(sentry_ip: str, bound: str) -> str | None:
    """The new local address that reaches the Sentry when it differs from bound, else None."""
    local = local_route(sentry_ip)
    return local if local and not local.startswith("127.") and local != bound else None


def is_this_device(address: str) -> bool:
    """Loopback or one of this machine's addresses: the OS picks it as the source to reach itself."""
    return address.startswith("127.") or local_route(address) == address


def announce_label(app) -> str:
    """The announced app name as a log label: printable ASCII without backslashes, like the C core."""
    return "".join(c for c in str(app) if " " <= c <= "~" and c != "\\")[:63]


def parse_announce(data: bytes) -> tuple[str, int | None]:
    """EFB discovery broadcast, e.g. {"App":"ForeFlight","GDL90":{"port":4000}}."""
    try:
        d = json.loads(data.decode("utf-8", "strict"))
        if isinstance(d["GDL90"]["port"], bool):   # int(true) is 1; C finds no digits
            return "", None
        port = int(d["GDL90"]["port"])
        return announce_label(d.get("App", "?")), port if 0 < port < 65536 else None
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return "", None


# Receiver HTTP failures: network errors, bad URLs and malformed responses (http.client raises
# HTTPException, not OSError, for a bad status line or a truncated body). None may end a worker thread.
RECEIVER_ERRORS = (OSError, ValueError, http.client.HTTPException)


def enable_public(host: str, timeout: float = 5.0) -> tuple[bool, str]:
    """POST {"publicGDL90": true}, the same request sentry_ctl.py enable sends."""
    req = urllib.request.Request(f"http://{host}/settings/?action=set", data=b'{"publicGDL90":true}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with RECEIVER.open(req, timeout=timeout) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        e.close()  # also a refused redirect: close its response
        return False, f"HTTP {e.code}"
    except RECEIVER_ERRORS as e:
        return False, str(e)


POWER_SAVINGS_WARNING = (
    "WARNING: Sentry Power Savings is ON. It shuts the Sentry off after 30 min without "
    "ForeFlight connected. Disable it in Sentry's web settings; ForeFlight is optional.")


def power_savings_on(settings_json: str) -> bool | None:
    """True/False from the Sentry's settings JSON, None if it can't be read."""
    try:
        v = json.loads(settings_json)["Power"]["powerSavings"]
    except (ValueError, KeyError, TypeError):
        return None
    return v if isinstance(v, bool) else None


def check_power_savings(host: str, log, timeout: float = 3.0) -> bool | None:
    """Read the settings and log a warning if Power Savings is on. Read-only."""
    try:
        with RECEIVER.open(f"http://{host}/settings/?action=get", timeout=timeout) as r:
            on = power_savings_on(r.read().decode("utf-8", "replace"))
    except RECEIVER_ERRORS as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()  # also a refused redirect: close its response
        log(f"could not read Sentry settings to check Power Savings: {e}")
        return None
    if on:
        log(POWER_SAVINGS_WARNING)
    return on


def run(a) -> int:
    def log(msg):
        print(time.strftime("%H:%M:%S ") + msg, flush=True)

    ref = None
    if a.ref:
        lat, lon = (float(x) for x in a.ref.split(","))
        ref = (lat, lon)
    bridge = Bridge(sentry_ip=a.sentry_ip, ownship_icao=int(a.ownship_icao, 16) if a.ownship_icao else None,
                    ref=ref, stale_s=a.stale, relay=a.relay, log=log)
    dests = [_parse_dest(d) for d in a.dest]
    # An EFB on this device on the input port shares it, as in the GUIs: bind only the address that
    # reaches the Sentry, so its unicast reaches the bridge and loopback output reaches the EFB.
    shared = any(host.startswith("127.") and port == a.listen_port for host, port in dests)
    bind_ip = "0.0.0.0"
    if shared:
        bind_ip = local_route(a.sentry_ip) if a.sentry_ip else None
        if not bind_ip or bind_ip.startswith("127."):
            log(f"ERROR: sharing UDP {a.listen_port} with an EFB on this device needs --sentry-ip of a Sentry on "
                f"another device, reachable from this one (join its WiFi first).")
            return 2
    def open_input(ip):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: SO_REUSEADDR would let another socket take the port and split its packets
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif ip != "0.0.0.0":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((ip, a.listen_port))
        except OSError:
            sock.close()
            raise
        return sock

    try:
        rx = open_input(bind_ip)
    except OSError as e:
        if shared:
            log(f"ERROR: cannot share UDP port {a.listen_port} ({e}): the EFB holding it does not allow port "
                f"sharing. Close it, or give the EFB another port and use --dest 127.0.0.1:<that port>.")
        else:
            log(f"ERROR: cannot listen on UDP port {a.listen_port} ({e}). Another program is probably "
                f"using it: an EFB app or a second copy of this bridge. Close it and try again.")
        return 2
    if shared:
        log(f"sharing UDP {a.listen_port} with the EFB on this device (bound to {bind_ip})")
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rawlog = open(a.log_raw, "a", buffering=1) if a.log_raw else None
    log(f"listening :{a.listen_port} from {a.sentry_ip or 'any'}; sending to {dests}; relay={a.relay}")

    ann = None
    if a.announce_port:
        ann = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ann.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            ann.bind(("0.0.0.0", a.announce_port))
        except OSError as e:
            log(f"note: EFB discovery off, port {a.announce_port} unavailable ({e})")
            ann.close()
            ann = None
    dynamic: dict[tuple[str, int], float] = {}

    def send(frames):
        for d in dests:
            for fr in frames:
                try:
                    tx.sendto(fr, d)
                except OSError as e:
                    log(f"send to {d} failed: {e}")
                    break

    next_tick = time.monotonic() + a.interval
    checked_starts = 0
    http_host = a.sentry_ip if a.http_port == 80 else f"{a.sentry_ip}:{a.http_port}"

    # Sentry web requests run on one worker thread, in order, so a slow or absent web server
    # never pauses traffic forwarding.
    web = queue.Queue()

    def web_worker():
        while True:
            web.get()()

    def auto_enable():
        ok, info = enable_public(http_host)
        log(f"auto-enable: compatibility mode {'requested' if ok else 'FAILED'} ({info})")

    contact = {"ok": None, "pending": False, "last": None, "rx": None}

    def keepalive():
        """Read-only status request; logs only when the result changes."""
        try:
            with RECEIVER.open(f"http://{http_host}/?action=get", timeout=3.0) as r:
                r.read(65536)
                ok, info = True, f"HTTP {r.status}"
        except RECEIVER_ERRORS as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            ok, info = False, str(e)
        finally:
            contact["pending"] = False
        if ok != contact["ok"]:
            log(f"keepalive: receiver status {'OK' if ok else 'FAILED'} ({info})")
            contact["ok"] = ok

    threading.Thread(target=web_worker, name="sentry-web", daemon=True).start()
    next_stat = time.monotonic() + a.stats_every
    socks = [rx] + ([ann] if ann else [])
    next_route = time.monotonic() + 5
    try:
        while True:
            timeout = max(0.0, next_tick - time.monotonic())
            ready, _, _ = select.select(socks, [], [], timeout)
            now = time.monotonic()
            if rx in ready:
                data, (src, _) = rx.recvfrom(65535)
                if rawlog:
                    rawlog.write(json.dumps({"t": round(time.time(), 4), "src": src, "hex": data.hex()}) + "\n")
                if not a.sentry_ip or src == a.sentry_ip:
                    contact["rx"] = now
                relay = bridge.feed(data, src, now)
                if relay:
                    send(relay)
            if ann is not None and ann in ready:
                data, (src, _) = ann.recvfrom(4096)
                app, port = parse_announce(data)
                key = (src, port)
                # Noted only. Traffic goes to --dest, never to an address that merely announced.
                if (port and src != a.sentry_ip and (key in dynamic or len(dynamic) < MAX_APPS)
                        and not (port == a.listen_port and is_this_device(src))):
                    if key not in dynamic:
                        log(f"app '{app}' announced at {src}:{port}; not sent (pass --dest)")
                    dynamic[key] = time.monotonic()
            if a.sentry_ip and bridge.stream_starts != checked_starts:
                checked_starts = bridge.stream_starts
                web.put(lambda: check_power_savings(http_host, log))
            if a.auto_enable and bridge.wants_enable(now):
                if not a.sentry_ip:
                    log("--auto-enable needs --sentry-ip")
                else:
                    web.put(auto_enable)
            # One request at a time; a slow receiver delays the next one instead of queueing more.
            if a.sentry_ip and not contact["pending"] and keepalive_due(now, contact["last"], contact["rx"]):
                contact.update(pending=True, last=now)
                # Its own thread: a slow status page must not delay the checks on the web queue.
                threading.Thread(target=keepalive, name="sentry-keepalive", daemon=True).start()
            # A shared input is bound to the address that reached the Sentry at start. While silent, follow
            # a new one (this computer rejoined the Sentry's WiFi), as the GUIs do.
            if shared and now >= next_route and (contact["rx"] is None or now - contact["rx"] >= 5):
                next_route = now + 5
                local = changed_route(a.sentry_ip, bind_ip)
                if local:
                    try:
                        replacement = open_input(local)
                    except OSError as e:
                        log(f"the route to the Sentry changed to {local}, but UDP {a.listen_port} could not be bound there ({e})")
                    else:
                        rx.close()
                        rx = socks[0] = replacement
                        bind_ip = local
                        log(f"the route to the Sentry changed: listening on {local}:{a.listen_port}")
            m = time.monotonic()
            if m >= next_tick:
                for d in [k for k, seen in dynamic.items() if m - seen > ANNOUNCE_TTL_S]:
                    del dynamic[d]
                    log(f"app at {d[0]}:{d[1]} stopped announcing, removed")
                send(bridge.tick(now))
                next_tick = m + a.interval
            if m >= next_stat:
                log(bridge.summary() + f" noted={len(dynamic)}")
                next_stat = m + a.stats_every
    except KeyboardInterrupt:
        log("stopped")
    return 0


DOUBLE_CLICK_BANNER = """Sentry bridge
-------------
1. Join this computer to the Sentry's WiFi (SentryPlus_XXXX).
2. Put your tablet on the same WiFi and pick a GDL90 device in your EFB.
Start it with --dest <efb-ipv4>:4000 --auto-enable. It does not broadcast.
Close this window to stop.
"""


def double_clicked(argv=None) -> bool:
    """A packaged build started with no arguments, e.g. by double-clicking the .exe."""
    return bool(getattr(sys, "frozen", False)) and not (sys.argv[1:] if argv is None else argv)


def main(argv=None) -> int:
    if double_clicked(argv):
        print(DOUBLE_CLICK_BANNER, flush=True)
        argv = ["--auto-enable"]
        try:
            return _main(argv)
        finally:
            try:
                input("\nBridge stopped. Press Enter to close this window.")
            except (EOFError, KeyboardInterrupt):
                pass
    return _main(argv)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen-port", type=int, default=4000)
    ap.add_argument("--sentry-ip", default="192.168.4.1", help="only accept data from this IP ('' = any)")
    ap.add_argument("--dest", action="append", default=None,
                    help="unicast host:port to send GDL90 to, repeatable (required)")
    ap.add_argument("--relay", action="store_true", help="also forward the Sentry's standard frames")
    ap.add_argument("--ownship-icao", help="this aircraft's ICAO hex, filtered from traffic")
    ap.add_argument("--ref", help="fallback reference LAT,LON when the Sentry has no GPS fix")
    ap.add_argument("--stale", type=float, default=15.0, help="stop sending a target after N s without position")
    ap.add_argument("--interval", type=float, default=1.0, help="traffic report interval (s)")
    ap.add_argument("--stats-every", type=float, default=10.0)
    ap.add_argument("--log-raw", help="also record every received datagram to this .jsonl")
    ap.add_argument("--announce-port", type=int, default=ANNOUNCE_PORT,
                    help="note EFB announcements; do not send to them (0 = off)")
    ap.add_argument("--auto-enable", action="store_true",
                    help="re-enable compatibility mode when the Sentry comes up encrypted after a "
                         "stream gap (power cycle / bridge start); never after a mid-stream Disable")
    ap.add_argument("--http-port", type=int, default=80, help="Sentry web port (default 80)")
    a = ap.parse_args(argv)
    if not 0 < a.listen_port < 65536:
        ap.error("bad --listen-port")
    if not 0 < a.http_port < 65536:
        ap.error("bad --http-port")
    if not 0 <= a.announce_port < 65536:
        ap.error("bad --announce-port")
    # A zero or negative interval would resend traffic in a tight loop.
    if not (a.interval > 0 and a.stale > 0 and a.stats_every > 0):
        ap.error("--interval, --stale and --stats-every must be greater than 0")
    if not a.dest:
        ap.error("pass --dest HOST:PORT (unicast). Broadcast is not used.")
    for dest in a.dest:
        host = dest.rpartition(":")[0] or dest
        try:  # resolve once, as the C CLI does, so a placeholder or typo stops here
            ip = ipaddress.IPv4Address(socket.gethostbyname(host))
        except OSError:
            ap.error(f"cannot resolve --dest {dest}")
        if ip.is_multicast or ip.is_unspecified or int(ip) in (0xFFFFFFFF, 0xC0A804FF):
            ap.error(f"use a unicast address, not {host}")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
