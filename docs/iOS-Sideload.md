# Install on iPhone or iPad

Sentry Bridge for iPhone and iPad is not in the App Store, and the releases do not include a ready-made
app file yet. You build it on a Mac and install it with your own Apple ID, at no cost. Read
[Compatibility](Compatibility.md#iphone-and-ipad) first.

## What you need

- An iPhone or iPad with iOS or iPadOS 26 or later.
- A Mac with Xcode 26 or later.
- A free Apple ID. A paid Apple Developer membership is not needed.

## Install from Xcode

1. Open `ios/SentryBridge.xcodeproj` in Xcode.
2. Under **Signing & Capabilities**, choose your Apple ID's Personal Team.
3. Connect the iPhone or iPad and run the app on it.
4. If iOS asks, turn on **Settings → Privacy & Security → Developer Mode** and restart.
5. Open Sentry Bridge and allow **Local Network** access when asked.

## Or install with SideStore or AltStore

`python3 tools/build_ios_ipa.py` on the Mac writes an unsigned `.ipa` to `dist/`. Copy it to the device
and open it in [SideStore](https://sidestore.io) or [AltStore](https://altstore.io), set up with their
own guides.

## Keep it working

- With a free Apple ID the app works for **7 days** at a time. Refresh it (from Xcode, SideStore or
  AltStore) before then, and before a flight.
- A free Apple ID allows 3 sideloaded apps at a time.
- To update, install the newer build the same way; settings are kept.

## Try it without a receiver

**Diagnostics → Demo with sample data** plays simulated traffic in the app only. Nothing is sent to an
EFB. Stop ends the demo.
