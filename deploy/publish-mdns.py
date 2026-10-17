"""Publish a WiFi IPv4 alias without renaming the Raspberry Pi."""
import argparse
import json
import signal
import subprocess
import threading


def address(interface):
    result = subprocess.run(["ip", "-json", "-4", "address", "show", "dev", interface],
                            capture_output=True, timeout=3, check=True)
    for link in json.loads(result.stdout):
        for item in link.get("addr_info", []):
            if item.get("scope") == "global":
                return item["local"]
    return None


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--interface", default="wlan0")
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *ignored: stop.set())
    process = None
    current = None
    try:
        while not stop.is_set():
            try:
                ip = address(args.interface)
            except (OSError, ValueError, subprocess.SubprocessError):
                ip = None
            if ip != current or process is not None and process.poll() is not None:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
                process = None
                current = ip
                if ip:
                    process = subprocess.Popen(["avahi-publish-address", "-R", "sentrybridge.local", ip])
            stop.wait(5)
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


if __name__ == "__main__":
    main()
