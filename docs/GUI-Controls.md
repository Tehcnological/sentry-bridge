# GUI controls

The local adapter uses the existing shared web interface and C decoder when `sbcore.dll`,
`libsbcore.so` or `libsbcore.dylib` is available. Without the library it uses the existing Python
reference decoder. It does not build anything at startup. No GitHub Actions minutes are used.

On this Windows checkout, double-click **Run-Gui.cmd**. It opens the interface and reuses a
running instance. The script's execution-policy exception applies only to its own process.
On Linux/macOS, run `python tools/gui_server.py --open` in the configured project environment.
The Android APK includes these controls. The Windows build packages them into a
windowed executable. See [Building from source](Building.md).

The live adapter runs at `http://127.0.0.1:8766/`; open it through **Run-Gui.cmd** or the address
`gui_server.py` prints, which carries a one-time launch code (valid once, for a minute). The
independent offline layout preview remains
at port 8765. The live adapter hides synthetic status selectors. Closing a browser tab leaves
the bridge running. **Stop** ends forwarding; **Settings → Exit Sentry Bridge** closes the
adapter and its packet capture. Restart the adapter to reconnect after Exit.

**Check for updates** (Receiver settings) is the only connection beyond the receiver's WiFi: at most
once a day, while the bridge is stopped, it asks GitHub for the latest release and shows **Update
available** with a link. It never downloads or installs anything. It is on by default on Windows,
off on Android, and not offered when F-Droid installed the app. Turn it off to keep the app
free of automatic update requests. Opening external documentation or release links still uses
the browser and contacts their hosts. See [Privacy](Privacy.md).

## Function coverage

| Existing function | GUI location |
|---|---|
| Start/stop forwarding, destinations and selected EFB ports | Bridge |
| EFB discovery: list announcing EFBs and fill in an Other device address | Bridge → Find EFB |
| `sentry-ctl enable` / `disable` | Settings → Receiver → Compatibility Mode |
| Automatic restoration after a power cycle | Settings → Receiver → Automatic restoration; saved immediately |
| Backup-before-enable, optional packet verification and duration | Settings → Mode verification |
| Receiver address, UDP/HTTP ports, relay, ownship filtering, fallback position, target expiry, report/log interval and discovery port | Settings → Bridge settings → Save settings |
| `sentry-ctl status` and settings readback | Settings → Read receiver; Diagnostics → Receiver tools → Read status |
| `sentry-ctl backup` | Diagnostics → Receiver tools → Backup settings; downloadable ZIP |
| `sentry-ctl check` | Diagnostics → Receiver tools → Check mode and duration |
| `sentry-ctl logs`, one/all downloads and size cap | Diagnostics → Receiver tools |
| `capture record` / bridge raw recording | Diagnostics → Packet capture → Record packets / Stop recording |
| `capture analyze`, JSONL/pcap/pcapng import, input port and hex samples | Diagnostics → Packet capture |
| FIS-B comparison, source/output recordings and ports | Diagnostics → Weather comparison |
| WebSocket probe, path and duration | Diagnostics → WebSocket diagnostics |
| Simulator, GPS/no-fix, UAT and weather options | Diagnostics → Simulator |
| Download/delete saved captures, reports, backups and logs | Diagnostics → Files |
| Offline privacy policy and license notices | Settings → Privacy / Licenses |
| Bridge log and job result | Diagnostics → Bridge log / Last result |

The simulator includes a mock receiver HTTP service and real loopback UDP traffic. Enable it
while stopped; Custom GDL90 on this device is selected automatically. Its output is restricted
to loopback. It never saves simulated receiver ports over the real receiver configuration.
It exercises transport and control, not EFB display or actual Sentry hardware.
Turn off the simulator before saving receiver settings; simulated connection values are temporary.

Compatibility Mode is backed up before enabling by default. The receiver does not expose
`publicGDL90` in settings JSON. The switch uses observed packets, marks stopped observations
as last checked, and labels unverified requests explicitly. Read receiver includes a packet
check. Plain-text update acknowledgements and gzip settings responses are supported.
Mode checks and recordings reuse the forwarding socket
while running, avoiding split packets between competing listeners. A standalone check or
capture owns the port exclusively. Advanced settings require stopping first.
Automatic recovery attempts a backup, but a full file list or storage failure never prevents
the mode-change request. It keeps the five most recent automatic backups
(`sentry-auto-backup.zip`); manual backups are never removed automatically. Manual backup-before-enable still requires a successful backup.

HTTP work and analysis run off the UDP thread. The interface polls once per second while
visible, slows while hidden, and bounds displayed logs. Downloads stream in chunks. Captures
and uploads are capped at 32 MiB; receiver logs at 20 MiB each, 20 files per download. The Files
list survives adapter restarts and has a 100-file limit. Uploaded files retain a sanitized name.
The source adapter stores generated/private files in ignored `.local-tools/gui-data/`;
packaged Windows uses `%LOCALAPPDATA%/SentryBridge` and Android uses app-private storage;
only explicit download
tokens can fetch them. The HTTP service serves a fixed asset allowlist and rejects external
Host/Origin values, redirects from receiver settings endpoints and arbitrary filesystem paths.
Every request needs the per-launch session token or its HttpOnly cookie; requests without it are
refused. The browser is opened with a one-time launch code, which is exchanged for the cookie, so
the token never appears on a browser command line (other accounts can read those on Linux). The
token is saved owner-only in the user's data folder (0700 on Linux and macOS), so a second launch
can ask the running instance for a new launch URL. It does not protect against programs running as
the same user.
The web assets, including offline privacy and full license texts, remain below the
100 KiB compressed target, with no external fonts or UI libraries. Legal text is
loaded only when opened. Asset size is not a benchmark on an older device.

## Platform limitations and remaining adapters

Power Savings can be changed in Sentry's native web settings; ForeFlight is optional. Settings
includes **Open Sentry web settings**, using the configured receiver IP and HTTP port. The bridge
shows the value it reads. A direct bridge write operation still needs hardware verification.
Factory reset, calibration,
firmware installation and arbitrary settings restoration are not existing bridge functions
and are not introduced through unverified receiver requests.

The local adapter can run on Windows, Linux and macOS with Python installed. It binds
only to loopback. Android includes native Settings/Diagnostics controls. Apple work proceeds separately. Hardware validation and low-end
device measurements remain required before a GUI release. ESP32 cannot run this Python server.

The GUI requirement applies to every platform: the table is the parity checklist for their
native adapters. A CLI fallback or an unconnected toggle does not satisfy it. Build, signing,
release and fuzz tools are developer operations, not cockpit controls.

The Bridge tab has one GDL90 route, with no per-EFB profiles: **This device** (127.0.0.1) or
**Other device** (its IPv4 address, or **Find EFB**), both on **EFB UDP port** from Settings
(default 4000). EFBs take a Stratux or GDL90 device. On this device, an EFB port equal to the
receiver port shares that port with the EFB: the bridge binds only the address that reaches the
receiver, and one EFB at a time receives the output. Windows and macOS allow this beside any
EFB; Linux and Android only when the EFB also allows port sharing, otherwise Start reports the
port in use and another EFB port is needed. Receiver tools (captures, mode checks) bind the
same way. If the computer joins the receiver's WiFi after Start, the bridge follows the new route
within 5 s; a shared input that hears nothing for 15 s logs why (the receiver must send to this
address).
