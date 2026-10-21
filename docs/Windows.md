# Windows

Requires Windows 10 or 11.

## Install and run

1. Download `sentry-bridge-gui.exe` from the [Releases page](https://github.com/Tehcnological/sentry-bridge/releases).
   Windows SmartScreen may warn about an unrecognized app. You can check the file against the
   `SHA256SUMS` file on the same release page.
2. Connect the computer to the Sentry's WiFi.
3. Open the app and choose where your EFB runs:
   - **This device**: an EFB on this computer. It shares UDP port 4000 with the bridge.
   - **Other device**: enter the EFB device's IP address, or use **Find EFB**.
4. Press **Start**. In the EFB, select a Stratux or GDL90 (Custom GDL90) device on UDP port 4000.

Once, in **Settings → Open Sentry web settings**, turn off the Sentry's **Power Savings**.

**Stop** ends forwarding. **Settings → Exit Sentry Bridge** closes the app. If the app opened in your
browser rather than its own window, closing the browser tab does not stop the bridge; use Exit.

## Firewall

When Windows asks, allow Sentry Bridge on the network the Sentry's WiFi uses. Windows usually treats
that network as **Public**. Do not turn off the firewall.

## Data

Settings and saved files are in `%LOCALAPPDATA%\SentryBridge`. Recordings contain location data; see
[Privacy](Privacy.md). The app checks GitHub once a day for a new version and only shows a notice; turn
**Check for updates** off in Settings to stop this.

## Command-line tools

The release also includes `sentry-bridge.exe`, a bridge without a window, and `sentry-ctl.exe`, which
reads and changes the Sentry's settings. Most people do not need them; see
[Command-line tools](Command-Line.md).
