# How it works

```
                 UDP 4000                         UDP (GDL90)
  Sentry  ─────────────────────▶  Sentry Bridge  ─────────────▶  EFB app
  Compatibility Mode               decodes traffic               GPS + traffic
  0x00 0x0A 0x0B  standard         0x21 1090ES  ┐
  0x21 0x24       vendor wrapped   0x24 UAT     ┴▶ 0x14 traffic reports
```

## The Sentry's two modes

| | ForeFlight mode (default) | Compatibility Mode |
|---|---|---|
| GPS, heartbeat | Encrypted | Standard GDL90 |
| Traffic | Encrypted | Raw ADS-B in uAvionix wrappers (`0x21`, `0x24`) |
| AHRS, CO, baro, battery | Encrypted (ForeFlight only) | Not sent |
| Other EFB apps see | Nothing | GPS only, no traffic |

Compatibility Mode is a hidden setting (`publicGDL90`) in the Sentry's web interface. ForeFlight
recognizes it and shows a "Sentry in Compatibility Mode" banner with a Disable button. It resets to
ForeFlight mode on every power-up, so the bridge turns it back on (`--auto-enable`).

## What the bridge does

1. **Listens** on UDP 4000 for the Sentry's stream and passes standard frames through when relaying.
2. **Decodes 1090ES** (`0x21`): each frame wraps a 14-byte Mode S DF17/18 extended squitter.
   Positions use global CPR from even/odd pairs, or local CPR against the Sentry's own position.
3. **Decodes UAT** (`0x24`): each frame wraps a 48-byte UAT ADS-B message exactly as received,
   without error correction. The bridge applies Reed-Solomon RS(48,34) correction and drops anything
   uncorrectable, since uncorrected frames often look plausible but carry wrong data.
4. **Tracks targets** and sends one standard GDL90 Traffic Report (`0x14`) per aircraft per second.
   Positions are never extrapolated; a target stops being sent 15 s after its last position.
5. **Watches the mode.** If the Sentry comes up in ForeFlight mode after a power cycle, the bridge
   turns Compatibility Mode back on. If someone presses Disable in ForeFlight while it runs, the bridge
   only warns.
6. **Checks Power Savings**, which would shut the Sentry off mid-flight, and warns if it is on.

## One core, several platforms

| Component | Where it runs |
|---|---|
| `core/` (C99, no sockets, no allocation) | Inside the Windows/Linux/macOS binary and the Android app |
| `cli/` | Native command-line bridge (`sentry-bridge.exe`) |
| `android/` | Kotlin service and screen, calling the core through JNI |
| `bridge.py` | Python reference implementation and optional legacy headless service |
| `tools/gui_server.py` | Desktop controller, using the C core when available |

The test suite runs simulated traffic (a busy, lossy scenario with many aircraft, and UAT messages
with injected byte errors), structured fuzz and random input through both the Python bridge and the C
core (directly and through the Android JNI layer), and requires byte-identical output. The decoders
were also validated against real Sentry captures during development.
