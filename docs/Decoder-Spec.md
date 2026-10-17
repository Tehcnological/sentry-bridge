# Decoder specification (clean-room input)

This is the only input for writing `core/sbdecode.c`. It describes what the 1090ES and
UAT decoders must compute: field positions from the published message formats (ICAO
Doc 9871 / DO-260B for 1090ES, DO-282B for UAT) and the exact numeric behaviour the
parity tests require. It contains no code from any other decoder.

The author of `core/sbdecode.c` must not read `core/sbcore.c`, `bridge.py`, `uat.py`,
`sim.py`, pyModeS, dump978 or any other ADS-B decoder source while writing it.

## Conventions

- Bit numbering: bit 0 is the most significant bit of byte 0. A field "a..b" spans bits
  a to b inclusive, most significant first, read as an unsigned integer.
- `floor` is the C library `floor`. `mod(a, n)` for integers is the floor modulo: the
  result is in `[0, n)` for positive `n`, also for negative `a`.
- `fmod360(x)` for a double: `r = fmod(x, 360.0)`; if `r != 0` and `r < 0`, add `360.0`.
- `DEG` is the double constant `180.0 / M_PI`; convert radians to degrees by multiplying
  by `DEG`. Evaluate every formula in the written order and grouping, in double precision:
  the results must match a reference bit for bit.
- Everything in `sbdecode.c` is `static` and prefixed `sbd_`; the file is included by
  `core/sbcore.c` (`#include "sbdecode.c"`) and must compile on its own as C99 with
  `<stdint.h>`, `<stddef.h>`, `<string.h>` and `<math.h>` (define `M_PI` if missing).
  No heap, no globals that change, no I/O.

## Interface

```c
typedef struct {
    int has_spd;  double spd;   /* knots */
    int has_trk;  double trk;   /* degrees */
    int has_vs;   int32_t vs;   /* feet per minute */
    int gs_type;                /* 1 = ground speed / true track, 0 = airspeed / heading */
} sbd_velocity;

typedef struct {
    int payload_type, qualifier, fec_errors;  /* fec_errors: left 0, the caller sets it */
    uint32_t address;
    int has_pos;  double lat, lon;
    int has_alt, alt_geo, has_alt2;  int32_t alt_ft, alt2_ft;
    int nic, airground;
    int has_gs, has_trk, has_vs, track_type;
    double gs_kt, track_deg;  int32_t vs_fpm;
    int has_ms, emitter, emergency, nacp, callsign_is_flight_id;
    char callsign[9];
} sbd_uat;

static int  sbd_nl(double lat);
static int  sbd_typecode(const uint8_t *msg);
static int  sbd_cpr_odd(const uint8_t *msg);
static void sbd_callsign(const uint8_t *msg, char out[9]);
static int  sbd_altitude(const uint8_t *msg, int tc, int32_t *feet);
static int  sbd_cpr_global(const uint8_t *even, const uint8_t *odd, double t_even, double t_odd,
                           double *lat, double *lon);
static void sbd_cpr_local(const uint8_t *msg, double span, double lat_ref, double lon_ref,
                          double *lat, double *lon);
static void sbd_airborne_velocity(const uint8_t *msg, sbd_velocity *v);
static void sbd_surface_velocity(const uint8_t *msg, sbd_velocity *v);
static int  sbd_uat_mdb(const uint8_t *mdb, size_t len, sbd_uat *r);
```

`msg` is a 14-byte 1090ES message (DF17/18). Its ME field is message bits 32..87; below,
"ME bit k" means message bit 32 + k.

## 1090ES

### Type code and CPR format
- `sbd_typecode`: ME bits 0..4.
- `sbd_cpr_odd`: ME bit 21 (0 = even, 1 = odd).

### NL (number of longitude zones)
`sbd_nl(lat)`:
1. If `fabs(lat) <= 1e-08`: 59.
2. Else if `fabs(fabs(lat) - 87) <= 1e-08 + 1e-05 * 87`: 2.
3. Else if `lat > 87 || lat < -87`: 1.
4. Else: `a = 1 - cos(M_PI / (2 * 15))`, `b = pow(cos(M_PI / 180 * fabs(lat)), 2)`,
   result `floor(2 * M_PI / acos(1 - a / b))` as an int.

### Identification (type codes 1-4)
Eight 6-bit characters at ME bits 8..55. Character values: 1-26 are `A`-`Z`, 32 is space,
48-57 are `0`-`9`; every other value is invalid. `sbd_callsign` writes the valid
characters in order, leaving out invalid values **and spaces**, then a terminating NUL.

### Altitude (type codes 9-18 and 20-22)
`raw` = ME bits 8..19 (12 bits). Return 1 and set `*feet`, or 0 for "no altitude".
- Type codes 20-22 (GNSS height, metres): `*feet = (int32_t)(raw * 3.28084)`; return 1.
- Type codes 9-18 (barometric): form a 13-bit code by inserting a 0 bit (the M bit) after
  the first six bits of `raw`. Label the 13 bits, most significant first:
  `C1 A1 C2 A2 C4 A4 M B1 Q B2 D2 B4 D4`.
  - All 13 bits zero: return 0.
  - Q = 1: drop M and Q, read the remaining 11 bits as `n`; altitude `n * 25 - 1000`.
  - Q = 0 (Gillham code): `g500` = the 8 bits `D2 D4 A1 A2 A4 B1 B2 B4` and `g100` = the
    3 bits `C1 C2 C4`, each a Gray code; convert both to binary (`n500`, `n100`).
    If `n100` is 0, 5 or 6: return 0. If `n100` is 7, use 5. If `n500` is odd,
    `n100 = 6 - n100`. Altitude `n500 * 500 + n100 * 100 - 1300`.

### CPR fields
`YZ` = ME bits 22..38 divided by `131072.0`; `XZ` = ME bits 39..55 divided by `131072.0`.

### Global airborne position
`sbd_cpr_global(even, odd, t_even, t_odd, ...)` with `YZ0, XZ0` from `even` and `YZ1, XZ1`
from `odd`. Return 1 and set the position, or 0.
1. `j = (long)floor(59 * YZ0 - 60 * YZ1 + 0.5)`.
2. `lat0 = (360.0 / 60) * (mod(j, 60) + YZ0)`, `lat1 = (360.0 / 59) * (mod(j, 59) + YZ1)`.
   Subtract 360 from either if it is `>= 270`.
3. If `sbd_nl(lat0) != sbd_nl(lat1)`: return 0.
4. If `t_even > t_odd`: `lat = lat0`, `nl = sbd_nl(lat)`, `ni = max(nl, 1)`, `XZ = XZ0`.
   Otherwise: `lat = lat1`, `nl = sbd_nl(lat)`, `ni = max(nl - 1, 1)`, `XZ = XZ1`.
5. `m = (long)floor(XZ0 * (nl - 1) - XZ1 * nl + 0.5)` (the same formula in both cases).
6. `lon = (360.0 / ni) * (mod(m, ni) + XZ)`; if `lon > 180`, subtract 360.

### Local position (airborne and surface)
`sbd_cpr_local(msg, span, lat_ref, lon_ref, ...)`; `span` is 360.0 (airborne) or 90.0
(surface); `F` = `sbd_cpr_odd(msg)`.
1. `dlat = F ? span / 59 : span / 60`.
2. `j = (long)floor(0.5 + lat_ref / dlat - YZ)`; `lat = dlat * (j + YZ)`.
3. `ni = sbd_nl(lat) - F`; `dlon = ni > 0 ? span / ni : span`.
4. `m = (long)floor(0.5 + lon_ref / dlon - XZ)`; `lon = dlon * (m + XZ)`.
5. If `lon > 180` subtract 360, else if `lon < -180` add 360 (once).

### Airborne velocity (type code 19)
Start with every field zero. `sub` = ME bits 5..7.
- `sub` 1 or 2 (ground speed): `gs_type = 1`. `ew` = ME bits 14..23, `ns` = ME bits 25..34.
  If both are non-zero: subtract 1 from each; multiply each by 4 if `sub == 2`; negate `ew`
  if ME bit 13 is 1 and `ns` if ME bit 24 is 1 (call them `we` and `sn`, as longs).
  `spd = (double)(long)sqrt((double)(sn * sn + we * we))` (truncated), `has_spd = 1`.
  `t = atan2((double)we, (double)sn) * DEG`; `trk = t >= 0 ? t : t + 360`, `has_trk = 1`.
- Any other `sub` (airspeed): `gs_type = 0`. If ME bit 13 is 1: `trk = (ME bits 14..23)
  / 1024.0 * 360.0`, `has_trk = 1`. `as` = ME bits 25..34; if non-zero: `as - 1`, times 4
  if `sub == 4`, as `spd` (double), `has_spd = 1`.
- Vertical rate, for every `sub`: `vr` = ME bits 37..45; if non-zero:
  `vs = (ME bit 36 ? -1 : 1) * (vr - 1) * 64`, `has_vs = 1`.

### Surface velocity (type codes 5-8)
Start with every field zero; `gs_type` stays 0. `mov` = ME bits 5..11.
- If ME bit 12 is 1: `trk = (ME bits 13..19) * 360.0 / 128`, `has_trk = 1`.
- If `mov == 0` or `mov > 124`: no speed. Otherwise `has_spd = 1` and:
  `mov == 1`: 0.0; `mov == 124`: 175.0; otherwise from the first matching range,
  `spd = base + (mov - first) * step`:

  | mov | first | base | step |
  |---|---|---|---|
  | 2-8 | 2 | 0.125 | 0.125 |
  | 9-12 | 9 | 1 | 0.25 |
  | 13-38 | 13 | 2 | 0.5 |
  | 39-93 | 39 | 15 | 1 |
  | 94-108 | 94 | 70 | 2 |
  | 109-123 | 109 | 100 | 5 |

## UAT ADS-B message data block

`sbd_uat_mdb(mdb, len, r)`: `len` is 18 (basic) or 34 (long), already error-corrected.
Clear `*r` first. Return 0 to reject, 1 otherwise.

| Field | Bits | Use |
|---|---|---|
| payload type | 0..4 | reject if > 10, or if `len == 18` and not 0 |
| address qualifier | 5..7 | `qualifier` |
| address | 8..31 | `address` |
| latitude | 32..54 (23) | |
| longitude | 55..78 (24) | |
| altitude type | 79 | `alt_geo` |
| altitude | 80..91 (12) | |
| NIC | 92..95 | `nic` |
| air/ground state | 96..97 | `airground` |
| north/south (or ground speed) | 99..109 (11) | |
| east/west (or track) | 110..120 (11) | |
| vertical rate | 121..131 (11) | |

- Position: `rlat`, `rlon` as read. If either is non-zero: `has_pos = 1`,
  `lat = rlat * 360.0 / 16777216.0` minus 180 if `> 90.0`, and
  `lon = rlon * 360.0 / 16777216.0` minus 360 if `> 180.0`.
- Altitude: raw 0 means none; else `alt_ft = (raw - 1) * 25 - 1000`, `has_alt = 1`.
- Air/ground 0 or 1 (airborne; 1 = supersonic). In each 11-bit velocity field the top bit
  is the sign (1 = south / west) and the low 10 bits the magnitude.
  - If both magnitudes are non-zero: `mult` = 4 if supersonic else 1;
    `ns = (mag_ns - 1) * mult * (sign_ns ? -1 : 1)`, the same for `ew` (as longs);
    `gs_kt = hypot((double)ns, (double)ew)`, `has_gs = 1`; and if `ns` or `ew` is non-zero:
    `track_deg = fmod360(atan2((double)ew, (double)ns) * DEG)`, `has_trk = 1`, `track_type = 1`.
  - Vertical rate field: bit 121 is the source (ignored), bit 122 the sign (1 = down),
    bits 123..131 the magnitude. If the magnitude is non-zero:
    `vs_fpm = (mag - 1) * 64 * (sign ? -1 : 1)`, `has_vs = 1`.
- Air/ground 2 (on the ground): ground speed = low 10 bits of bits 99..109; if non-zero,
  `gs_kt = mag - 1`, `has_gs = 1`. In bits 110..120, bits 110..111 are the track type
  and bits 112..120 the angle; if the type is non-zero: `track_deg = angle * 360.0 / 512.0`,
  `has_trk = 1`, `track_type` = the type.
- Air/ground 3: no velocity.
- Long blocks only (`len == 34`):
  - Payload types 1 and 3 (mode status), `has_ms = 1`. Three 16-bit words: `w0` = bits
    136..151, `w1` = 152..167, `w2` = 168..183. Base-40 alphabet: values 0-9 are `0`-`9`,
    10-35 are `A`-`Z`, 36 and 37 are space, 38 and 39 are `.`.
    `emitter = (w0 / 1600) % 40`; the callsign is the characters for `(w0 / 40) % 40`,
    `w0 % 40`, then for each of `w1` and `w2`: `(w / 1600) % 40`, `(w / 40) % 40`, `w % 40`
    (8 characters), with trailing spaces removed.
    `emergency` = bits 184..186, `nacp` = bits 200..203,
    `callsign_is_flight_id` = bit 214.
  - Payload types 1, 2, 5 and 6: secondary altitude = bits 232..243, decoded like the
    altitude above into `alt2_ft` / `has_alt2`.

## Provenance

The 1090ES and UAT field decoders in `core/sbdecode.c` were written from this document
alone by an author who did not read `core/sbcore.c`, `bridge.py`, `uat.py`, pyModeS or
dump978. Numeric behaviour is checked against the Python reference by the parity tests,
which compare outputs only.
