"""The one GDL90 route and its validation; does not change or start a bridge.

EFBs take a Stratux or GDL90 device on UDP 4000. There are no per-EFB profiles: This device sends
to 127.0.0.1 and Other device to its IPv4 address, both on the EFB port from Settings. On this
device an EFB port equal to the receiver port shares that port with the EFB (see udp_listener)."""
import ipaddress
import errno
import socket
import sys

HOSTS = {"android", "ios", "windows", "macos", "linux", "pi", "esp32"}
NO_EFB_HOSTS = {"pi", "esp32"}  # no EFB runs on these, so only Other device


def interface_broadcasts():
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        import ctypes
        import struct
        size = ctypes.c_ulong(0)
        api = ctypes.WinDLL("iphlpapi").GetIpAddrTable
        api(None, ctypes.byref(size), False)
        if not size.value:
            return set()
        buffer = ctypes.create_string_buffer(size.value)
        if api(buffer, ctypes.byref(size), False):
            return set()
        raw = buffer.raw
        count = struct.unpack_from("<I", raw)[0]
        result = set()
        for index in range(count):
            offset = 4 + index * 24
            address = int.from_bytes(raw[offset:offset + 4], "big")
            mask = int.from_bytes(raw[offset + 8:offset + 12], "big")
            if mask < 0xfffffffe:
                result.add(str(ipaddress.IPv4Address(address | (0xffffffff ^ mask))))
        return result
    if sys.platform.startswith("linux") and hasattr(socket, "if_nameindex"):
        try:
            import fcntl
            import struct
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                result = set()
                for _, name in socket.if_nameindex():
                    try:
                        raw = fcntl.ioctl(probe.fileno(), 0x8919, struct.pack("256s", name.encode()[:15]))
                        value = socket.inet_ntoa(raw[20:24])
                        if value != "0.0.0.0":
                            result.add(value)
                    except OSError:
                        pass
                return result
        except ImportError:
            pass
    if sys.platform == "darwin":
        return _getifaddrs_broadcasts(bsd=True)
    return set()


def _getifaddrs_broadcasts(bsd):
    """IPv4 interface broadcast addresses from getifaddrs(3). macOS has no SIOCGIFBRDADDR shortcut
    here, and its connect() accepts a broadcast address without SO_BROADCAST, so ask directly."""
    import ctypes
    import ctypes.util

    class Ifaddrs(ctypes.Structure):
        pass
    Ifaddrs._fields_ = [("next", ctypes.POINTER(Ifaddrs)), ("name", ctypes.c_char_p),
                        ("flags", ctypes.c_uint), ("addr", ctypes.c_void_p), ("netmask", ctypes.c_void_p),
                        ("broadaddr", ctypes.c_void_p), ("data", ctypes.c_void_p)]
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        return set()
    head = ctypes.POINTER(Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        return set()
    result = set()
    try:
        node = head
        while node:
            entry = node.contents
            if entry.flags & 0x2 and entry.addr and entry.broadaddr:  # IFF_BROADCAST
                raw = ctypes.string_at(entry.broadaddr, 8)
                family = raw[1] if bsd else int.from_bytes(raw[0:2], sys.byteorder)
                if family == socket.AF_INET:
                    value = socket.inet_ntoa(raw[4:8])
                    if value != "0.0.0.0":
                        result.add(value)
            node = entry.next
    finally:
        libc.freeifaddrs(head)
    return result


def check_endpoint(address, port, listen_port=None):
    """Ask the OS for the local route without sending any packets.

    Broadcast connect requires SO_BROADCAST, which is deliberately never enabled.
    Comparing the selected source also finds local interface addresses beyond loopback.
    """
    local = None
    if address in interface_broadcasts():
        raise ValueError("Use the EFB device's unicast address, not a broadcast address.")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((address, port))
            local = probe.getsockname()[0]
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM, 10013):
                raise ValueError("Use the EFB device's unicast address, not a broadcast address.") from None
            # An offline network is allowed during configuration.
    if port == listen_port and (ipaddress.IPv4Address(address).is_loopback or address == local):
        raise ValueError("EFB output and receiver input ports must differ on this device.")
    return local


def port_number(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("UDP port must be a whole number from 1 to 65535.")
    return value


def validate_route(request, settings):
    """The single endpoint for {host, topology, address}, on settings["efbPort"]."""
    if not isinstance(request, dict):
        raise ValueError("Configuration must be a JSON object.")
    host, topology = request.get("host"), request.get("topology")
    if not isinstance(host, str) or host not in HOSTS:
        raise ValueError("Choose a bridge host.")
    if topology not in ("same", "network"):
        raise ValueError("Choose where the EFB runs.")
    port = port_number(settings["efbPort"])
    if topology == "same":
        if host in NO_EFB_HOSTS:
            raise ValueError("No EFB runs on this host. Choose Other device.")
        address, shared = "127.0.0.1", port == settings["listenPort"]
        if port == settings.get("announcePort"):  # this device's discovery listener would take the output
            raise ValueError("On this device the EFB port must differ from the discovery port.")
    else:
        address = request.get("address")
        if not isinstance(address, str):
            raise ValueError("Enter the EFB device's IPv4 address.")
        try:
            ip = ipaddress.IPv4Address(address.strip())
        except ipaddress.AddressValueError as error:
            raise ValueError("Enter a valid IPv4 address, not a hostname.") from error
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_reserved or int(ip) < 0x01000000 or int(ip) == 0xffffffff:
            raise ValueError("Use the other EFB device's unicast IPv4 address.")
        address, shared = str(ip), False
        check_endpoint(address, port, settings["listenPort"])
    warnings = [f"Shares UDP {port} with the EFB on this device. One EFB at a time receives it; close other EFBs."] if shared else []
    return {"routes": [{"address": address, "port": port, "shared": shared}], "warnings": warnings,
            "displayVerified": False, "appliedToBridge": False}


def migrate(request, settings):
    """A route saved by v0.1.6 or earlier (per-EFB selections) as the one route, and its first
    selection's port as the EFB port when Settings has none. Returns (request, settings, note):
    note says which saved EFBs are no longer sent to, or is None."""
    if not isinstance(request, dict) or "selections" not in request:
        return request, settings, None
    selections = [s for s in request.get("selections") or [] if isinstance(s, dict)] or [{}]
    first = selections[0]
    route = {"host": request.get("host"), "topology": request.get("topology"), "address": first.get("address", "")}
    if "efbPort" not in settings and type(first.get("port")) is int:
        settings = {**settings, "efbPort": first["port"]}
    dropped = [f"{s.get('address') or '?'}:{s.get('port', '?')}" for s in selections[1:]]
    note = (f"Saved EFBs upgraded to one route ({route['address'] or 'address needed'}); no longer sent to: "
            + ", ".join(dropped) + ".") if dropped else None
    return route, settings, note
