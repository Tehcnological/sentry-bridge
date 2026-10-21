"""EFB announcements and the device sweep, without importing the Python traffic decoder or NumPy."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import socket

ANNOUNCE_TTL_S = 30.0
# A TCP knock on a port nothing listens on: a device answers at once, either refusing or accepting,
# while an unused address stays silent. Windows retries a refused connect for up to about two seconds.
PROBE_PORT, PROBE_TIMEOUT_S = 7, 3.0


def answers(address, port=PROBE_PORT, timeout=PROBE_TIMEOUT_S):
    """Whether a device is at this address: it accepted or refused a TCP connection."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def sweep(local, exclude=(), probe=answers):
    """Devices answering on local's /24 network, other than local and the excluded addresses."""
    network = ipaddress.IPv4Network(f"{local}/24", strict=False)
    skip = {local, *exclude}
    hosts = [str(host) for host in network.hosts() if str(host) not in skip]
    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:  # all at once: one timeout for the sweep
        return [host for host, up in zip(hosts, pool.map(probe, hosts)) if up]


def follow_choice(candidates, current):
    """Where to send after the EFB at current stopped answering: the one other device that answers,
    or None when there are none or several (the user chooses then)."""
    others = [address for address in candidates if address != current]
    return others[0] if len(others) == 1 else None


def parse_announce(data):
    try:
        value = json.loads(data.decode("utf-8", "strict"))
        if isinstance(value["GDL90"]["port"], bool):   # int(true) is 1; C finds no digits
            return "", None
        port = int(value["GDL90"]["port"])
        # Same label rule as bridge.announce_label and the C core: printable ASCII, no backslash.
        app = "".join(c for c in str(value.get("App", "?")) if " " <= c <= "~" and c != "\\")[:63]
        return app, port if 0 < port < 65536 else None
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return "", None
