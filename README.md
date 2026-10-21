# Sentry Bridge

[![Latest release](https://img.shields.io/github/v/release/Tehcnological/sentry-bridge?label=release)](https://github.com/Tehcnological/sentry-bridge/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Tehcnological/sentry-bridge/total)](https://github.com/Tehcnological/sentry-bridge/releases)
[![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Android%20%7C%20iOS-informational)](#download)
[![License](https://img.shields.io/github/license/Tehcnological/sentry-bridge)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/Tehcnological/sentry-bridge)](https://github.com/Tehcnological/sentry-bridge/commits/master)
[![Open issues](https://img.shields.io/github/issues/Tehcnological/sentry-bridge)](https://github.com/Tehcnological/sentry-bridge/issues)

Sentry Bridge lets you see traffic from a uAvionix **Sentry** or **Sentry Plus** ADS-B receiver in
EFB apps other than ForeFlight, such as Garmin Pilot, FltPlan Go or AvareX.

Normally the Sentry talks only to ForeFlight, over an encrypted link. In its **Compatibility Mode**
it also sends GPS in the standard GDL90 format that other apps understand, but it still sends
traffic in a raw form those apps cannot use. Sentry Bridge reads that raw traffic, decodes it, and
sends it on to your EFB as standard GDL90 traffic reports.

```
Sentry (Compatibility Mode)  ──WiFi──▶  Sentry Bridge  ──WiFi──▶  your EFB app
```

## Download

Get the latest version from the [Releases page](https://github.com/Tehcnological/sentry-bridge/releases).

| Platform | What to download | Guide |
|---|---|---|
| Android 8+ | `sentry-bridge.apk` | [Android](docs/Android.md) |
| Windows 10/11 | `sentry-bridge-gui.exe` | [Windows](docs/Windows.md) |
| iPhone, iPad (iOS 26+) | `sentry-bridge.ipa`, installed by sideloading | [iPhone and iPad](docs/iOS-Sideload.md) |
| macOS, Linux | no download; unmaintained ([open an issue](https://github.com/Tehcnological/sentry-bridge/issues) if you would use it) | |
| Small WiFi boards (ESP32-C3, Pico W, Pico 2 W) | build from source; experimental | [Small-board firmware](docs/Firmware.md) |

On iPhone or iPad, add the Sentry Bridge source to your sideloading app to install it and get updates:

<p align="center">
  <a href="https://tehcnological.github.io/sentry-bridge/add-source.html?app=sidestore"><img alt="Add to SideStore" src="https://img.shields.io/badge/Add_to-SideStore-7C3AED?style=for-the-badge"></a>
  <a href="https://tehcnological.github.io/sentry-bridge/add-source.html?app=altstore"><img alt="Add to AltStore" src="https://img.shields.io/badge/Add_to-AltStore-018084?style=for-the-badge"></a>
  <a href="https://tehcnological.github.io/sentry-bridge/add-source.html?app=feather"><img alt="Add to Feather" src="https://img.shields.io/badge/Add_to-Feather-E8590C?style=for-the-badge"></a>
</p>

## Quick start

1. Connect the device running Sentry Bridge to the Sentry's WiFi (named like `SentryPlus_XXXX`).
2. Open Sentry Bridge and choose where your EFB runs. Sentry Bridge feeds **one EFB at a time**:
   - **This device**: the EFB is on the same phone, tablet or computer (not available on Android).
     Only one EFB app on it receives the traffic; close the others.
   - **Other device**: the EFB is on one other device on the Sentry's WiFi. Enter its IP address,
     or tap **Find EFB** to look it up.

   Not both, and not several devices.
3. Press **Start**.
4. In your EFB, select a **Stratux** or **GDL90** device as the traffic source (UDP port 4000).
5. Once, in **Settings → Open Sentry web settings**, turn off the Sentry's **Power Savings**.
   Otherwise the Sentry switches itself off after 30 minutes without ForeFlight connected.

The Sentry returns to ForeFlight mode every time it powers up. With **Automatic restoration** on
(the default), Sentry Bridge switches it back to Compatibility Mode by itself.

## What to expect

- Traffic and GPS reach your EFB, and weather data is passed on (weather display in EFBs is not yet
  confirmed). The Sentry's attitude (AHRS), carbon monoxide, temperature, barometric and battery
  data do not reach the EFB, because Compatibility Mode does not include them.
- Sentry Bridge shows the Sentry's **battery, CO level and temperature** on its own screen and warns
  when CO reaches the Sentry's alarm level (at 50 ppm on iPhone and iPad).
- Testing so far covers the Sentry Plus with ForeFlight, Garmin Pilot, AvareX and FltPlan Go on an
  iPad, and the Windows app with a Sentry Plus. Details and known limits are in
  [Compatibility](docs/Compatibility.md).

**Safety:** Sentry Bridge gives advisory information only. It is not certified for navigation or
collision avoidance and does not replace see-and-avoid, required equipment or a weather briefing.

## Roadmap

No dates, and plans can change. To ask for something or to help,
[open an issue](https://github.com/Tehcnological/sentry-bridge/issues).

- **Several EFBs at once.** Today the app sends to one EFB: on the same device, or on one other
  device. Next is feeding several at the same time, such as the EFB on this tablet plus one or two
  other tablets, on Windows, Android, iPhone and iPad.
- **Small WiFi boards.** Firmware for the ESP32-C3, Pico W and Pico 2 W is in the source but not yet
  tested with a Sentry. Once it is, it becomes a release download. A board with a screen and a case,
  the LilyGO T-Dongle, is the next target.
- **CO alarm level on iPhone and iPad.** Use the Sentry's own alarm level instead of a fixed 50 ppm.

## More

- [Documentation index](docs/Home.md)
- [Privacy](docs/Privacy.md): traffic is processed on your device; nothing is sent to the project.
- [Building from source](docs/Building.md)

## License

Sentry Bridge is **GPL-3.0-only**, with an additional permission for app stores; see
[LICENSE](LICENSE) and [NOTICE](NOTICE.txt). Third-party components keep their own licenses; see
[Credits and legal](docs/Credits-and-Legal.md).

Not affiliated with or endorsed by uAvionix, ForeFlight or any EFB vendor; product names belong to
their owners. Sentry Bridge uses the Sentry's own Compatibility Mode. It does not decrypt the
ForeFlight link or change the Sentry's firmware.
