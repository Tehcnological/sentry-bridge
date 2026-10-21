# Command-line tools

Most people should use the app. These tools are for running the bridge without a window, scripting,
and investigating the Sentry. The Python tools need the packages in `requirements.txt`.

## sentry-bridge / bridge.py

The bridge without a window. `sentry-bridge` is the native build (Windows release, or built from
`cli/`); `bridge.py` is the Python version. Both take the same options and produce the same output.

```
sentry-bridge [--listen-port N] [--sentry-ip IP] [--dest HOST:PORT ...] [--relay]
              [--ownship-icao HEX] [--ref LAT,LON] [--stale S] [--interval S]
              [--stats-every S] [--announce-port N] [--auto-enable] [--http-port N]
```

| Option | Default | Meaning |
|---|---|---|
| `--dest HOST:PORT` | required | Where to send GDL90 (an EFB's address and port). Repeat for several EFBs. Broadcast addresses are refused |
| `--auto-enable` | off | Switch Compatibility Mode back on when the Sentry powers up in ForeFlight mode |
| `--relay` | off | Also pass on the Sentry's own GPS and heartbeat frames, for an EFB on another port |
| `--ownship-icao HEX` | none | Your aircraft's ICAO address, so it is not shown as traffic |
| `--listen-port N` | 4000 | UDP port the Sentry sends to |
| `--sentry-ip IP` | 192.168.4.1 | Accept data only from this address (`''` accepts any) |
| `--http-port N` | 80 | The Sentry's web port |
| `--ref LAT,LON` | none | A position to use while the Sentry has no GPS fix |
| `--stale S` | 15 | Drop an aircraft S seconds after its last position |
| `--interval S` | 1 | Seconds between traffic reports |
| `--stats-every S` | 10 | Seconds between status lines |
| `--announce-port N` | 63093 | Listen for EFB announcements, for information only (0 = off) |

`bridge.py` also has `--log-raw FILE` to record every received datagram.

Examples:

```bash
# EFB on another device on the Sentry's WiFi
sentry-bridge --dest 192.168.4.20:4000 --auto-enable

# EFB on the same computer, sharing port 4000
sentry-bridge --relay --dest 127.0.0.1:4000 --auto-enable

# EFB on the same computer, listening on port 4001 instead
sentry-bridge --relay --dest 127.0.0.1:4001 --auto-enable
```

On Linux, sharing port 4000 works only if the EFB allows it; otherwise the bridge says so and the EFB
needs another port. If your own aircraft shows up as traffic, the bridge logs its ICAO address; restart
with `--ownship-icao`.

Every 10 seconds the bridge prints a status line such as:

```
mode=PUBLIC dgrams=926 ... targets=16 positioned=13 (uat 2) sent=2200 ...
```

`mode` is `PUBLIC` (Compatibility Mode), `ENCRYPTED` (ForeFlight mode: nothing to bridge) or `?`.

## sentry_ctl.py (sentry-ctl.exe)

Reads and changes the Sentry's settings. Run it on a computer connected to the Sentry's WiFi.

| Command | What it does |
|---|---|
| `status` | Firmware versions, WiFi name, connected clients |
| `backup` | Save the Sentry's status and settings to `backups/` |
| `check` | Listen for 8 seconds and report the mode: `ENCRYPTED` or `PUBLIC` |
| `enable` | Back up, switch Compatibility Mode on, and confirm |
| `disable` | Switch it off (back to ForeFlight mode); a power cycle does the same |
| `logs [--get all]` | List, and optionally download, the Sentry's stored logs |

## capture.py

Records and analyzes the Sentry's data, for example to report a new firmware version.

```bash
python3 capture.py record -o capture.jsonl --seconds 600
python3 capture.py analyze capture.jsonl --report report.txt   # also reads pcap and pcapng
```

The report lists every message type, its rate, the decoded traffic, and which bytes change in
messages that are not understood yet. It leaves out your own position, address and callsign unless you
add `--show-ownship`. Do not share raw recordings publicly: they contain your position.

## ws_probe.py

Records one of the Sentry's live data feeds and lists every field it carries. Read-only.

```bash
python3 ws_probe.py --seconds 120                 # the status feed behind the Sentry's web page
python3 ws_probe.py --path /stats --seconds 30    # battery, CO, temperature and other statistics
```

## tools/check_weather.py

Checks that weather frames reached the EFB unchanged, by comparing a recording of the Sentry's data
with a recording of the bridge's output:

```bash
python3 tools/check_weather.py receiver.jsonl efb.jsonl --report dist/weather-check.json
```

Exit code 0 means the frames match, 1 a mismatch, 2 no weather to compare or an error. It checks the data, not how
an EFB displays it.

## sim.py

A simulated Sentry in Compatibility Mode, for testing without hardware:

```bash
python3 bridge.py --sentry-ip 127.0.0.1 --dest 127.0.0.1:4100 &
python3 sim.py --dest 127.0.0.1:4000          # --uat adds UAT traffic, --fisb a weather frame
```

## License texts

`sentry-bridge --license` and `sentry-ctl --license` print the license texts. The app shows them in
**Settings → Licenses**.
