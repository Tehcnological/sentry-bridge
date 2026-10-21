# How it works

```
                 UDP 4000                          UDP (GDL90)
  Sentry  ─────────────────────▶  Sentry Bridge  ─────────────▶  EFB app
  (Compatibility Mode)             decodes traffic                GPS + traffic
```

## The Sentry's two modes

| | ForeFlight mode (default) | Compatibility Mode |
|---|---|---|
| GPS | Encrypted, ForeFlight only | Standard GDL90, any EFB |
| Traffic | Encrypted, ForeFlight only | Raw ADS-B messages that EFBs cannot read |
| AHRS, CO, pressure, battery | ForeFlight only | Not sent to apps |

Compatibility Mode is a setting in the Sentry's web interface. It resets to ForeFlight mode at every
power-up, so Sentry Bridge switches it back on when that happens. ForeFlight also works in
Compatibility Mode; it shows a banner with a **Disable** button. If someone presses Disable while the
bridge runs, the bridge warns instead of switching it back.

## What the bridge does

1. **Receives** the Sentry's data on UDP port 4000.
2. **Decodes traffic** from both ADS-B bands: 1090 MHz (1090ES) and 978 MHz (UAT). UAT messages
   arrive without error correction, so the bridge corrects them and drops any it cannot correct;
   corrupted messages can look valid but carry wrong positions.
3. **Sends** one standard GDL90 traffic report per aircraft per second to the EFB. Positions are never
   predicted; an aircraft is dropped 15 seconds after its last position.
4. **Keeps the Sentry in Compatibility Mode** after power cycles, and warns if Power Savings is on.
5. **Shows the Sentry's battery, CO and temperature** on its own screen. The Sentry publishes these on
   a separate statistics feed, not in the data EFBs receive.

## One decoder, several apps

The traffic decoder is written once, in C (`core/`), and used by every app:

| Part | Used by |
|---|---|
| `core/` | All apps: Windows, Linux, macOS, Android, iPhone and iPad, small boards |
| `cli/` | The command-line bridge (`sentry-bridge`) |
| `android/`, `ios/` | The phone and tablet apps |
| `tools/gui_server.py`, `web/` | The desktop app; Android shares the same screens |
| `bridge.py` | A Python version used as the reference in tests |

Tests feed simulated traffic, corrupted messages and random data through both the Python and the C
decoders and require identical output.
