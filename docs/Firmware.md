# Small-board firmware

A small WiFi board can run the bridge next to the receiver, so EFBs on any phone or tablet get
traffic with no app on that device and nothing to set up. This is the way to use Android EFBs:
on Android, an EFB and a bridge app cannot share UDP 4000 on the same phone.

**Status:** builds from source for all three boards; not yet tested with a receiver and not yet
part of a release.

## Supported boards

| Board | File | Status LED |
|---|---|---|
| ESP32-C3 (for example a C3 SuperMini) | `sentry-bridge-esp32c3.bin` | GPIO 8 (blue LED on SuperMini boards) |
| Raspberry Pi Pico W | `sentry-bridge-pico-w.uf2` | onboard LED |
| Raspberry Pi Pico 2 W | `sentry-bridge-pico2-w.uf2` | onboard LED |

## What it does

1. Joins the strongest open WiFi network whose name starts with **Sentry**.
2. Listens on UDP 4000, where the receiver sends Compatibility Mode GDL90 to every connected device.
3. Decodes 1090ES and UAT traffic and broadcasts GDL90 traffic reports to the receiver's network on
   UDP 4000, once per second. EFBs keep getting the receiver's own GPS, heartbeat and ownship
   frames directly, so the board sends only traffic.
4. Reads the receiver's status every 30 seconds (every 5 while it is silent) so it keeps streaming,
   and turns Compatibility Mode back on when the receiver comes up in ForeFlight mode after a power
   cycle (up to 3 tries, 20 seconds apart).

The LED is on while receiver data arrives.

## Install

**Pico W and Pico 2 W:** hold **BOOTSEL** while plugging the board into a computer. It appears as
a USB drive (`RPI-RP2` or `RP2350`). Copy the `.uf2` file for your board onto it; the board
restarts by itself.

**ESP32-C3:** in Chrome or Edge, open [esptool-js](https://espressif.github.io/esptool-js/),
connect to the board, set the flash address to **0x0**, choose `sentry-bridge-esp32c3.bin` and
program it. Some boards need **BOOT** held while plugging in. From a terminal:

```bash
esptool.py --chip esp32c3 write_flash 0x0 sentry-bridge-esp32c3.bin
```

## Use

1. Power the board from any USB port near the receiver.
2. In the receiver's web settings, disable **Power Savings**.
3. On each EFB device, join the receiver's WiFi and choose the EFB's Stratux or GDL90 input on
   UDP 4000.

## Limits

- Traffic is broadcast to every device on the receiver's WiFi.
- Your own aircraft is not filtered out. Set your aircraft (tail number or ICAO address) in the EFB
  so it hides it.
- There is no setup page yet. The receiver address (192.168.4.1) and port (4000) are fixed.
- Status output is on the board's USB serial port, at any baud rate.

## Licenses

The firmware is GPL-3.0-only like the rest of Sentry Bridge, with a section 7 permission in
`NOTICE.txt` for linking the vendors' WiFi libraries and firmware, which are not available as
source. The third-party notices for each image are in `firmware/notices/Third-Party.txt`.

## Build

`tools/build_firmware.py` builds all three images into `dist/` on Linux or WSL, with ESP-IDF 5.4.1,
pico-sdk 2.1.1 and the Arm GNU toolchain 14.2 under `~/fw` (see its help). It also writes
`dist/firmware-report.json` with the source and image hashes. The board-independent loop
(`firmware/common`) is covered by `tests/test_firmware_common.py`.
