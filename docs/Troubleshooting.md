# Troubleshooting

## The EFB shows GPS but no traffic

- Check that Sentry Bridge is running and its status shows aircraft.
- If the bridge shows aircraft but the EFB does not, check that the EFB's GDL90 port matches the
  bridge's **EFB UDP port** (Settings, default 4000) and that the EFB's address is right.
- On Linux, an EFB on the same computer must allow sharing port 4000; otherwise use another port in
  both apps. On Android the EFB must be on another device.

## The bridge says the Sentry is in ForeFlight mode

The Sentry returns to ForeFlight mode at every power-up, and ForeFlight's **Disable** button switches
Compatibility Mode off. Turn on **Compatibility Mode** in Settings, and keep **Automatic restoration**
on.

## The Sentry switches itself off after about 30 minutes

That is the Sentry's **Power Savings**. Open **Settings → Open Sentry web settings** and turn it off.
ForeFlight is not required.

## No data from the Sentry

- The device running the bridge must be connected to the Sentry's WiFi.
- **Firewall:** allow incoming UDP port 4000 on that network. Windows usually treats the Sentry's WiFi
  as a Public network.
- **Address clash:** the Sentry uses `192.168.4.x` addresses. If another network on the same device uses
  that range too (some home routers do), or a VPN is active, disconnect it while using the Sentry.
- Another app on the device may already be using port 4000.

## My own aircraft appears as traffic

Enter your aircraft's ICAO address in **Settings → Bridge settings → Ownship ICAO**. The bridge log
suggests the address when a target sits on top of your own position.

## An Android update will not install

An APK signed with a different key cannot replace the installed app. Export anything you want to keep,
uninstall the app, then install the new APK. Uninstalling removes the app's settings and files.

## Battery, CO and temperature show "-"

They appear only while the bridge is forwarding and the Sentry is reachable. After the Sentry restarts
they return within about 15 seconds of it reconnecting. iPhone and iPad do not show them yet.
