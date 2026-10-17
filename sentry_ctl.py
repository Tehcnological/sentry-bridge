#!/usr/bin/env python3
"""Sentry control: status, settings backup, publicGDL90 on/off, live mode check.

Run on a machine joined to the Sentry's WiFi (Sentry is 192.168.4.1 by default).
Uses only the Python standard library.

    python3 sentry_ctl.py status
    python3 sentry_ctl.py backup
    python3 sentry_ctl.py check            # listen on :4000, report encrypted vs public
    python3 sentry_ctl.py enable           # backs up first, then sets publicGDL90=true
    python3 sentry_ctl.py disable          # sets publicGDL90=false (ForeFlight mode)
    python3 sentry_ctl.py logs             # list the Sentry's stored logs (/data/)
    python3 sentry_ctl.py logs --get all   # download them to sentry-logs/ (read-only)

publicGDL90 is ForeFlight's "Compatibility Mode". It does not show up in the settings JSON,
and on fw 1.0.32 it does NOT survive a power cycle: every power-up (including the Power
Savings auto-off) returns the Sentry to encrypted ForeFlight mode. `disable` restores
ForeFlight mode without a power cycle. In the cockpit, run bridge.py with --auto-enable.
"""

from __future__ import annotations

import argparse
import html.parser
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import gdl90

DEFAULT_HOST = "192.168.4.1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the 3xx response is raised as an HTTPError instead of being followed


# Receiver requests stay on the receiver's network: no system or environment proxy (a laptop's
# proxy settings would otherwise capture 192.168.4.1) and no redirects to another host.
RECEIVER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

# Keepalive for every bridge (the C CLI and Android mirror it): a Sentry Plus stopped streaming to a
# receive-only bridge after ~16 min until its status page was loaded, so read GET /?action=get at
# start, every 30 s while receiving and every 5 s while silent (3 s without data).
KEEPALIVE_S, SILENT_CONTACT_S, SILENT_AFTER_S = 30.0, 5.0, 3.0


def keepalive_due(now: float, last_contact: float | None, last_rx: float | None) -> bool:
    silent = last_rx is None or now - last_rx >= SILENT_AFTER_S
    return last_contact is None or now - last_contact >= (SILENT_CONTACT_S if silent else KEEPALIVE_S)


def _http(host: str, path: str, body: dict | None = None, timeout: float = 5.0) -> tuple[int, str]:
    url = f"http://{host}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with RECEIVER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        with e:  # also a refused redirect: close its response
            return e.code, e.read().decode("utf-8", "replace")


def _pretty(text: str) -> str:
    try:
        return json.dumps(json.loads(text), indent=2)
    except ValueError:
        return text


def cmd_status(a) -> int:
    code, text = _http(a.host, "/?action=get")
    print(f"HTTP {code}")
    print(_pretty(text))
    return 0 if code == 200 else 1


def do_backup(host: str, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = []
    for name, path in (("status", "/?action=get"), ("settings", "/settings/?action=get")):
        code, text = _http(host, path)
        if code != 200:
            raise RuntimeError(f"GET {path} returned HTTP {code}: {text[:200]}")
        p = outdir / f"sentry-{name}-{stamp}.json"
        p.write_text(_pretty(text) + "\n")
        paths.append(p)
    return paths[-1]


def cmd_backup(a) -> int:
    p = do_backup(a.host, Path(a.outdir))
    print(f"Saved status and settings backups, latest: {p}")
    return 0


def listen_ids(port: int, seconds: float, sentry_ip: str | None) -> dict[int, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.settimeout(0.5)
    counts: dict[int, int] = {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data, (src, _) = s.recvfrom(4096)
        except socket.timeout:
            continue
        if sentry_ip and src != sentry_ip:
            continue
        for f in gdl90.parse(data):
            if f.crc_ok:
                counts[f.msg_id] = counts.get(f.msg_id, 0) + 1
    s.close()
    return counts


def describe_mode(counts: dict[int, int]) -> str:
    if not counts:
        return "NO DATA (are you on the Sentry WiFi? is :4000 free? firewall?)"
    if gdl90.MSG_UAV_ENCRYPTED in counts:
        return "ENCRYPTED (ForeFlight mode)"
    if gdl90.MSG_HEARTBEAT in counts or gdl90.MSG_OWNSHIP in counts:
        return "PUBLIC (standard GDL90)"
    return "UNKNOWN"


# ---------------------------------------------------------------- stored logs (/data/)
# ForeFlight reads /data/ on connect (see CA796CE/foreflight-sentry-decryption,
# foreflight-connection-flow.md). It is a Mongoose directory index of stored telemetry
# logs whose CSV columns include GPS accuracy, packet counts and IMU values.

LOG_COLUMN_HINTS = re.compile(r"co\b|co_|carbon|batt|volt|charg|temp|pitch|roll|yaw|head|ahrs|"
                              r"accel|gyro|press|baro|alarm", re.I)


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("?", "/", "..", "#", "http:", "https:")):
                self.links.append(href)


def log_name(href: str) -> str | None:
    """The decoded file name of a /data/ link, or None when it is not a plain file name: a path,
    a parent reference such as %2e%2e, or control characters. ':' is allowed here (a clock time in
    a name); local_log_path replaces it, because on Windows it would name a drive or a stream."""
    name = urllib.parse.unquote(href)
    if not name or ".." in name or any(c in name for c in "/\\?#") or any(ord(c) < 32 for c in name):
        return None
    return name


def local_log_path(folder: Path, name: str) -> Path:
    """Where a listed log is saved: inside folder, with ':' replaced (Windows drive or stream)."""
    return folder / name.replace(":", "_")


def list_logs(host: str, timeout: float = 10.0) -> list[str]:
    code, text = _http(host, "/data/", timeout=timeout)
    if code != 200:
        raise RuntimeError(f"GET /data/ returned HTTP {code}: {text[:200]}")
    p = _Links()
    p.feed(text)
    seen, out = set(), []
    for name in p.links:
        if name not in seen and log_name(name):
            seen.add(name)
            out.append(name)
    return out


def fetch_log(host: str, name: str, dest: Path, max_bytes: int, timeout: float = 30.0) -> tuple[int, bool]:
    """Download one log. Returns (bytes written, truncated)."""
    url = f"http://{host}/data/{urllib.parse.quote(urllib.parse.unquote(name))}"
    n, truncated = 0, False
    with RECEIVER.open(url, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            if n + len(chunk) > max_bytes:
                f.write(chunk[: max_bytes - n])
                n, truncated = max_bytes, True
                break
            f.write(chunk)
            n += len(chunk)
    return n, truncated


def describe_columns(path: Path) -> str:
    """First line of a CSV-ish log: column count and any columns that look like CO/battery/attitude."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            header = f.readline().strip()
    except OSError:
        return ""
    if "," not in header:
        return "  (no CSV header)"
    cols = [c.strip() for c in header.split(",") if c.strip()]
    hits = [c for c in cols if LOG_COLUMN_HINTS.search(c)]
    out = f"  {len(cols)} columns"
    if hits:
        out += "; of interest: " + ", ".join(hits)
    return out


def cmd_logs(a) -> int:
    names = list_logs(a.host)
    if not names:
        print("No logs listed under /data/.")
        return 0
    print(f"{len(names)} file(s) in /data/:")
    for n in names:
        print(f"  {urllib.parse.unquote(n)}")
    if not a.get:
        print("Download with: python3 sentry_ctl.py logs --get all   (or --get NAME)")
        return 0
    want = names if a.get == "all" else [n for n in names if urllib.parse.unquote(n) == a.get or n == a.get]
    if not want:
        print(f"No log named {a.get!r}.")
        return 1
    outdir = Path(a.logdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for n in want:
        local = local_log_path(outdir, log_name(n))
        size, trunc = fetch_log(a.host, n, local, int(a.max_mb * 1024 * 1024))
        print(f"saved {local} ({size} bytes{', TRUNCATED at --max-mb' if trunc else ''})")
        desc = describe_columns(local)
        if desc:
            print(desc)
    return 0


def cmd_check(a) -> int:
    print(f"Listening on UDP :{a.port} for {a.seconds:.0f} s ...")
    counts = listen_ids(a.port, a.seconds, a.host.split(":")[0])
    for mid in sorted(counts):
        print(f"  0x{mid:02X} {gdl90.KNOWN_IDS.get(mid, '?'):<22} {counts[mid]}")
    print(f"Mode: {describe_mode(counts)}")
    return 0 if counts else 1


def _set_public(a, value: bool) -> int:
    if value and not a.skip_backup:
        p = do_backup(a.host, Path(a.outdir))
        print(f"Backup saved: {p}")
    code, text = _http(a.host, "/settings/?action=set", {"publicGDL90": value})
    print(f"HTTP {code}: {text.strip()[:200]}")
    if code != 200:
        return 1
    if a.no_verify:
        return 0
    time.sleep(1.0)
    counts = listen_ids(a.port, a.verify_seconds, a.host.split(":")[0])
    mode = describe_mode(counts)
    print(f"Stream now: {mode}  ids={[hex(k) for k in sorted(counts)]}")
    want = "PUBLIC" if value else "ENCRYPTED"
    if not mode.startswith(want):
        print(f"WARNING: expected {want}. Check the stream before flying with this unit.")
        return 2
    if value:
        print("Note: compatibility mode lasts until the next power cycle. "
              "Run `disable` (or power cycle) to restore full ForeFlight mode.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # argparse exits before required subcommands; no network request is made.
    if list(sys.argv[1:] if argv is None else argv) == ["--license"]:
        root = Path(__file__).resolve().parent
        for name in ("NOTICE.txt", "LICENSE", "notices/dump978-GPL-2.0.txt", "notices/Python-3.11.0.txt"):
            print((root / name).read_text(encoding="utf-8"))
        return 0
    ap.add_argument("--host", default=DEFAULT_HOST, help="Sentry IP[:port] (default %(default)s)")
    ap.add_argument("--port", type=int, default=4000, help="GDL90 UDP port to listen on")
    ap.add_argument("--outdir", default="backups", help="where backups go")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print device status JSON")
    sub.add_parser("backup", help="save status + settings JSON")
    lg = sub.add_parser("logs", help="list (and optionally download) the Sentry's stored logs, read-only")
    lg.add_argument("--get", help="file name to download, or 'all'")
    lg.add_argument("--logdir", default="sentry-logs")
    lg.add_argument("--max-mb", type=float, default=20.0, help="per-file size cap")
    c = sub.add_parser("check", help="listen and report encrypted vs public mode")
    c.add_argument("--seconds", type=float, default=8.0)
    for name in ("enable", "disable"):
        p = sub.add_parser(name, help=f"{name} publicGDL90")
        p.add_argument("--skip-backup", action="store_true")
        p.add_argument("--no-verify", action="store_true", help="skip the listen check afterward")
        p.add_argument("--verify-seconds", type=float, default=6.0)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            return cmd_status(a)
        if a.cmd == "backup":
            return cmd_backup(a)
        if a.cmd == "logs":
            return cmd_logs(a)
        if a.cmd == "check":
            return cmd_check(a)
        return _set_public(a, a.cmd == "enable")
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
