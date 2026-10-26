# Privacy

Effective 2026-10-26. This policy describes Sentry Bridge on Android, Windows,
iPhone and iPad, and the version run from Python. Features and stored files differ
between platforms; iPhone and iPad location use is described below.
The project does not operate a flight-data collection service.

## Receiver and EFB data

The bridge receives receiver status, aircraft addresses and callsigns, positions,
altitudes, velocities, GPS and available weather frames over the local network.
It processes these on the bridge device and forwards selected GDL90 data to your
configured EFB destination. Announcements are not forwarded.
To find EFB devices, Find EFB and the check that follows an EFB to a new address try a TCP
connection to the devices on the receiver's network; no data is sent to them.
UDP traffic and receiver HTTP requests are not encrypted. Other devices on the
network may observe them. Use a network you trust.

## iPhone and iPad: location and notifications

GPS cross-reference is off by default. When you turn it on, the app asks for Always location
access and, while the bridge forwards, compares this device's position with the
position the Sentry reports. The comparison runs on the device only. The phone's
position is never stored, never written to diagnostics, never sent to the EFB or
anywhere else, and never replaces or changes ownship or traffic data. Diagnostics
note only that a warning appeared and the distance. Stopping the bridge stops
location updates. To run without the check, stop the bridge, choose an option without
GPS cross-reference in Settings → Background running, then start again. You can also
remove location permission in Settings → Privacy & Security → Location Services.

Notifications are local: the app schedules them on the device to report a stopped
bridge or background running that stopped. No push service is used and nothing
leaves the device. The sideloaded build keeps running in the background by playing
silence; it does not use the microphone or record anything.

## Local storage and retention

Settings and destination addresses are stored locally. The iPhone and iPad app keeps
a short diagnostic log in memory while it runs; it does not have the recording and
backup tools described in the next paragraphs. Its phone or tablet GPS position is
not included in that log.

On Android and desktop, appearance choices and session credentials are also stored
locally. Their diagnostic tools can record flight tracks, aircraft identifiers,
receiver settings, logs, captures, uploaded files and analysis reports. These can
identify aircraft and locations even when a report omits its ownship summary.
Recordings are not automatically sent to the project.

Delete saved diagnostic files in Diagnostics → Files. Five automatic receiver
backups are retained; older automatic backups are removed as new ones are made.
Manual files remain until deleted; file/size limits are not a timed deletion policy.
Copies exported through the browser or Android file picker remain wherever you
saved them, including any cloud-synced folder. Delete those copies separately.

On Android, clear app storage in system Settings or uninstall to remove app-private
data; exported files remain. On Windows, close the application before removing
its data directory under %LOCALAPPDATA%/SentryBridge. Source GUI data defaults to
.local-tools/gui-data. Browser storage,
backups, filesystem snapshots and system logs may retain additional copies.

## Internet connections

Optional update checks request release metadata from GitHub, at most once daily
while stopped. They send normal connection metadata such as IP address and HTTP
headers; they do not upload captures or flight data. Checks notify only and do not
download or install executable updates. Windows defaults to enabled; Android
defaults to disabled and suppresses checks for recognized F-Droid installs.
Turn off Check for updates in Settings to disable these requests. Saved choices
are preserved. GitHub handles requests under its own privacy policy.

Opening documentation, release links or the receiver settings page uses your
browser and connects to that destination. Installation and building may download
dependencies. The project includes no advertising or analytics SDK and does not
send automatic crash reports. This does not describe data collection by your
operating system, browser, receiver, EFB or chosen export destination.

## Web controls and sharing

Desktop controls bind to loopback and use session authentication. These controls are not a secure
cloud storage service. Do not post raw recordings, backups, secrets or location
histories in issues. Review and redact reports before sharing; public posts may
be copied and retained by others.

## Contact and changes

Use the source repository's issue tracker for policy questions, without including
private flight data. Before public distribution the publisher must provide a
working public source/support URL and a private contact method for sensitive
requests. This revision does not invent a legal business identity or promise a
private support channel that has not been established. Material behavior changes
require updating this policy and the packaged copy.
