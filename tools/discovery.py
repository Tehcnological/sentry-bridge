"""EFB announcements without importing the Python traffic decoder or NumPy."""
import json

ANNOUNCE_TTL_S = 30.0


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
