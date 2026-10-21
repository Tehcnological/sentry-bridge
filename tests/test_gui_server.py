"""Exercise GUI actions against real UDP sockets and the offline HTTP/UDP receiver."""
import base64
import gzip
import hashlib
import ipaddress
import re
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
import zipfile

import gdl90
import sim
import bridge
import ws_probe
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling test modules, when run on their own
from tools.native_core import decoder
from tools.gui_runtime import DEFAULTS, Runtime, validate_settings
from tools import gui_server
from tools.discovery import sweep as real_sweep
from tools.gui_server import Controller, Handler
from tools.routes import validate_route

gui_server.RELEASES = "http://127.0.0.1:9/"  # update checks in tests never reach GitHub (port 9 refuses)


ROOT = Path(__file__).resolve().parent.parent


def validate_settings_routes(request, settings):
    return validate_route(request, settings)["routes"]


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError("Timed out waiting for GUI operation")


class TestGUI(unittest.TestCase):
    def setUp(self):
        # Tests never probe the real network: no devices found, and every EFB answers (nothing to follow).
        for name, value in (("sweep", []), ("answers", True)):
            patcher = patch.object(gui_server.discovery, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        real_connect = ws_probe.connect

        def loopback_only(host, *args, **kwargs):
            if not ipaddress.IPv4Address(host).is_loopback:
                raise AssertionError(f"a test tried to reach {host}")
            return real_connect(host, *args, **kwargs)
        patcher = patch.object(ws_probe, "connect", loopback_only)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Controller(self.tmp.name)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def simulator(self):
        self.c.action("simulator", {"enabled": True})
        self.c.runtime.settings["intervalSeconds"] = .1

    def test_keepalive_schedule_and_one_request_at_a_time(self):
        busy = [True]
        calls = []
        runtime = Runtime({"sentryIp": "192.0.2.1"}, on_contact=lambda: calls.append(1) or not busy[0])
        runtime.last_contact = None
        runtime._contact(100.0)                 # not forwarding: never contacts
        self.assertEqual(calls, [])
        runtime.forwarding = True
        runtime._contact(100.0)                 # previous request still running: retried, not counted
        self.assertIsNone(runtime.last_contact)
        busy[0] = False
        runtime._contact(100.1)
        self.assertEqual(runtime.last_contact, 100.1)
        runtime._contact(105.0)                 # silent: every 5 s
        self.assertEqual(runtime.last_contact, 100.1)
        runtime._contact(105.2)
        self.assertEqual(runtime.last_contact, 105.2)
        runtime.last_rx = 134.0                 # receiving: every 30 s
        runtime._contact(134.5)
        self.assertEqual(runtime.last_contact, 105.2)
        runtime._contact(135.3)
        self.assertEqual(runtime.last_contact, 135.3)

    def test_keepalive_never_waits_for_the_controller_lock(self):
        """Stop holds the controller lock while it joins the UDP thread that asks for a keepalive."""
        held, release = threading.Event(), threading.Event()

        def stop_holding_the_lock():
            with self.c.lock:
                held.set()
                release.wait(5)
        holder = threading.Thread(target=stop_holding_the_lock)
        holder.start()
        held.wait(2)
        self.c.http = lambda *args, **kwargs: {}
        started = time.monotonic()
        try:
            self.assertTrue(self.c._contact())
            self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()
            holder.join()

    def test_simulator_receives_keepalive_outside_the_job_slot(self):
        gets = []
        http = self.c.http
        self.c.http = lambda path, *args, **kwargs: gets.append(path) or http(path, *args, **kwargs)
        self.simulator()
        self.c.action("start", self.route(free_port()))
        wait_for(lambda: self.c.contact_ok is True)
        self.assertIn("/?action=get", gets)
        self.assertIn("Keepalive: receiver status OK", "\n".join(self.c.runtime.logs))

    def test_sensors_come_from_the_statistics_websocket_while_forwarding(self):
        self.simulator()
        self.assertIsNone(self.c.status()["sensors"])
        self.c.action("start", self.route(free_port()))
        wait_for(lambda: self.c.status()["sensors"] is not None)
        # The cabin sensor, not the warmer baro sensor on the board.
        self.assertEqual(self.c.status()["sensors"], {"batteryPercent": 80, "coPpm": 0, "temperatureC": 21.5})
        self.c.action("stop", {})
        wait_for(lambda: not self.c.sensor_thread.is_alive())
        self.assertIsNone(self.c.status()["sensors"])

    def stats_peer(self, mode="refuse"):
        """A /stats peer. refuse: answers 404, so the reader waits to retry. pings: sends nothing but
        pings and counts pongs. silent: completes the handshake, then sends nothing and keeps the
        connection open. slow-handshake: trickles an endless handshake. partial-frame: completes
        the handshake, then trickles a frame that never ends. Counts connections, upgrade requests and
        connections still open."""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self.addCleanup(server.close)
        peer = dict(port=server.getsockname()[1], connections=0, requests=0, open=0, pongs=0)

        def handle(conn):
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = conn.recv(1024)
                    if not chunk:
                        return
                    request += chunk
                peer["requests"] += 1
                if mode == "refuse":
                    conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    return
                key = re.search(rb"Sec-WebSocket-Key: (\S+)", request).group(1)
                accept = base64.b64encode(hashlib.sha1(key + ws_probe.GUID.encode()).digest())
                head = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                if mode == "silent":  # like a receiver that restarted: open, never a message, never closed
                    conn.sendall(head)
                    while conn.recv(4096):
                        pass
                    return
                if mode == "slow-handshake":
                    data, every = head[:-2] + b"X" * 100000, .1  # the blank line never comes
                elif mode == "partial-frame":
                    conn.sendall(head)
                    data, every = b"\x81\x7e\x10\x00" + b"x" * 4095, .1  # 4096 bytes announced, one missing
                else:
                    conn.sendall(head)
                    data, every = b"\x89\x00" * 100000, .01  # pings, never a data message
                conn.settimeout(every)
                try:
                    for index in range(0, len(data), 2 if mode == "pings" else 1):
                        conn.sendall(data[index:index + (2 if mode == "pings" else 1)])
                        try:
                            reply = conn.recv(4096)
                            if not reply:
                                return
                            peer["pongs"] += reply.count(b"\x8a")
                        except socket.timeout:
                            pass
                except OSError:
                    return

        def serve():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                peer["connections"] += 1
                peer["open"] += 1

                def run(conn=conn):
                    try:
                        handle(conn)
                    except OSError:
                        pass
                    finally:
                        peer["open"] -= 1
                threading.Thread(target=run, daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
        return peer

    def forward_to(self, peer):
        """Forwarding as far as the sensor reader can tell, without a UDP listener."""
        self.c.http = lambda *args, **kwargs: {}
        self.c.runtime.settings.update(sentryIp="127.0.0.1", httpPort=peer["port"])
        self.c.runtime.forwarding = True
        self.c.runtime.active = lambda: True
        self.c.runtime.started_at = getattr(self.c.runtime, "started_at", 0) + 1  # a new session (monotonic ticks are 16 ms on Windows)
        self.c._contact()

    def test_exit_stops_a_sensor_reader_that_only_receives_pings(self):
        peer = self.stats_peer("pings")
        self.forward_to(peer)
        wait_for(lambda: peer["pongs"] > 0)
        self.c.close()
        self.assertFalse(self.c.sensor_thread.is_alive())
        pongs = peer["pongs"]
        time.sleep(.5)
        self.assertEqual(peer["pongs"], pongs)  # nothing sent after Exit returned

    def test_exit_stops_a_sensor_reader_in_a_trickled_handshake_or_frame(self):
        for mode in ("slow-handshake", "partial-frame"):
            with self.subTest(mode):
                if mode != "slow-handshake":  # the first run uses setUp's controller
                    self.c = Controller(Path(self.tmp.name) / mode)
                peer = self.stats_peer(mode)
                self.forward_to(peer)
                wait_for(lambda: peer["requests"] == 1)
                time.sleep(.6)  # bytes keep arriving, faster than any socket timeout
                started = time.monotonic()
                self.c.close()
                self.assertLess(time.monotonic() - started, 2)
                self.assertFalse(self.c.sensor_thread.is_alive())
                wait_for(lambda: peer["open"] == 0)

    def test_exit_during_a_slow_connect_sends_no_upgrade_request(self):
        peer = self.stats_peer("pings")
        connecting = threading.Event()

        def slow(read, write, failed, timeout):
            connecting.set()  # the connection is reported complete only after Exit
            time.sleep(timeout)
            return [], [], []
        with patch.object(ws_probe.select, "select", slow):
            self.forward_to(peer)
            connecting.wait(2)
            self.c.close()
            self.assertFalse(self.c.sensor_thread.is_alive())
        time.sleep(.5)
        self.assertEqual(peer["requests"], 0)

    def test_a_silent_stats_connection_is_replaced(self):
        # A receiver that restarts never closes the old connection; it just goes quiet. The reader must notice
        # and connect again instead of waiting on it for the rest of the session.
        # Pings, or a frame trickled a byte at a time, keep bytes arriving but are no message either.
        for mode in ("silent", "pings", "partial-frame"):
            with self.subTest(mode):
                if mode != "silent":  # the first run uses setUp's controller
                    self.c.close()
                    self.c = Controller(Path(self.tmp.name) / mode)
                peer = self.stats_peer(mode)
                with patch.object(gui_server, "SENSORS_SILENT_S", .5), patch.object(gui_server, "SENSORS_RETRY_S", .2):
                    self.forward_to(peer)
                    wait_for(lambda: peer["connections"] >= 2)

    def follow(self, answering, swept):
        """Forwarding to 192.168.4.19 with fast checks; answering() says whether the EFB answers each time."""
        self.c.http = lambda *args, **kwargs: {}
        self.c._read_sensors = lambda *args: None  # no sensor connection to the default receiver
        self.c.local_address = lambda: "192.168.4.4"
        self.c.routes_request = dict(host="windows", topology="network", address="192.168.4.19")
        self.c.runtime.routes = [("192.168.4.19", 4000)]
        self.c.runtime.forwarding = True
        self.c.runtime.active = lambda: True
        self.c.runtime.last_rx = time.monotonic() + 3600  # receiver data keeps arriving
        self.c.runtime.started_at = getattr(self.c.runtime, "started_at", 0) + 1  # a new session (monotonic ticks are 16 ms on Windows)
        return (patch.object(gui_server, "EFB_CHECK_S", .05), patch.object(gui_server, "EFB_MISSES", 2),
                patch.object(gui_server.discovery, "answers", side_effect=lambda address: answering(address)),
                patch.object(gui_server.discovery, "sweep", return_value=swept))

    def test_an_efb_that_moved_is_followed_to_the_only_other_device(self):
        calls = []
        patches = self.follow(lambda address: calls.append(address) or len(calls) <= 2, ["192.168.4.6"])
        with patches[0], patches[1], patches[2], patches[3]:
            self.c._contact()
            wait_for(lambda: "now sending to 192.168.4.6" in "\n".join(self.c.runtime.logs))  # logged last
        self.assertEqual(self.c.runtime.routes, [("192.168.4.6", 4000)])
        self.assertEqual(self.c.routes_request["address"], "192.168.4.6")  # the screen and the next Start

    def test_an_efb_is_not_moved_when_several_devices_answer_or_it_never_answered(self):
        for answered_before, swept in ((True, ["192.168.4.6", "192.168.4.7"]), (False, ["192.168.4.6"])):
            with self.subTest(answered_before=answered_before, swept=swept):
                self.c.close()
                self.c = Controller(Path(self.tmp.name) / str(len(swept)))
                calls = []
                patches = self.follow(lambda address: calls.append(address) or (answered_before and len(calls) == 1), swept)
                with patches[0], patches[1], patches[2], patches[3]:
                    self.c._contact()
                    wait_for(lambda: len(calls) >= 6)
                self.assertEqual(self.c.runtime.routes, [("192.168.4.19", 4000)])
                self.assertEqual("stopped answering" in "\n".join(self.c.runtime.logs), answered_before)

    def test_an_efb_that_answers_the_sweep_again_is_kept(self):
        calls = []
        patches = self.follow(lambda address: calls.append(address) or len(calls) == 1, ["192.168.4.6", "192.168.4.19"])
        with patches[0], patches[1], patches[2], patches[3] as sweep:
            self.c._contact()
            wait_for(lambda: sweep.call_count >= 2)
        self.assertEqual(self.c.runtime.routes, [("192.168.4.19", 4000)])
        self.assertNotIn("stopped answering", "\n".join(self.c.runtime.logs))

    def test_exit_during_a_probe_starts_no_sweep_and_waits_for_the_follower(self):
        calls, probing = [], threading.Event()

        def slow(address):
            calls.append(address)
            if len(calls) == 3:
                probing.set()
                time.sleep(.5)  # Exit arrives while this probe runs
            return len(calls) == 1
        patches = self.follow(slow, ["192.168.4.6"])
        with patches[0], patches[1], patches[2], patches[3] as sweep:
            self.c._contact()
            probing.wait(5)
            self.c.close()
            self.assertFalse(self.c.follow_thread.is_alive())
            self.assertEqual(sweep.call_count, 0)
        self.assertEqual(self.c.runtime.routes, [("192.168.4.19", 4000)])

    def test_no_follow_while_no_receiver_data_arrives(self):
        # Without receiver data the computer may be on another network: nothing is probed or moved.
        calls = []
        patches = self.follow(lambda address: calls.append(address) or False, ["192.168.4.6"])
        self.c.runtime.last_rx = None
        with patches[0], patches[1], patches[2], patches[3] as sweep:
            self.c._contact()
            time.sleep(.5)
        self.assertEqual((calls, sweep.call_count), ([], 0))
        self.assertEqual(self.c.runtime.routes, [("192.168.4.19", 4000)])

    def test_find_efb_adds_devices_found_by_the_sweep(self):
        self.c.local_address = lambda: "192.168.4.4"
        self.c.runtime.settings.update(announcePort=free_port())
        with patch.object(gui_server.discovery, "sweep", return_value=["192.168.4.6"]):
            found = self.c.discover(.3)
        self.assertEqual(found["efbs"], [dict(app="Device", address="192.168.4.6", port=4000)])

    def test_exit_during_find_efb_cancels_and_waits_for_the_sweep(self):
        self.c.local_address = lambda: "192.168.4.4"
        self.c.runtime.settings.update(announcePort=free_port())
        started, outcome = [], []

        def probe(address):
            started.append(time.monotonic())
            time.sleep(.5)  # Exit arrives while the probes run
            return True

        def find():
            try:
                outcome.append(self.c.discover(5))
            except gui_server.Closing as error:
                outcome.append(error)
        job = threading.Thread(target=find)
        with patch.object(gui_server.discovery, "sweep", side_effect=lambda local, **kwargs: real_sweep(local, probe=probe, **kwargs)):
            job.start()
            wait_for(lambda: started)
            self.c.close()
            job.join(10)
            done = time.monotonic()
            self.assertFalse(any(t.name == "gui-sweep" and t.is_alive() for t in threading.enumerate()))
            time.sleep(.3)
        self.assertIsInstance(outcome[0], gui_server.Closing)
        self.assertFalse([t for t in started if t > done])

    def test_restart_cancels_the_waiting_reader_and_uses_the_new_receiver(self):
        old, new = self.stats_peer(), self.stats_peer("pings")
        self.forward_to(old)
        wait_for(lambda: old["connections"] == 1)
        first = self.c.sensor_thread
        self.forward_to(new)  # Stop, new receiver, Start while the first reader waits to retry
        wait_for(lambda: new["connections"] == 1)
        first.join(2)
        self.assertFalse(first.is_alive())
        time.sleep(.2)
        self.assertEqual(old["connections"], 1)

    def test_restart_during_a_trickled_frame_closes_the_old_reader_first(self):
        old, new = self.stats_peer("partial-frame"), self.stats_peer("pings")
        self.forward_to(old)
        wait_for(lambda: old["requests"] == 1)
        first = self.c.sensor_thread
        self.forward_to(new)
        wait_for(lambda: new["connections"] == 1)
        self.assertFalse(first.is_alive())  # the replacement connects only after the old reader ended
        wait_for(lambda: old["open"] == 0)

    def send_simulator_packets(self):
        receiver = self.c.simulator
        packets = (receiver.sim.datagrams(0) if receiver.public else
                   [gdl90.frame(bytes([0x26]) + bytes(19))])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for packet in packets:
                sender.sendto(packet, ("127.0.0.1", receiver.port))

    def synchronized_simulator(self):
        # These tests own packet delivery; a 0.7 s window must not race the simulator's
        # 0.5 s cadence or the scheduler. Keep the real UDP transport and decoder.
        with patch("tools.gui_runtime.Simulator._run", lambda receiver: receiver.stop_event.wait()):
            self.simulator()
        observe = self.c.observe_mode

        def synchronized_observation(seconds):
            clock = SimpleNamespace(elapsed=0.0, time=time.time)
            clock.monotonic = lambda: clock.elapsed

            def deliver(_):
                key = "0" if self.c.simulator.public else str(0x26)
                before = self.c.runtime.snapshot()["frames"].get(key, 0)
                self.send_simulator_packets()
                wait_for(lambda: self.c.runtime.snapshot()["frames"].get(key, 0) > before)
                clock.elapsed = seconds

            clock.sleep = deliver
            # Patch only the controller's clock, leaving socket and decoder timing real.
            with patch("tools.gui_server.time", clock):
                return observe(seconds)

        self.c.observe_mode = synchronized_observation

    def job(self, action, body=None):
        self.c.action(action, body or {})
        wait_for(lambda: not self.c.busy())
        self.assertEqual(self.c.job["state"], "done", self.c.job)
        return self.c.job["result"]

    def route(self, port):
        """This device on port: the EFB port is a setting; the route only says where the EFB runs."""
        self.c.runtime.settings = {**self.c.runtime.settings, "efbPort": port}
        return dict(host="windows", topology="same", address="127.0.0.1")

    def test_receiver_switches_backup_and_packet_verification(self):
        self.synchronized_simulator()
        result = self.job("compatibility", dict(enabled=True, seconds=.7))
        self.assertTrue(result["verification"]["mode"].startswith("PUBLIC"), result)
        with zipfile.ZipFile(self.c.file(result["backup"])) as archive:
            self.assertEqual(set(archive.namelist()), {"sentry-status.json", "sentry-settings.json"})
            self.assertNotIn("publicGDL90", json.loads(archive.read("sentry-settings.json")))
        result = self.job("compatibility", dict(enabled=False, seconds=.7))
        self.assertTrue(result["verification"]["mode"].startswith("ENCRYPTED"), result)
        self.assertFalse(self.c.runtime.active())

    def test_real_forwarding_capture_and_check_share_input_socket(self):
        self.synchronized_simulator()
        self.job("compatibility", dict(enabled=True, verify=False))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
            rx.bind(("127.0.0.1", 0))
            rx.settimeout(4)
            self.c.action("start", self.route(rx.getsockname()[1]))
            capture_id = self.c.action("capture-start", {})["capture"]
            self.send_simulator_packets()
            observed = set()
            end = time.monotonic() + 4
            while time.monotonic() < end and not {7, 0x14}.issubset(observed):
                observed.update(f.msg_id for f in gdl90.parse(rx.recv(65535)) if f.crc_ok)
            self.assertTrue({0, 7, 0x14}.issubset(observed), observed)
            wait_for(lambda: not self.c.busy())
            result = self.job("check", {"seconds": .7})
            self.assertTrue(result["mode"].startswith("PUBLIC"))
            self.assertTrue(self.c.runtime.snapshot()["running"])
            self.c.action("capture-stop", {})
            self.assertTrue(self.c.runtime.snapshot()["running"])
            self.assertGreater(self.c.file(capture_id).stat().st_size, 100)
            report = self.job("analyze", {"capture": capture_id})
            self.assertEqual(report["exitCode"], 0)
            self.assertIn("PUBLIC", report["text"])

    def test_auto_restore_then_manual_disable_is_respected(self):
        self.simulator()
        self.c.action("start", self.route(free_port()))
        wait_for(lambda: self.c.simulator.public, 12)
        wait_for(lambda: not self.c.busy(), 12)
        self.job("compatibility", {"enabled": False, "seconds": .7})
        time.sleep(1)
        self.assertFalse(self.c.simulator.public)
        self.assertTrue(self.c.runtime.snapshot()["running"])

    def test_auto_restore_works_at_file_limit_after_receiver_restart(self):
        self.simulator()
        for _ in range(100):
            _, path = self.c.artifact("kept.txt", "report")
            path.write_text("keep")
            self.c.complete_file(next(reversed(self.c.artifacts)))
        self.c.action("start", self.route(free_port()))
        wait_for(lambda: self.c.simulator.public, 12)
        wait_for(lambda: not self.c.busy(), 12)
        self.assertEqual(self.c.job["state"], "done", self.c.job)
        self.c.action("stop", {})
        self.c.simulator.public = False
        self.c.action("start", self.route(free_port()))
        wait_for(lambda: self.c.simulator.public, 12)
        wait_for(lambda: not self.c.busy(), 12)
        self.assertEqual(len(self.c.artifacts), 100)
        self.assertTrue(self.c.runtime.snapshot()["running"])

    def test_auto_restore_survives_failed_backup_and_cleans_partial_file(self):
        self.simulator()
        def failed_backup(*args):
            _, path = self.c.artifact("partial.zip", "backup")
            path.write_bytes(b"incomplete")
            raise OSError("disk full")
        with patch.object(self.c, "backup", side_effect=failed_backup):
            self.c._auto_enable()
            wait_for(lambda: not self.c.busy(), 12)
        self.assertEqual(self.c.job["state"], "done", self.c.job)
        self.assertTrue(self.c.simulator.public)
        self.assertEqual(self.c.artifacts, {})
        self.assertEqual(list(self.c.data_dir.glob("*-partial.zip")), [])

    def test_auto_restore_keeps_only_recent_automatic_backups(self):
        self.simulator()
        self.c.observe_mode = lambda seconds: {"mode": "PUBLIC (standard GDL90)"}  # backups only
        _, manual = self.c.artifact("sentry-backup.zip", "backup")
        manual.write_bytes(b"manual")
        self.c.complete_file(next(reversed(self.c.artifacts)))
        for _ in range(7):
            self.c.simulator.public = False
            self.c._auto_enable()
            wait_for(lambda: not self.c.busy(), 12)
            self.assertEqual(self.c.job["state"], "done", self.c.job)
        names = [item["name"] for item in self.c.artifacts.values()]
        self.assertEqual(names.count("sentry-auto-backup.zip"), 5)
        self.assertEqual(names.count("sentry-backup.zip"), 1)
        self.assertEqual(len(list(self.c.data_dir.glob("*-sentry-auto-backup.zip"))), 5)
        self.assertTrue(manual.is_file())

    def test_exit_during_automatic_backup_sends_nothing_further(self):
        self.c.runtime.settings = {**self.c.runtime.settings, "listenPort": free_port(), "announcePort": 0}
        reading, release = threading.Event(), threading.Event()
        calls = []

        def receiver(path, body=None, raw=False):
            calls.append((path, body))
            if len(calls) == 1:  # hold the backup's first status read until Exit is underway
                reading.set()
                release.wait(10)
            return {}

        self.c.http = receiver
        with patch.object(self.c.runtime, "start", wraps=self.c.runtime.start) as start:
            self.assertTrue(self.c._auto_enable())
            self.assertTrue(reading.wait(5))
            self.assertEqual(self.c.action("quit", {}), {"exiting": True})
            closer = threading.Thread(target=self.c.close)
            closer.start()
            time.sleep(.3)  # close() is now waiting for the job
            release.set()
            closer.join(10)
            self.assertFalse(closer.is_alive())
            start.assert_not_called()
        self.assertEqual(calls, [("/?action=get", None)])  # no settings read, no publicGDL90 request
        self.assertEqual((self.c.job["state"], self.c.job["error"]), ("failed", "Sentry Bridge is closing."))
        self.assertFalse(any("continuing mode recovery" in line for line in self.c.runtime.logs))
        self.assertFalse(self.c.runtime.active())
        with self.assertRaises(ValueError):
            self.c.action("check", {"seconds": 1})

    def test_exit_during_an_empty_log_download_requests_no_further_log(self):
        reading, release = threading.Event(), threading.Event()
        requested = []

        class Response:
            headers = {}

            def __init__(self, url):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                if self.url.endswith("/first.csv"):  # hold the first read, then report EOF
                    reading.set()
                    release.wait(10)
                return b""

        class Opener:
            def open(self, request, timeout):
                requested.append(request.full_url)
                return Response(request.full_url)

        self.c.device_logs = ["first.csv", "second.csv"]
        with patch("sentry_ctl.RECEIVER", Opener()):
            self.c.action("download-logs", {"name": "all"})
            self.assertTrue(reading.wait(5))
            self.assertEqual(self.c.action("quit", {}), {"exiting": True})
            closer = threading.Thread(target=self.c.close)
            closer.start()
            time.sleep(.3)  # close() is now waiting for the job
            release.set()
            closer.join(10)
            self.assertFalse(closer.is_alive())
        self.assertEqual([url.rsplit("/", 1)[1] for url in requested], ["first.csv"])
        self.assertEqual((self.c.job["state"], self.c.job["error"]), ("failed", "Sentry Bridge is closing."))
        self.assertEqual(self.c.artifacts, {})  # the incomplete log archive is discarded
        self.assertEqual(sorted(p.name for p in self.c.data_dir.iterdir()
                                if p.name.endswith((".zip", ".download"))), [])

    def test_exit_stops_a_running_diagnostic_worker(self):
        marker = self.c.data_dir / "worker-finished"
        sleeper = [sys.executable, "-c", f"import time, pathlib; time.sleep(30); pathlib.Path({str(marker)!r}).touch()"]
        self.c._submit("Probe WebSocket", lambda: self.c.run_worker(sleeper, 60))
        time.sleep(1)  # the worker process is running
        self.assertEqual(self.c.action("quit", {}), {"exiting": True})
        started = time.monotonic()
        self.c.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((self.c.job["state"], self.c.job["error"]), ("failed", "Sentry Bridge is closing."))
        time.sleep(1)
        self.assertFalse(marker.exists())

    def worker_with_child(self):
        """A worker that starts a child; the child leaves a marker only if it survives 2.5 s."""
        started, survived = self.c.data_dir / "child-started", self.c.data_dir / "child-survived"
        child = f"import time, pathlib; time.sleep(2.5); pathlib.Path({str(survived)!r}).touch()"
        parent = (f"import subprocess, sys, time, pathlib; subprocess.Popen([sys.executable, '-c', {child!r}]); "
                  f"pathlib.Path({str(started)!r}).touch(); time.sleep(30)")
        return [sys.executable, "-c", parent], started, survived

    def test_exit_stops_a_worker_and_its_children(self):
        command, started, survived = self.worker_with_child()
        self.c._submit("Analyze capture", lambda: self.c.run_worker(command, 60))
        wait_for(started.exists)
        self.assertEqual(self.c.action("quit", {}), {"exiting": True})
        begun = time.monotonic()
        self.c.close()
        self.assertLess(time.monotonic() - begun, 5)
        self.assertEqual(self.c.job["error"], "Sentry Bridge is closing.")
        time.sleep(3.5)
        self.assertFalse(survived.exists(), "the worker's child kept running after Exit")

    def test_worker_timeout_stops_its_children(self):
        command, started, survived = self.worker_with_child()
        with self.assertRaisesRegex(ValueError, "did not finish within 1.5 seconds"):
            self.c.run_worker(command, 1.5)
        self.assertTrue(started.exists())
        time.sleep(3.5)
        self.assertFalse(survived.exists(), "the worker's child kept running after the timeout")

    def test_diagnostic_worker_timeout_stops_the_process(self):
        with self.assertRaisesRegex(ValueError, "did not finish within 0.5 seconds"):
            self.c.run_worker([sys.executable, "-c", "import time; time.sleep(30)"], .5)
        self.assertEqual(self.c.run_worker([sys.executable, "-c", "print('done')"], 30), (0, "done" + os.linesep))

    def test_find_efb_listens_for_announcements_while_stopped(self):
        port = free_port()
        self.c.runtime.settings = {**self.c.runtime.settings, "announcePort": port}
        bound = threading.Event()
        real_listener = gui_server.udp_listener

        def listener(listen_port):  # signal once the discovery socket is bound
            sock = real_listener(listen_port)
            bound.set()
            return sock

        with patch.object(self.c, "efb_candidate", lambda source, efb_port: True), \
             patch("tools.gui_server.udp_listener", listener):  # loopback sender
            self.c.action("discover", {"seconds": 1.5})
            self.assertTrue(bound.wait(5))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                for data in (b'{"App":"ForeFlight","GDL90":{"port":4000}}', b"not json",
                             b'{"App":"Avare","GDL90":{"port":4001}}', b'{"App":"ForeFlight","GDL90":{"port":4000}}'):
                    sender.sendto(data, ("127.0.0.1", port))
            wait_for(lambda: not self.c.busy())
        self.assertEqual(self.c.job["state"], "done", self.c.job)
        # Repeats are listed once; invalid announcements are ignored.
        self.assertEqual(self.c.job["result"]["efbs"], [{"app": "ForeFlight", "address": "127.0.0.1", "port": 4000},
                                                        {"app": "Avare", "address": "127.0.0.1", "port": 4001}])

    def remote_announcement(self, address, port, app="ForeFlight"):
        data = json.dumps({"App": app, "GDL90": {"port": port}}).encode()
        return SimpleNamespace(recvfrom=lambda size: (data, (address, 63093)))

    def test_running_bridge_offers_a_remote_efb_on_port_4000(self):
        runtime = self.c.runtime  # default receiver input port 4000
        self.assertEqual(runtime.settings["listenPort"], 4000)
        runtime.ann = self.remote_announcement("192.168.4.30", 4000)
        runtime._announce([runtime.ann], time.monotonic())
        self.assertIn(("192.168.4.30", 4000), runtime.discovered)
        with patch.object(runtime, "active", return_value=True):
            result = self.c.discover(1)
        self.assertEqual(result, {"efbs": [{"app": "ForeFlight", "address": "192.168.4.30", "port": 4000}]})

    def test_discovery_list_is_bounded_pruned_and_cleared_on_restart(self):
        runtime = Runtime({**DEFAULTS, "listenPort": free_port(), "announcePort": free_port()})
        with patch("tools.gui_runtime.check_endpoint", return_value="192.168.4.2"):
            for round_number in range(4):  # 32 new EFBs per round; earlier rounds expire
                now = round_number * 40.0
                for host in range(32):
                    runtime.ann = self.remote_announcement(f"192.168.{4 + round_number}.{host + 10}", 4001)
                    runtime._announce([runtime.ann], now)
                self.assertEqual(len(runtime.discovered), 32)
                self.assertTrue(all(seen == now for _, seen in runtime.discovered.values()))
            for host in range(40):  # more than the limit at once: the newest are not stored
                runtime.ann = self.remote_announcement(f"192.168.9.{host + 10}", 4001)
                runtime._announce([runtime.ann], 200.0)
        self.assertEqual(len(runtime.discovered), 32)
        runtime.start(forwarding=False)
        try:
            self.assertEqual(runtime.discovered, {})
        finally:
            runtime.stop()

    def test_find_efb_uses_the_running_bridges_list(self):
        now = time.monotonic()
        self.c.runtime.discovered = {("192.168.4.20", 4000): ("ForeFlight", now),
                                     ("192.168.4.21", 4000): ("Stale", now - 60)}
        with patch.object(self.c.runtime, "active", return_value=True), \
             patch.object(self.c.runtime, "ann", object(), create=True), \
             patch.object(self.c, "efb_candidate", lambda source, efb_port: True), \
             patch("tools.gui_server.udp_listener", side_effect=AssertionError("port is the bridge's")):
            result = self.c.discover(1)
        self.assertEqual(result, {"efbs": [{"app": "ForeFlight", "address": "192.168.4.20", "port": 4000}]})

    def test_running_bridge_records_announcing_efbs_for_find_efb(self):
        runtime = Runtime({**DEFAULTS, "listenPort": free_port(), "announcePort": free_port()})
        runtime.ann = SimpleNamespace(recvfrom=lambda size: (b'{"App":"ForeFlight","GDL90":{"port":4000}}', ("192.168.4.30", 63093)))
        with patch("tools.gui_runtime.check_endpoint", return_value="192.168.4.2"):
            runtime._announce([runtime.ann], 5.0)
        self.assertEqual(runtime.discovered, {("192.168.4.30", 4000): ("ForeFlight", 5.0)})

    def test_find_efb_offers_only_valid_other_device_addresses(self):
        self.assertFalse(self.c.efb_candidate("127.0.0.1", 4000))
        self.assertFalse(self.c.efb_candidate(self.c.runtime.settings["sentryIp"], 4000))
        with patch("tools.gui_server.check_endpoint", side_effect=ValueError("broadcast")):
            self.assertFalse(self.c.efb_candidate("192.168.4.255", 4000))
        with patch("tools.gui_server.check_endpoint", return_value="192.168.4.2"):
            self.assertTrue(self.c.efb_candidate("192.168.4.20", 4000))
        self.c.runtime.settings = {**self.c.runtime.settings, "announcePort": 0}
        with self.assertRaisesRegex(ValueError, "discovery is off"):
            self.c.discover(1)
        self.c.closing.set()
        self.c.runtime.settings = {**self.c.runtime.settings, "announcePort": free_port()}
        with self.assertRaisesRegex(ValueError, "closing"):
            self.c.discover(1)

    def test_exit_does_not_wait_for_a_running_receiver_job(self):
        self.simulator()
        self.c.action("check", {"seconds": 30})
        wait_for(lambda: self.c.runtime.active())
        self.assertEqual(self.c.action("quit", {}), {"exiting": True})
        started = time.monotonic()
        self.c.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.c.job["state"], "failed")
        self.assertIn("closing", self.c.job["error"])
        self.assertFalse(self.c.runtime.active())

    def test_failed_job_does_not_consume_file_slots(self):
        def fail():
            _, path = self.c.artifact("bad.zip", "logs")
            path.write_bytes(b"partial")
            raise OSError("download failed")
        self.c._submit("Download", fail)
        wait_for(lambda: not self.c.busy())
        self.assertEqual(self.c.job["state"], "failed")
        self.assertEqual(self.c.artifacts, {})
        self.assertEqual(list(self.c.data_dir.glob("*-bad.zip")), [])

    def test_completed_backup_survives_later_job_failure(self):
        def fail_after_backup():
            token, path = self.c.artifact("good.zip", "backup")
            path.write_bytes(b"completed backup")
            self.c.complete_file(token)
            raise OSError("mode verification failed")
        self.c._submit("Mode", fail_after_backup)
        wait_for(lambda: not self.c.busy())
        self.assertEqual(len(self.c.artifacts), 1)
        self.assertTrue(next(iter(self.c.artifacts.values()))["path"].is_file())

    def test_receiver_settings_cannot_be_silently_discarded_by_simulator(self):
        original = dict(self.c.runtime.settings)
        self.simulator()
        with self.assertRaisesRegex(ValueError, "Turn off the simulator"):
            self.c.action("settings", {**self.c.runtime.settings, "autoRestore": False})
        self.c.action("simulator", {"enabled": False})
        self.assertEqual(self.c.runtime.settings, original)

    def test_non_loopback_local_output_cannot_feed_input_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.168.4.3", 4000))
            address = probe.getsockname()[0]
        self.c.action("settings", {"sentryIp": "", "listenPort": free_port()})
        self.c.runtime.settings = {**self.c.runtime.settings, "efbPort": self.c.runtime.settings["listenPort"]}
        request = dict(host="windows", topology="network", address=address)
        with self.assertRaisesRegex(ValueError, "ports must differ"):
            self.c.action("start", request)
        self.assertFalse(self.c.runtime.active())

    def test_packet_fault_is_logged_and_forwarding_continues(self):
        self.simulator()
        self.job("compatibility", dict(enabled=True, verify=False))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
            rx.bind(("127.0.0.1", 0))
            rx.settimeout(4)
            self.c.action("start", self.route(rx.getsockname()[1]))
            wait_for(lambda: hasattr(self.c.runtime, "core"))
            core = self.c.runtime.core
            real, faults = core.feed, []

            def faulty(data, source, now):
                if len(faults) < 3:
                    faults.append(now)
                    raise RuntimeError("synthetic decoder fault")
                return real(data, source, now)
            core.feed = faulty
            wait_for(lambda: len(faults) == 3)
            self.assertTrue(self.c.runtime.active())
            end, traffic = time.monotonic() + 4, 0
            while time.monotonic() < end and not traffic:
                traffic = sum(f.msg_id == 0x14 for f in gdl90.parse(rx.recv(65535)) if f.crc_ok)
            self.assertTrue(traffic, "traffic stopped after a packet fault")
            self.assertTrue(self.c.runtime.active())
            self.assertIn("synthetic decoder fault", self.c.runtime.snapshot()["error"])

    def test_repeated_packet_faults_do_not_stop_traffic_reports(self):
        runtime = Runtime({"announcePort": 0, "intervalSeconds": .125})
        runtime.core, _ = decoder(runtime.settings, runtime.log)
        receiver = sim.SimSentry()
        for packet in receiver.datagrams(0):
            runtime.core.feed(packet, "192.168.4.1", 0)
        self.assertTrue(runtime.core.tick(0), "Preload known targets before injecting faults")
        runtime.forwarding = True
        runtime.routes = [("127.0.0.1", 4001)]
        runtime.rx = Mock()
        runtime.ann = None
        packet = gdl90.frame(bytes([0x26]) + bytes(19))
        runtime.rx.recvfrom.return_value = (packet, ("192.168.4.1", 4000))
        sender = Mock()
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, .125, .25, .375]), strftime=time.strftime)
        cycles = []

        def ready(*args):
            cycles.append(True)
            if len(cycles) == 3:
                runtime.stop_event.set()
            return [runtime.rx], [], []

        with patch.object(runtime.core, "feed", side_effect=RuntimeError("every packet fails")) as feed, \
             patch("tools.gui_runtime.time", clock), \
             patch("tools.gui_runtime.select.select", side_effect=ready), \
             patch("tools.gui_runtime.socket.socket", return_value=sender):
            runtime._run()
        self.assertEqual(feed.call_count, 3)
        reports = [frame for call in sender.sendto.call_args_list
                   for frame in gdl90.parse(call.args[0]) if frame.crc_ok and frame.msg_id == 0x14]
        self.assertEqual(len(reports), 3 * len(receiver.tracks))
        self.assertEqual(sum("Skipped a packet" in line for line in runtime.logs), 1)
        self.assertIn("every packet fails", runtime.view["error"])

    def test_auto_enable_requested_while_busy_runs_when_operation_finishes(self):
        self.simulator()
        entered, release, deferred = threading.Event(), threading.Event(), threading.Event()

        def operation():
            entered.set()
            if not release.wait(8):
                raise AssertionError("Test did not release the receiver operation")
            return {}

        original = self.c.runtime.on_enable
        def on_enable():
            accepted = original()
            if not accepted:
                deferred.set()
            return accepted

        self.c.runtime.on_enable = on_enable
        self.c._submit("Blocked operation", operation)
        try:
            self.assertTrue(entered.wait(8))
            self.c.runtime.start(validate_settings_routes(self.route(free_port()), self.c.runtime.settings))
            self.assertTrue(deferred.wait(8), "Wait until an actual enable request is deferred")
            self.assertTrue(self.c.busy())
            release.set()
            wait_for(lambda: self.c.simulator.public)
            wait_for(lambda: not self.c.busy())
            self.assertEqual(self.c.job["state"], "done", self.c.job)
        finally:
            release.set()

    def test_close_finishes_shutdown_when_bridge_thread_is_slow(self):
        with tempfile.TemporaryDirectory() as data:
            c = Controller(data)
            c.action("simulator", {"enabled": True})
            simulator = c.simulator
            with patch.object(c.runtime, "stop", side_effect=ValueError("Bridge is still stopping; try again.")):
                c.close()
            self.assertTrue(simulator.stop_event.is_set())
            self.assertTrue(c.executor._shutdown)

    def test_recording_failure_does_not_stop_weather_or_traffic_forwarding(self):
        class FullDisk:
            def tell(self): return 0
            def write(self, value): raise OSError("No space left on device")
            def flush(self): pass
            def close(self): raise OSError("No space left on device")
        self.simulator()
        self.job("compatibility", dict(enabled=True, verify=False))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0)); receiver.settimeout(4)
            self.c.action("start", self.route(receiver.getsockname()[1]))
            with self.c.runtime.lock:
                self.c.runtime.recording = (FullDisk(), None)
            wait_for(lambda: not self.c.runtime.snapshot()["recording"])
            self.assertTrue(self.c.runtime.snapshot()["running"])
            self.assertIn("Recording stopped", self.c.runtime.snapshot()["error"])
            seen = set()
            while not {7, 0x14}.issubset(seen):
                seen.update(frame.msg_id for frame in gdl90.parse(receiver.recv(65535)) if frame.crc_ok)
            self.assertTrue({7, 0x14}.issubset(seen))

    def test_recording_close_error_on_stop_releases_input_port(self):
        class CloseFails:
            def close(self): raise OSError("No space left on device")
        port = free_port()
        runtime = Runtime({**DEFAULTS, "sentryIp": "127.0.0.1", "listenPort": port, "announcePort": 0})
        runtime.start(forwarding=False)
        with runtime.lock:
            runtime.recording = (CloseFails(), None)
        runtime.stop()
        self.assertFalse(runtime.active())
        self.assertIn("Recording could not be saved", runtime.snapshot()["error"])
        runtime.start(forwarding=False)  # binds the same UDP port again
        runtime.stop()

    def test_gps_status_follows_heartbeat_freshness_only(self):
        runtime = Runtime({**DEFAULTS, "listenPort": free_port(), "announcePort": 0})
        clock = SimpleNamespace(monotonic=lambda: 100.0, strftime=time.strftime, time=time.time)
        cases = (("absent", None, True, False, None),
                 ("fresh valid", 99.0, True, True, 1.0),
                 ("fresh invalid", 99.0, False, False, 1.0),
                 ("expired while weather continues", 95.0, True, False, 5.0))
        with patch("tools.gui_runtime.time", clock):
            for name, heartbeat, valid, gps, age in cases:
                runtime.last_rx, runtime.last_heartbeat, runtime.view["gps"] = 100.0, heartbeat, valid
                snapshot = runtime.snapshot()
                self.assertEqual((snapshot["gps"], snapshot["heartbeatSeconds"]), (gps, age), name)
        runtime.start(forwarding=False)  # a restart forgets the previous heartbeat
        try:
            self.assertIsNone(runtime.snapshot()["heartbeatSeconds"])
        finally:
            runtime.stop()

    def test_only_complete_crc_valid_heartbeats_refresh_gps(self):
        runtime = Runtime({**DEFAULTS, "sentryIp": "127.0.0.1", "listenPort": free_port(), "announcePort": 0})
        runtime.core = SimpleNamespace(feed=lambda data, source, now: [])
        runtime.forwarding = False
        valid, invalid = gdl90.encode_heartbeat(True, 100), gdl90.encode_heartbeat(False, 100)
        bad_crc = valid[:-3] + bytes([valid[-3] ^ 1]) + valid[-2:]
        uplink = gdl90.frame(bytes([0x07]) + bytes(435))

        class OneDatagram:
            def __init__(self, data): self.data = data
            def recvfrom(self, size): return self.data, ("127.0.0.1", 4000)

        def receive(data, now):
            runtime.rx = OneDatagram(data)
            runtime._receive([runtime.rx], now, lambda frames, now: None)

        receive(valid, 0.0)
        self.assertEqual((runtime.gps, runtime.last_heartbeat), (True, 0.0))
        for t in range(1, 11):  # undersized, bad-CRC and weather-only datagrams
            receive((gdl90.frame(bytes([0])), bad_crc, uplink)[t % 3], float(t))
        self.assertEqual((runtime.gps, runtime.last_heartbeat), (True, 0.0))
        receive(uplink + invalid, 11.0)  # a complete heartbeat later in a datagram counts
        self.assertEqual((runtime.gps, runtime.last_heartbeat), (False, 11.0))
        receive(valid, 12.0)
        self.assertEqual((runtime.gps, runtime.last_heartbeat), (True, 12.0))

    def test_gps_fix_expires_when_heartbeats_stop_but_weather_continues(self):
        port = free_port()
        runtime = Runtime({**DEFAULTS, "sentryIp": "127.0.0.1", "listenPort": port, "announcePort": 0})
        runtime.start(forwarding=False)
        uplink = gdl90.frame(bytes([0x07]) + bytes(435))
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(gdl90.encode_heartbeat(True, 100), ("127.0.0.1", port))
                wait_for(lambda: runtime.snapshot()["gps"])
                with patch("tools.gui_runtime.HEARTBEAT_STALE_S", 0.5):
                    end = time.monotonic() + 1.5
                    while time.monotonic() < end:  # weather keeps the receiver "live"
                        sender.sendto(uplink, ("127.0.0.1", port))
                        time.sleep(.1)
                    snapshot = runtime.snapshot()
            self.assertLess(snapshot["lastPacketSeconds"], 1)
            self.assertFalse(snapshot["gps"])
        finally:
            runtime.stop()

    def test_logs_downloaded_with_limit_and_safe_archive_names(self):
        self.simulator()
        logs = self.job("logs")["logs"]
        self.assertEqual(logs, ["simulator.csv"])
        result = self.job("download-logs", {"name": "all", "maxMb": 1})
        with zipfile.ZipFile(self.c.file(result["download"])) as archive:
            self.assertEqual(archive.read("1-simulator.csv"), b"time,battery\n0,100\n")
        self.assertFalse(result["files"][0]["truncated"])

    def test_simulator_restores_real_settings_and_blocks_external_output(self):
        original = dict(self.c.runtime.settings)
        self.simulator()
        self.assertFalse(self.c.config_path.exists())
        with self.assertRaisesRegex(ValueError, "This device"):
            self.c.action("start", dict(host="windows", topology="network", address="192.168.4.3"))
        self.c.action("simulator", {"enabled": False})
        self.assertEqual(self.c.runtime.settings, original)

    def test_update_check_reports_a_newer_stable_release_once_a_day(self):
        from http.server import BaseHTTPRequestHandler
        hits, tag = [], ["v99.0.0"]

        class Releases(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hits.append(self.path)
                body = json.dumps({"tag_name": tag[0], "html_url": "https://github.com/x/y/releases/tag/" + tag[0]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Releases)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.object(gui_server, "RELEASES", f"http://127.0.0.1:{server.server_port}/latest"):
                self.c.update_due = 0
                self.c.runtime.settings = {**self.c.runtime.settings, "updateCheck": False}
                self.c.status()
                time.sleep(.3)
                self.assertEqual(hits, [])  # off: GitHub is never contacted
                self.c.runtime.settings = {**self.c.runtime.settings, "updateCheck": True}
                self.c.status()
                wait_for(lambda: self.c.update is not None)
                self.assertEqual(self.c.status()["update"]["version"], "99.0.0")
                self.c.status()
                time.sleep(.3)
                self.assertEqual(len(hits), 1)  # not due again for a day
                self.c.update_due = 0  # due: eight concurrent status requests start one check
                threads = [threading.Thread(target=self.c.status) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                time.sleep(.3)
                wait_for(lambda: not self.c.update_checking)
                self.assertEqual(len(hits), 2)
                other = Controller(self.tmp.name)
                try:
                    self.assertEqual(other.status()["update"]["version"], "99.0.0")
                    other.version = "99.0.0"  # that release is now installed: no notice for it
                    self.assertIsNone(other.status()["update"])
                finally:
                    other.close()
                tag[0], self.c.update_due = gui_server.app_version(), 0  # the current version is not an update
                self.c.status()
                wait_for(lambda: self.c.update is None)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(gui_server.newer("v0.2.1", "0.2.0") and gui_server.newer("v1.0.0", "0.9.9"))
        for tag_, current in (("v0.2.0", "0.2.0"), ("v0.1.9", "0.2.0"), ("v0.3.0-beta", "0.2.0"), (None, "0.2.0")):
            self.assertFalse(gui_server.newer(tag_, current), tag_)

    def test_settings_save_keeps_the_saved_route_startable(self):
        clash = {**DEFAULTS, "sentryIp": "192.0.2.1", "ownshipHex": "ABCDEF", "efbPort": 63093, "announcePort": 63093}
        # This device: the clash would make the saved route unstartable, so the save is refused.
        self.c.routes_request = {"host": "windows", "topology": "same"}
        self.c.save()
        with self.assertRaisesRegex(ValueError, "discovery port"):
            self.c.action("settings", clash)
        self.assertEqual(self.c.runtime.settings["efbPort"], 4000)
        # Other device: the EFB port is on that device, so the same ports save and survive a restart.
        self.c.routes_request = {"host": "windows", "topology": "network", "address": "192.0.2.10"}
        self.assertEqual(self.c.action("settings", clash), {"saved": True})
        other = Controller(self.tmp.name)
        try:
            self.assertEqual(other.runtime.settings["efbPort"], 63093)
            self.assertEqual(other.routes_request["address"], "192.0.2.10")
        finally:
            other.close()

    def test_unusable_saved_route_keeps_the_receiver_settings(self):
        self.c.close()
        clash = {**DEFAULTS, "sentryIp": "192.0.2.1", "ownshipHex": "ABCDEF", "efbPort": 63093, "announcePort": 63093}
        (Path(self.tmp.name) / "gui-settings.json").write_text(
            json.dumps({"settings": clash, "routes": {"host": "windows", "topology": "same"}}), encoding="utf-8")
        self.c = Controller(self.tmp.name)
        self.assertEqual((self.c.runtime.settings["sentryIp"], self.c.runtime.settings["ownshipHex"]), ("192.0.2.1", "ABCDEF"))
        self.assertIsNone(self.c.routes_request)
        self.assertTrue(any("not restored" in line for line in self.c.runtime.logs))

    def test_settings_validation_and_persistence(self):
        settings = {**DEFAULTS, "ownshipHex": "a12345", "autoRestore": False, "reference": [47.5, 8.5]}
        self.c.action("settings", settings)
        other = Controller(self.tmp.name)
        try:
            self.assertEqual(other.runtime.settings["ownshipHex"], "A12345")
            self.assertFalse(other.runtime.settings["autoRestore"])
        finally:
            other.close()
        for bad in ({"listenPort": True}, {"sentryIp": "localhost"}, {"ownshipHex": "XYZ"},
                    {"reference": [1, float("nan")]}, {"autoRestore": 1}, {"unknown": "x"},
                    {"announcePort": 4000}, {"staleSeconds": -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_settings(bad)

    def test_session_token_file_is_private_and_replaced_each_launch(self):
        session = Path(self.tmp.name) / "gui-session"
        self.assertEqual(session.read_text(encoding="ascii"), self.c.token)
        if os.name == "posix":
            session.chmod(0o644)   # a token file left readable by an earlier version
            previous = os.umask(0o022)
            try:
                other = Controller(self.tmp.name)
            finally:
                os.umask(previous)
            try:
                self.assertEqual(session.stat().st_mode & 0o777, 0o600)
                self.assertEqual(session.read_text(encoding="ascii"), other.token)
            finally:
                other.close()
        self.assertEqual([p.name for p in Path(self.tmp.name).glob("gui-session*")], ["gui-session"])

    def test_data_folder_is_private(self):
        folder = Path(self.tmp.name) / "shared"
        folder.mkdir()
        if os.name == "posix":
            folder.chmod(0o755)   # as an earlier version left it under umask 022
        other = Controller(folder)
        try:
            if os.name == "posix":   # Windows folders inherit the user profile's ACL
                self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertTrue((folder / "gui-session").is_file())
        finally:
            other.close()

    def test_session_token_waits_for_a_second_launch_reading_it(self):
        """On Windows an open reader of gui-session makes os.replace fail (WinError 5). POSIX has no
        such lock, so there the wrapper refuses while the reader is held, as Windows does."""
        session = Path(self.tmp.name) / "gui-session"
        replace, blocked, held = os.replace, threading.Event(), threading.Event()

        def observed_replace(source, target):
            if os.name != "nt" and held.is_set():
                blocked.set()
                raise PermissionError(13, "held by a reader", str(target))
            try:
                return replace(source, target)
            except PermissionError:
                blocked.set()   # the open reader really stopped a replacement
                raise

        started = {}
        with patch.object(gui_server.os, "replace", observed_replace):
            held.set()
            reader = session.open("r", encoding="ascii")
            try:
                launch = threading.Thread(target=lambda: started.update(controller=Controller(self.tmp.name)))
                launch.start()
                self.assertTrue(blocked.wait(10))
            finally:
                held.clear()
                reader.close()
            launch.join(30)
            other = started["controller"]
            try:
                self.assertEqual(session.read_text(encoding="ascii"), other.token)
            finally:
                other.close()
            # A reader that never closes: startup fails after the retries and leaves no temporary file.
            held.set()
            with session.open("r", encoding="ascii"), self.assertRaises(PermissionError):
                Controller(self.tmp.name)
        self.assertEqual([p.name for p in Path(self.tmp.name).glob("gui-session*")], ["gui-session"])

    def test_saved_files_survive_restart_and_index_cannot_escape_data_directory(self):
        token, path = self.c.artifact("result.txt", "report")
        path.write_text("saved")
        self.c.complete_file(token)
        other = Controller(self.tmp.name)
        try:
            self.assertEqual(other.file(token).read_text(), "saved")
            self.assertEqual(other.status()["artifacts"][0]["name"], "result.txt")
        finally:
            other.close()
        self.c.index_path.write_text(json.dumps({"a" * 32: {"name": "../../secrets", "kind": "report"}}))
        other = Controller(self.tmp.name)
        try:
            self.assertEqual(other.artifacts, {})
        finally:
            other.close()

    def test_capture_auto_stops_and_cannot_download_active_recording(self):
        self.simulator()
        self.job("compatibility", {"enabled": True, "verify": False})
        token = self.c.action("capture-start", {"seconds": .8})["capture"]
        with self.assertRaisesRegex(ValueError, "Stop the capture"):
            self.c.file(token)
        wait_for(lambda: not self.c.runtime.active())
        self.assertGreater(self.c.file(token).stat().st_size, 0)
        self.c.action("delete-file", {"id": token})
        with self.assertRaises(ValueError):
            self.c.file(token)

    def test_busy_configuration_and_conflicting_ports_rejected(self):
        self.simulator()
        self.c.action("check", {"seconds": .5})
        with self.assertRaisesRegex(ValueError, "Wait"):
            self.c.action("settings", {})
        wait_for(lambda: not self.c.busy())
        port = self.c.runtime.settings["listenPort"]
        with self.assertRaisesRegex(ValueError, "cannot share"):  # simulator: the receiver is on this device
            self.c.action("start", self.route(port))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as held:
            held.bind(("0.0.0.0", port))
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.c.action("start", self.route(free_port()))

    def test_upload_analysis_weather_and_no_arbitrary_file_access(self):
        data = sim.uplink_0x07(__import__("random").Random(1))
        text = json.dumps(dict(t=1, src="192.168.4.1", hex=data.hex())) + "\n"
        uploaded = [self.c.action("upload", {"name": "flight.jsonl", "data": base64.b64encode(text.encode()).decode()})["capture"] for _ in range(2)]
        result = self.job("weather", {"source": uploaded[0], "output": uploaded[1]})
        self.assertEqual(result["exitCode"], 0, result)
        self.assertEqual(json.loads(result["text"])["matchedFrames"], 1)
        for token in ("../../local-signing/key.p12", str(Path(self.tmp.name) / "anything")):
            with self.assertRaises(ValueError):
                self.c.file(token)
        with self.assertRaises(ValueError):
            self.c.action("upload", {"name": "evil.py", "data": "eA=="})

    def test_websocket_probe_uses_receiver_settings_and_exports_results(self):
        from test_ws_probe import FakeSentry
        receiver = FakeSentry()
        receiver.start()
        self.c.action("settings", {"sentryIp": "127.0.0.1", "httpPort": receiver.port})
        result = self.job("websocket", {"seconds": 1})
        receiver.join(timeout=3)
        self.assertEqual(result["exitCode"], 0, result)
        self.assertIn("status.battery", result["text"])
        self.assertTrue(self.c.file(result["recording"]).is_file())
        with self.assertRaises(ValueError):
            self.c.action("websocket", {"path": "/\r\nInjected: x"})

    def test_runtime_adapter_matches_reference_with_filter_reference_and_full_weather(self):
        settings = validate_settings({"sentryIp": "192.168.4.1", "ownshipHex": "A00001",
                                      "reference": list(sim.ORIGIN), "staleSeconds": 5, "relay": True})
        core, engine = decoder(settings, lambda text: None)
        reference = bridge.Bridge(sentry_ip=settings["sentryIp"], ownship_icao=0xA00001,
                                  ref=sim.ORIGIN, stale_s=5, relay=True, log=lambda text: None)
        receiver = sim.SimSentry(tracks=sim.default_tracks() + sim.uat_tracks(), fisb=True)
        for second in range(8):
            for data in receiver.datagrams(second):
                self.assertEqual(core.feed(data, "192.168.4.1", second), reference.feed(data, "192.168.4.1", second))
            self.assertEqual(core.tick(second), reference.tick(second))
        self.assertEqual(core.stats.uat_msgs, reference.stats.uat_msgs)
        self.assertEqual(core.stats.frames[7], 8)
        self.assertEqual(core.tick(20), reference.tick(20))

    def test_receiver_redirects_are_refused(self):
        from http.server import BaseHTTPRequestHandler
        followed = []

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/elsewhere":
                    followed.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.c.action("settings", {"sentryIp": "127.0.0.1", "httpPort": server.server_port})
            with self.assertRaisesRegex(ValueError, "redirected"):
                self.c.http("/?action=get")
            self.assertEqual(followed, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_malformed_receiver_http_is_a_receiver_error(self):
        # The keepalive thread catches ValueError only; a bad status line must arrive as one.
        from test_bridge import malformed_http_server
        host, close = malformed_http_server()
        try:
            self.c.action("settings", {"sentryIp": "127.0.0.1", "httpPort": int(host.rsplit(":", 1)[1])})
            with self.assertRaisesRegex(ValueError, "Cannot reach the receiver"):
                self.c.http("/?action=get")
        finally:
            close()

    def test_compressed_receiver_json_and_plain_text_acknowledgement(self):
        from http.server import BaseHTTPRequestHandler
        class Compressed(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                text = (b'<a href="gzip.csv">gzip.csv</a>' if self.path == "/data/" else
                        b"time,battery\n0,100\n" if self.path == "/data/gzip.csv" else
                        b'{"Power":{"powerSavings":false}}')
                body = gzip.compress(text)
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Compressed)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.c.action("settings", {"sentryIp": "127.0.0.1", "httpPort": server.server_port})
            self.assertFalse(self.c.http("/settings/?action=get")["Power"]["powerSavings"])
            self.assertEqual(self.c.http("/settings/?action=set", {"publicGDL90": True}), {"httpStatus": 200})
            self.assertEqual(self.job("logs")["logs"], ["gzip.csv"])
            result = self.job("download-logs", {"name": "gzip.csv", "maxMb": 1})
            with zipfile.ZipFile(self.c.file(result["download"])) as archive:
                self.assertEqual(archive.read("1-gzip.csv"), b"time,battery\n0,100\n")
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class TestGUIHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Controller(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.controller = self.c
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.c.close()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        return urlopen(Request(self.url + path, headers={"Authorization": "Bearer " + self.c.token, **(headers or {})}))

    def assertStatus(self, code, request):
        with self.assertRaises(HTTPError) as failure:
            urlopen(request)
        self.assertEqual(failure.exception.code, code)
        failure.exception.close()

    def test_http_routes_origin_checks_and_downloads(self):
        auth = {"Authorization": "Bearer " + self.c.token}
        with self.get("/api/status") as response:
            self.assertEqual(json.load(response)["adapter"], "local")
        self.assertStatus(403, Request(self.url + "/api/actions/settings", data=b"{}", headers={
            **auth, "Content-Type": "application/json", "Origin": "https://evil.example"}))
        for path in ("/local-signing/signing-certificate.der", "/../gui-settings.json", "/api/files/../../anything"):
            self.assertStatus(404, Request(self.url + path, headers=auth))
        token, path = self.c.artifact("result.txt", "report")
        path.write_text("example")
        with self.get("/api/files/" + token) as r:
            self.assertIn("attachment", r.headers["Content-Disposition"])
            self.assertEqual(r.read(), b"example")
        self.assertStatus(400, Request(self.url + "/api/actions/settings", data=b"[]",
                                       headers={**auth, "Content-Type": "application/json"}))

    def test_exit_stays_available_and_cancels_a_running_job(self):
        # The shared page (desktop and Android) must not disable Exit while a job runs.
        script = (ROOT / "web/app.js").read_text(encoding="utf-8")
        busy = next(line for line in script.splitlines() if 'button.disabled = pending || s.job.state === "running"' in line)
        self.assertNotIn("#quit", busy)
        self.assertIn('$("quit").disabled = pending;', script)
        # Find EFB only reads announcements: usable while forwarding; Use stays under the route lock.
        self.assertIn('$("find-efb").disabled = pending || s.job.state === "running";', script)
        self.assertIn('$("discovered").querySelectorAll("button").forEach(button => button.disabled = locked);', script)
        # Pressing Exit mid-job reaches the adapter, which accepts it and cancels the job.
        def job():
            while True:
                self.c.ensure_open()
                time.sleep(.05)
        self.c._submit("Check mode", job)
        with self.get("/api/status") as response:
            self.assertEqual(json.load(response)["job"]["state"], "running")
        request = Request(self.url + "/api/actions/quit", data=b"{}", headers={
            "Authorization": "Bearer " + self.c.token, "Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response), {"exiting": True})
        self.c.close()
        self.assertEqual((self.c.job["state"], self.c.job["error"]), ("failed", "Sentry Bridge is closing."))

    def late_body_status(self, port, headers):
        """POST whose body arrives after the server has decided: the client must still get the
        reply, not a reset (WinError 10053 before the fix)."""
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall((f"POST /api/actions/stop HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{headers}"
                            "Content-Length: 2\r\n\r\n").encode())
            time.sleep(.3)
            client.sendall(b"{}")
            time.sleep(.2)
            return client.recv(4096).split(b"\r\n", 1)[0].decode()

    def test_rejected_post_with_a_late_body_still_gets_its_reply(self):
        port = self.server.server_port
        self.assertIn(" 403 ", self.late_body_status(port, "Content-Type: application/json\r\n"))
        auth = f"Authorization: Bearer {self.c.token}\r\n"
        self.assertIn(" 400 ", self.late_body_status(port, auth + "Content-Type: text/plain\r\n"))

    def test_first_window_fits_small_screens(self):
        from tools import desktop_app
        self.assertEqual(desktop_app.first_window_size((2560, 1400)), (1200, 780))
        self.assertEqual(desktop_app.first_window_size((1366, 728)), (1200, 655))   # 1366x768 laptop, taskbar
        self.assertEqual(desktop_app.first_window_size((1280, 680)), (1152, 612))   # 1920x1080 at 150%
        # Only the first launch passes a size; afterwards Edge's saved window size is used.
        with patch("tools.desktop_app.subprocess.Popen") as popen, patch("tools.desktop_app.Path.is_file", return_value=True), \
             patch.dict(os.environ, {"PROGRAMFILES(X86)": "C:\\Program Files (x86)"}), \
             patch("tools.desktop_app.sys.platform", "win32"), patch("tools.desktop_app.first_window_size", return_value=(900, 600)), \
             patch("tools.desktop_app.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True):  # Windows-only constant
            data = Path(self.tmp.name)
            desktop_app.app_window("http://127.0.0.1:1/", data)
            self.assertIn("--window-size=900,600", popen.call_args.args[0])
            (data / "window-profile").mkdir()
            desktop_app.app_window("http://127.0.0.1:1/", data)
            self.assertFalse(any(a.startswith("--window-size") for a in popen.call_args.args[0]))

    def test_desktop_second_launch_opens_a_one_time_url(self):
        from tools import desktop_app
        status = self.c.status
        opened = []
        with patch.object(self.c, "status", lambda: {**status(), "desktop": True}), \
                patch.object(desktop_app, "app_window", lambda url, data_dir: opened.append(url)):
            self.assertEqual(desktop_app.main(["--port", str(self.server.server_port), "--data-dir", self.tmp.name]), 0)
        self.assertEqual(len(opened), 1)
        self.assertNotIn(self.c.token, opened[0])
        self.assertTrue(self.c.use_launch_code(opened[0].split("launch=", 1)[1]))

    def exhaust_launch_codes(self):
        for _ in range(8):
            self.c.session_url(self.server.server_port)

    def assert_instance_kept(self, token):
        """The running instance still answers its original token, which is still the saved one."""
        self.assertEqual(self.c.session_path.read_text(encoding="ascii"), token)
        with self.get("/api/status") as response:
            self.assertEqual(json.load(response)["adapter"], "local")

    def test_desktop_relaunch_reports_a_launch_failure_and_keeps_the_instance(self):
        from tools import desktop_app
        status, token = self.c.status, self.c.token
        args = ["--port", str(self.server.server_port), "--data-dir", self.tmp.name]
        opened = []

        def no_second_instance(*args, **kwargs):
            raise AssertionError("a second instance was started")
        # A plain function, not a mock: subclassing it (DesktopController) fails at once too.
        self.exhaust_launch_codes()
        with patch.object(self.c, "status", lambda: {**status(), "desktop": True}), \
                patch.object(desktop_app, "app_window", lambda url, data_dir: opened.append(url)), \
                patch.object(gui_server, "Controller", no_second_instance):
            with self.assertRaisesRegex(RuntimeError, "wait a minute"):
                desktop_app.main(args)
            self.assert_instance_kept(token)
            # Any other launch-endpoint failure is reported the same way.
            with patch.object(self.c, "session_url", side_effect=OSError("broken")), \
                    patch("socketserver.BaseServer.handle_error"):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    desktop_app.main(args)
            self.assert_instance_kept(token)
            # Once the unused codes expire, the same instance opens a window again.
            later = time.monotonic() + Controller.LAUNCH_CODE_SECONDS + 1
            with patch.object(gui_server.time, "monotonic", return_value=later):
                self.assertEqual(desktop_app.main(args), 0)
        self.assertEqual(len(opened), 1)
        self.assertNotIn(token, opened[0])
        self.assert_instance_kept(token)

    def test_start_gui_relaunch_reports_a_launch_failure_and_keeps_the_instance(self):
        if sys.platform != "win32":
            return  # Start-Gui.ps1 is the Windows launcher; the desktop launcher test covers POSIX
        token = self.c.token
        environment = dict(os.environ, SB_GUI_HEADLESS="1")

        def start_gui():
            return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                   str(ROOT / "tools" / "Start-Gui.ps1"), "-Port", str(self.server.server_port),
                                   "-DataDir", self.tmp.name], env=environment, capture_output=True, text=True, timeout=60)
        self.exhaust_launch_codes()
        result = start_gui()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("wait a minute", result.stderr)
        self.assert_instance_kept(token)
        # Once the unused codes expire, the same instance gives a one-time URL.
        with self.c.lock:
            self.c.launch_codes = {code: 0 for code in self.c.launch_codes}
        result = start_gui()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        url = result.stdout.strip()
        self.assertTrue(url.startswith(self.url + "/?launch="), url)
        self.assertNotIn(token, url)
        self.assert_instance_kept(token)

    def test_session_token_is_required_and_swapped_for_a_cookie(self):
        self.assertEqual(self.c.session_path.read_text(encoding="ascii"), self.c.token)
        json_post = {"Content-Type": "application/json"}
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer é"},
                        {"Cookie": f"sb{self.server.server_port}=wrong"}):
            for path in ("/", "/app.js", "/api/status", "/favicon.svg"):
                self.assertStatus(403, Request(self.url + path, headers=headers))
            self.assertStatus(403, Request(self.url + "/api/actions/stop", data=b"{}", headers={**json_post, **headers}))
            self.assertStatus(403, Request(self.url + "/api/validate", data=b"{}", headers={**json_post, **headers}))
        self.assertStatus(403, Request(self.url + "/?launch=wrong"))
        # The session token never works in a URL: browser command lines are readable on Linux.
        self.assertStatus(403, Request(self.url + "/?token=" + self.c.token))
        self.assertStatus(403, Request(self.url + "/?launch=" + self.c.token))
        # A second launch asks for a one-time URL with the token; without it, it gets none.
        self.assertStatus(403, Request(self.url + "/api/launch", data=b"", method="POST"))
        with urlopen(Request(self.url + "/api/launch", data=b"", method="POST",
                             headers={"Authorization": "Bearer " + self.c.token})) as response:
            launch_url = json.load(response)["url"]
        self.assertNotIn(self.c.token, launch_url)
        self.assertTrue(launch_url.startswith(self.url + "/?launch="), launch_url)
        # The launcher URL redirects to / with an HttpOnly cookie that then authorizes requests.
        opener = build_opener(NoRedirects)
        with self.assertRaises(HTTPError) as failure:
            opener.open(launch_url)
        redirect = failure.exception
        self.assertEqual((redirect.code, redirect.headers["Location"]), (303, "/"))
        cookie = redirect.headers["Set-Cookie"]
        redirect.close()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        with urlopen(Request(self.url + "/api/status", headers={"Cookie": cookie.split(";")[0]})) as response:
            self.assertEqual(json.load(response)["adapter"], "local")
        # Used once, and an unused code expires.
        self.assertStatus(403, Request(launch_url))
        expired = self.c.session_url(self.server.server_port)
        with patch.object(gui_server.time, "monotonic", return_value=time.monotonic() + Controller.LAUNCH_CODE_SECONDS + 1):
            self.assertStatus(403, Request(expired))
        # At most 8 unused codes at a time.
        for _ in range(8):
            self.c.session_url(self.server.server_port)
        with self.assertRaises(ValueError):
            self.c.session_url(self.server.server_port)


if __name__ == "__main__":
    unittest.main()
