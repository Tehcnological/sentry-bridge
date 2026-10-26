# Install on iPhone or iPad

Sentry Bridge for iPhone and iPad is not in the App Store. Each release includes `sentry-bridge.ipa`, an
app file that your installation app signs for your Apple ID or certificate. Read
[Compatibility](Compatibility.md#iphone-and-ipad) first.

## What you need

- An iPhone or iPad with iOS or iPadOS 26 or later.
- `sentry-bridge.ipa` from the [Releases page](https://github.com/Tehcnological/sentry-bridge/releases).
- One way to sign it:
  - **SideStore or AltStore** with a free Apple ID: no cost, but the app must be refreshed every 7 days.
  - **A paid Apple Developer account**: the app keeps working for up to a year between installs.
  - **Feather** with a developer certificate you already have: signs and installs on the device itself.

## Add the Sentry Bridge source

SideStore, AltStore and Feather can install Sentry Bridge from its source and show new versions as
updates. On the iPhone or iPad, tap your app's button in the [README](../README.md#download), or add
this source URL in the app's **Sources** screen:

```
https://raw.githubusercontent.com/Tehcnological/sentry-bridge/master/altsource.json
```

You can also download `sentry-bridge.ipa` from the release and open it in any of them.

## Install with SideStore or AltStore (free Apple ID)

1. Set up [SideStore](https://sidestore.io) or [AltStore](https://altstore.io) with their own guides.
2. Install Sentry Bridge from its source, or open `sentry-bridge.ipa` in the app.
3. If iOS asks, turn on **Settings → Privacy & Security → Developer Mode** and restart.
4. Open Sentry Bridge. If asked, choose **Background audio** to keep forwarding while you use your EFB.
   If your version offers **Bridge Session** instead, choose that and check its time limit. Allow
   notifications and, when you start the bridge, **Local Network** access.
5. If you choose **GPS cross-reference**, allow location and set it to **Always** for background use.
   It checks the Sentry position; it does not supply your EFB with the phone or iPad position.

With a free Apple ID the app works for **7 days** at a time. Refresh it in SideStore or AltStore before
then, and before a flight. A free Apple ID allows 3 sideloaded apps at a time.

## Install with a paid Apple Developer account

Sign in to SideStore, AltStore or another signing tool with the paid account, then install the IPA as
above. The app works for up to a year before it needs reinstalling, and the 3-app limit does not apply.

## Install with Feather

[Feather](https://github.com/khcrysalis/Feather) signs and installs apps on the iPhone or iPad with a
developer certificate and provisioning profile you import. Install Sentry Bridge from its source (or
import `sentry-bridge.ipa`), sign it with your certificate and install it. The app works for as long as the certificate is valid; if Apple revokes
the certificate, the app stops opening.

## Updating

With the source added, SideStore, AltStore and Feather show new versions as updates. Otherwise install
the IPA from the newer release the same way. Settings are kept.

## Before using it with your EFB

Connect to the Sentry's WiFi, start the bridge and check that your EFB receives fresh data. Then switch
to the EFB and check again. If you plan to lock the screen, try that on the ground too.

In **Settings → Background running**, choose the option you want before starting. Background audio
plays silence to help keep forwarding while another app is open. A phone call interrupts it; the app
tries to resume afterward. iOS can still stop the app. If it stops, reopen Sentry Bridge and press
**Start** again. Allow its notifications in the Focus you use while flying.

GPS cross-reference is optional, uses more battery, and needs **Always** location access for background
use. It shows position warnings in Sentry Bridge only. See
[Compatibility and limitations](Compatibility.md#iphone-and-ipad) for the choices, warnings and what
still needs testing on a real device.

## If AltStore reports a permission mismatch

The download and its store listing must describe the same permissions. If AltStore says they do not
match, refresh the source listing and try again. If the error remains, report the app version and the
exact message in the [issue tracker](https://github.com/Tehcnological/sentry-bridge/issues).
A corrected listing and matching download are needed; changing location permission in iOS Settings
will not fix an installation-listing mismatch.

## Try it without a receiver

**Diagnostics → Demo with sample data** plays simulated traffic in the app only. Nothing is sent to an
EFB. Stop ends the demo.

## Building the app yourself

See [Building from source](Building.md#iphone-and-ipad).
