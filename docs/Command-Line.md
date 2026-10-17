# Command-line tools

## sentry-bridge / bridge.py

`sentry-bridge` (native, built from `cli/`) and `bridge.py` (Python) take the same options and
behave identically; the C build is checked against the Python one by the test suite.

```
sentry-bridge [--listen-port N] [--sentry-ip IP] [--dest HOST:PORT ...] [--relay]
              [--ownship-icao HEX] [--ref LAT,LON] [--stale S] [--interval S]
              [--stats-every S] [--announce-port N] [--auto-enable] [--http-port N]
```

| Option | Default | Meaning |
|---|---|---|
| `--listen-port N` | 4000 | UDP port the Sentry sends to |
| `--sentry-ip IP` | 192.168.4.1 | Only accept data from this address (`''` = any) |
| `--dest HOST:PORT` | required | Unicast address to send GDL90 to; repeat for several destinations. Broadcast is rejected |
| `--relay` | off | Also forward the Sentry's standard frames (GPS, heartbeat), for an EFB on another port |
| `--ownship-icao HEX` | none | Your aircraft's ICAO address, removed from traffic |
| `--ref LAT,LON` | none | Position reference to use while the Sentry has no GPS fix |
| `--stale S` | 15 | Stop sending a target S seconds after its last position |
| `--interval S` | 1 | Traffic report interval |
| `--stats-every S` | 10 | Status line interval |
| `--announce-port N` | 63093 | Note EFB announcements (at most 32). They are not destinations. Pass `--dest` to send (0 = off) |
| `--auto-enable` | off | Turn Compatibility Mode back on when the Sentry powers up in ForeFlight mode |
| `--http-port N` | 80 | The Sentry's web port |

`bridge.py` also has `--log-raw FILE` to record every received datagram.

Typical setups:

```bash
# One EFB on another device on the Sentry WiFi
sentry-bridge --dest 192.168.4.20:4000 --auto-enable

# EFB on the same machine, sharing port 4000 with the bridge
sentry-bridge --relay --dest 127.0.0.1:4000 --auto-enable

# EFB on the same machine, listening on 4001 instead
sentry-bridge --relay --dest 127.0.0.1:4001 --auto-enable
```

Sharing binds only the address that reaches the Sentry (`--sentry-ip`). The bridge attempts to
rebind a quiet shared input after a route/address change. Host checks are not device validation;
if data does not resume, restart and check the receiver/EFB network addresses. On Linux the EFB must allow port sharing
(`SO_REUSEADDR`); otherwise the bridge reports it and the EFB needs another port.

If your own aircraft appears as a target, the bridge logs a hint with its ICAO address; restart with
`--ownship-icao`.

The status line, every 10 s:

```
mode=PUBLIC dgrams=926 frames[...] modes=3153 other=0 uat=18(fixed 14, bad 32) targets=16 positioned=13 (uat 2) sent=2200 ...
```

`mode` is `PUBLIC` (Compatibility Mode), `ENCRYPTED` (ForeFlight mode, nothing to bridge) or `?`.

## sentry_ctl.py

Talks to the Sentry's web interface. Run it on a machine joined to the Sentry's WiFi.

| Command | What it does |
|---|---|
| `status` | Device status: firmware versions, SSID, client count |
| `backup` | Save the status and settings JSON to `backups/` |
| `check` | Listen for 8 s and report ENCRYPTED or PUBLIC |
| `enable` | Back up, turn Compatibility Mode on, verify |
| `disable` | Turn it off (back to ForeFlight mode); a power cycle does the same |
| `logs [--get all]` | List, and optionally download, the Sentry's stored telemetry logs (read-only) |

## capture.py

Records and analyzes the Sentry's UDP stream. Useful for reporting a new firmware version.

```bash
python3 capture.py record -o capture.jsonl --seconds 600
python3 capture.py analyze capture.jsonl --report report.txt   # also reads pcap and pcapng
```

The report lists every message type, its rate, decoded traffic, and which bytes change in frames
that are not yet understood. Its "Last ownship" line leaves out your position, address and
callsign unless you add `--show-ownship`. Review a report before sharing it, and do not share raw
`.jsonl` or pcap captures publicly: they contain your position.

## tools/check_weather.py

Compare receiver and bridge-output recordings for complete FIS-B frame forwarding:

```bash
python3 tools/check_weather.py receiver.jsonl efb.jsonl --report dist/weather-check.json
python3 tools/check_weather.py session.pcapng session.pcapng --input-port 4000 --output-port 4001
```

The captures must cover the same interval and contain the intended receiving ports. Exit 0
means matching frames, 1 means a mismatch or invalid frame, and 2 means no input weather or a
comparison error. The counts-only report does not establish weather display in an EFB. See
[Compatibility](Compatibility.md) for what has been verified on hardware.

## ws_probe.py

Records the Sentry's WebSocket status stream (the data behind its status web page) and lists every
field it carries. Read-only.

```bash
python3 ws_probe.py --seconds 120
```

## sim.py

A simulated Sentry in Compatibility Mode, for testing without hardware:

```bash
python3 bridge.py --sentry-ip 127.0.0.1 --dest 127.0.0.1:4100 &
python3 sim.py --dest 127.0.0.1:4000          # add --uat for UAT targets with byte errors
```

`--fisb` adds a FIS-B uplink (`0x07`) each second, filled with placeholder data, to test relaying.

## License texts

`sentry-bridge --license` and `sentry-ctl --license` print their distribution notices
and license text without contacting a receiver. The GUI provides offline Privacy
and Licenses pages in Settings.
