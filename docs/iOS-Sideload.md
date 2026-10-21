# Install on iPhone or iPad

Sentry Bridge for iPhone and iPad is not in the App Store. Each release includes `sentry-bridge.ipa`, an
unsigned app file: it gets a signature when you install it, with one of the options below. Read
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
updates. On the iPhone or iPad, open [Add the source](https://tehcnological.github.io/sentry-bridge/add-source.html)
and tap your app, or add this source URL in the app's **Sources** screen:

```
https://raw.githubusercontent.com/Tehcnological/sentry-bridge/master/altsource.json
```

You can also download `sentry-bridge.ipa` from the release and open it in any of them.

## Install with SideStore or AltStore (free Apple ID)

1. Set up [SideStore](https://sidestore.io) or [AltStore](https://altstore.io) with their own guides.
2. Install Sentry Bridge from its source, or open `sentry-bridge.ipa` in the app.
3. If iOS asks, turn on **Settings → Privacy & Security → Developer Mode** and restart.
4. Open Sentry Bridge and allow **Local Network** access when asked.

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

## Try it without a receiver

**Diagnostics → Demo with sample data** plays simulated traffic in the app only. Nothing is sent to an
EFB. Stop ends the demo.

## For developers

To build the app yourself, open `ios/SentryBridge.xcodeproj` in Xcode 26 or later on a Mac, choose your
team under **Signing & Capabilities**, and run it on the device. `python3 tools/build_ios_ipa.py`
builds the same unsigned `dist/sentry-bridge.ipa` the release uses.
