# Compatibility and limitations

Sentry Bridge passes on what the Sentry sends in Compatibility Mode, as standard GDL90. It cannot add
data the Sentry does not send in that mode, and it does not change the Sentry's firmware. Read the
section for your platform before relying on it in flight.

## All platforms

- **EFBs receive traffic and GPS.** Weather data is passed on, but weather display in EFBs has not
  been confirmed yet.
- **No AHRS, CO, temperature, barometric or battery data in the EFB.** Compatibility Mode does not
  include them, for any app, ForeFlight included. Sentry Bridge reads battery, CO and temperature
  separately and shows them on its own screen, with a warning at the Sentry's CO alarm level (at
  50 ppm on iPhone and iPad).
- **Turn off Power Savings** in the Sentry's web settings. It switches the Sentry off after 30 minutes
  without ForeFlight connected. ForeFlight is not required.
- **Compatibility Mode resets at every power-up.** Keep Automatic restoration on so the bridge switches
  it back.

**Tested so far:** a Sentry Plus (WiFi firmware 1.0.32) with ForeFlight, Garmin Pilot, AvareX and
FltPlan Go on an iPad, the Windows app with a Sentry Plus, including recovery after the Sentry
restarts, and the Android app sending to an iPad, including following it to a new address after the
Sentry restarts. Not yet tested: other EFB versions and other devices.

## Android

- **The EFB must be on another device.** The Sentry always sends to UDP port 4000. Android lets two apps
  share a port only if both allow it, and the Android EFBs tested (FltPlan Go, Garmin Pilot) do not. So
  an EFB on the same phone cannot receive from the bridge. Use the Android app to feed an iPad or a
  second tablet.
- Garmin Pilot for Android does not accept GDL90 input at all. On Android, use an EFB with GDL90 input,
  such as FltPlan Go.
- Keep the bridge's notification visible; battery savers that stop background apps also stop the bridge.
- If the EFB device rejoins the WiFi with a new address, the bridge switches to it when it is the only
  other device on the WiFi. Otherwise the bridge log says so; choose the EFB with **Find EFB**.

## Windows

- Enter the EFB's address and port (Custom GDL90).
- Allow Sentry Bridge through the firewall on the Sentry's WiFi network. Windows usually treats that
  network as Public.
- An EFB on the same computer shares UDP port 4000 with the bridge.

## macOS and Linux

Unmaintained: there is no download, and the desktop app is not tested there. It may still run from
source. If you would use it on macOS or Linux, [open an issue](https://github.com/Tehcnological/sentry-bridge/issues).

## iPhone and iPad

- Not in the App Store: download `sentry-bridge.ipa` from the release and sideload it; see
  [Install on iPhone or iPad](iOS-Sideload.md). Requires iOS or iPadOS 26.
- **Bridge Session** (on by default) keeps the bridge running in the background for up to 4 hours; iOS
  can end it sooner. With it off in Settings, the bridge runs only while the app is open.
- Allow **Local Network** access when asked; without it the bridge cannot reach the Sentry or the EFB.
- An EFB on the same iPad works: select a Stratux or GDL90 device in it. Only one EFB at a time receives
  the traffic, so close other EFBs.
- For an EFB on another device, **Find EFB** lists EFBs that announce themselves and other devices on the
  Sentry's WiFi as "Device". If that EFB stops answering (it rejoined the WiFi with a new address) and
  exactly one other device is on the WiFi, the bridge switches to it and saves the new address.

## Small boards (ESP32-C3, Pico W, Pico 2 W)

- The board feeds every EFB on the Sentry's WiFi, including Android EFBs. It builds but has not been
  tested with a receiver yet. See [Small-board firmware](Firmware.md).
