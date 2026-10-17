"""The one GDL90 route: This device or Other device on the EFB port, port sharing and old saved routes."""
import ctypes
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from routes import interface_broadcasts, migrate, validate_route  # noqa: E402
import routes  # noqa: E402

PORTS = {"listenPort": 4000, "efbPort": 4000}


def route(topology="same", host="android", address=None):
    request = {"host": host, "topology": topology}
    if address is not None:
        request["address"] = address
    return request


class TestRoutes(unittest.TestCase):
    def test_this_device_on_the_receiver_port_shares_it(self):
        result = validate_route(route(), PORTS)
        self.assertEqual(result["routes"], [{"address": "127.0.0.1", "port": 4000, "shared": True}])
        self.assertIn("One EFB at a time", result["warnings"][0])
        self.assertFalse(result["appliedToBridge"])
        self.assertFalse(result["displayVerified"])
        other = validate_route(route(), {"listenPort": 4000, "efbPort": 4001})
        self.assertEqual(other["routes"], [{"address": "127.0.0.1", "port": 4001, "shared": False}])
        self.assertEqual(other["warnings"], [])

    def test_other_device_uses_its_address_on_the_efb_port(self):
        for host in ("windows", "pi", "esp32", "android"):
            with self.subTest(host=host):
                result = validate_route(route("network", host, " 192.168.7.23 "), PORTS)
                self.assertEqual(result["routes"], [{"address": "192.168.7.23", "port": 4000, "shared": False}])

    def test_discovery_port_clash_matters_only_on_this_device(self):
        ports = {"listenPort": 4000, "efbPort": 63093, "announcePort": 63093}
        with self.assertRaisesRegex(ValueError, "discovery port"):
            validate_route(route(), ports)
        result = validate_route(route("network", "windows", "192.168.7.23"), ports)
        self.assertEqual(result["routes"][0]["port"], 63093)
        from tools.gui_runtime import DEFAULTS, validate_settings
        self.assertEqual(validate_settings({**DEFAULTS, "efbPort": 63093})["efbPort"], 63093)

    def test_no_efb_hosts_cannot_choose_this_device(self):
        for host in ("pi", "esp32"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "Other device"):
                validate_route(route(host=host), PORTS)

    def test_known_interface_broadcasts_cannot_be_selected(self):
        for address in interface_broadcasts():
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_route(route("network", address=address), PORTS)

    def test_invalid_ports_addresses_and_request_shapes_rejected(self):
        for port in (True, 0, 65536, -1, "4001", 4001.5):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_route(route(), {"listenPort": 4000, "efbPort": port})
        for address in ("", "efb.local", "::1", "0.0.0.0", "0.0.0.1", "127.0.0.2", "224.0.0.1", "255.255.255.255",
                        "192.168.007.1", [], None):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_route(route("network", address=address), PORTS)
        for request in ([], {}, {"host": [], "topology": "same"}, {"host": "android", "topology": "both"}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_route(request, PORTS)

    def test_v016_routes_migrate_to_one_route_and_keep_their_port(self):
        old_same = dict(host="android", platform="android", topology="same",
                        selections=[dict(id="avare-android", port=4001, address="127.0.0.1"),
                                    dict(id="custom-gdl90", port=43211, address="127.0.0.1")])
        request, settings, note = migrate(old_same, {"listenPort": 4000})
        self.assertEqual(request, {"host": "android", "topology": "same", "address": "127.0.0.1"})
        self.assertEqual(settings["efbPort"], 4001)
        self.assertIn("no longer sent to: 127.0.0.1:43211", note)  # the dropped EFB is named
        old_network = dict(host="windows", platform="ios", topology="network",
                           selections=[dict(id="custom-gdl90", port=4000, address="192.168.4.3")])
        request, settings, note = migrate(old_network, {"listenPort": 4000, "efbPort": 4002})
        self.assertEqual(request["address"], "192.168.4.3")
        self.assertEqual(settings["efbPort"], 4002)  # a saved EFB port wins
        self.assertIsNone(note)
        current = route("network", address="192.168.4.3")
        self.assertEqual(migrate(current, {"listenPort": 4000}), (current, {"listenPort": 4000}, None))
        self.assertEqual(migrate(None, {}), (None, {}, None))

    def test_getifaddrs_broadcast_lookup_handles_bsd_and_linux_layouts(self):
        class Entry(ctypes.Structure):
            pass
        Entry._fields_ = [("next", ctypes.POINTER(Entry)), ("name", ctypes.c_char_p),
                          ("flags", ctypes.c_uint), ("addr", ctypes.c_void_p),
                          ("netmask", ctypes.c_void_p), ("broadcast", ctypes.c_void_p),
                          ("data", ctypes.c_void_p)]

        for bsd in (True, False):
            with self.subTest(bsd=bsd):
                buffers, entries = [], []
                for flags, family, address in ((2, socket.AF_INET, "192.0.2.127"),
                                               (0, socket.AF_INET, "192.0.2.255"),
                                               (2, socket.AF_INET6, "192.0.2.255"),
                                               (2, socket.AF_INET, "0.0.0.0")):
                    header = bytes([16, family]) if bsd else family.to_bytes(2, sys.byteorder)
                    buffer = ctypes.create_string_buffer(header + bytes(2) + socket.inet_aton(address) + bytes(8))
                    buffers.append(buffer)
                    entry = Entry()
                    entry.flags = flags
                    entry.addr = entry.broadcast = ctypes.addressof(buffer)
                    entries.append(entry)
                for first, second in zip(entries, entries[1:]):
                    first.next = ctypes.pointer(second)

                def getifaddrs(output):
                    ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(Entry)))[0] = ctypes.pointer(entries[0])
                    return 0

                library = Mock(getifaddrs=getifaddrs)
                with patch("ctypes.util.find_library", return_value="test-c"), \
                     patch("ctypes.CDLL", return_value=library):
                    self.assertEqual(routes._getifaddrs_broadcasts(bsd), {"192.0.2.127"})
                library.freeifaddrs.assert_called_once()
        if sys.platform.startswith("linux"):
            self.assertEqual(routes._getifaddrs_broadcasts(bsd=False), interface_broadcasts())


class TestPortSharing(unittest.TestCase):
    """The shared bind beside an EFB's own 0.0.0.0 listener on one port (gui_runtime.udp_listener)."""
    RECEIVER = "192.0.2.1"  # TEST-NET-1: only routed, never sent to

    def setUp(self):
        from tools.gui_runtime import receiver_route, udp_listener
        self.udp_listener = udp_listener
        try:
            self.local = receiver_route(self.RECEIVER, 9)
        except ValueError:
            self.skipTest("no route to a non-loopback address on this machine")

    def efb(self, reuse):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if reuse:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(1)
        return sock

    def test_receiver_unicast_reaches_the_bridge_and_loopback_reaches_the_efb(self):
        with self.efb(reuse=True) as efb:
            port = efb.getsockname()[1]
            bridge = self.udp_listener(port, self.RECEIVER)
            with bridge, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                bridge.settimeout(1)
                self.assertEqual(bridge.getsockname(), (self.local, port))
                tx.sendto(b"receiver", (self.local, port))
                tx.sendto(b"output", ("127.0.0.1", port))
                self.assertEqual(bridge.recv(64), b"receiver")
                self.assertEqual(efb.recv(64), b"output")

    def test_an_efb_that_does_not_share_is_reported(self):
        with self.efb(reuse=False) as efb:
            port = efb.getsockname()[1]
            if sys.platform.startswith("linux"):  # and Android: both sockets need SO_REUSEADDR
                with self.assertRaisesRegex(ValueError, "does not share it"):
                    self.udp_listener(port, self.RECEIVER)
            else:  # Windows and BSD/macOS allow the pair when only the bridge sets its option
                self.udp_listener(port, self.RECEIVER).close()

    def test_receiver_tools_bind_like_a_shared_route(self):
        from tools.gui_runtime import Runtime
        with self.efb(reuse=True) as efb:
            port = efb.getsockname()[1]
            runtime = Runtime({"sentryIp": self.RECEIVER, "listenPort": port, "announcePort": 0})
            runtime.share_input = True  # the controller sets this from the saved route
            runtime.start(forwarding=False)
            try:
                self.assertEqual(runtime.rx.getsockname(), (self.local, port))
            finally:
                runtime.stop()

    def test_shared_input_follows_a_changed_route_and_explains_silence_once(self):
        from tools import gui_runtime
        with self.efb(reuse=True) as efb:
            port = efb.getsockname()[1]
            runtime = gui_runtime.Runtime({"sentryIp": self.RECEIVER, "listenPort": port, "announcePort": 0})
            runtime.rx = self.udp_listener(port, self.RECEIVER)
            runtime.shared, runtime.silence_noted, runtime.started_at, runtime.next_route_check = True, False, 0.0, 0.0
            try:
                # The computer joined another network after Start: the receiver is now reached from loopback here.
                with patch("tools.gui_runtime.receiver_route", return_value="127.0.0.1"):
                    runtime._shared_input(100.0)
                    self.assertEqual(runtime.rx.getsockname(), ("127.0.0.1", port))
                    self.assertIn("route to the receiver changed", runtime.logs[-1])
                    runtime._shared_input(102.0)  # checked every 5 s
                    runtime._shared_input(106.0)
                    runtime._shared_input(112.0)
                notes = [line for line in runtime.logs if "No receiver data" in line]
                self.assertEqual(len(notes), 1, list(runtime.logs))
                self.assertIn("if it broadcasts", notes[0])
            finally:
                runtime.rx.close()

    def test_receiver_on_this_device_or_unset_cannot_share(self):
        for receiver, message in (("127.0.0.1", "receiver on this device"), ("", "Receiver IP")):
            with self.subTest(receiver=receiver), self.assertRaisesRegex(ValueError, message):
                self.udp_listener(4000, receiver)


if __name__ == "__main__":
    unittest.main()
