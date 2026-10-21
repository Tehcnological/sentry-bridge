# Controls

The Windows and Android apps share the same three screens: **Bridge**, **Settings** and
**Diagnostics**. The iPhone and iPad app has its own design with the main controls.

## Bridge

| Control | What it does |
|---|---|
| Start / Stop | Starts or stops forwarding traffic to the EFB |
| This device / Other device | Where the one EFB runs: on this device, or on one other device at an IP address you enter. The bridge feeds one EFB at a time. Android offers Other device only |
| Find EFB | Lists EFBs that announce themselves on the WiFi, and the other devices that answer on it ("Device"); **Use** fills in the address |
| Check setup | Checks the chosen address and port before you start |
| Status | Receiver state, GPS, traffic count, weather frames and time since the last packet |
| Battery, CO, Temperature | The Sentry's own readings, while forwarding. CO turns red at the Sentry's alarm level |

## Settings

| Control | What it does |
|---|---|
| Compatibility Mode | Switches the Sentry's Compatibility Mode on or off |
| Automatic restoration | Switches Compatibility Mode back on after the Sentry restarts. Keep it on |
| Open Sentry web settings | Opens the Sentry's own settings page, for example to turn off Power Savings |
| Read receiver | Reads the Sentry's status and settings |
| Mode verification | Whether to back up the Sentry's settings and confirm the change from received data, and for how long |
| Bridge settings | Receiver address and ports, EFB UDP port, Ownship ICAO, relay of the Sentry's GPS frames, a fallback position, how long aircraft stay listed, and report intervals. Stop the bridge to change them |
| Check for updates | Once a day, while stopped, asks GitHub whether a new version exists and shows a link. Never downloads anything. On by default on Windows, off on Android |
| Appearance | Light or dark theme and motion; on Android, the Material style |
| Privacy, Licenses | The privacy policy and license texts, available offline |
| Exit Sentry Bridge | Stops the bridge and closes the app (Windows) |

## Diagnostics

| Tool | What it does |
|---|---|
| Receiver tools | Read the Sentry's status, back up its settings, check which mode it is in, list and download its stored logs |
| Packet capture | Record the Sentry's data to a file, or analyze a recording (also pcap and pcapng files) |
| Weather comparison | Compare weather frames from the Sentry with what reached the EFB |
| WebSocket diagnostics | Record one of the Sentry's live data feeds, for example `/stats` |
| Simulator | A simulated Sentry with traffic, for trying the app without hardware. Its output stays on this device |
| Files | Download or delete saved recordings, reports, backups and logs |
| Bridge log, Last result | What the bridge has been doing, and the result of the last tool |

Recordings and backups can contain positions and aircraft identifiers; see [Privacy](Privacy.md).

## For developers

- **Run from source:** on Windows, double-click `Run-Gui.cmd`. On macOS or Linux (unmaintained and
  untested; [open an issue](https://github.com/Tehcnological/sentry-bridge/issues) if you would use it), run `python tools/gui_server.py --open`. The app is a local web server at `http://127.0.0.1:8766/`. It
  uses the C decoder when the compiled library is present, otherwise the Python one.
- **Access:** the server listens only on this computer. Every request needs a per-launch session
  token, handed to the browser through a one-time launch code. This does not protect against other
  programs running as the same user.
- **Data:** saved files live in `%LOCALAPPDATA%\SentryBridge` (packaged Windows app),
  `.local-tools/gui-data/` (from source) or app storage (Android). Captures are limited to 32 MiB and the
  file list to 100 files.
- **Layout preview:** `python tools/preview_ui.py --port 8765` serves the screens with sample data,
  without a receiver.
