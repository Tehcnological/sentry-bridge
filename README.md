# Sentry Bridge

Sentry Bridge converts traffic from a uAvionix Sentry or Sentry Plus in Compatibility
Mode into GDL90 for compatible EFBs. It decodes 1090ES and 978 MHz UAT traffic and
relays available standard GPS, heartbeat and weather frames.

## Project status

Version **0.2.0**. Android and Windows apps share Bridge, Settings and Diagnostics screens and a C decoder.
Linux/macOS desktop operation is from source. [Firmware for small boards](docs/Firmware.md)
(ESP32-C3, Raspberry Pi Pico W and Pico 2 W) bridges for EFBs on any device, including Android; it
builds but is not yet tested with a receiver.

A native iOS/iPadOS 26+ app installs outside the App Store; see
[iPhone and iPad](docs/iOS-Sideload.md). Its background Bridge Session is experimental.

## Setup

1. Join the Sentry's WiFi and open Sentry Bridge.
2. Choose **This device** for an EFB on the bridge device, or **Other device** and
   enter the EFB device's IPv4 address. **Find EFB** lists apps that announce themselves.
3. In the EFB, choose its Stratux or GDL90 input, normally UDP 4000. Read the
   compatibility dialog, then press **Start**.
4. In **Settings → Open Sentry web settings**, disable **Power Savings**. ForeFlight
   is optional. Leave **Automatic restoration** enabled to request Compatibility
   Mode again after receiver power cycles.

On iPhone, iPad, Windows and macOS the bridge shares UDP 4000 with one EFB on the same
device. On Android, use the bridge for an EFB on another device: Android EFBs do not
share UDP 4000. Linux needs the EFB to allow port sharing, or another EFB port.
For another device, output goes to its configured address. An announcement does not
add a destination. Desktop targets are Custom GDL90. Ports remain editable in Settings.

Use [Android](docs/Android.md) or [Windows](docs/Windows.md) for platform setup and
download/source instructions.
Use **Stop** to end forwarding. Desktop browser fallback can keep the bridge
running after a tab closes; **Settings → Exit Sentry Bridge** ends the controller.

## Compatibility and safety

Hardware coverage is limited: Sentry Plus WiFi firmware 1.0.32 with ForeFlight, Garmin
Pilot, AvareX and FltPlan Go on one iPad. EFB output from the Android app has not been
verified on a device yet. That is not a promise for every version, device or flight. See [Compatibility](docs/Compatibility.md).

- AHRS, CO, temperature, barometric sensor and battery data are unavailable to EFBs
  in Compatibility Mode, including ForeFlight. The bridge does not invent them.
- Weather-frame transport is tested with synthetic inputs; actual EFB weather
  display remains unverified. Receipt counts do not prove a usable weather picture.
- Receiver restarts, WiFi recovery, sustained background operation and older-device
  performance are not yet verified on hardware.
- Advisory information only. Not certified for navigation or collision avoidance.
  Do not use it as a substitute for see-and-avoid, required equipment or a weather briefing.

## Controls and privacy

Settings contains receiver controls, routing, automatic restoration and appearance.
Diagnostics contains backups, logs, captures, analysis, weather comparison,
WebSocket tools and a loopback simulator. See [GUI controls](docs/GUI-Controls.md).
Dark appearance is the default; Android Expressive styling defaults on. Saved
appearance choices survive updates.

Traffic is processed locally. Optional GitHub update checks are enabled by default
on Windows and disabled on Android; they only notify. Captures and backups can
contain flight tracks and identifiers. Review [Privacy](docs/Privacy.md) before
recording or sharing data. Privacy and license texts are also available offline in Settings.

## Development

[Documentation](docs/Home.md) · [Building from source](docs/Building.md) ·
[Reproducible Android builds](docs/Reproducible-Builds.md)

## License

Sentry Bridge is **GPL-3.0-only**, with an additional permission for app stores; see
[LICENSE](LICENSE) and [NOTICE](NOTICE.txt). Third-party components retain their
licenses; [Credits and legal](docs/Credits-and-Legal.md) lists them.

Not affiliated with or endorsed by uAvionix, ForeFlight or the named EFB vendors.
Product names identify compatibility targets and remain their owners' trademarks.
The bridge uses Compatibility Mode; it does not decrypt the encrypted stream or
install receiver firmware.
