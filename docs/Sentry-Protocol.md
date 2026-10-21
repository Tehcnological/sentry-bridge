# Sentry protocol notes

How a uAvionix Sentry talks to apps over its WiFi: what it sends, what its web interface offers, and
what Sentry Bridge uses. Written for developers and the curious; you do not need it to use the app.

Observed on a Sentry Plus with WiFi firmware 1.0.32 and ADS-B firmware 2.4.45 / 2.6.14. Other firmware
versions are untested; recordings from them are welcome (see [Command-line tools](Command-Line.md)).
Items marked *unconfirmed* have not been checked on hardware yet.

## Network basics

- The Sentry runs its own WiFi network. It is at `192.168.4.1` and gives devices `192.168.4.x`
  addresses. The network is open by default, and the web interface has no password, so anyone who joins
  can change settings. Consider setting a WiFi password.
- It sends its data as UDP to **port 4000** of each device on its WiFi, one copy per device, never as a
  broadcast. The port cannot be changed.
- It sends to devices that got an address from it, plus EFBs it recognizes from an announcement (see
  [EFB discovery](#efb-discovery)). A device on another network that the Sentry has joined does not
  receive the data. *Unconfirmed:* at most 5 recipients at a time, and a device drops off the list
  when it leaves, when sending fails, or when nothing listens on port 4000.

## The two modes

| | ForeFlight mode | Compatibility Mode |
|---|---|---|
| When | After every power-up | After it is switched on |
| Data | Encrypted, for ForeFlight only | Standard GDL90 GPS, plus raw traffic messages |
| AHRS, CO, pressure, battery | ForeFlight only | Not in the data stream |

The mode changes what is sent, not where it is sent.

**Switching Compatibility Mode on:** `POST /settings/?action=set` with the JSON body
`{"publicGDL90": true}` (`false` switches it off). `true`/`false` and `1`/`0` work; other values such as
`"enable"` return HTTP 200 but do nothing. The current mode is not reported by the settings; watch the
data stream instead.

- It **resets to ForeFlight mode at every power-up**, including after Power Savings switches it off.
- ForeFlight shows a "Sentry in Compatibility Mode" banner with a **Disable** button. In this mode
  ForeFlight still shows GPS and traffic, but loses CO and temperature alerts, AHRS, pressure and battery.

## The data stream (UDP port 4000)

In Compatibility Mode, each GDL90 message type:

| ID | Rate | Contents |
|---|---|---|
| `0x00` | 1 per second | Standard heartbeat |
| `0x0A` | 1 per second | Standard ownship report (address 000000, pressure altitude) |
| `0x0B` | 1 per second | Standard ownship geometric altitude |
| `0x21` | 5 to 20 per second | 1090 MHz traffic: `ff ff ff` + the 14-byte Mode S message (DF17/18), with a uAvionix trailer in place of the parity |
| `0x24` | in bursts | 978 MHz UAT traffic: `ff ff ff` + the 48-byte message (34 data + 14 parity bytes), **not error corrected** |
| `0x25` | 1 per second | Device identification, always the same |
| `0x28` | every 5 seconds | Sensor summary (subtype `06 01`); not decoded and too slow for attitude |

In ForeFlight mode the stream contains only `0x25` and the encrypted `0x26`.

**UAT messages need error correction.** They use Reed-Solomon RS(48,34) over GF(256), primitive
polynomial 0x187, first consecutive root 120 (as in dump978). On the ground most arrive damaged: in one
recording 4 of 60 were clean, 14 correctable and 42 not. Drop the ones that cannot be corrected; they
carry wrong addresses and positions that still look plausible. The callsign field alternates between
the flight ID and the squawk code, so keep the last flight ID.

## Web interface (port 80)

| Request | Returns |
|---|---|
| `GET /?action=get` | Firmware versions, WiFi name, number of connected devices, upstream WiFi, serial numbers |
| `GET /settings/?action=get` | Settings: `wifi`, `client`, `led`, `ahrs`, `coAlarmLevel`, `Power` |
| `POST /settings/?action=set` | Changes only the keys you send |
| `GET /data/` | A list of the stored logs |
| `GET /data/<name>` | One stored log; the current one may still be growing |
| `GET /data?action=get` | The log list as JSON (`numFiles`, `currCSV`, `currLOG`, `files`); add `&file=<prefix>` for details of matching files |
| `GET /data?action=get&file=currCSV` | The current log as text (`file=currLOG`: the current diagnostic log) |
| `GET /stats` | Statistics page; its live feed is described [below](#live-statistics-stats) |
| `GET /calibration?action=get` | Stored calibration values, manufacturing date and serial |
| `GET /networkInfo?action=get` | WiFi state and nearby networks. Starts a WiFi scan, which takes seconds |
| `GET /coredump` | Crash dump page; `?action=download` returns the stored dump |
| `GET /espUpdate`, `GET /pingUpdate` | Firmware update pages |

Nothing needs a password, and nothing depends on the mode. An unknown address returns the status page,
so a successful response to a guessed address does not mean that page exists. Pages are served
compressed (`curl --compressed`). *Unconfirmed:* the `/data?action=get`, `/calibration`,
`/networkInfo`, `/coredump` and firmware update requests.

**Requests that change the Sentry, even as plain GET requests:**

- `/data?action=rm&file=<prefix>` deletes every log whose name starts with the prefix.
- `/coredump?action=erase` erases the crash dump; `/coredump?action=test` crashes the Sentry on purpose.
- `/settings/?action=set` changes settings, and with some keys runs calibration (`calAHRS`), a factory
  reset (`loadDefaults`) or tests (`tests`).
- A firmware upload to `/espUpdate` or `/pingUpdate` installs it and restarts the Sentry.

The settings page's **Save** button sends every field at once; an empty password box can be rejected or
clear a stored password. To change one setting, send only that key.

**What Sentry Bridge uses:** it reads `/?action=get` and the settings, switches `publicGDL90`, reads the
`/stats` feed, and downloads logs when asked. It never sends the requests in the list above.

### Power settings

- `Power.powerSavings` switches the Sentry off after 30 minutes without ForeFlight connected. With a
  bridge and no ForeFlight, that can happen in flight, and the Sentry comes back in ForeFlight mode.
  Turn it off in the Sentry's web settings (`http://192.168.4.1/settings/`) or in ForeFlight.
- `Power.autoOnEnabled` switches the Sentry on when USB power is connected.

## Live statistics (`/stats`)

A WebSocket at `ws://192.168.4.1/stats` sends a JSON message about twice a second. It carries the
sensor readings that Compatibility Mode leaves out of the data stream, and they keep updating in
Compatibility Mode. Sentry Bridge shows battery, CO and temperature from it.

| Field | Meaning |
|---|---|
| `batteryCapacity_%`, `batteryVoltage_mv` | Battery charge (%) and voltage (mV) |
| `CO_SENSOR_PPM`, `CO_SENSOR_ADC_MV`, `CO_SENSOR_STATE` | Carbon monoxide (ppm), raw sensor voltage, sensor state |
| `sensor.ats.temp_c`, `sensor.ats.humidity_pRH` | Temperature (°C) and relative humidity (%) |
| `sensor.baro.pressure_mbar`, `sensor.baro.altitude_m`, `sensor.baro.temp_c` | Pressure (mbar), pressure altitude (m), temperature of the pressure sensor (°C) |
| `sensor.imu.roll`, `sensor.imu.pitch` | Attitude; units and directions unknown |
| `sensor.als_Lux` | Ambient light (lux) |

The battery and CO fields are at the top level of the JSON; the others are under `sensor`. The message
also has counters for the system, data link, GPS and traffic (`uptime_s`, `gdl90`, `host`, `NMEA`,
`traffic` and others). The pressure sensor reads several degrees warmer than `sensor.ats.temp_c`, so
the `ats` value is the better cabin temperature. Attitude values swing widely and are not usable for
flight without calibration. `/stats?action=get` does not return JSON; use the WebSocket.

## Status feed (`/`)

The Sentry's status page opens a WebSocket at `ws://192.168.4.1/` that sends a JSON message once a
second: `clientCount`, `stationSsid`, `stationState`, and `status` with `latitude` and `longitude`
(degrees × 10⁷), `gnssAltitude` and `baroAltitude` (mm), `gpsFix`, `gpsSatellites`, `pressure`,
`temperature`, `NACp` and `NIC`. In Compatibility Mode `pressure` and `temperature` stay 0.

## Stored logs (`/data/`)

*Unconfirmed.* The Sentry records a log of its sensors about once a second while it is on.

- Each power-on starts a new file named `L<YYMMDDhhmmss>.csv`, once the clock is set. After a GPS fix it
  can be renamed to add the position (two signed numbers, likely degrees × 10⁷). The time zone of the
  name is unknown. A file grows until power-off.
- The newest 16 or so files are kept; older ones are deleted when a new file starts. Diagnostic logs
  (`M…log`) are pruned the same way. Downloading never deletes anything.
- Values are separated by a comma and a space; a row can end with an extra separator.

| Columns | Units |
|---|---|
| `Timestamp` | Unix time, seconds |
| `HeapAvailable`, `HeapLowWaterFree` | Bytes |
| `Sensor::battery_mV`, `Sensor::batteryCapacity_%` | mV, % |
| `Sensor::CO_Sensor_ppm`, `Sensor::CO_Sensor_Healthy`, `Sensor::CO_SensorTemp_C` | ppm, yes/no, °C |
| `Sensor::baroAlt_mm`, `Sensor::baroTemp_CE2` | mm, hundredths of °C |
| `Sensor::IMU::attX`, `attY`, `attZ` | Attitude; scale and axes unknown |
| `Sensor::IMU::offsetX`, `offsetY`, `offsetZ` | Offsets; units unknown |
| `Sensor::IMU::quaternionW`, `quaternionX`, `quaternionY`, `quaternionZ` | No units |
| `Sensor::IMU::accelX`, `accelY`, `accelZ` | Acceleration; units unknown |
| `Sensor::IMU::rateX`, `rateY`, `rateZ` | Rotation rate; units unknown |

Every IMU column has the full `Sensor::IMU::` prefix. Pressure is not logged, only pressure altitude.

## EFB discovery

EFBs, including ForeFlight, announce themselves with a UDP broadcast to port 63093, for example
`{"App":"ForeFlight","GDL90":{"port":4000}}`. Sentry Bridge's **Find EFB** lists them; it does not send
to an EFB just because it announced.

The Sentry also listens on port 63093. *Unconfirmed:* it recognizes only a few exact announcements
(ForeFlight and FliteDeck Pro, both on port 4000), so other app names, ports or formatting are ignored.
An announcement can add a device to its recipients, always on port 4000; it cannot move the data to
another port.

## Ports the Sentry listens on

*Unconfirmed.*

| Port | Use |
|---|---|
| TCP 80 | Web pages, settings, live feeds and firmware updates |
| UDP 4000 | Also accepts data from devices |
| UDP 63093 | EFB announcements |
| UDP 67 | Gives out addresses on its WiFi (DHCP) |
| UDP 68 | Gets an address when it joins another WiFi network |

No HTTPS, mDNS, SSH or Telnet service is known.

## Open questions

- **Weather (FIS-B):** no weather messages (`0x07`) have been seen yet, because no ground station was in
  range. If they arrive, Sentry Bridge passes them on unchanged (tested with simulated messages).
- **Attitude:** the units and directions of the `/stats` and log attitude values are unknown.
