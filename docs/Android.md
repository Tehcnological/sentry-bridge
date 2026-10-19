# Android

Requires Android 8 or newer. The app runs the bridge in the background with a notification, next to
your EFB. The current 0.2.1 source uses shared **Bridge**, **Settings** and **Diagnostics** screens;
see [GUI controls](GUI-Controls.md) for every control and [Building from source](Building.md) for Android
tool limits.

## Install
1. Download `sentry-bridge.apk` from the latest [release](https://github.com/Tehcnological/sentry-bridge/releases).
2. Open it on the tablet. Android asks once to allow installs from your browser or files app.

Updates install over the previous version and keep your settings.

## Run it with an EFB on the same tablet
1. Join the Sentry's WiFi (named like `SentryPlus_XXXX`).
2. Open **Sentry Bridge**. Under **Destination**, choose **This device** and tap **Start**.
3. In your EFB, choose a Stratux or GDL90 device on UDP 4000.

The Sentry sends to port 4000 on the tablet. The bridge binds only the tablet's Sentry WiFi address
on 4000, so the EFB keeps its own 4000 listener: the Sentry's data reaches the bridge and the
bridge's output reaches the EFB, including GPS, heartbeat and traffic. One EFB at a time receives
it. Android lets two apps share a port only when both allow it; if the EFB does not, the bridge
reports that the port is in use. Then set **EFB UDP port** in Settings (for example 4001) and the
same port in the EFB. The bridge attempts to rebind a quiet shared input after WiFi address changes.
Physical-device reconnect behavior remains a hardware check; if data does not resume, Stop/Start
and confirm both devices are still on the receiver network.

## Run it on a spare phone for other devices
Choose **Other device** and enter the EFB device's IPv4 address on the Sentry's WiFi. The bridge
sends to that address on the EFB UDP port (4000 by default) and does not broadcast. An EFB that
only announces itself does not receive traffic until you tap **Use** and start with that address.

**Find EFB** lists EFBs that announce themselves on the Sentry's WiFi for a few seconds; tap
**Use** to fill in an address. EFBs that do not announce do not appear; enter their address from
the EFB device's WiFi details. The address can change when the device rejoins the WiFi; if
traffic stops after a reconnect, check it and update it here.

## Settings
| Setting | Meaning |
|---|---|
| Compatibility Mode | Turns the Sentry's Compatibility Mode on or off now, for example after someone pressed Disable in ForeFlight |
| Automatic restoration | Recommended. Turns Compatibility Mode back on after the Sentry powers up, since it resets to ForeFlight mode every time |
| Bridge settings → EFB UDP port | Where the bridge sends (default 4000). On this device, the receiver port shares it with the EFB |
| Bridge settings → Ownship ICAO | Hides your own aircraft from the traffic picture (6 hex digits) |

Automatic restoration saves a settings backup first and keeps the five most recent automatic
backups in **Diagnostics → Files**.

## Status messages
| Message | What to do |
|---|---|
| Waiting for Sentry | Join the Sentry's WiFi and check the Sentry is on |
| No receiver data | Check the Sentry is on and in range |
| Compatibility Mode is off | Turn on **Compatibility Mode** in Settings |
| Power Savings is on | Turn it off in Sentry's own web settings; ForeFlight is optional (see [Troubleshooting](Troubleshooting.md)) |
| Port 4000 is already in use | Change your EFB's GDL90 port to the one shown for it, or close it, then Start again |

## Battery and background
The service holds a wake lock and a WiFi lock while running, so the screen can turn off. Some
Android skins stop background apps aggressively; if the bridge stops on its own, exclude Sentry
Bridge from battery optimization in Android's settings.
