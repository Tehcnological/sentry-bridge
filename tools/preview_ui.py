"""Serve the offline interface prototype on loopback; never controls the receiver or bridge."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .routes import validate_route
except ImportError:
    from routes import validate_route

ROOT = Path(__file__).resolve().parent.parent
ASSETS = {"/": ("web/app.html", "text/html; charset=utf-8"),
          "/app.html": ("web/app.html", "text/html; charset=utf-8"),
          "/app.css": ("web/app.css", "text/css; charset=utf-8"),
          "/app.js": ("web/app.js", "text/javascript; charset=utf-8"),
          "/legal.js": ("web/legal.js", "text/javascript; charset=utf-8"),
          "/privacy.html": ("web/privacy.html", "text/html; charset=utf-8"),
          "/legal.html": ("web/legal.html", "text/html; charset=utf-8"),
          "/favicon.svg": ("web/favicon.svg", "image/svg+xml")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def route_settings(self):
        """The ports a route check uses; the live GUI uses the bridge's saved settings."""
        return {"listenPort": 4000, "efbPort": 4000}

    body_read = False

    def read_body(self, size):
        self.body_read = True
        return self.rfile.read(size)

    def discard_body(self):
        """Read a rejected request's declared body before replying. Closing the socket while the
        body is still arriving resets the connection, and the client loses the reply
        (WinError 10053 on Windows). Bodies over 64 KiB are not read; the connection closes."""
        if self.body_read or self.command != "POST":
            return
        self.body_read = True
        self.close_connection = True
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if 0 < size <= 64 * 1024:
            try:
                self.connection.settimeout(3)
                self.rfile.read(size)
            except OSError:
                pass

    def reply(self, status, data, content_type="application/json; charset=utf-8", head=False):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        if status >= 400:
            self.discard_body()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def allowed_origin(self):
        return self.headers.get("Origin") in {None, f"http://127.0.0.1:{self.server.server_port}",
                                             f"http://localhost:{self.server.server_port}"}

    def do_GET(self):
        self.get_response()

    def do_HEAD(self):
        self.get_response(head=True)

    def get_response(self, head=False):
        if not self.allowed_host():
            self.reply(403, {"error": "Loopback host required."}, head=head)
            return
        path = urlsplit(self.path).path
        if path == "/api/status":
            self.reply(200, {"adapter": "preview"}, head=head)
        elif path in ASSETS:
            name, content_type = ASSETS[path]
            self.reply(200, (ROOT / name).read_bytes(), content_type, head=head)
        else:
            self.reply(404, {"error": "Not found."}, head=head)

    def do_POST(self):
        if not self.allowed_host() or not self.allowed_origin():
            self.reply(403, {"error": "Loopback origin required."})
            return
        if self.path != "/api/validate":
            self.reply(404, {"error": "Not found."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Use a JSON request of at most 8192 bytes.")
            self.connection.settimeout(3)
            request = json.loads(self.read_body(size))
            result = validate_route(request, self.route_settings())
        except (ValueError, OSError) as error:
            self.reply(400, {"error": str(error)})
            return
        self.reply(200, result)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"Interface preview: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
