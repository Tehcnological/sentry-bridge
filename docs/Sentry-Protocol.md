# Sentry protocol notes

Observed on a Sentry Plus with WiFi firmware 1.0.32 and ADS-B firmware 2.4.45 / 2.6.14. Other
firmware versions are untested; captures from them are welcome (see [Command-line tools](Command-Line.md)).

## Network
- The Sentry runs its own WiFi access point at `192.168.4.1` and hands out `192.168.4.x` addresses.
- It sends GDL90 as unicast UDP to port 4000 of each WiFi client, never broadcast. The port is
  fixed: no setting or announcement changes it.
- Recipients are the WiFi clients that have an address from its DHCP server, plus EFBs it
  recognizes from an announcement (see [EFB discovery](#efb-discovery)), up to 5 at a time. A
  client drops off when it leaves the WiFi, when sending to it fails, or when it answers with ICMP
  port unreachable (nothing listening on 4000); the list is rechecked about every 5 s. Not yet confirmed with a capture.
- Compatibility Mode changes what is sent, not where it is sent.
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
| `GET /data/<name>` | One stored file; the current one may still be growing |
| `GET /data?action=get` | JSON inventory: `numFiles`, `currCSV`, `currLOG`, `files`. With `&file=<prefix>`, metadata for matching files (name, size, type, start/end time and position where known) |
| `GET /data?action=get&file=currCSV` | The current telemetry CSV as text (`file=currLOG`: the current diagnostic log). Flushes the logger first |
| `GET /stats` | Statistics page; its WebSocket at `/stats` pushes nested JSON about twice a second (see below) |
| `GET /calibration?action=get` | Stored calibration: ADC reference (mV), CO sensitivity and offset, manufacturing date and serial, whether defaults are in use |
| `GET /networkInfo?action=get` | Station WiFi state, last disconnect reason and a list of visible access points. Starts a WiFi scan and can take seconds |
| `GET /coredump` | Crash dump page; `?action=download` returns the stored dump |
| `GET /espUpdate`, `GET /pingUpdate` | Firmware upload pages for the WiFi and attached processors |

No route asks for authentication, and none is limited by Compatibility Mode. An unknown path returns
the status page, so a 200 at a guessed URL does not mean the page exists.

**Requests that change the Sentry, including plain GETs:**
- `/data?action=rm&file=<prefix>` deletes every file whose name starts with the prefix (case-insensitive).
- `/coredump?action=erase` erases the crash dump; `/coredump?action=test` crashes the firmware on purpose.
- `/settings/?action=set` writes settings and can run calibration, factory defaults or tests.
- A multipart POST to `/espUpdate` or `/pingUpdate` installs firmware and reboots or interrupts reception.

Sentry Bridge only reads `/?action=get` and the settings, sets `publicGDL90`, and downloads `/data/`
files. Anyone on the Sentry's WiFi can make the requests above, another reason to set a WiFi password.

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

### Statistics WebSocket (`/stats`)
About twice a second it sends nested JSON with system, traffic, communication and sensor statistics.
The sensor fields are read before Compatibility Mode removes sensor reports from the UDP stream, so
this is the likeliest live source of what Compatibility Mode drops:

| Field | Meaning |
|---|---|
| `batteryVoltage_mv`, `batteryCapacity_%` | Battery voltage (mV) and charge (%) |
| `CO_SENSOR_PPM`, `CO_SENSOR_ADC_MV`, `CO_SENSOR_STATE` | CO concentration (ppm), raw sensor voltage, sensor state |
| `sensor.baro.pressure_mbar`, `sensor.baro.altitude_m`, `sensor.baro.temp_c` | Pressure (mbar), baro altitude (m), temperature (°C) |
| `sensor.ats.temp_c`, `sensor.ats.humidity_pRH` | Temperature (°C), relative humidity (%) |
| `sensor.imu.roll`, `sensor.imu.pitch` | Attitude; units and sign conventions unknown |
| `sensor.als_Lux` | Ambient light (lux) |

`/stats?action=get` is not a JSON getter; use the WebSocket.

### Stored logs (`/data/`)
- A telemetry CSV starts when logging starts after the clock is set, named `L<YYMMDDhhmmss>.csv`.
  After a GPS fix it can be renamed to add two signed coordinates (likely degrees × 10⁷) separated
  by underscores. The time zone of the name is unknown.
- One row about every second. A file grows for the whole power-on session; there is no size-based
  rotation.
- When a new file starts, files dated in the future are removed and the oldest `L` files are pruned
  to 15, so about 16 remain including the current one. `M…log` diagnostic files are pruned the same
  way. Downloading never deletes.
- Values are separated by a comma and a space, and a row can end with a trailing separator.

| Columns | Units |
|---|---|
| `Timestamp` | Unix time, seconds |
| `HeapAvailable`, `HeapLowWaterFree` | Bytes |
| `Sensor::battery_mV`, `Sensor::batteryCapacity_%` | mV, % |
| `Sensor::CO_Sensor_ppm`, `Sensor::CO_Sensor_Healthy`, `Sensor::CO_SensorTemp_C` | ppm, boolean, °C |
| `Sensor::baroAlt_mm`, `Sensor::baroTemp_CE2` | mm, hundredths of °C |
| `Sensor::IMU::attX`, `attY`, `attZ` | Attitude; scale and axes unknown |
| `Sensor::IMU::offsetX`, `offsetY`, `offsetZ` | Offsets; units unknown |
| `Sensor::IMU::quaternionW`, `quaternionX`, `quaternionY`, `quaternionZ` | Unitless |
| `Sensor::IMU::accelX`, `accelY`, `accelZ` | Acceleration; units unknown |
| `Sensor::IMU::rateX`, `rateY`, `rateZ` | Rotation rate; units unknown |

All IMU columns carry the full `Sensor::IMU::` prefix. Pressure is not logged, only baro altitude.

The routes, statistics fields and log format in this section are not yet confirmed on hardware.
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

The Sentry also listens on 63093, but it recognizes only a few exact announcements (ForeFlight and
FliteDeck Pro, both on port 4000) rather than parsing the JSON. Other app names, ports, spacing or
field order are ignored, and the announced port is never used: an announcement can add a recipient
on 4000 but cannot move the stream to another port. A unicast announcement to the Sentry should work
as well as a broadcast.

## Listening ports
| Port | Use |
|---|---|
| TCP 80 | Web pages, settings API, WebSocket status and firmware updates |
| UDP 4000 | Also accepts incoming data from clients |
| UDP 63093 | EFB announcements |
| UDP 67 | DHCP server for its WiFi |
| UDP 68 | DHCP client, only when joined to another WiFi network |

No HTTPS, mDNS, SSH or Telnet service is known. For the pages on port 80, see [HTTP API](#http-api-port-80).

The recipient rules, announcement matching and port list above are not yet confirmed with a capture
on hardware.

## Open questions
- **FIS-B weather**: no `0x07` uplink frames have been seen, because no ground station was in range.
  Whether weather passes through in standard form is unknown. If it does, the bridge relays the
  frames unchanged (tested with simulated uplinks).
- **CO, battery, temperature, attitude**: not in the UDP stream in Compatibility Mode. The
  [statistics WebSocket](#statistics-websocket-stats) should carry them live; confirm that its values
  stay fresh in Compatibility Mode and find the IMU units before relying on them.
