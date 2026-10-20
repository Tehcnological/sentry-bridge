# Compatibility and limitations

Sentry Bridge forwards the Sentry's Compatibility Mode data as standard GDL90. It does not change the
Sentry's firmware and cannot add data the Sentry does not send in that mode. Read the section for your
platform before relying on the bridge in flight. This page will grow as hardware testing covers more
devices and EFBs.

## All platforms

- ForeFlight can still connect directly to Sentry for GPS and traffic in Compatibility Mode. The sensor
  limitations below also apply to ForeFlight.
- AHRS, CO, temperature, barometric sensor and battery data are unavailable to apps in Compatibility Mode.
  Sentry Bridge reads battery, CO and temperature from the Sentry's statistics page while forwarding and
  shows them on its own screen (Windows, macOS, Linux and Android), with a warning at the Sentry's CO alarm
  level. EFBs do not receive them, and attitude is not forwarded. iOS does not show them yet.
- Weather frames are relayed; actual EFB weather display still needs hardware verification.
- Disable Power Savings in Sentry's web settings; ForeFlight is optional. Power Savings shuts Sentry down
  after 30 minutes without ForeFlight connected.
- Compatibility Mode resets on power-up. Keep automatic restoration on; turning the mode off stops bridging.
- Limited hardware testing so far: Sentry Plus, WiFi firmware 1.0.32, with ForeFlight, Garmin Pilot,
  AvareX and FltPlan Go on one iPad. The Android app receives and decodes traffic on a device, but EFB
  output on Android has not been verified yet. Other devices, EFB versions and long background use
  need validation.

## Android

- **Android bridges for an EFB on another device only.** An EFB on the same Android phone cannot
  receive from the bridge: the Sentry always sends to UDP 4000, Android lets two apps share a port only
  if both allow it, and EFBs tested so far (FltPlan Go, Garmin Pilot) do not. The app therefore offers
  no This device destination. iPhone and iPad do not have this limit.
- Garmin Pilot for Android does not accept GDL90 input, from the bridge or from the receiver directly.
  Garmin Pilot works on iPhone and iPad. On Android, use an EFB with GDL90 input such as FltPlan Go,
  or run the bridge for a Garmin Pilot iPad (**Other device**).
- After a WiFi reconnect the EFB device's address can change. If data does not resume, Stop/Start and
  check the receiver and EFB addresses.
- Keep the bridge's notification visible; battery savers that stop background apps also stop the bridge.

## Windows, macOS and Linux

- Desktop EFB targets are Custom GDL90: enter the EFB's known address and port.
- Allow Sentry Bridge through the firewall on the network the Sentry WiFi uses, including Public networks
  on Windows.
- With the EFB on the same computer, the bridge shares UDP 4000 with it. On Linux the EFB must allow port
  sharing; otherwise set another EFB port in Settings and in the EFB.

## Small boards (ESP32-C3, Pico W, Pico 2 W)

- Firmware for these boards bridges for every EFB on the receiver's WiFi, including Android EFBs.
  It builds for all three boards but has not been tested with a receiver yet. See
  [Small-board firmware](Firmware.md).

## iPhone and iPad

- Not in the App Store: install free with your own Apple ID; see [Install on iPhone or iPad](iOS-Sideload.md).
- Requires iOS or iPadOS 26 or later.
- The bridge forwards while the app is visible. The Bridge Session option keeps it running in the
  background for a chosen 60 to 240 minutes; it is experimental and iOS can end it earlier. Tested in
  the background for up to 34 minutes on one iPad.
- Allow Local Network access when asked; without it the bridge cannot reach the Sentry or the EFB.
- On the same device the bridge shares UDP 4000 with the EFB (choose a Stratux or GDL90 device in the
  EFB), and one EFB at a time receives it; close other EFBs. The device must be on the Sentry's WiFi;
  on another network Start says to join it. After a WiFi address change the bridge reattaches by itself.
- For an EFB on another device, enter that device's address (or use Find EFB); it receives on UDP 4000.
