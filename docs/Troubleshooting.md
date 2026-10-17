# Troubleshooting

## The EFB shows GPS but no traffic
- Is the bridge running, and does its status show aircraft? If it shows aircraft but the EFB does
  not, check the EFB's GDL90 port matches where the bridge sends (**EFB UDP port** in Settings,
  default 4000).
- With an EFB on the same device, the bridge shares port 4000 with it. On Android and Linux the EFB
  must allow port sharing; if the bridge reports the port in use, set another EFB port in Settings
  and the same port in the EFB.

## The bridge says the Sentry is in ForeFlight (encrypted) mode
Compatibility Mode resets on every power-up, and ForeFlight's Disable button turns it off. Use
the **Compatibility Mode** switch in the app's Settings, `--auto-enable` on the command line, or
`sentry_ctl.py enable`.

## The Sentry turns itself off after about 30 minutes
That is **Power Savings**: it shuts the Sentry off after 30 minutes without ForeFlight connected.
Use **Settings → Open Sentry web settings** and turn it off there. ForeFlight is optional.
The bridge warns while it is on.

## No data from the Sentry
- The device running the bridge must be on the Sentry's WiFi.
- **Firewall**: allow incoming UDP 4000 on that network. Windows usually labels the Sentry's WiFi as
  Public; allow the bridge on Public networks. On Linux with firewalld, put only the Sentry's WiFi
  connection in the trusted zone:
  `sudo nmcli connection modify "<Sentry connection>" connection.zone trusted`.
- **Address clash**: the Sentry uses `192.168.4.x`. If another network on the same machine uses that
  range too (some home routers do), or a VPN routes it, traffic to the Sentry goes the wrong way.
  Check with `ip route get 192.168.4.1` (Linux) or `route print` (Windows), and disconnect the other
  network or VPN while using the Sentry.
- Another app may already hold port 4000 on that device.

## My own aircraft appears as traffic
Set your ICAO address (`--ownship-icao`, or the field in the Android app). The bridge logs a hint
with the address when a target sits on top of your own position.

## Updates to the Android app will not install
Builds signed with different keys cannot replace each other. Uninstall the app, then install the new
APK only after exporting anything you need to retain: uninstalling removes app-private settings
and files. Prefer a verified update signed with the existing key. See [Building](Building.md).
