"""Windowed desktop entry point and fixed diagnostic worker for frozen builds."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import webbrowser
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

TOOL_MODULES = {"capture.py": "capture", "tools/check_weather.py": "tools.check_weather", "ws_probe.py": "ws_probe"}


def tool_worker(script, result, arguments):
    if script not in TOOL_MODULES:
        raise ValueError("Unsupported diagnostic tool.")
    stream = io.StringIO()
    code = 0
    with redirect_stdout(stream), redirect_stderr(stream):
        previous = sys.argv
        try:
            sys.argv = [script, *arguments]
            runpy.run_module(TOOL_MODULES[script], run_name="__main__")
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else int(error.code is not None)
        except Exception as error:
            print(str(error))
            code = 1
        finally:
            sys.argv = previous
    Path(result).write_text(json.dumps(dict(exitCode=code, text=stream.getvalue()[:65536])), encoding="utf-8")
    return code


def launch_url(url, token):
    """A one-time browser URL from the running instance; the token itself must not reach a browser
    command line. Any failure is an error for the user, never a reason to start a second instance:
    that would replace the saved token the running instance still uses."""
    request = Request(url + "api/launch", data=b"", method="POST", headers={"Authorization": "Bearer " + token})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return json.loads(response.read(65536))["url"]
    except HTTPError as error:
        with error:
            try:
                detail = json.loads(error.read(65536))["error"]
            except (ValueError, KeyError, TypeError):
                detail = f"HTTP {error.code}"
        raise RuntimeError(f"Sentry Bridge is already running but did not open a window: {detail}") from None
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"Sentry Bridge is already running but did not answer ({error}). "
                           "Try again in a moment.") from None


def default_data_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "SentryBridge"
    return Path.home() / ".local/share/sentry-bridge"


def first_window_size(work_area=None):
    """1200 x 780 (landscape, two columns), reduced to 90% of the Windows work area (screen minus taskbar,
    in the logical pixels Edge's --window-size uses) on small or scaled screens."""
    if work_area is None:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if not ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return 1200, 780
        work_area = (rect.right - rect.left, rect.bottom - rect.top)
    return min(1200, int(work_area[0] * .9)), min(780, int(work_area[1] * .9))


def app_window(url, data_dir):
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
            if base:
                edge = Path(base) / "Microsoft/Edge/Application/msedge.exe"
                if edge.is_file():
                    profile = data_dir / "window-profile"
                    # First launch only: a landscape window that fits the screen. Later launches keep the size Edge
                    # saved in the profile, so a window the user resized stays that size.
                    size = [] if profile.exists() else ["--window-size=%d,%d" % first_window_size()]
                    return subprocess.Popen([str(edge), "--app=" + url, "--no-first-run", *size,
                                             "--disable-background-mode", "--user-data-dir=" + str(profile)],
                                            creationflags=subprocess.CREATE_NO_WINDOW)
    webbrowser.open(url)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--tool", choices=TOOL_MODULES)
    parser.add_argument("--result", type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.tool:
        if args.result is None:
            parser.error("--result is required for a diagnostic worker")
        return tool_worker(args.tool, args.result, args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments)
    from tools.gui_server import Controller, Handler, LoopbackServer
    url = f"http://127.0.0.1:{args.port}/"
    if args.port:
        existing = None
        try:
            # A running instance saved its session token in the same private data folder.
            token = (args.data_dir / "gui-session").read_text(encoding="ascii").strip()
            probe = Request(url + "api/status", headers={"Authorization": "Bearer " + token})
            with build_opener(ProxyHandler({})).open(probe, timeout=1) as response:
                existing = json.loads(response.read(65536))
        except (OSError, ValueError):
            pass  # no running instance with this token: start one below
        if isinstance(existing, dict) and existing.get("desktop") is True and existing.get("adapter") == "local":
            # It runs. From here on an error is reported (launch_url, app_window), not handled by
            # starting a second instance.
            if not args.no_window:
                app_window(launch_url(url, token), args.data_dir)
            return 0
    class DesktopController(Controller):
        def status(self):
            result = super().status()
            result["desktop"] = True
            return result
    controller = DesktopController(args.data_dir)
    try:
        with LoopbackServer(("127.0.0.1", args.port), Handler) as server:
            server.controller = controller
            window = None if args.no_window else app_window(controller.session_url(server.server_port), args.data_dir)
            if window is not None:
                def close_with_window():
                    window.wait()
                    server.shutdown()
                threading.Thread(target=close_with_window, daemon=True).start()
            server.serve_forever()
    finally:
        controller.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), "Sentry Bridge", 0x10)
        else:
            raise
