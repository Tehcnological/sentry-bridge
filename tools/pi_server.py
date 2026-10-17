"""Authenticated Raspberry Pi web controller using the shared native-core GUI runtime."""
import argparse
import base64
import hmac
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.gui_server import Controller, Handler


class PiController(Controller):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.startup_path = self.data_dir / "pi-startup.json"
        self.auto_start = True
        if self.startup_path.exists():
            try:
                self.auto_start = json.loads(self.startup_path.read_text())["autoStart"] is True
            except (ValueError, KeyError, OSError):
                self.auto_start = False
        if self.auto_start and self.routes_request:
            try:
                if self.routes_request.get("topology") != "network":
                    raise ValueError("Select Other device and save an EFB destination before automatic startup.")
                from tools.routes import validate_route
                self.runtime.start(validate_route(self.routes_request, self.runtime.settings)["routes"])
            except (ValueError, OSError) as error:
                self.runtime.view["error"] = str(error)

    def status(self):
        result = super().status()
        result["host"] = "linux" if self.simulator else "pi"
        result["service"] = "pi"
        result["autoStart"] = self.auto_start
        return result

    def action(self, name, request):
        if name == "autostart":
            if not isinstance(request.get("enabled"), bool):
                raise ValueError("Start on boot must be on or off.")
            self.auto_start = request["enabled"]
            temporary = self.startup_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"autoStart": self.auto_start}))
            temporary.replace(self.startup_path)
            return {"saved": True}
        if name == "quit":
            raise ValueError("Use Stop to stop forwarding. The Pi web service stays available.")
        if name == "start" and not self.simulator and request.get("topology") != "network":
            raise ValueError("Select Other device and enter the EFB device's address.")
        return super().action(name, request)


class PiHandler(Handler):
    def allowed_host(self):
        return self.headers.get("Host") in self.server.allowed_hosts

    def allowed_origin(self):
        return self.headers.get("Origin") in {None, "http://" + self.headers.get("Host", "")}

    def authenticate(self):
        # Compare bytes: compare_digest rejects non-ASCII str, and http.server decodes headers as Latin-1.
        supplied = self.headers.get("Authorization", "").encode("latin-1", "replace")
        expected = b"Basic " + base64.b64encode(self.server.credentials.encode())
        if self.allowed_host() and hmac.compare_digest(supplied, expected):
            return True
        self.discard_body()  # see PreviewHandler.discard_body
        self.send_response(401 if self.allowed_host() else 403)
        self.send_header("WWW-Authenticate", 'Basic realm="Sentry Bridge", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def session_ok(self):
        return True  # every request already passed HTTP Basic authentication

    def get_response(self, head=False):
        if self.authenticate():
            super().get_response(head)

    def do_POST(self):
        if self.authenticate():
            super().do_POST()


def local_names():
    names = {"sentrybridge.local", socket.gethostname(), socket.gethostname() + ".local", "localhost", "127.0.0.1"}
    try:
        result = subprocess.run(["ip", "-json", "-4", "address", "show"], capture_output=True, timeout=3, check=True)
        for interface in json.loads(result.stdout):
            names.update(a["local"] for a in interface.get("addr_info", []) if a.get("family") == "inet")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/sentry-bridge"))
    parser.add_argument("--credentials", type=Path, default=Path("/var/lib/sentry-bridge/web-password"))
    args = parser.parse_args(argv)
    credentials = args.credentials.read_text().strip()
    if not credentials.startswith("bridge:") or len(credentials) < 20:
        raise ValueError("Create a private web-password file containing bridge:<random password>.")
    controller = PiController(args.data_dir)
    try:
        with ThreadingHTTPServer((args.bind, args.port), PiHandler) as server:
            server.controller, server.credentials = controller, credentials
            server.allowed_hosts = {f"{name}:{server.server_port}" for name in local_names()}
            if server.server_port == 80:
                server.allowed_hosts.update(local_names())
            stop = threading.Event()
            def refresh_addresses():
                while not stop.wait(10):
                    names = local_names()
                    server.allowed_hosts = {f"{name}:{server.server_port}" for name in names}
                    if server.server_port == 80:
                        server.allowed_hosts.update(names)
            threading.Thread(target=refresh_addresses, daemon=True).start()
            port_text = "" if server.server_port == 80 else f":{server.server_port}"
            print(f"Sentry Bridge: http://sentrybridge.local{port_text}/", flush=True)
            try:
                server.serve_forever()
            finally:
                stop.set()
    finally:
        controller.close()


if __name__ == "__main__":
    main()
