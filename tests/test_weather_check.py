"""Weather transport evidence must fail on loss, corruption, or missing observations."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import check_weather
import gdl90


def uplink(sequence=0, block_size=432):
    return gdl90.frame(bytes((0x07, sequence, 0xFF, 0xFF)) + b"\x7e" * block_size)


class TestWeatherCheck(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def recording(self, name, datagrams):
        path = self.root / name
        path.write_text("".join(json.dumps({"t": i, "src": "192.168.4.1", "hex": data.hex()}) + "\n"
                                for i, data in enumerate(datagrams)))
        return path

    def test_identical_messages_match_across_datagram_boundaries(self):
        ownship = gdl90.encode_report(gdl90.Report(address=1, lat=40, lon=-100, alt_ft=2000), 0x0A)
        source = self.recording("input.jsonl", [ownship + uplink(), uplink(1) + uplink()])
        output = self.recording("output.jsonl", [uplink(), uplink(1), uplink()])
        report = self.root / "report.json"
        with redirect_stdout(io.StringIO()):
            result = check_weather.main([str(source), str(output), "--report", str(report)])
        self.assertEqual(result, 0)
        evidence = json.loads(report.read_text())
        self.assertEqual(evidence["matchedFrames"], 3)
        self.assertFalse(evidence["weatherDisplayVerified"])
        self.assertNotIn("192.168.4.1", report.read_text())
        self.assertNotIn("lat", report.read_text())

    def test_missing_duplicate_is_not_hidden_by_a_set_comparison(self):
        source = self.recording("input.jsonl", [uplink(), uplink()])
        output = self.recording("output.jsonl", [uplink()])
        report = check_weather.compare(source, output)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["missingFrames"], 1)

    def test_changed_weather_with_a_valid_crc_is_rejected(self):
        source = self.recording("input.jsonl", [uplink()])
        output = self.recording("output.jsonl", [uplink(1)])
        report = check_weather.compare(source, output)
        self.assertEqual(report["status"], "failed")
        self.assertEqual((report["missingFrames"], report["unexpectedFrames"]), (1, 1))

    def test_bad_crc_and_wrong_length_are_reported_on_either_side(self):
        bad_crc = bytearray(uplink())
        bad_crc[2] ^= 1
        for defect, field in ((bad_crc, "CrcFailures"), (uplink(block_size=200), "LengthFailures")):
            for side in ("input", "output"):
                with self.subTest(side=side, defect=field):
                    source = self.recording("input.jsonl", [defect if side == "input" else uplink()])
                    output = self.recording("output.jsonl", [defect if side == "output" else uplink()])
                    report = check_weather.compare(source, output)
                    self.assertEqual(report["status"], "failed")
                    self.assertEqual(report[side + field], 1)

    def test_no_input_weather_is_inconclusive_and_returns_two(self):
        source = self.recording("input.jsonl", [gdl90.encode_heartbeat(True, 1)])
        output = self.recording("output.jsonl", [])
        with redirect_stdout(io.StringIO()):
            result = check_weather.main([str(source), str(output)])
        self.assertEqual(result, 2)
        self.assertEqual(check_weather.compare(source, output)["status"], "inconclusive")

    def test_shared_pcap_filters_destination_port_not_source_port(self):
        path = self.root / "both.pcap"
        with path.open("wb") as capture:
            capture.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101))
            for destination_port in (4000, 4001):
                payload = uplink()
                udp = struct.pack("!HHHH", 4000, destination_port, len(payload) + 8, 0) + payload
                ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, len(udp) + 20, 0, 0, 64, 17, 0,
                                 socket.inet_aton("192.168.4.1"), socket.inet_aton("192.168.4.2")) + udp
                capture.write(struct.pack("<IIII", destination_port, 0, len(ip), len(ip)) + ip)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check_weather.main([str(path), str(path)]), 0)
        report = check_weather.compare(path, path)
        self.assertEqual((report["inputFrames"], report["outputFrames"]), (1, 1))

    def test_original_capture_cannot_be_used_as_report_or_both_jsonl_inputs(self):
        source = self.recording("input.jsonl", [uplink()])
        output = self.recording("output.jsonl", [uplink()])
        original = source.read_bytes()
        for arguments in ([str(source), str(output), "--report", str(source)],
                          [str(source), str(source)]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                check_weather.main(arguments)
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(source.read_bytes(), original)

    def test_malformed_recording_returns_two(self):
        source = self.root / "input.jsonl"
        source.write_text('{"hex": "invalid"}\n')
        output = self.recording("output.jsonl", [uplink()])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(check_weather.main([str(source), str(output)]), 2)
