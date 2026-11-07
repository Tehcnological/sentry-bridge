# Several EFBs at once: design

Status: approved design, not yet built. Target: two pull requests, described under Rollout.

## Goal

Send GDL90 to more than one EFB at the same time, for example:

- a laptop or phone running the bridge and feeding two tablets, or
- a tablet running the bridge and its own EFB, plus one or two other tablets.

Today the bridge sends to one route: This device (127.0.0.1) or one Other device address.

## Decisions

| Topic | Decision |
|-------|----------|
| Choosing EFBs | A This device switch plus a list of Other device addresses. Find EFB adds to the list. |
| Limit | Up to 16 Other device addresses, plus This device. |
| Port | Every destination uses the one EFB port from Settings. |
| Following a moved EFB | The current rule, applied to each address. |
| Status | Each address shows its own state; the main status and warnings are unchanged. |
| Rollout | Two pull requests: shared GUI, desktop and Android first, then iOS. |

## 1. Saved route and checks

The saved route becomes:

```json
{"host": "windows", "thisDevice": true, "addresses": ["192.168.4.15", "192.168.4.16"]}
```

It replaces `topology` and `address` in the desktop and Android settings, the GUI status API and
`web/app.js`.

- At most 16 addresses. Start needs at least one destination: This device on, or one address.
- Hosts with no EFB (`NO_EFB_HOSTS`, ESP32) refuse `thisDevice: true`.
- Each address is checked as today: unicast IPv4, not loopback, not a broadcast address, and not
  this device on the receiver port. Duplicates are refused. Every error starts with the address,
  for example `192.168.4.300: Enter a valid IPv4 address, not a hostname.`
- `validate_route` returns This device first, then the addresses in list order. The shared-port
  warning comes only from This device.
- Upgrade in `migrate()`: `{topology: "same"}` becomes `thisDevice: true, addresses: []`, and
  `{topology: "network", address: X}` becomes `thisDevice: false, addresses: [X]` (an empty X gives
  an empty list). The existing v0.1.6 `selections` upgrade runs first. Android does the same in
  Kotlin.
- When the follower moves an address, it replaces that entry in place and saves the route.
- An older app that reads the new shape reports "Choose where the EFB runs." and the user chooses
  again. Older apps are not changed.

## 2. Screen (shared web GUI: desktop and Android)

- A This device switch replaces the This device / Other device radio pair. It is hidden on
  Android (except the simulator), Linux and ESP32: EFBs there do not share the receiver port.
  This is a display rule only, as on Android today.
- Below it, the Other devices list. Each row shows the address, its state while running
  (answering, not answering, not seen yet) and a Remove button. Under the list are an address field
  and an Add button; Enter also adds. Check errors appear under the field. At 16 addresses Add is
  disabled with a note.
- Find EFB stays available while the list is shown. Its Use button becomes Add. A result already in
  the list shows Added and cannot be pressed.
- The summary line reads, for example, `UDP 4000 · This device + 2 others`. Check setup and the
  output summary list every route.
- List edits follow the address field's rule today: they apply at the next Start. Addresses moved by
  the follower show up in the list at once.
- While running, the status API carries `efbs: [{address, state}]`. EFB states never reach the
  Android notification or the iOS warnings.

## 3. Follower and states

One follower per run, as today. Every `EFB_CHECK_S` (30 s), while receiver data arrives:

1. Check every listed address at the same time, the way `discovery.sweep` probes, so a check lasts
   one probe timeout. This device is not checked and has no state.
2. States: `not seen yet` until an address first answers, then `answering`; `not answering` from
   its first missed check. States reset at every Start.
3. Following starts only when exactly one address that answered before has missed `EFB_MISSES` (2)
   checks in a row. The bridge sweeps the receiver's /24 network:
   - if the silent address answers the sweep, it stays (a short interruption);
   - if exactly one device answers that is not in the list, not this device and not the receiver,
     the silent address is replaced by it and the route is saved;
   - otherwise nothing moves, and the log says once which address stopped answering, the devices
     found, and "Choose one with Find EFB."
4. When two or more addresses are silent at once, nothing moves; the log says so once.
5. An address that never answered is never moved (a firewall may block the check).

Code: `tools/gui_server.py` `_follow_efb`, `tools/discovery.py` `follow_choice` (gains the
"not already in the list" rule), Android `NativeGui.watchEfb`.

## Rollout

### Pull request 1: shared GUI, desktop, Android

- `tools/routes.py`: new shape, checks, upgrade.
- `tools/discovery.py`, `tools/gui_server.py`: list follower, `efbs` in the status API.
- `web/app.js`, `web/app.html`, `web/app.css`: switch, list, Add and Remove, Linux hides This device.
- Android `NativeGui.kt` (`validateRoutes`, upgrade, `watchEfb`) and `BridgeService.kt` (its single
  `destination` and `followed` become a list).
- `tools/gui_runtime.py` already sends to a list of routes. The C core and CLI already take several
  destinations, and decoder output does not change, so the parity tests are unchanged.
- Docs: GUI-Controls, Compatibility (the follow paragraph), Android, and the README roadmap item.

### Pull request 2: iOS

- Settings: the single `destination` becomes the list, with an upgrade for older saved settings
  (the v0.3.0 settings in `ios/tests/WorkerSmoke.swift` cover it).
- `ios/SentryBridge/BridgeIO.c`: a list of destinations in place of one.
- `ios/SentryBridge/BridgeWorker.swift`: the list follower and states. SwiftUI: the switch and list.

## Testing

- Route checks: each error, duplicates, the 16 limit, This device on a no-EFB host, at least one
  destination, and the order of returned routes.
- Upgrade from `{topology: "same"}`, `{topology: "network"}` with and without an address, and the
  v0.1.6 `selections` shape.
- `follow_choice` with a list: the silent address recovers, one new device, a device already in the
  list, none, several.
- Follower with fake probes: one silent address moves, two silent addresses move nothing, a
  recovery, and an address never seen.
- The Android host diagnostics run the same route and upgrade cases, so Python and Kotlin agree.
- iOS: CI builds it and runs the adapter, host and simulator UI tests.
- Hardware, before each merge: two tablets from a laptop, then from a phone, and following after a
  receiver power cycle with two tablets listed. For iOS, a look on an iPad.

## Not included

- A port for each EFB. Every destination uses the EFB port from Settings.
- Broadcast output. Destinations stay unicast.
- Warnings or notifications when one EFB stops answering.
