"""Local GUI adapter for bridge, sentry-ctl, capture and diagnostic tools.

The same web assets also run as an offline design preview. This adapter is an actual
bridge; its HTTP control surface deliberately binds only to loopback.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
import gzip
import hmac
import io
import ipaddress
import json
from pathlib import Path
import re
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
import uuid
import webbrowser
import zipfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import sentry_ctl
from tools.discovery import ANNOUNCE_TTL_S, parse_announce
from tools.routes import check_endpoint, migrate, validate_route
from tools.gui_runtime import CAPTURE_LIMIT, DEFAULTS, DISCOVERY_LIMIT, Runtime, Simulator, number, udp_listener, validate_settings
from tools.preview_ui import Handler as PreviewHandler

AUTO_BACKUP = "sentry-auto-backup.zip"
# Update check: GitHub's latest stable release, at most once a day, only while the bridge is stopped.
RELEASES = "https://api.github.com/repos/Tehcnological/sentry-bridge/releases/latest"
UPDATE_INTERVAL_S, UPDATE_RETRY_S = 24 * 3600, 3600


def app_version():
    return re.search(r'#define SB_VERSION "([0-9.]+)"', (ROOT / "core/sbcore.h").read_text(encoding="utf-8")).group(1)


def newer(tag, current):
    """Whether release tag vX.Y.Z is newer than X.Y.Z. Anything else (a pre-release suffix) is not."""
    try:
        return tuple(map(int, tag.removeprefix("v").split("."))) > tuple(map(int, current.split(".")))
    except (AttributeError, ValueError):
        return False
AUTO_BACKUPS_KEPT = 5  # automatic restoration backs up on every receiver power-up


def replace_file(temporary, target, attempts=10):
    """os.replace, retried for a moment: on Windows a reader of the destination (a scanner, or a
    second launch reading the session token) makes it fail with PermissionError until it closes."""
    for attempt in range(attempts):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(.05 * (attempt + 1))


class LoopbackServer(ThreadingHTTPServer):
    """The GUI's HTTP server: one instance per port. http.server sets SO_REUSEADDR, which on
    Windows lets a second socket bind a port that is in use, so a second GUI could share it."""
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


class Closing(ValueError):
    """Exit was requested. A receiver job stops at its next stage; this is not a job failure to
    work around (automatic restoration must not continue after its backup is cancelled)."""


class Controller:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            # Captures hold flight tracks and backups the receiver settings: this account only,
            # including a folder an earlier version created with the default umask.
            try:
                self.data_dir.chmod(0o700)
            except PermissionError:
                pass  # a folder owned by another account keeps its mode; its owner controls access
        self.lock = threading.RLock()
        self.closing = threading.Event()  # Exit was requested: running jobs stop at their next check
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gui-tool")
        self.job = dict(state="idle", action=None, result=None, error=None)
        self.artifacts = {}
        self.index_path = self.data_dir / "gui-files.json"
        if self.index_path.exists():
            try:
                index = json.loads(self.index_path.read_text(encoding="utf-8"))
                for token, item in list(index.items())[:100]:
                    name = item["name"]
                    if not re.fullmatch(r"[a-f0-9]{32}", token) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
                        continue
                    path = self.data_dir / (token + "-" + name)
                    if path.is_file():
                        self.artifacts[token] = dict(path=path, name=name, kind=item["kind"], complete=True)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.artifacts = {}
        self.device = None
        self.mode_observation = None
        self.mode_requested = None
        self.device_logs = []
        self.simulator = None
        self.capture_id = None
        self.update_path = self.data_dir / "update-check.json"
        try:
            saved = json.loads(self.update_path.read_text(encoding="utf-8"))
            self.update, self.update_due = saved.get("update"), float(saved["checked"]) + UPDATE_INTERVAL_S
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.update, self.update_due = None, 0.0
        self.update_checking = False
        self.update_lock = threading.Lock()  # status() runs on several HTTP threads
        try:
            self.version = app_version()
        except (OSError, AttributeError):
            self.version = None  # no version to compare: no notice
        self.config_path = self.data_dir / "gui-settings.json"
        self.routes_request = None
        settings = DEFAULTS
        upgrade_note = None
        if self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                routes, raw, upgrade_note = migrate(saved.get("routes"), saved["settings"])
                settings = validate_settings(raw)
            except (OSError, ValueError, KeyError, TypeError):
                settings, routes = DEFAULTS, None
            if routes:
                try:  # an unusable route costs only the route, never the receiver settings
                    validate_route(routes, settings)
                    self.routes_request = routes
                except (ValueError, TypeError) as error:
                    upgrade_note = " ".join(filter(None, [upgrade_note, f"The saved EFB route was not restored ({error}). "
                                                                       "Choose it again on the Bridge tab."]))
        self.contacting = False
        self.contact_ok = None
        self.runtime = Runtime(settings, self._auto_enable, self._stream, self._contact)
        if upgrade_note:
            self.runtime.log(upgrade_note)
        # Per-launch session token, saved so a second launch can open this instance. mkstemp makes a
        # new owner-only (0600) file whatever the umask, so other local accounts cannot read it.
        self.token = secrets.token_urlsafe(32)
        self.session_path = self.data_dir / "gui-session"
        handle, temporary = tempfile.mkstemp(dir=self.data_dir, prefix="gui-session-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="ascii") as out:
                out.write(self.token)
            replace_file(temporary, self.session_path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    LAUNCH_CODE_SECONDS = 60
    launch_codes = None  # one-time browser launch code -> monotonic expiry

    def session_url(self, port):
        """A browser URL with a one-time launch code, never the session token: a browser's command
        line, which holds the URL, is readable by other accounts on Linux. The code works once,
        within LAUNCH_CODE_SECONDS, and is swapped for the session cookie."""
        code = secrets.token_urlsafe(32)
        with self.lock:
            now = time.monotonic()
            self.launch_codes = {c: t for c, t in (self.launch_codes or {}).items() if t > now}
            if len(self.launch_codes) >= 8:
                raise ValueError("Too many unused launch links; wait a minute and try again.")
            self.launch_codes[code] = now + self.LAUNCH_CODE_SECONDS
        return f"http://127.0.0.1:{port}/?launch={code}"

    def use_launch_code(self, code):
        """True once for a current launch code; the code is then gone."""
        supplied = code.encode("latin-1", "replace")
        with self.lock:
            now = time.monotonic()
            for known, expiry in list((self.launch_codes or {}).items()):
                if hmac.compare_digest(supplied, known.encode()):
                    del self.launch_codes[known]
                    return expiry > now
        return False

    def ensure_open(self):
        if self.closing.is_set():
            raise Closing("Sentry Bridge is closing.")

    def close(self):
        with self.lock:  # after this, no job can start the receiver listener (see observe_mode)
            self.closing.set()
        try:
            self.runtime.stop()
        except ValueError as error:  # the UDP thread is a daemon; finish the rest of shutdown anyway
            self.runtime.log(f"Shutdown: {error}")
        finally:
            if self.simulator:
                self.simulator.close()
            self.executor.shutdown(wait=True, cancel_futures=True)

    def busy(self):
        return self.job["state"] == "running"

    def _submit(self, action, function):
        with self.lock:
            if self.busy():
                raise ValueError("Wait for the current receiver operation to finish.")
            self.job = dict(state="running", action=action, result=None, error=None)

        def run():
            before = set(self.artifacts)
            try:
                result = function()
                with self.lock:
                    self.job.update(state="done", result=result)
            except Exception as error:
                self.discard_new_files(before)
                self.runtime.log(f"{action}: {error}")
                with self.lock:
                    self.job.update(state="failed", error=str(error))
        self.executor.submit(run)
        return {"queued": True}

    def _auto_enable(self):
        """Called from the UDP thread. Returns False when busy, so the runtime keeps the request."""
        if self.lock.acquire(blocking=False):
            try:
                if not self.busy():
                    self._submit("Automatic restoration", self.auto_restore)
                    return True
            finally:
                self.lock.release()
        return False

    def auto_restore(self):
        before = set(self.artifacts)
        try:
            # Keep room for the new file: old automatic backups must not fill the 100-file list.
            self.prune_auto_backups(AUTO_BACKUPS_KEPT - 1)
            saved = self.backup(AUTO_BACKUP)
        except Closing:
            raise
        except Exception as error:
            self.discard_new_files(before)
            self.runtime.log(f"Automatic backup unavailable: {error}; continuing mode recovery.")
            saved = {}
        self.ensure_open()
        return {**saved, **self.compatibility(True, backup=False, verify=True)}

    def prune_auto_backups(self, keep):
        with self.lock:
            automatic = [token for token, item in self.artifacts.items()
                         if item["name"] == AUTO_BACKUP and item.get("complete")]
            removed = automatic[:max(0, len(automatic) - keep)]
            for token in removed:
                item = self.artifacts.pop(token)
                try:
                    item["path"].unlink(missing_ok=True)
                except OSError as error:
                    self.runtime.log(f"Old automatic backup could not be removed: {error}")
            if removed:
                self.save_index()

    def discard_new_files(self, before):
        with self.lock:
            for token in set(self.artifacts) - before:
                if self.artifacts[token].get("complete"):
                    continue
                item = self.artifacts.pop(token)
                try:
                    item["path"].unlink(missing_ok=True)
                except OSError as error:
                    self.runtime.log(f"Incomplete file could not be removed: {error}")
            try:
                self.save_index()
            except OSError as error:
                self.runtime.log(f"File index could not be saved: {error}")

    def _contact(self):
        """Called from the UDP thread: one read-only keepalive at a time, outside the job slot so it
        never blocks a receiver operation. Returns False while the previous one is still running.
        Never takes self.lock: Stop holds it while joining this thread. Only this thread sets the
        flag and only the request thread clears it, so a plain attribute is enough."""
        if self.contacting:
            return False
        self.contacting = True

        def run():
            try:
                self.http("/?action=get")
                ok, info = True, "OK"
            except ValueError as error:  # includes Closing: nothing is sent after Exit
                ok, info = False, str(error)
            finally:
                self.contacting = False
            if ok != self.contact_ok and not self.closing.is_set():
                self.contact_ok = ok
                self.runtime.log(f"Keepalive: receiver status {info}")
        threading.Thread(target=run, name="gui-keepalive", daemon=True).start()
        return True

    def input_shared(self):
        """Whether the saved route shares the receiver port with an EFB on this device, so receiver
        tools (captures, mode checks) bind the port the same way while that EFB holds it."""
        try:
            return bool(self.routes_request) and validate_route(self.routes_request, self.runtime.settings)["routes"][0]["shared"]
        except ValueError:
            return False

    def _stream(self):
        if self.lock.acquire(blocking=False):
            try:
                if not self.busy():
                    self._submit("Read receiver", self.read_device)
            finally:
                self.lock.release()

    @property
    def host(self):
        s = self.runtime.settings
        if not s["sentryIp"]:
            raise ValueError("Set the receiver IP address in Settings first.")
        return f"{s['sentryIp']}:{s['httpPort']}"

    def http(self, path, body=None, raw=False):
        self.ensure_open()  # never send a receiver request, above all a mode change, after Exit
        data = None if body is None else json.dumps(body).encode()
        req = Request(f"http://{self.host}{path}", data=data,
                      headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with sentry_ctl.RECEIVER.open(req, timeout=5) as response:  # no proxy, no redirects
                text = response.read(2 * 1024 * 1024 + 1)
                if len(text) > 2 * 1024 * 1024:
                    raise ValueError("Receiver response exceeds 2 MiB.")
                if body is not None:
                    # The real settings endpoint may acknowledge with HTML/plain text.
                    # HTTP success requests a mode change; only UDP observations confirm it.
                    return {"httpStatus": response.status}
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(text)) as compressed:
                        text = compressed.read(2 * 1024 * 1024 + 1)
                    if len(text) > 2 * 1024 * 1024:
                        raise ValueError("Receiver response exceeds 2 MiB after decompression.")
        except HTTPError as error:
            code = error.code
            error.close()
            if 300 <= code < 400:  # RECEIVER refuses to follow it
                raise ValueError("Receiver redirected the request. Check its IP address.") from None
            raise ValueError(f"Receiver request failed (HTTP {code}). Check the receiver address and mode.") from None
        except (URLError, OSError, HTTPException, EOFError):  # HTTPException: bad status line, truncated body
            raise ValueError("Cannot reach the receiver. Check Sentry power, WiFi and Receiver IP in Settings.") from None
        if raw:
            return text.decode("utf-8", "replace")
        try:
            value = json.loads(text)
        except (ValueError, UnicodeError):
            raise ValueError("Receiver returned invalid JSON. Check the Receiver IP and HTTP port.") from None
        if not isinstance(value, dict):
            raise ValueError("Receiver returned an unexpected response. Check the Receiver IP and HTTP port.")
        return value

    def read_device(self):
        status = self.http("/?action=get")
        self.ensure_open()
        settings = self.http("/settings/?action=get")
        self.ensure_open()
        self.device = dict(status=status, settings=settings)
        if isinstance(settings.get("Power"), dict) and settings["Power"].get("powerSavings") is True:
            self.runtime.log("Power Savings is on. Disable it in Sentry's web settings before flight.")
        return self.device

    def artifact(self, name, kind):
        with self.lock:
            if len(self.artifacts) >= 100:
                raise ValueError("File limit reached. Delete unused downloads in Diagnostics → Files.")
            token = uuid.uuid4().hex
            path = self.data_dir / (token + "-" + name)
            self.artifacts[token] = dict(path=path, name=name, kind=kind, complete=False)
            self.save_index()
            return token, path

    def save_index(self):
        data = {token: dict(name=item["name"], kind=item["kind"]) for token, item in self.artifacts.items()
                if item.get("complete")}
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        replace_file(temporary, self.index_path, attempts=5)  # callers hold self.lock: keep it short

    def complete_file(self, token):
        with self.lock:
            self.artifacts[token]["complete"] = True
            self.save_index()

    def file(self, token, kinds=None):
        item = self.artifacts.get(token)
        if item is None or (kinds and item["kind"] not in kinds):
            raise ValueError("Select a file from this GUI session.")
        if token == self.capture_id and self.runtime.snapshot()["recording"]:
            raise ValueError("Stop the capture before downloading or analyzing it.")
        return item["path"]

    def backup(self, name="sentry-backup.zip"):
        device = self.read_device()
        self.ensure_open()
        token, path = self.artifact(name, "backup")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in device.items():
                archive.writestr(f"sentry-{name}.json", json.dumps(value, indent=2))
        self.complete_file(token)
        return {"backup": token}

    def observe_mode(self, seconds):
        number(seconds, .2, 30, "Mode check duration")
        runtime = self.runtime
        with self.lock:  # close() sets closing under the same lock, then stops the runtime
            self.ensure_open()
            temporary = not runtime.active()
            if temporary:
                runtime.capture_only = False
                runtime.share_input = self.input_shared()
                runtime.start(forwarding=False)
        before = runtime.snapshot()["frames"]
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                self.ensure_open()
                time.sleep(.1)
            after = runtime.snapshot()["frames"]
            counts = {int(key): count - before.get(key, 0) for key, count in after.items()
                      if count > before.get(key, 0)}
            result = {"mode": sentry_ctl.describe_mode(counts), "frames": counts, "checkedAt": time.time()}
            self.mode_observation = result
            return result
        finally:
            if temporary:
                runtime.stop()

    def compatibility(self, enabled, backup=True, verify=True, seconds=3):
        saved = self.backup() if enabled and backup else {}
        self.ensure_open()
        self.http("/settings/?action=set", {"publicGDL90": enabled})
        self.mode_requested = enabled
        self.mode_observation = None
        device = self.read_device()
        # Settings readback and packet verification are separate evidence.
        result = {**saved, "settings": device["settings"], "verification": "Skipped"}
        if verify:
            result["verification"] = self.observe_mode(seconds)
            expected = "PUBLIC" if enabled else "ENCRYPTED"
            if not result["verification"]["mode"].startswith(expected):
                result["warning"] = "Setting sent; expected packets were not observed. Check WiFi and receiver power."
        return result

    def save(self):
        if self.simulator:
            return  # Never persist bench ports or replace the real receiver configuration.
        text = json.dumps(dict(settings=self.runtime.settings, routes=self.routes_request), indent=2)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.config_path)

    def check_update(self):
        """Ask GitHub for the latest stable release in the background when due. Sends nothing else;
        offline or any failure just tries again later."""
        with self.update_lock:
            if (not self.runtime.settings["updateCheck"] or self.update_checking or time.time() < self.update_due
                    or self.runtime.active() or self.simulator):
                return
            self.update_checking = True

        def run():
            try:
                request = Request(RELEASES, headers={"Accept": "application/vnd.github+json", "User-Agent": "sentry-bridge"})
                with build_opener().open(request, timeout=5) as response:
                    release = json.loads(response.read(256 * 1024))
                tag, url = release["tag_name"], release["html_url"]
                fresh = newer(tag, self.version) and isinstance(url, str) and url.startswith("https://github.com/")
                self.update = dict(version=tag.removeprefix("v"), url=url) if fresh else None
                self.update_due = time.time() + UPDATE_INTERVAL_S
                temporary = self.update_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(dict(checked=time.time(), update=self.update)), encoding="utf-8")
                replace_file(temporary, self.update_path)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException):
                self.update_due = time.time() + UPDATE_RETRY_S
            finally:
                self.update_checking = False
        threading.Thread(target=run, name="gui-update-check", daemon=True).start()

    def current_update(self):
        """The remembered release, while it is still newer than this app (not once it is installed)."""
        update = self.update if self.runtime.settings["updateCheck"] else None
        return update if isinstance(update, dict) and newer("v" + str(update.get("version")), self.version) else None

    def status(self):
        self.check_update()
        v = self.runtime.snapshot()
        with self.lock:
            v.update(adapter="local", host="windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux",
                     simulator=self.simulator is not None, settings=deepcopy(self.runtime.settings),
                     routesRequest=deepcopy(self.routes_request), device=deepcopy(self.device),
                     modeObservation=deepcopy(self.mode_observation), modeRequested=self.mode_requested,
                     update=self.current_update(), updateSupported=True,
                     deviceLogs=list(self.device_logs), job=deepcopy(self.job),
                     artifacts=[dict(id=token, name=item["name"], kind=item["kind"],
                                     ready=item.get("complete", False) and item["path"].is_file() and not (token == self.capture_id and v["recording"]))
                                for token, item in self.artifacts.items()])
        return v

    def toggle_simulator(self, request):
        enabled = request.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("Simulator must be on or off.")
        if self.runtime.active():
            raise ValueError("Stop the bridge and capture before changing receiver.")
        for key in ("gps", "uat", "weather"):
            if key in request and not isinstance(request[key], bool):
                raise ValueError("Simulator options must be on or off.")
        if enabled and not self.simulator:
            self.real_settings = deepcopy(self.runtime.settings)
            self.real_routes = deepcopy(self.routes_request)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.simulator = Simulator(port, request.get("gps", True), request.get("uat", True), request.get("weather", True))
            # Keep the EFB port: simulator traffic goes to the EFB on this device.
            self.runtime.settings = {**DEFAULTS, "sentryIp": "127.0.0.1", "listenPort": port, "efbPort": self.real_settings["efbPort"],
                                     "httpPort": self.simulator.http.server_port, "announcePort": 0}
            self.routes_request = None
        elif not enabled and self.simulator:
            self.simulator.close()
            self.simulator = None
            self.runtime.settings = self.real_settings
            self.routes_request = self.real_routes
        self.device = None
        self.mode_observation = None
        self.mode_requested = None
        self.device_logs = []
        return {"simulator": enabled}

    def list_logs(self):
        links = sentry_ctl._Links()
        links.feed(self.http("/data/", raw=True))
        self.device_logs = list(dict.fromkeys(name for name in map(sentry_ctl.log_name, links.links) if name))[:100]
        return {"logs": self.device_logs}

    def fetch_log(self, name, path, limit):
        self.ensure_open()  # no new receiver request after Exit
        request = Request(f"http://{self.host}/data/{quote(name, safe='')}")
        size = 0
        with sentry_ctl.RECEIVER.open(request, timeout=30) as response, path.open("wb") as out:
            compressed = gzip.GzipFile(fileobj=response) if response.headers.get("Content-Encoding", "").lower() == "gzip" else None
            source = compressed or response
            try:
                while True:
                    chunk = source.read(min(65536, limit - size + 1))
                    self.ensure_open()  # after every blocking read, including the one that hits EOF
                    if not chunk:
                        break
                    out.write(chunk[:limit - size])
                    size += len(chunk)
                    if size > limit:
                        return limit, True
            finally:
                if compressed:
                    compressed.close()
        return size, False

    def download_logs(self, request):
        names = self.device_logs if request.get("name") == "all" else [request.get("name")]
        if not names or any(name not in self.device_logs for name in names):
            raise ValueError("Refresh the receiver log list and select a listed log.")
        number(request.get("maxMb", 20), 1, 20, "Log size limit", True)
        if len(names) > 20:
            raise ValueError("Download at most 20 logs at a time.")
        token, path = self.artifact("sentry-logs.zip", "logs")
        info = []
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, name in enumerate(names):
                scratch = self.data_dir / f"{token}-{index}.download"
                try:
                    size, truncated = self.fetch_log(name, scratch, request.get("maxMb", 20) * 1024 * 1024)
                    archive.write(scratch, f"{index + 1}-{name.replace(':', '_')}")  # ':' breaks Windows extraction
                    info.append(dict(name=name, bytes=size, truncated=truncated))
                finally:
                    scratch.unlink(missing_ok=True)
        self.ensure_open()  # a cancelled download stays incomplete and is discarded
        self.complete_file(token)
        return {"download": token, "files": info}

    def efb_candidate(self, source, efb_port):
        """Whether an announcing device can be an Other device destination: not the receiver,
        not loopback, and a destination that validate_route would accept."""
        settings = self.runtime.settings
        if source == settings["sentryIp"] or ipaddress.IPv4Address(source).is_loopback:
            return False
        try:
            check_endpoint(source, efb_port, settings["listenPort"])
        except ValueError:
            return False
        return True

    def discover(self, seconds):
        """EFBs announcing themselves on the discovery port (JSON with a GDL90 port), for filling in
        an Other device address. Uses the running bridge's list when it holds the port; otherwise
        listens briefly. Never sends anything."""
        settings = self.runtime.settings
        port = settings["announcePort"]
        if not port:
            raise ValueError("EFB discovery is off. Set the EFB discovery port in Settings.")
        found = {}

        def add(source, efb_port, app):
            if efb_port and ((source, efb_port) in found or len(found) < DISCOVERY_LIMIT) and self.efb_candidate(source, efb_port):
                found[(source, efb_port)] = app or "?"

        deadline = time.monotonic() + seconds
        with self.lock:
            self.ensure_open()
            shared = self.runtime.active() and self.runtime.ann is not None
            listener = None if shared else udp_listener(port)
        try:
            if listener:
                listener.settimeout(.2)
            while time.monotonic() < deadline:
                self.ensure_open()
                if shared:
                    with self.runtime.lock:
                        heard = list(self.runtime.discovered.items())
                    for (source, efb_port), (app, seen) in heard:
                        if time.monotonic() - seen <= ANNOUNCE_TTL_S:
                            add(source, efb_port, app)
                    time.sleep(.2)
                    continue
                try:
                    data, (source, _) = listener.recvfrom(4096)
                except socket.timeout:
                    continue
                app, efb_port = parse_announce(data)
                add(source, efb_port, app)
        finally:
            if listener:
                listener.close()
        return {"efbs": [dict(app=app, address=address, port=efb_port)
                         for (address, efb_port), app in sorted(found.items())]}

    def run_worker(self, command, seconds, cwd=None):
        """Run a diagnostic worker process. Exit or an overrun stops it (with its children: a
        one-file build runs as launcher plus child), so close() never waits for a probe to finish.
        Returns (exit code, combined output)."""
        self.ensure_open()
        with tempfile.TemporaryFile() as output:
            # POSIX: a session of its own makes the worker the leader of a process group that holds
            # exactly its descendants, so stopping the group never touches unrelated processes.
            process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                                       start_new_session=sys.platform != "win32")
            deadline = time.monotonic() + seconds
            try:
                while True:
                    try:
                        process.wait(timeout=.1)
                        break
                    except subprocess.TimeoutExpired:
                        self.ensure_open()
                        if time.monotonic() > deadline:
                            raise ValueError(f"Diagnostic tool did not finish within {seconds:g} seconds.") from None
            finally:
                if process.poll() is None:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)  # the worker's own group: it and its children
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=10)
            output.seek(0)
            return process.returncode, output.read(65536 * 4).decode("utf-8", "replace")

    def tool(self, script, arguments, name, seconds=60):
        token, path = self.artifact(name, "report")
        # Fixed scripts, explicit arguments, no shell or user-selected executable/path.
        if getattr(sys, "frozen", False):
            from tools.desktop_app import TOOL_MODULES
            if script not in TOOL_MODULES:
                raise ValueError("Unsupported diagnostic tool.")
            result_path = self.data_dir / (token + ".result.json")
            try:
                self.run_worker([sys.executable, "--tool", script, "--result", str(result_path),
                                 "--", *map(str, arguments)], seconds)
                record = json.loads(result_path.read_text(encoding="utf-8"))
                text = record["text"][:65536]
                code = record["exitCode"]
            finally:
                result_path.unlink(missing_ok=True)
            self.ensure_open()
            path.write_text(text, encoding="utf-8")
            self.complete_file(token)
            return {"report": token, "exitCode": code, "text": text}
        code, text = self.run_worker([sys.executable, str(ROOT / script), *map(str, arguments)], seconds, cwd=ROOT)
        text = text[:65536]
        self.ensure_open()
        path.write_text(text, encoding="utf-8")
        self.complete_file(token)
        return {"report": token, "exitCode": code, "text": text}

    def action(self, name, request):
        if name == "upload":
            return self.upload(request)
        with self.lock:
            before = set(self.artifacts)
            try:
                return self._action(name, request)
            except Exception:
                self.discard_new_files(before)
                raise

    def upload(self, request):
        """Decode and write an uploaded capture without holding the controller lock, so status
        polling and automatic mode recovery are not stalled by a 32 MiB upload."""
        if not isinstance(request, dict):
            raise ValueError("Use a JSON object.")
        suffix = Path(str(request.get("name", ""))).suffix.lower()
        if suffix not in {".jsonl", ".pcap", ".pcapng"}:
            raise ValueError("Choose a JSONL, pcap or pcapng capture.")
        try:
            data = base64.b64decode(request.get("data", ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError("Invalid capture upload.") from None
        if not data or len(data) > CAPTURE_LIMIT:
            raise ValueError("Capture must be between 1 byte and 32 MiB.")
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(request["name"]).stem)[:80] or "capture"
        with self.lock:
            if self.busy():
                raise ValueError("Wait for the current receiver operation to finish.")
            token, path = self.artifact(stem + suffix, "capture")
        try:
            path.write_bytes(data)
            self.complete_file(token)
        except Exception:
            # Remove only this upload: a job may have started its own file while this one was written.
            with self.lock:
                self.artifacts.pop(token, None)
                path.unlink(missing_ok=True)
                self.save_index()
            raise
        return {"capture": token}

    def _action(self, name, request):
        if not isinstance(request, dict):
            raise ValueError("Use a JSON object.")
        if name == "quit":
            # Exit never waits for a receiver job: close() stops it at its next stage.
            with self.lock:
                self.closing.set()
            return {"exiting": True}
        with self.lock:
            if name != "stop":
                self.ensure_open()
            if self.busy() and name != "stop":
                raise ValueError("Wait for the current receiver operation to finish.")
            if name == "stop":
                self.runtime.stop()
                return {"stopped": True}
            if name == "settings":
                if self.simulator:
                    raise ValueError("Turn off the simulator before saving receiver settings.")
                if self.runtime.active():
                    raise ValueError("Stop the bridge or capture before saving settings.")
                settings = validate_settings(request)
                # This device's route depends only on the ports, so check it with them; another device's
                # address is checked at Start (it may need the receiver's WiFi).
                if (self.routes_request or {}).get("topology") == "same":
                    validate_route(self.routes_request, settings)
                self.runtime.settings = settings
                self.save()
                self.device = None
                self.mode_observation = None
                self.mode_requested = None
                return {"saved": True}
            if name == "simulator":
                return self.toggle_simulator(request)
            if name == "start":
                routes = validate_route(request, self.runtime.settings)["routes"]
                if self.simulator and any(route["address"] != "127.0.0.1" for route in routes):
                    raise ValueError("Simulator traffic can only be sent to This device.")
                self.runtime.capture_only = False
                self.runtime.start(routes)
                self.routes_request = request
                self.save()
                return {"started": True}
            if name == "capture-start":
                if self.runtime.snapshot()["recording"]:
                    raise ValueError("A capture is already running.")
                number(request.get("seconds", 0), 0, 3600, "Capture duration")
                token, path = self.artifact("receiver.jsonl", "capture")
                self.runtime.share_input = self.input_shared()
                self.runtime.begin_capture(path, request.get("seconds", 0))
                self.capture_id = token
                self.complete_file(token)
                return {"capture": token}
            if name == "capture-stop":
                self.runtime.end_capture()
                return {"stopped": True}
            if name == "delete-file":
                token = request.get("id")
                path = self.file(token)
                path.unlink(missing_ok=True)
                del self.artifacts[token]
                self.save_index()
                return {"deleted": True}
            if name == "read":
                def read_and_check():
                    device = self.read_device()
                    return {**device, "verification": self.observe_mode(3)}
                return self._submit("Read receiver", read_and_check)
            if name == "backup":
                return self._submit("Backup receiver", self.backup)
            if name == "check":
                seconds = number(request.get("seconds", 8), .2, 30, "Mode check duration")
                return self._submit("Check mode", lambda: self.observe_mode(seconds))
            if name == "compatibility":
                for key in ("enabled", "backup", "verify"):
                    if key in request and not isinstance(request[key], bool):
                        raise ValueError("Receiver switches must be on or off.")
                if "enabled" not in request:
                    raise ValueError("Choose Compatibility Mode on or off.")
                seconds = number(request.get("seconds", 3), .2, 30, "Verification duration")
                return self._submit("Compatibility Mode", lambda: self.compatibility(
                    request["enabled"], request.get("backup", True), request.get("verify", True), seconds))
            if name == "logs":
                return self._submit("List receiver logs", self.list_logs)
            if name == "download-logs":
                return self._submit("Download receiver logs", lambda: self.download_logs(request))
            if name == "analyze":
                path = self.file(request.get("capture"), {"capture"})
                port = number(request.get("port", 4000), 1, 65535, "Capture input port", True)
                samples = number(request.get("samples", 3), 0, 10, "Hex samples", True)
                return self._submit("Analyze capture", lambda: self.tool("capture.py", ["--port", port, "analyze", path, "--hexdump", samples], "capture-report.txt"))
            if name == "weather":
                source = self.file(request.get("source"), {"capture"})
                output = self.file(request.get("output"), {"capture"})
                ip = number(request.get("inputPort", 4000), 1, 65535, "Input port", True)
                op = number(request.get("outputPort", 4001), 1, 65535, "Output port", True)
                return self._submit("Compare weather", lambda: self.tool("tools/check_weather.py", [source, output, "--input-port", ip, "--output-port", op], "weather-report.txt"))
            if name == "discover":
                seconds = number(request.get("seconds", 6), 1, 30, "Discovery duration")
                return self._submit("Find EFB", lambda: self.discover(seconds))
            if name == "websocket":
                seconds = number(request.get("seconds", 10), 1, 60, "WebSocket duration")
                path = request.get("path", "/")
                if not isinstance(path, str) or not path.startswith("/") or len(path) > 256 or any(c in path for c in "\r\n "):
                    raise ValueError("WebSocket path must start with / and contain no spaces.")
                self.host  # Validate receiver address before constructing fixed tool arguments.
                def probe():
                    token, out = self.artifact("websocket.jsonl", "websocket")
                    report = self.tool("ws_probe.py", ["--host", self.runtime.settings["sentryIp"],
                            "--port", self.runtime.settings["httpPort"], "--path", path,
                            "--seconds", seconds, "--out", out, "--report", ""],
                            "websocket-report.txt", seconds + 15)
                    if out.is_file():
                        self.complete_file(token)
                        report["recording"] = token
                    else:
                        self.discard_new_files({key for key in self.artifacts if key != token})
                    return report
                return self._submit("Probe WebSocket", probe)
            raise ValueError("Unknown GUI action.")


class Handler(PreviewHandler):
    """Loopback controller. Like the Android app, every request needs this launch's session token,
    so other programs on the computer cannot drive the receiver controls."""

    def cookie_name(self):
        return f"sb{self.server.server_port}"  # cookies are shared across ports on one host

    def route_settings(self):
        return self.server.controller.runtime.settings

    def session_ok(self):
        expected = self.server.controller.token.encode()
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(self.cookie_name())
        except CookieError:
            cookie = None
        # Compare bytes: compare_digest rejects non-ASCII str, and headers decode as Latin-1.
        return any(hmac.compare_digest(value.encode("latin-1", "replace"), expected)
                   for value in (supplied, cookie.value if cookie else "") if value)

    def deny_session(self, head=False):
        self.reply(403, {"error": "Open Sentry Bridge from its shortcut or app window."}, head=head)

    def get_response(self, head=False):
        if not self.allowed_host():
            self.reply(403, {"error": "Loopback host required."}, head=head)
            return
        url = urlsplit(self.path)
        launch = parse_qs(url.query).get("launch", [""])[0]
        if url.path == "/" and launch and self.server.controller.use_launch_code(launch):
            # The launcher's one-time URL: swap the code for a cookie and drop it from the address bar.
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{self.cookie_name()}={self.server.controller.token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif not self.session_ok():
            self.deny_session(head)
        elif self.path == "/api/status":
            self.reply(200, self.server.controller.status(), head=head)
        elif self.path.startswith("/api/files/"):
            token = self.path.removeprefix("/api/files/")
            try:
                path = self.server.controller.file(token)
                if not path.is_file():
                    raise ValueError("File is not ready.")
            except (ValueError, OSError) as error:
                self.reply(404, {"error": str(error)}, head=head)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="' + self.server.controller.artifacts[token]["name"] + '"')
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not head:
                    with path.open("rb") as source:
                        while chunk := source.read(65536):
                            self.wfile.write(chunk)
        else:
            super().get_response(head)

    def do_POST(self):
        if not self.allowed_host() or not self.session_ok():
            self.deny_session()
            return
        if self.path == "/api/validate":
            return super().do_POST()
        if not self.allowed_host() or not self.allowed_origin():
            self.reply(403, {"error": "Loopback origin required."})
            return
        if self.path == "/api/launch":
            # A second launch (desktop app, Start-Gui.ps1) holds the token and asks for a browser URL.
            try:
                self.reply(200, {"url": self.server.controller.session_url(self.server.server_port)})
            except ValueError as error:
                self.reply(429, {"error": str(error)})
            return
        if not self.path.startswith("/api/actions/"):
            self.reply(404, {"error": "Not found."})
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            limit = 45 * 1024 * 1024 if self.path == "/api/actions/upload" else 8192
            if not 0 < size <= limit or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Use a JSON request within the upload limit.")
            self.connection.settimeout(15 if limit > 8192 else 3)
            body = json.loads(self.read_body(size))
            result = self.server.controller.action(self.path.removeprefix("/api/actions/"), body)
        except (ValueError, OSError, TypeError) as error:
            self.reply(400, {"error": str(error)})
        else:
            self.reply(200, result)
            if result.get("exiting"):
                threading.Thread(target=self.server.shutdown, daemon=True).start()


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true")
    # GUI settings and files belong to this checkout, even when SB_LOCAL_TOOLS points the build
    # scripts at another clone's tools; that folder is never written by the GUI.
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local-tools" / "gui-data")
    args = parser.parse_args(argv)
    controller = Controller(args.data_dir)
    try:
        with LoopbackServer(("127.0.0.1", args.port), Handler) as server:
            server.controller = controller
            url = controller.session_url(server.server_port)
            print(f"Sentry Bridge: {url}", flush=True)
            if args.open:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        controller.close()


if __name__ == "__main__":
    main()
