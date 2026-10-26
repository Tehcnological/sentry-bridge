# Compatibility and limitations

Sentry Bridge sends the Sentry's traffic and GPS to your EFB in a format it can read (GDL90).
It uses the Sentry's Compatibility Mode and cannot provide data that mode leaves out. Read the
section for your device to understand what works and what still needs testing.

## All platforms

- **One EFB at a time.** Sentry Bridge sends to one destination: an EFB on the same device, or one
  other device on the Sentry's WiFi. It cannot feed this device and another device together, or
  several devices. With **This device**, only one EFB app on that device receives the traffic.
- **EFBs receive traffic and GPS.** Weather data is passed on, but weather display in EFBs has not
  been confirmed yet.
- **No attitude (AHRS), CO, temperature, air-pressure or battery readings in the EFB.** Compatibility Mode does not
  include them, for any app, ForeFlight included. Sentry Bridge reads battery, CO and temperature
  separately and shows them on its own screen, with a warning at the Sentry's CO alarm level (at
  50 ppm on iPhone and iPad).
- **Turn off Power Savings** in the Sentry's web settings. It switches the Sentry off after 30 minutes
  without ForeFlight connected. ForeFlight is not required.
- **Compatibility Mode resets at every power-up.** Keep **Automatic restoration** on
  (**Restore Compatibility Mode** on iPhone and iPad) so the bridge switches it back.

**Tested so far:** a Sentry Plus (WiFi firmware 1.0.32) with ForeFlight, Garmin Pilot, AvareX and
FltPlan Go on an iPad, the Windows app with a Sentry Plus, including recovery after the Sentry
restarts, and the Android app sending to an iPad, including following it to a new address after the
Sentry restarts. Not yet tested: other EFB versions and other devices.

## iPhone and iPad

Requires iOS or iPadOS 26 or later. The app is installed outside the App Store; follow
[Install on iPhone or iPad](iOS-Sideload.md). The choices below depend on the version you have installed.

### Before departure

1. Check that Sentry Bridge still opens. If you use a free Apple ID, refresh the app before its
   seven-day expiry; do this while you have an internet connection.
2. Join the Sentry's WiFi and allow **Local Network** access. Choose **This device** for an EFB on
   this iPad or iPhone, or **Other device** for an EFB on another device on the same WiFi.
3. In the EFB, select **Stratux** or **GDL90**, normally on port **4000**. Close other EFB apps on
   that device. Start the bridge and confirm that the EFB is receiving fresh data.
4. Switch to your EFB and check again. Test screen locking on the ground if you intend to use it.
   Allow bridge notifications in the Focus you use while flying.

### When you switch apps or lock the screen

Newer versions let you choose in **Settings → Background running**, and ask the first time you open
the app after the update. If your version has a **Bridge Session** switch instead, its background
time is limited as explained below.

- **Background audio** is the recommended choice where offered. The app plays silence to help it
  keep running while you use your EFB or lock the screen. It is designed to mix with EFB alerts and
  music. A phone call interrupts the audio; the app tries to restart it when the interruption ends.
- **Background audio + GPS cross-reference** adds a check of the Sentry's position against this
  device's GPS. Location updates also provide another way to keep the bridge running. This uses more
  battery. See the position-check explanation below.
- **Bridge Session**, if offered instead of Background audio, asks iOS for up to the chosen session
  length, at most four hours. iOS can refuse it or end it early. If refused, forwarding continues
  while the app is open. If an accepted session ends without GPS cross-reference running,
  forwarding stops. Open the app and press **Start** again.
- **Bridge Session + GPS cross-reference**, where offered, lets location updates support continued
  forwarding after a session is refused or ends. It needs location permission; it is not a guarantee
  that iOS will keep the app running.
- **Only while the app is open**, where offered, stops forwarding when you leave the app or lock
  the screen.

Background audio has no four-hour timer, but iOS can still interrupt, pause or close the app. Returning to the app does not automatically restart a stopped bridge. Check its
status and press **Start** when needed. Background running and recovery through a real phone call
have not yet been verified on hardware for these new options.

### What GPS cross-reference does

This optional check warns on the bridge's main screen if the Sentry has no usable GPS fix, or if
its position stays more than about **half a nautical mile** from this device's position for
**30 seconds**. The allowance increases slightly with the device's reported GPS uncertainty.
A poor or stale device position is not compared. Agreement must return for ten seconds before
an existing disagreement warning clears.

**Your EFB still receives the Sentry's position.** The iPhone or iPad position is only used for
this check: it is not sent to the EFB, saved as a track, or used to change your aircraft's position
or traffic reports. The check does not correct a bad Sentry position and has not been tested
against a real receiver in flight. Its warning appears in Sentry Bridge, not in your EFB.

Choose **Always** in the app's location permissions for background use. If the app says
**GPS cross-reference needs location set to Always**, tap **Open Settings** and change the permission.
Choosing the option alone does not grant permission. If the bridge is still running, the check starts
when access is granted; if it stopped while you were in Settings, press **Start** again. iOS shows a
location indicator while it is in use.
An iPad without its own usable GPS position may leave the check waiting for a device position.

### Notifications and lost data

Allow notifications so the app can warn you if it stops running. The warning is intended to appear
about **30 seconds** after forwarding updates stop. Pressing **Stop** cancels it. A warning cannot
confirm whether your EFB received data, and delivery after iOS pauses or closes the app has not yet
been verified on hardware.

A Focus or notification setting can delay or hide a warning. Time Sensitive delivery is requested
where the installation allows it, but is not available with a free Apple ID. Allow Sentry Bridge
explicitly in your flying Focus; do not depend on a warning always sounding. If you see a stopped
bridge warning or stale data in the EFB, open Sentry Bridge, check the WiFi and press **Start** if stopped.

### An EFB on another device

**Find EFB** can show both named EFBs and entries labelled **Device**. A Device entry only means
something answered on the WiFi; it does not prove that an EFB is running there. Confirm the selected
address belongs to your EFB device.

The bridge can follow a device whose address changes after it rejoins the Sentry's WiFi. It first
needs to have received a reply from the selected device. After two missed checks, it searches again
and switches only when there is one other answering device. That device might not be your EFB.
With several devices, or if the EFB never answers, use **Find EFB** or enter its WiFi address yourself.
Check reception in the EFB after a reconnection.

### What still needs a real-device check

The new background options have been built and tested on computers and simulators; the Sideload
app has been installed on an iPad. That does not yet establish phone-call recovery, warning delivery
when iOS stops the app, GPS cross-reference with a real Sentry, battery use over a flight, or
installation and updating of the new release through SideStore. Time Sensitive warnings with a
Focus also remain untested on hardware.

## Android

- **The EFB must be on another device.** The Android bridge and the EFBs tested cannot share the
  Sentry connection on one phone. Use the Android app to feed an iPad or a second tablet.
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

## Small boards (ESP32-C3, Pico W, Pico 2 W)

- The board feeds every EFB on the Sentry's WiFi, including Android EFBs. It builds but has not been
  tested with a receiver yet. See [Small-board firmware](Firmware.md).
