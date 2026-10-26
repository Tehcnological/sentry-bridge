# Troubleshooting

## The EFB shows GPS but no traffic

- Check that Sentry Bridge is running and its status shows aircraft.
- If the bridge shows aircraft but the EFB does not, check that the EFB's GDL90 port matches the
  bridge's **EFB UDP port** (Settings, default 4000) and that the EFB's address is right. The EFB
  device's address can change after the Sentry restarts; **Find EFB** lists the devices on the WiFi.
- On Android, if the Sentry was off, the phone may have switched to another saved WiFi network. Switch
  it back to the Sentry's WiFi.
- On Android the EFB must be on another device.

## The bridge says the Sentry is in ForeFlight mode

The Sentry returns to ForeFlight mode at every power-up, and ForeFlight's **Disable** button switches
Compatibility Mode off. Turn on **Compatibility Mode** in Settings, and keep **Automatic restoration**
on. On iPhone and iPad, this setting is called **Restore Compatibility Mode**.

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

On Windows or Android, enter your aircraft's ICAO address in **Settings → Bridge settings → Ownship ICAO**. The bridge log
suggests the address when a target sits on top of your own position.

## An Android update will not install

An APK signed with a different key cannot replace the installed app. Export anything you want to keep,
uninstall the app, then install the new APK. Uninstalling removes the app's settings and files.

## Battery, CO and temperature show "-"

They appear only while the bridge is forwarding and the Sentry is reachable. After the Sentry restarts
they return within about 15 seconds of it reconnecting.

## iPhone or iPad stops forwarding when I switch to the EFB

Open Sentry Bridge and check its status. If stopped, press **Start** again. Before switching apps,
check **Settings → Background running**. Choose **Background audio** where offered. If your version
uses **Bridge Session**, iOS can refuse it or end it early; read the message on the bridge screen.
**Only while the app is open** stops when you switch apps or lock the screen.

A phone call interrupts background audio. The app tries to resume it, but recovery on a real device
has not yet been verified for the new background options. Check fresh data in the EFB afterward.
See [iPhone and iPad limitations](Compatibility.md#iphone-and-ipad).

## GPS cross-reference says it needs Always location

In Sentry Bridge, tap **Open Settings** beside the note and set location access to **Always**.
The note updates when you return. If the bridge is still running, the check starts without a restart.
If the bridge stopped while you were in Settings, press **Start** again. If you do not want that access, stop the bridge
and choose an option without GPS cross-reference in **Settings → Background running**.

If the check is waiting for a phone or tablet position, that device may not have a usable GPS fix.
The EFB continues to receive the Sentry's position. This check never substitutes the device's position.

## I did not hear a stopped-bridge warning on iPhone or iPad

Allow Sentry Bridge notifications in iOS Settings and in the Focus you use while flying. A free Apple
ID installation cannot use Time Sensitive delivery, so a Focus can silence its normal notifications.
The warning is intended to arrive about 30 seconds after updates stop, but actual delivery after iOS
suspends or closes the app has not yet been verified on hardware. Check the EFB's data freshness too.

## AltStore reports that permissions do not match

See [installation troubleshooting](iOS-Sideload.md#if-altstore-reports-a-permission-mismatch).
Refreshing the source may fetch a corrected listing. If it still fails, report the version and exact
error; changing location access in iOS Settings will not repair the listing.
