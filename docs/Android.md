# Android

Requires Android 8 or newer. The app runs in the background with a notification and sends traffic to
an EFB on **another device**, such as an iPad. An EFB on the same phone cannot receive from it; see
[Compatibility](Compatibility.md#android).

## Install

1. Download `sentry-bridge.apk` from the [Releases page](https://github.com/Tehcnological/sentry-bridge/releases).
2. Open it on the phone. Android asks once to allow installs from your browser or file manager.

Updates install over the previous version and keep your settings.

## Set up

1. Connect this phone and the EFB device to the Sentry's WiFi (named like `SentryPlus_XXXX`).
2. Open **Sentry Bridge**. Enter the EFB device's IP address, or tap **Find EFB** and then **Use** next
   to your EFB.
3. Tap **Start**.
4. In the EFB, select a **Stratux** or **GDL90** device on UDP port 4000.

**Find EFB** lists EFBs that announce themselves on the WiFi, and other devices on it as "Device". If
yours does not appear, find its IP address in the EFB device's WiFi details.

The EFB device's address can change when it rejoins the WiFi, for example after the Sentry restarts. The
bridge checks the EFB every 30 seconds. If it stops answering and exactly one other device is on the
Sentry's WiFi, the bridge switches to that device by itself and notes it in the log. With several
devices, choose the right one with **Find EFB**.

## Settings

| Setting | What it does |
|---|---|
| Compatibility Mode | Switches the Sentry's Compatibility Mode on or off now |
| Automatic restoration | Switches Compatibility Mode back on after the Sentry restarts. Keep it on |
| Bridge settings → EFB UDP port | The port the bridge sends to on the EFB device (default 4000) |
| Bridge settings → Ownship ICAO | Your aircraft's ICAO address (6 hex digits), so it is not shown as traffic |

Before switching Compatibility Mode on by itself, the app saves a backup of the Sentry's settings. It
keeps the five most recent ones in **Diagnostics → Files**.

## Status messages

| Message | What to do |
|---|---|
| Waiting for Sentry | Connect to the Sentry's WiFi and check the Sentry is on |
| No receiver data | Check the Sentry is on and in range |
| Compatibility Mode is off | Turn on **Compatibility Mode** in Settings |
| Power Savings is on | Turn it off in the Sentry's web settings (**Settings → Open Sentry web settings**) |
| Port 4000 is already in use | Another app on this phone, often an EFB, is using the port. Close it and press Start again |

## Battery

The app keeps the phone's WiFi awake while running, so the screen can turn off. Some phones stop
background apps aggressively; if the bridge stops by itself, exclude Sentry Bridge from battery
optimization in Android's settings.
