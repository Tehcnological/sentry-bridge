"""Real UDP regression checks for both desktop frontends; all data is synthetic."""

from pathlib import Path
import socket
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling test modules, when run on their own
import test_cli as cli_support  # noqa: E402
import bridge
import gdl90
import sim
import uat


class NetworkSession:
    def __init__(self, command, relay=True, stale=15, receiver_ip="127.0.0.1"):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.http = cli_support.FakeSentryHTTP(power_savings=False)
        self.port = cli_support.free_udp()
        self.run = cli_support.Running(command, [
            "--listen-port", str(self.port), "--sentry-ip", receiver_ip,
            "--http-port", str(self.http.port), "--announce-port", "0",
            "--dest", f"127.0.0.1:{self.rx.getsockname()[1]}",
            "--interval", "0.1", "--stale", str(stale),
            "--stats-every", "60", "--ref", "40,-100",
            *(["--relay"] if relay else []),
        ])

    def __enter__(self):
        if not self.run.wait_for("listening"):
            self.close()
            raise RuntimeError(f"Bridge did not start:\n{self.run.log}")
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.run.stop()
        self.http.close()
        self.rx.close()
        self.tx.close()

    def send(self, data):
        self.tx.sendto(data, ("127.0.0.1", self.port))

    def receive(self, seconds):
        frames = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.rx.settimeout(max(0.001, min(0.1, end - time.monotonic())))
            try:
                data, _ = self.rx.recvfrom(65535)
            except socket.timeout:
                continue
            frames.extend(gdl90.parse(data))
        if self.run.p.poll() is not None:
            raise RuntimeError(f"Bridge exited unexpectedly:\n{self.run.log}")
        return frames


def weather(sequence=0):
    # Worst-case stuffing: a complete 432-byte block of flags and escape bytes.
    return gdl90.frame(bytes((0x07, sequence, 0xFF, 0xFF)) + b"\x7e\x7d" * 216)


def traffic_packet(model, second):
    payload = sim.uat_payload(0xA0CA70, 40.05, -99.92, 2000, 70, 40, 500,
                              ptype=1, callsign="N42UA")
    clean_uat = sim.wrap_0x24(uat.rs_encode(payload, uat.LONG_ROOTS))
    return b"".join(model.datagrams(second)) + clean_uat


class TestNetwork(unittest.TestCase):
    def test_large_stuffed_weather_and_multiple_frames_survive_udp(self):
        expected = [weather(i) for i in range(8)]
        self.assertTrue(all(len(frame) > 850 for frame in expected))
        heartbeat = gdl90.encode_heartbeat(True, 10)
        ownship = gdl90.encode_report(gdl90.Report(address=1, lat=40, lon=-100,
                                                  alt_ft=2000), gdl90.MSG_OWNSHIP)
        bad_crc = bytearray(weather(30))
        bad_crc[2] ^= 1
        self.assertFalse(gdl90.parse(bad_crc)[0].crc_ok)
        vendor = b"".join(gdl90.frame(bytes((mid,)) + bytes(18))
                          for mid in (0x21, 0x24, 0x25, 0x26, 0x28))
        for name, command in cli_support.IMPLS.items():
            with self.subTest(implementation=name), NetworkSession(command) as session:
                session.send(heartbeat + ownship + b"".join(expected) + bad_crc + vendor)
                received = session.receive(1)
                self.assertTrue(all(frame.crc_ok for frame in received))
                self.assertCountEqual([gdl90.frame(frame.msg) for frame in received],
                                      [heartbeat, ownship, *expected], session.run.log)

    def test_relay_disabled_still_decodes_traffic_without_forwarding_weather(self):
        model = sim.SimSentry(fisb=True)
        for name, command in cli_support.IMPLS.items():
            with self.subTest(implementation=name), NetworkSession(command, relay=False) as session:
                session.send(traffic_packet(model, 0) + weather())
                received = session.receive(1)
                self.assertTrue(received, session.run.log)
                self.assertEqual({frame.msg_id for frame in received}, {gdl90.MSG_TRAFFIC})
                addresses = {gdl90.decode_report(frame.msg).address for frame in received}
                self.assertEqual(addresses, {track.icao for track in model.tracks} | {0xA0CA70})

    def test_foreign_sender_is_filtered_but_the_receiver_is_accepted(self):
        for name, command in cli_support.IMPLS.items():
            # Change the accepted address instead of binding an alias unavailable on macOS.
            with self.subTest(implementation=name, source="foreign"), NetworkSession(command, receiver_ip="127.0.0.2") as session:
                session.send(weather(1))
                self.assertEqual(session.receive(0.3), [], session.run.log)
            with self.subTest(implementation=name, source="receiver"), NetworkSession(command) as session:
                session.send(weather(2))
                received = session.receive(0.5)
                self.assertEqual([gdl90.frame(frame.msg) for frame in received], [weather(2)])

    def test_stream_gap_expires_targets_and_recovers_without_restart(self):
        model = sim.SimSentry(fisb=True)
        expected_addresses = {track.icao for track in model.tracks} | {0xA0CA70}
        for name, command in cli_support.IMPLS.items():
            with self.subTest(implementation=name), NetworkSession(command, stale=1) as session:
                session.send(traffic_packet(model, 0))
                first = session.receive(0.7)
                self.assertEqual({gdl90.decode_report(frame.msg).address for frame in first
                                  if frame.msg_id == gdl90.MSG_TRAFFIC}, expected_addresses)
                # Drain reports while they age, then observe a real stream gap.
                session.receive(1.3)
                silent = session.receive(bridge.STREAM_GAP_S)
                self.assertEqual(silent, [], session.run.log)
                # One Power Savings read per stream start; keepalive kept asking while silent.
                self.assertEqual(session.http.gets.count("/settings/?action=get"), 1)
                self.assertGreaterEqual(session.http.gets.count("/?action=get"), 2, session.run.log)

                # Resumed GPS/weather must not revive old traffic positions.
                session.send(gdl90.encode_heartbeat(True, 12) + weather(12))
                resumed = session.receive(0.4)
                self.assertEqual([gdl90.frame(frame.msg) for frame in resumed
                                  if frame.msg_id == gdl90.MSG_UPLINK], [weather(12)])
                self.assertFalse(any(frame.msg_id == gdl90.MSG_TRAFFIC for frame in resumed))
                self.assertEqual(session.http.gets.count("/settings/?action=get"), 2)

                # Fresh positions restore both 1090 and UAT traffic in the same process.
                session.send(traffic_packet(model, 13))
                fresh = session.receive(0.7)
                self.assertEqual({gdl90.decode_report(frame.msg).address for frame in fresh
                                  if frame.msg_id == gdl90.MSG_TRAFFIC}, expected_addresses)
