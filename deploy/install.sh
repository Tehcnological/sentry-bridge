#!/usr/bin/env bash
# Install the bridge on a Raspberry Pi (Raspberry Pi OS Bookworm Lite, NetworkManager).
# Usage, from the project folder:  sudo ./deploy/install.sh "<Sentry SSID>"   (asks for the password)
set -euo pipefail

SSID="${1:?usage: sudo $0 <sentry-ssid>   (asks for the WiFi password)}"
if [ $# -ge 2 ]; then
  PSK="$2"
  echo "note: a password given on the command line stays in shell history; omit it to be asked." >&2
else
  # Not echoed and not in shell history. Enter for an open network.
  read -rsp "WiFi password for $SSID (Enter for an open network): " PSK
  echo
fi
DIR=/opt/sentry-bridge
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "== packages"
apt-get update -qq
apt-get install -y -qq python3-venv

echo "== files to $DIR"
install -d "$DIR"
install -m 0644 "$SRC"/gdl90.py "$SRC"/uat.py "$SRC"/requirements.txt "$SRC"/LICENSE "$SRC"/NOTICE.txt "$DIR"/
install -d "$DIR/notices"
install -m 0644 "$SRC"/notices/*.txt "$DIR/notices/"
install -m 0755 "$SRC"/bridge.py "$SRC"/capture.py "$SRC"/sentry_ctl.py "$SRC"/sim.py "$SRC"/ws_probe.py "$DIR"/
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "== WiFi profile 'sentry' for SSID $SSID"
nmcli -t -f NAME con show | grep -qx sentry && nmcli con delete sentry >/dev/null
if [ -n "$PSK" ]; then
  # shortcut: nmcli takes the password as an argument, visible in ps for this one call during
  # install; write a 0600 keyfile under /etc/NetworkManager instead if the Pi has other accounts.
  nmcli con add type wifi ifname wlan0 con-name sentry ssid "$SSID" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" >/dev/null
else
  nmcli con add type wifi ifname wlan0 con-name sentry ssid "$SSID" >/dev/null
fi
# Prefer the Sentry whenever it is in range. Power save off keeps UDP latency low.
nmcli con modify sentry connection.autoconnect yes connection.autoconnect-priority 100 \
  802-11-wireless.powersave 2

echo "== service"
[ -f /etc/default/sentry-bridge ] || install -m 0644 "$SRC"/deploy/sentry-bridge.default /etc/default/sentry-bridge
install -m 0644 "$SRC"/deploy/sentry-bridge.service /etc/systemd/system/sentry-bridge.service
systemctl daemon-reload
systemctl enable --now sentry-bridge

echo
echo "Done. Edit /etc/default/sentry-bridge: set --dest to the tablet's IPv4 address (required, no"
echo "broadcast), optionally --ownship-icao. Then: sudo systemctl restart sentry-bridge"
echo "Logs: journalctl -u sentry-bridge -f"
echo "Recommended for cockpit power cuts: sudo raspi-config nonint enable_overlayfs  (then reboot)"
