"""Exercise the actual APK-compiled Kotlin capture tools with independent Python fixtures.

Needs a completed local Android build and org.json 20240303 in .local-tools/test-deps
(or SB_LOCAL_TOOLS/test-deps). The tools folder is only read; scratch files go to build/local.
This host check does not replace WebView/background/hardware validation on Android.
"""
import json
import os
from pathlib import Path
import random
import socket
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(os.environ.get("SB_LOCAL_TOOLS") or ROOT / ".local-tools")
sys.path.insert(0, str(ROOT))
import gdl90
import sentry_ctl
from tools.check_weather import compare


def main():
    env = json.loads((TOOLS / "android-environment.json").read_text(encoding="utf-8-sig"))
    java = Path(env["javaHome"]) / "bin/java.exe"
    javac = Path(env["javaHome"]) / "bin/javac.exe"
    stdlib = next((Path(env["gradleUserHome"]) / "caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-stdlib/2.0.21").rglob("*.jar"))
    dependencies = [ROOT / "android/app/build/tmp/kotlin-classes/release", stdlib,
                    Path(env["sdkRoot"]) / "platforms/android-34/android.jar", TOOLS / "test-deps/json-20240303.jar"]
    # org.json's host implementation must come before Android's non-executable SDK stubs.
    dependencies.insert(0, dependencies.pop())
    if not all(path.exists() for path in dependencies):
        raise RuntimeError("Build Android and install the documented host-test dependency first.")
    (ROOT / "build/local").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "build/local") as folder:
        folder = Path(folder)
        cp = os.pathsep.join(map(str, [folder, *dependencies]))
        # The host android.net.Network double precedes the SDK stubs on the class path (folder is first).
        subprocess.run([str(javac), "-cp", cp, "-d", str(folder), str(ROOT / "tests/jni/AndroidDiagnosticsReplay.java"),
                        str(ROOT / "tests/jni/host/android/net/Network.java")], check=True)
        def run(*arguments, okay=True):
            result = subprocess.run([str(java), "-cp", cp, "AndroidDiagnosticsReplay", *map(str, arguments)], capture_output=True, text=True, timeout=30)
            if okay and result.returncode:
                raise AssertionError(result.stderr)
            return result
        rng = random.Random(281)
        for counts in ({}, {0: 1}, {10: 1}, {38: 1}, {38: 1, 0: 1}, {33: 1}, {36: 1}):
            assert run("mode", json.dumps(counts)).stdout.strip() == sentry_ctl.describe_mode(counts)
        # Receiver log links: the same file names, and the same rejections, as sentry_ctl.log_name.
        links = ["log_0001.csv", "big%20log.csv", "a+b.csv", "%2e%2e", "..", "sub%2flog.csv", "a%5cb", "c:log.csv",
                 "log%0a.csv", "?n=1", "../"]
        expected = [sentry_ctl.log_name(link) or "-" for link in links]
        assert expected[2] == "a+b.csv", expected
        assert run("lognames", *links).stdout.splitlines() == expected
        # A malformed escape rejects that link only (URLDecoder threw for the whole list before).
        assert run("lognames", "%zz.csv", "log.csv").stdout.splitlines() == ["-", "log.csv"]
        # Receiver sensors: the same readings as ws_probe.sensor_values for the shared cases.
        cases = json.loads((ROOT / "tests/data/stats-cases.json").read_text(encoding="utf-8"))
        shown = [json.loads(line) for line in run("sensors", *(json.dumps(case["message"]) for case in cases)).stdout.splitlines()]
        assert shown == [case["expected"] for case in cases], shown
        assert "passed" in run("power").stdout
        assert "passed" in run("gps").stdout
        # GPS freshness refreshes only on complete CRC-valid heartbeats, valid or invalid GPS bit.
        valid, invalid = gdl90.encode_heartbeat(True, 100), gdl90.encode_heartbeat(False, 100)
        bad_crc = valid[:-3] + bytes([valid[-3] ^ 1]) + valid[-2:]
        weather = gdl90.frame(bytes([7]) + rng.randbytes(435))
        datagrams = [valid, invalid, gdl90.frame(bytes([0])), gdl90.frame(bytes(6)), bad_crc, weather,
                     weather + invalid, gdl90.frame(bytes([0x21]) + rng.randbytes(20))]
        expected = [str(any(f.crc_ok and f.msg_id == 0 and len(f.msg) >= 7 for f in gdl90.parse(d))).lower()
                    for d in datagrams]
        assert expected == ["true", "true", "false", "false", "false", "false", "true", "false"], expected
        assert run("heartbeat", *(d.hex() for d in datagrams)).stdout.split() == expected
        assert "contained" in run("disk").stdout
        assert "passed" in run("routes").stdout
        assert "passed" in run("settings").stdout
        # The shared bind to this host's own address (it stands in for the Sentry) after a late WiFi callback.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # selects a route; sends nothing
            local = probe.getsockname()[0]
            probe.close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        assert "passed" in run("sharedbind", local, port).stdout
        # The update check compares release tags exactly as the desktop does.
        from tools.gui_server import newer
        for tag, current in (("v0.2.1", "0.2.0"), ("v1.0.0", "0.9.9"), ("v0.2.0", "0.2.0"), ("v0.1.9", "0.2.0"), ("v0.3.0-beta", "0.2.0")):
            assert run("newer", tag, current).stdout.strip() == str(newer(tag, current)).lower(), tag
        assert "passed" in run("rebind", local, port).stdout
        assert "passed" in run("recovery", folder).stdout
        assert "passed" in run("closing", folder).stdout
        assert "passed" in run("handshake", folder).stdout
        assert "passed" in run("discovery").stdout
        assert "passed" in run("late-body").stdout
        for index in range(100):
            message = bytes([index % 256]) + rng.randbytes(rng.randrange(1, 500))
            data = gdl90.frame(message)
            if index % 2:
                data = data[:-2] + bytes([data[-2] ^ 1]) + data[-1:]
            expected = [dict(hex=f.msg.hex(), crc=f.crc_ok) for f in gdl90.parse(data)]
            assert json.loads(run("frames", data.hex()).stdout) == expected
        uplink = gdl90.frame(bytes([7]) + rng.randbytes(435))
        bad = gdl90.frame(bytes([7, 1, 2]))
        source, out = folder / "source.jsonl", folder / "out.jsonl"
        def jsonl(path, packets):
            path.write_text("".join(json.dumps(dict(t=i, hex=data.hex())) + "\n" for i, data in enumerate(packets)))
        for incoming, outgoing in [([uplink], [uplink]), ([uplink] * 2, [uplink]), ([], []), ([bad], [bad])]:
            jsonl(source, incoming); jsonl(out, outgoing)
            assert json.loads(run("weather", source, out, 4000, 4001).stdout) == compare(source, out)
        # One shared pcap with distinct ports, with Ethernet and IPv4/UDP headers.
        packets = []
        for port in (4000, 4001):
            udp = struct.pack("!HHHH", 9999, port, len(uplink) + 8, 0) + uplink
            ip = bytes([0x45, 0]) + struct.pack("!H", len(udp) + 20) + bytes(4) + bytes([64, 17]) + bytes(2) + bytes([192,168,4,1,192,168,4,2]) + udp
            packet = bytes(12) + b"\x08\x00" + ip
            packets.append(packet)
        pcap = folder / "both.pcap"
        pcap.write_bytes(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1) + b"".join(struct.pack("<IIII", i, 0, len(p), len(p)) + p for i,p in enumerate(packets)))
        assert json.loads(run("weather", pcap, pcap, 4000, 4001).stdout) == compare(pcap, pcap)
        assert "0x07: 1 frames, 0 bad CRC" in run("analyze", pcap, 4000).stdout
        def block(kind, body):
            size = len(body) + 12
            return struct.pack("<II", kind, size) + body + struct.pack("<I", size)
        pcapng = folder / "both.pcapng"
        body = block(0x0a0d0d0a, struct.pack("<IHHq", 0x1a2b3c4d, 1, 0, -1)) + block(1, struct.pack("<HHI", 1, 0, 65535))
        for p in packets:
            body += block(6, struct.pack("<IIIII", 0, 0, 1000000, len(p), len(p)) + p + bytes(-len(p) % 4))
        pcapng.write_bytes(body)
        assert json.loads(run("weather", pcapng, pcapng, 4000, 4001).stdout) == compare(pcapng, pcapng)
        pcapng.write_bytes(body[:-5])
        assert run("analyze", pcapng, 4000, okay=False).returncode != 0
        print("Android Kotlin diagnostics: receiver log names as sentry_ctl, GPS-only mode parity, notification warning, heartbeat GPS freshness, complete-heartbeat detection, Exit during jobs and a slow WebSocket handshake, and contained disk failure; 100 CRC/framing comparisons; JSONL/pcap/pcapng weather parity; truncated capture rejection passed.")


if __name__ == "__main__":
    main()
