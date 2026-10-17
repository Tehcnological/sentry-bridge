# Sentry protocol notes

Observed on a Sentry Plus with WiFi firmware 1.0.32 and ADS-B firmware 2.4.45 / 2.6.14. Other
firmware versions are untested; captures from them are welcome (see [Command-line tools](Command-Line.md)).

## Network
- The Sentry runs its own WiFi access point at `192.168.4.1` and hands out `192.168.4.x` addresses.
- It sends GDL90 over UDP to port 4000 of each WiFi client.
- Its web interface on port 80 has no authentication, and the WiFi is open by default. Anyone who
  joins can change settings, so consider setting a WiFi password.

## Compatibility Mode (`publicGDL90`)
- Set with `POST /settings/?action=set` and the JSON body `{"publicGDL90": true}` (`false` to undo).
  Booleans or 0/1 work; strings such as `"enable"` return HTTP 200 but do nothing.
- Not reported by `GET /settings/?action=get`.
- **Resets to ForeFlight mode on every power-up**, including shutdowns by Power Savings.
- ForeFlight shows a "Sentry in Compatibility Mode" banner with a Disable button. Connecting
  ForeFlight does not change the mode. In this mode ForeFlight still shows GPS and traffic, but
  loses CO and temperature alerts, AHRS, baro pressure and battery.

## UDP stream in Compatibility Mode

| ID | Rate | Contents |
|---|---|---|
| `0x00` | 1 Hz | Standard Heartbeat |
| `0x0A` | 1 Hz | Standard Ownship report (address 000000, pressure altitude) |
| `0x0B` | 1 Hz | Standard Ownship Geometric Altitude |
| `0x21` | 5 to 20/s | `ff ff ff` + 14-byte 1090ES DF17/18 squitter, with a uAvionix trailer in place of the Mode S parity |
| `0x24` | bursty | `ff ff ff` + 48-byte UAT long ADS-B message (34 data + 14 parity bytes), **not error corrected** |
| `0x25` | 1 Hz | Constant device identification frame |
| `0x28` | 0.2 Hz | Sensor summary, subtype `06 01`. Two fields change with motion; not decoded. Too slow for attitude |

In ForeFlight mode the stream contains only `0x25` and the encrypted `0x26`.

### UAT (`0x24`)
- Reed-Solomon RS(48,34) over GF(256), primitive polynomial 0x187, first consecutive root 120 (as in dump978).
- On the ground most frames arrive with byte errors; in one recording 4 of 60 were clean, 14 were
  correctable and 42 were not. Uncorrectable frames must be dropped: they carry wrong addresses and
  positions that still look plausible.
- The callsign field alternates between the flight ID and the squawk code. Keep the last flight ID.

## HTTP API (port 80)

| Request | Result |
|---|---|
| `GET /?action=get` | Firmware versions, SSID, client count, upstream WiFi, serial numbers |
| `GET /settings/?action=get` | Settings: `wifi`, `client`, `led`, `ahrs`, `coAlarmLevel`, `Power` |
| `POST /settings/?action=set` | Partial update: only the posted keys change |
| `GET /data/` | Directory index of stored telemetry logs (CSV) |

Other keys the settings page posts: `loadDefaults` (factory reset), `calAHRS`, and
`tests: {coAlarm | Display | LED}`. `?action=set` reads the JSON body regardless of HTTP verb.
Pages are served gzip-compressed (`curl --compressed`).

Avoid the settings page's Save button for single changes: it posts every field, and an empty
password box can be rejected or clear a stored password. Post only the key you want to change.

### Power settings
- `Power.powerSavings`: shuts the Sentry off after 30 minutes without ForeFlight connected. With a
  bridge and no ForeFlight, that means mid-flight, and the Sentry comes back in ForeFlight mode.
  Turn it off in Sentry's native web settings (`http://192.168.4.1/settings/`) while joined to
  the receiver WiFi, or in ForeFlight's Sentry settings. ForeFlight is not required.
- `Power.autoOnEnabled`: powers the Sentry on when USB power is applied.

## WebSocket (`ws://192.168.4.1/`)
The status page subscribes to a JSON message once per second: `clientCount`, `stationSsid`,
`stationState`, and `status` with `latitude` and `longitude` (degrees x 1e7), `gnssAltitude` and
`baroAltitude` (mm), `gpsFix`, `gpsSatellites`, `pressure`, `temperature`, `NACp`, `NIC`.
In Compatibility Mode `pressure` and `temperature` stay 0.

## EFB discovery
EFBs, including ForeFlight, announce themselves with a UDP broadcast to port 63093, for example
`{"App":"ForeFlight","GDL90":{"port":4000}}`. Find EFB lists them. The bridge does not send
traffic to an address that only announced. In testing, DHCP clients received the Sentry's stream
without announcing.

## Open questions
- **FIS-B weather**: no `0x07` uplink frames have been seen, because no ground station was in range.
  Whether weather passes through in standard form is unknown. If it does, the bridge relays the
  frames unchanged (tested with simulated uplinks).
- **CO, battery, temperature, attitude**: not found on any channel in Compatibility Mode (UDP, HTTP,
  WebSocket). Candidates: the stored logs under `/data/`, and the `0x28` summary.
