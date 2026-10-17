#!/usr/bin/env bash
# Install only reviewed public sources; no Git checkout, captures or signing files.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo.'; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DIR=/opt/sentry-bridge
apt-get update -qq
apt-get install -y python3-venv build-essential cmake avahi-daemon avahi-utils
id sentry-bridge >/dev/null 2>&1 || useradd --system --home /var/lib/sentry-bridge --shell /usr/sbin/nologin sentry-bridge
install -d "$DIR" "$DIR/tools" "$DIR/web" "$DIR/deploy" "$DIR/core" /var/lib/sentry-bridge
for file in bridge.py gdl90.py uat.py sentry_ctl.py capture.py sim.py ws_probe.py requirements.txt LICENSE NOTICE.txt; do
    install -m 0644 "$SRC/$file" "$DIR/"
done
for file in gui_server.py gui_runtime.py native_core.py routes.py preview_ui.py pi_server.py check_weather.py discovery.py; do
    install -m 0644 "$SRC/tools/$file" "$DIR/tools/"
done
install -d "$DIR/notices"
install -m 0644 "$SRC"/notices/*.txt "$DIR/notices/"
install -m 0644 "$SRC"/web/{privacy,legal}.html "$SRC/web/legal.js" "$DIR/web/"
install -m 0644 "$SRC"/web/app.{html,css,js} "$SRC/web/favicon.svg" "$DIR/web/"
install -m 0644 "$SRC"/core/sbcore.{c,h} "$DIR/core/"
cc -shared -fPIC -O2 -o "$DIR/libsbcore.so" "$DIR/core/sbcore.c" -lm
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"
if [ ! -e /var/lib/sentry-bridge/web-password ]; then
    # Created owner-only (0600) in one step, so the password is never readable by other accounts.
    "$DIR/venv/bin/python" -c 'import os, secrets; fd = os.open("/var/lib/sentry-bridge/web-password", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, ("bridge:" + secrets.token_urlsafe(24) + "\n").encode()); os.close(fd)'
fi
chmod 0600 /var/lib/sentry-bridge/web-password
chown sentry-bridge:sentry-bridge /var/lib/sentry-bridge /var/lib/sentry-bridge/web-password
install -m 0644 "$SRC/deploy/publish-mdns.py" "$DIR/deploy/"
install -m 0644 "$SRC"/deploy/sentry-bridge-{web,mdns}.service /etc/systemd/system/
systemctl disable --now sentry-bridge.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now avahi-daemon sentry-bridge-web sentry-bridge-mdns
echo 'Open http://sentrybridge.local/ (or the Pi WiFi IP).'
echo 'Login:'
cat /var/lib/sentry-bridge/web-password
