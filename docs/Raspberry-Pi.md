# Raspberry Pi web interface

The web build uses the shared interface and compiled C decoder. Forwarding continues with the
browser closed. Installation and `sentrybridge.local` discovery still need testing on a Pi
connected to a real Sentry; Windows/WSL tests validate the controller and protocols only.

## Install the development build

Use Raspberry Pi OS Lite with NetworkManager. Configure the Sentry WiFi in Raspberry Pi Imager,
or join it using the Pi's network settings. Copy the reviewed source to the Pi, then run:

```bash
sudo bash deploy/install-web.sh
```

The installer builds the C library on the Pi, installs the controller under `/opt/sentry-bridge`,
and starts the web and Avahi address services on boot. It disables the older headless service
so there is one owner of UDP 4000. It prints the initial `bridge` username and generated password.
The password is stored in `/var/lib/sentry-bridge/web-password` with access limited to its owner.
It never copies signing keys, workstation caches or recordings from the checkout.

## Use

Open **http://sentrybridge.local/** on a device joined to the same WiFi and enter the installation
password. If the alias does not resolve, use the Pi's WiFi IPv4 address. The alias uses Avahi;
WiFi client isolation, multicast filtering or another device using the name can prevent it from
working. Discovery on Sentry WiFi is a pending hardware check. The Pi's system hostname is retained.

Select **Other device**, enter the EFB device's WiFi IPv4 address, then press **Start**. The bridge
sends there on **EFB UDP port** (Settings, default 4000); in the EFB, choose a Stratux or GDL90 device.

**Settings → Start bridge on boot** is on by default. After the first successful Start, a reboot
starts forwarding to the saved destinations without opening a browser. Turn this setting off
if you want manual startup. **Stop** ends current forwarding; it keeps the web service available
and does not change the boot preference. Closing the browser leaves forwarding running.

Receiver controls, backups, logs, captures and weather comparison are available in the GUI.
The simulator is restricted to local Pi loopback traffic; it cannot send synthetic traffic to
an external EFB. Its simulated settings never replace the real receiver configuration.

This is plain HTTP on the local network. The password prevents unauthenticated control, but
traffic and the password are not encrypted. Use it on a trusted WiFi network, do not forward its
port to the Internet, and keep recordings private. The browser's own username/password prompt
is used; there is no separate setup dashboard.

## Receiver settings

Disable **Power Savings** in **Open Sentry web settings**, or open
**http://192.168.4.1/settings/** while joined to Sentry WiFi. ForeFlight is optional. Enable
**Automatic restoration** in the bridge; Compatibility Mode resets when the receiver powers up.
Auto Power On can be enabled in the Sentry's own settings if desired.

## Storage and maintenance

Private settings and downloads live in `/var/lib/sentry-bridge`. Captures stop at 32 MiB.
Delete unused files in Diagnostics. Make sure persistent storage remains writable; an overlay
filesystem can discard preferences and captures at reboot unless that directory is kept persistent.

For service troubleshooting:

```bash
systemctl status sentry-bridge-web sentry-bridge-mdns
journalctl -u sentry-bridge-web -u sentry-bridge-mdns
```

The older terminal-only installation remains available through `deploy/install.sh`; do not run
its UDP service alongside the web controller.
