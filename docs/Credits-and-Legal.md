# Credits and legal

Sentry Bridge is distributed under **GPL-3.0-only**, with an additional permission for app
stores (see [NOTICE](../NOTICE.txt)). Third-party components retain their licenses.
The full terms are in [LICENSE](../LICENSE).

## Prior work and components

- [foreflight-sentry-decryption](https://github.com/CA796CE/foreflight-sentry-decryption)
  documented Compatibility Mode, receiver web endpoints and wrapped 1090ES traffic.
  Its interface observations informed this project; no claim of vendor endorsement follows.
- [pyModeS](https://github.com/junzis/pyModeS/tree/v2.22.0), GPLv3, is the Python bridge's
  runtime decoder and the parity reference. The native decoder (`core/sbdecode.c`) was
  written clean-room from [Decoder-Spec.md](Decoder-Spec.md) by an author who did not read
  pyModeS or dump978; the parity tests compare outputs only.
- [mutability/dump978](https://github.com/mutability/dump978/blob/master/uat_decode.c)
  is the UAT field-layout reference cited by `uat.py`. That file is GPLv2 or later;
  any adaptation here is distributed under its version 3 option, with attribution retained.
- Kotlin standard library is Apache-2.0. Packaged Windows Python tools include
  CPython and its incorporated components under their respective terms.
- The Phosphor broadcast glyph is MIT; its full notice remains in web/app.html.
- The small-board firmware links ESP-IDF (Apache-2.0, with binary-only WiFi libraries) or the
  Raspberry Pi Pico SDK with cyw43-driver (Raspberry Pi devices only), plus FreeRTOS, lwIP,
  TinyUSB, newlib and others. A GPLv3 section 7 permission in NOTICE.txt allows that linking;
  the notices are in `firmware/notices/Third-Party.txt`.

Offline license texts are in Settings → Licenses. Build/test dependencies are
separate from the shipped runtime inventory; changing packaging requires another audit.

## Scope and safety

Not affiliated with or endorsed by uAvionix, ForeFlight or EFB vendors. Their names
identify products and remain their owners' trademarks. Compatibility Mode is used
through the receiver's settings interface. The bridge does not decrypt its encrypted
stream or install modified receiver firmware. These statements do not constitute
legal clearance of every use, trademark or patent.

Advisory only; not certified for navigation or collision avoidance. It does not
replace see-and-avoid, required equipment or a weather briefing. A warranty disclaimer
does not override mandatory law. See [Compatibility](Compatibility.md)
and [Privacy](Privacy.md).
