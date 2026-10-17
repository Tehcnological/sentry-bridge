# Windows

Use Windows 10/11. Sentry Bridge for Windows is a graphical app, a native console
bridge and a packaged receiver control tool.

## Graphical app

Download `sentry-bridge-gui.exe` from the project's release page when the matching
release is available, or [build from source](Building.md). Join Sentry WiFi,
open the app and choose This device or Other device. Enter the other EFB device's
IPv4 address, or use Find EFB. Desktop EFB targets are Custom GDL90. Press Start
after reading [Compatibility](Compatibility.md).

Configure the EFB's Stratux/GDL90 input on UDP 4000. For This device, port sharing
is used; alternate ports can be set in both apps. Other device uses unicast to
the entered address. An announcement does not add a destination.

Settings contains receiver controls and the link to Sentry's own settings. Disable
Power Savings there; ForeFlight is optional. Diagnostics provides captures, backups,
logs, analysis and a loopback simulator. [Control reference](GUI-Controls.md).

The dedicated Edge app window stops the controller when its last app window closes.
If the default browser fallback is used, closing its tab leaves the controller
running. Stop ends forwarding; Settings → Exit Sentry Bridge ends the controller.

## Network access and files

Allow inbound Sentry UDP on the network profile actually used by Sentry WiFi.
Windows may classify it as Public. Do not disable the firewall or enable access
on unrelated networks. SmartScreen may warn about an unrecognized executable;
verify its release origin and published hashes before deciding whether to run it.

App data is under `%LOCALAPPDATA%/SentryBridge`. Captures contain location data;
see [Privacy](Privacy.md). Optional daily GitHub update checks default on and can
be disabled in Settings; they do not install updates.

## Console tools

`sentry-bridge.exe` runs without Python. It requires `--dest` and does not
broadcast. Double-clicking it only shows how to start it; use `sentry-bridge-gui.exe`
for the app.
For example:

```
sentry-bridge.exe --relay --dest 127.0.0.1:4000 --auto-enable
```

`sentry-ctl.exe` provides receiver status, mode changes, backups and logs. The GUI
provides those end-user functions too. See [Command line](Command-Line.md).
Neither console operation nor host tests establish EFB display or aircraft suitability.
