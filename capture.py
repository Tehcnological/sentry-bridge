#!/usr/bin/env python3
"""Record and analyze the Sentry's GDL90 stream.

Record (no root needed, run on a machine joined to the Sentry WiFi):
    python3 capture.py record -o outdoor.jsonl --seconds 600

Analyze a recording, or a tcpdump/Wireshark capture (pcap or pcapng):
    python3 capture.py analyze outdoor.jsonl
    python3 capture.py analyze outdoor.pcap --report outdoor-report.txt

Attaching the report to an issue helps decode the parts still unknown
(FIS-B uplinks, UAT traffic, AHRS, CO, the 0x25/0x28 frames).
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import gdl90

_CALLSIGN_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"


def modes_callsign(body: bytes) -> str:
    """Callsign of an ADS-B identification message (TC 1-4), as pyModeS v2 decodes it, minus
    padding. Built in so the packaged tools need no pyModeS (GPLv3) or NumPy."""
    me = int.from_bytes(body[5:11], "big")  # ME bits 9-56: eight 6-bit characters
    chars = (_CALLSIGN_CHARS[(me >> (42 - 6 * i)) & 0x3F] for i in range(8))
    return "".join(c for c in chars if c != "#").strip("_ ")


# ------------------------------------------------------------------ recording

def cmd_record(a) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", a.port))
    s.settimeout(0.5)
    out = open(a.output, "a", buffering=1)
    start = time.time()
    last_print = start
    window: Counter = Counter()
    total = 0
    print(f"Recording UDP :{a.port} to {a.output}. Ctrl-C to stop.")
    try:
        while a.seconds <= 0 or time.time() - start < a.seconds:
            try:
                data, (src, sport) = s.recvfrom(65535)
            except socket.timeout:
                data = None
            now = time.time()
            if data:
                total += 1
                out.write(json.dumps({"t": round(now, 4), "src": src, "sport": sport, "hex": data.hex()}) + "\n")
                for f in gdl90.parse(data):
                    window[f"0x{f.msg_id:02X}" + ("" if f.crc_ok else "!")] += 1
            if now - last_print >= 5:
                ids = " ".join(f"{k}:{v}" for k, v in sorted(window.items())) or "no data"
                print(f"[{now - start:6.0f}s] datagrams={total}  last5s {ids}")
                window.clear()
                last_print = now
    except KeyboardInterrupt:
        pass
    out.close()
    print(f"Done. {total} datagrams saved to {a.output}")
    return 0


# ------------------------------------------------------------------ readers

def read_jsonl(path: Path, port: int):
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            yield r["t"], r.get("src", "?"), r.get("sport", 0), port, bytes.fromhex(r["hex"])


def _ip_udp(pkt: bytes):
    """Return (src, dst, sport, dport, payload) for an IPv4/UDP packet, else None."""
    if len(pkt) < 20 or pkt[0] >> 4 != 4:
        return None
    ihl = (pkt[0] & 0xF) * 4
    if pkt[9] != 17 or len(pkt) < ihl + 8:
        return None
    src = socket.inet_ntoa(pkt[12:16])
    dst = socket.inet_ntoa(pkt[16:20])
    sport, dport, ulen = struct.unpack("!HHH", pkt[ihl:ihl + 6])
    payload = pkt[ihl + 8: ihl + ulen]
    return src, dst, sport, dport, payload


def _strip_link(linktype: int, data: bytes):
    if linktype == 1:  # Ethernet
        off, et = 14, struct.unpack("!H", data[12:14])[0]
        while et == 0x8100 and len(data) >= off + 4:  # VLAN
            et = struct.unpack("!H", data[off + 2:off + 4])[0]
            off += 4
        return data[off:] if et == 0x0800 else None
    if linktype == 113:  # Linux cooked SLL
        return data[16:] if struct.unpack("!H", data[14:16])[0] == 0x0800 else None
    if linktype == 276:  # SLL2
        return data[20:] if struct.unpack("!H", data[0:2])[0] == 0x0800 else None
    if linktype in (0, 108):  # BSD loopback
        return data[4:]
    if linktype in (101, 12, 228):  # raw IPv4
        return data
    return None


def read_pcap(path: Path, port: int, flows: Counter):
    raw = path.read_bytes()
    magic = raw[:4]
    if magic == b"\x0a\x0d\x0d\x0a":
        yield from _read_pcapng(raw, port, flows)
        return
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        en, nano = "<", magic == b"\x4d\x3c\xb2\xa1"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        en, nano = ">", magic == b"\xa1\xb2\x3c\x4d"
    else:
        raise ValueError("not a pcap/pcapng file")
    linktype = struct.unpack(en + "I", raw[20:24])[0]
    off = 24
    while off + 16 <= len(raw):
        ts_s, ts_f, incl, _ = struct.unpack(en + "IIII", raw[off:off + 16])
        data = raw[off + 16: off + 16 + incl]
        off += 16 + incl
        t = ts_s + ts_f / (1e9 if nano else 1e6)
        yield from _emit(t, linktype, data, port, flows)


def _tsresol(body: bytes, en: str) -> float:
    """Seconds per timestamp unit of an interface block: option 9 (if_tsresol), default 1 us.
    The options follow link type, reserved and snap length (8 bytes)."""
    off = 8
    while off + 4 <= len(body):
        code, length = struct.unpack(en + "HH", body[off:off + 4])
        if code == 0:
            break
        if code == 9 and length >= 1 and off + 4 < len(body):
            value = body[off + 4]
            return 2.0 ** -(value & 0x7F) if value & 0x80 else 10.0 ** -value
        off += 4 + (length + 3) // 4 * 4
    return 1e-6


def _read_pcapng(raw: bytes, port: int, flows: Counter):
    off, en = 0, "<"
    iface_link: list[int] = []
    iface_res: list[float] = []
    while off + 12 <= len(raw):
        btype = struct.unpack(en + "I", raw[off:off + 4])[0]
        if btype == 0x0A0D0D0A:
            bom = raw[off + 8:off + 12]
            en = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            iface_link, iface_res = [], []
        blen = struct.unpack(en + "I", raw[off + 4:off + 8])[0]
        body = raw[off + 8: off + blen - 4]
        if btype == 1:
            iface_link.append(struct.unpack(en + "H", body[0:2])[0])
            iface_res.append(_tsresol(body, en))
        elif btype == 6 and iface_link:
            iid, th, tl, cap, _ = struct.unpack(en + "IIIII", body[:20])
            t = ((th << 32) | tl) * iface_res[iid]
            yield from _emit(t, iface_link[iid], body[20:20 + cap], port, flows)
        elif btype == 3 and iface_link:
            plen = struct.unpack(en + "I", body[:4])[0]
            yield from _emit(0.0, iface_link[0], body[4:4 + plen], port, flows)
        if blen < 12:
            break
        off += blen


def _emit(t, linktype, data, port, flows):
    ip = _strip_link(linktype, data)
    if ip is None:
        return
    r = _ip_udp(ip)
    if r is None:
        return
    src, dst, sport, dport, payload = r
    flows[(src, dst, sport, dport)] += 1
    if dport == port or sport == port:
        yield t, src, sport, dport, payload


# ------------------------------------------------------------------ analysis

def _variability(samples: list[bytes]) -> str:
    """Which byte offsets change across samples of one length."""
    if len(samples) < 2:
        return "n/a (one sample)"
    n = len(samples[0])
    varying = [i for i in range(n) if len({s[i] for s in samples}) > 1]
    if not varying:
        return "constant"
    # collapse runs
    runs, start, prev = [], varying[0], varying[0]
    for i in varying[1:]:
        if i == prev + 1:
            prev = i
            continue
        runs.append((start, prev))
        start = prev = i
    runs.append((start, prev))
    return ", ".join(f"{s}" if s == e else f"{s}-{e}" for s, e in runs)


def analyze(records, report_lines: list[str], hexdump: int = 3, show_ownship: bool = False):
    P = report_lines.append
    by_id: dict[int, list] = defaultdict(list)
    crc_bad = Counter()
    sources = Counter()
    n_dgram = 0
    t0 = t1 = None
    hb_valid = Counter()
    ownship = None
    traffic_addrs = set()
    raw21 = []
    for t, src, sport, dport, payload in records:
        n_dgram += 1
        sources[src] += 1
        t0 = t if t0 is None else min(t0, t)
        t1 = t if t1 is None else max(t1, t)
        for f in gdl90.parse(payload):
            if not f.crc_ok:
                crc_bad[f.msg_id] += 1
                continue
            by_id[f.msg_id].append((t, f.msg))
            if f.msg_id == gdl90.MSG_HEARTBEAT and len(f.msg) >= 7:
                hb_valid[gdl90.decode_heartbeat(f.msg)["gps_pos_valid"]] += 1
            elif f.msg_id == gdl90.MSG_OWNSHIP and len(f.msg) >= 28:
                ownship = gdl90.decode_report(f.msg)
            elif f.msg_id == gdl90.MSG_TRAFFIC and len(f.msg) >= 28:
                traffic_addrs.add(gdl90.decode_report(f.msg).address)
            elif f.msg_id == gdl90.MSG_UAV_TRAFFIC_RAW:
                raw21.append(f.msg)

    dur = (t1 - t0) if t0 is not None and t1 is not None else 0
    P(f"Datagrams: {n_dgram}   duration: {dur:.1f} s   sources: {dict(sources)}")
    P(f"CRC failures by id: {dict((hex(k), v) for k, v in crc_bad.items()) or 'none'}")
    ids = set(by_id)
    if gdl90.MSG_UAV_ENCRYPTED in ids:
        mode = "ENCRYPTED (ForeFlight mode)"
    elif ids & {gdl90.MSG_HEARTBEAT, gdl90.MSG_OWNSHIP}:
        mode = "PUBLIC (standard GDL90)"
    else:
        mode = "UNKNOWN / no data"
    P(f"Mode: {mode}")
    P("")
    P(f"{'ID':<6}{'name':<22}{'count':>7}{'rate/s':>8}{'len':>10}{'entropy':>9}")
    for mid in sorted(by_id):
        items = by_id[mid]
        lens = [len(m) for _, m in items]
        ent = sum(gdl90.entropy(m[1:]) for _, m in items) / len(items)
        rate = len(items) / dur if dur > 0 else 0
        lr = f"{min(lens)}" if min(lens) == max(lens) else f"{min(lens)}-{max(lens)}"
        P(f"0x{mid:02X}  {gdl90.KNOWN_IDS.get(mid, 'UNKNOWN'):<22}{len(items):>7}{rate:>8.2f}{lr:>10}{ent:>9.2f}")
    P("")

    if hb_valid:
        P(f"Heartbeat GPS position valid: {hb_valid[True]} / {sum(hb_valid.values())}")
    if ownship and show_ownship:
        P(f"Last ownship: addr={ownship.address:06X} lat={ownship.lat} lon={ownship.lon} "
          f"alt={ownship.alt_ft} ft nic={ownship.nic} nacp={ownship.nacp} "
          f"gs={ownship.gs_kt} trk={ownship.track_deg} cs='{ownship.callsign}'")
    elif ownship:
        # Reports get shared: keep this aircraft's position and identity out unless asked.
        P(f"Last ownship: nic={ownship.nic} nacp={ownship.nacp} "
          f"(position, address and callsign omitted; --show-ownship includes them)")
    if traffic_addrs:
        P(f"Standard 0x14 traffic targets: {len(traffic_addrs)}")
    if gdl90.MSG_UPLINK in by_id:
        lens = Counter(len(m) for _, m in by_id[gdl90.MSG_UPLINK])
        P(f"FIS-B uplinks (0x07): {len(by_id[gdl90.MSG_UPLINK])}, lengths {dict(lens)}  <-- weather present")
    else:
        P("FIS-B uplinks (0x07): none seen (outdoors with ground-station coverage needed)")

    if raw21:
        P("")
        P(f"0x21 raw Mode S frames: {len(raw21)}")
        dfs, tcs, icaos, cs = Counter(), Counter(), set(), {}
        lens = Counter(len(m) for m in raw21)
        prefixes = Counter(m[1:4].hex() for m in raw21)
        for m in raw21:
            body = m[4:]
            if not body:
                continue
            df = body[0] >> 3
            dfs[df] += 1
            if df in (17, 18) and len(body) >= 11:
                icaos.add(body[1:4].hex())
                tc = body[4] >> 3
                tcs[tc] += 1
                if 1 <= tc <= 4:
                    cs[body[1:4].hex()] = modes_callsign(body)
        P(f"  lengths {dict(lens)}  prefix bytes 1-3 {dict(prefixes.most_common(4))}")
        P(f"  DF counts {dict(dfs)}   typecodes {dict(sorted(tcs.items()))}")
        P(f"  unique addresses: {len(icaos)}   callsigns: {cs or 'none decoded'}")
        other = [m for m in raw21 if m[4:5] and (m[4] >> 3) not in (17, 18)]
        if other:
            P(f"  non-DF17/18 bodies (possible UAT or other): {len(other)}, sample {other[0].hex()}")

    unknown = [mid for mid in by_id if mid not in (0x00, 0x07, 0x0A, 0x0B, 0x14, 0x21)]
    if unknown:
        P("")
        P("Frames to decode (byte offsets that vary, per length; offset 0 = message ID):")
        for mid in sorted(unknown):
            groups = defaultdict(list)
            for _, m in by_id[mid]:
                groups[len(m)].append(m)
            for ln, ms in sorted(groups.items()):
                P(f"  0x{mid:02X} len {ln} x{len(ms)}: varying offsets {_variability(ms)}")
                seen = []
                for m in ms:
                    if m not in seen:
                        seen.append(m)
                    if len(seen) >= hexdump:
                        break
                for m in seen:
                    P(f"     {m.hex()}")
    return by_id


def cmd_analyze(a) -> int:
    path = Path(a.input)
    flows: Counter = Counter()
    if path.suffix == ".jsonl":
        records = list(read_jsonl(path, a.port))
    else:
        records = list(read_pcap(path, a.port, flows))
    lines: list[str] = [f"Sentry capture analysis: {path.name}", ""]
    analyze(records, lines, a.hexdump, a.show_ownship)
    if flows:
        lines += ["", "All UDP flows in capture (src -> dst sport/dport: packets):"]
        for (s, d, sp, dp), n in flows.most_common(15):
            lines.append(f"  {s} -> {d}  {sp}/{dp}: {n}")
    text = "\n".join(lines)
    print(text)
    if a.report:
        Path(a.report).write_text(text + "\n")
        print(f"\nReport written to {a.report}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4000)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("-o", "--output", default=f"sentry-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    r.add_argument("--seconds", type=float, default=0, help="0 = until Ctrl-C")
    an = sub.add_parser("analyze")
    an.add_argument("input")
    an.add_argument("--report")
    an.add_argument("--hexdump", type=int, default=3, help="sample frames per unknown id")
    an.add_argument("--show-ownship", action="store_true",
                    help="include this aircraft's position, address and callsign in the report")
    a = ap.parse_args(argv)
    return cmd_record(a) if a.cmd == "record" else cmd_analyze(a)


if __name__ == "__main__":
    sys.exit(main())
