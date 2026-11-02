# Security policy

## Reporting a problem

Please report security problems privately, not in a public issue: open the repository's
**Security** tab and choose **Report a vulnerability**. Include what you found, the version and
platform, and steps to reproduce it if you can.

You should get a reply within a week. Fixes go into the next release, and the report is credited
unless you ask otherwise.

## Supported versions

Only the latest release gets fixes. Please check that a problem still happens there first.

## Scope

Sentry Bridge runs on the Sentry's own WiFi network, which is open by default and whose web
interface has no password. Anyone who joins that network can change the Sentry's settings or send
it data, whatever Sentry Bridge does; setting a WiFi password on the Sentry is the protection
against that. Reports about Sentry Bridge itself are welcome, for example a way to make it send
data somewhere it should not, crash it from the network, or reach its control page from another
device.

Sentry Bridge gives advisory information only and is not certified for navigation or collision
avoidance.
