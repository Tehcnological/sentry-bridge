# Install on iPhone or iPad (free, without the App Store)

Sentry Bridge for iOS is not in the App Store. You can install it for free with a sideloading app
that signs it with your own Apple ID. Read [Compatibility and limitations](Compatibility.md#iphone-and-ipad)
first.

## What you need

- iPhone or iPad with iOS or iPadOS 26 or later.
- A free Apple ID (an Apple Developer membership is not needed).
- [SideStore](https://sidestore.io) (refreshes on the device) or [AltStore](https://altstore.io)
  (refreshes from a Mac or PC running AltServer). Follow their own setup guide; it needs a computer once.
- `SentryBridge-<version>-unsigned.ipa` from the project's GitHub release.

## Install

1. Set up SideStore or AltStore with their guide.
2. Download the `.ipa` to the device, then open it in SideStore or AltStore (or use its **+** button
   and choose the file).
3. If iOS asks, turn on **Settings > Privacy & Security > Developer Mode** and restart.
4. Open Sentry Bridge and allow **Local Network** access when asked.

## Keep it working

- A free Apple ID signs apps for **7 days**. Refresh Sentry Bridge in SideStore or AltStore before
  then; an expired app does not open until it is refreshed. Refresh before a flight.
- A free Apple ID allows **3 sideloaded apps** at a time, including SideStore itself.
- Updating: install the newer `.ipa` the same way; settings are kept.

## Without a receiver

**Diagnostics > Demo with sample data** plays recorded sample data in the app only. Nothing
is sent to an EFB. Stop ends the demo.

## Build it yourself

On a Mac with Xcode 26 or later, `python3 tools/build_ios_ipa.py` writes the unsigned `.ipa` to
`dist/`. To install directly from Xcode instead, open `ios/SentryBridge.xcodeproj`, choose your
Personal Team under Signing & Capabilities, and run on the connected device (also 7 days).
