"""Compare captured FIS-B frames before and after the bridge.

python tools/check_weather.py receiver.jsonl efb.jsonl --report dist/weather-check.json
python tools/check_weather.py session.pcapng session.pcapng --input-port 4000 --output-port 4001

Exit 0: complete matching frames. Exit 1: mismatch/corruption. Exit 2: no input
weather or a capture/argument error. This checks transport, not weather display.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import capture
import gdl90


def observations(path, port):
    records = (capture.read_jsonl(path, port) if path.suffix.lower() == ".jsonl"
               else capture.read_pcap(path, port, Counter()))
    frames = Counter()
    bad_crc = bad_length = 0
    for _, _, _, destination_port, data in records:
        if destination_port != port:
            continue
        for frame in gdl90.parse(data):
            if frame.msg_id != gdl90.MSG_UPLINK:
                continue
            if not frame.crc_ok:
                bad_crc += 1
            elif len(frame.msg) != 436:
                bad_length += 1
            else:
                frames[frame.msg] += 1
    return frames, bad_crc, bad_length


def compare(source, output, input_port=4000, output_port=4001):
    incoming, in_crc, in_length = observations(source, input_port)
    outgoing, out_crc, out_length = observations(output, output_port)
    missing = sum((incoming - outgoing).values())
    unexpected = sum((outgoing - incoming).values())
    status = ("failed" if in_crc + out_crc + in_length + out_length else
              "inconclusive" if not incoming else
              "failed" if missing or unexpected else "passed")
    return {
        "status": status,
        "inputPort": input_port,
        "outputPort": output_port,
        "inputFrames": sum(incoming.values()),
        "outputFrames": sum(outgoing.values()),
        "matchedFrames": sum((incoming & outgoing).values()),
        "missingFrames": missing,
        "unexpectedFrames": unexpected,
        "inputCrcFailures": in_crc,
        "outputCrcFailures": out_crc,
        "inputLengthFailures": in_length,
        "outputLengthFailures": out_length,
        "weatherDisplayVerified": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--input-port", type=int, default=4000)
    parser.add_argument("--output-port", type=int, default=4001)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not all(1 <= port <= 65535 for port in (args.input_port, args.output_port)):
        parser.error("Ports must be between 1 and 65535")
    same_file = args.source.resolve() == args.output.resolve()
    if same_file and (args.source.suffix.lower() == ".jsonl" or args.input_port == args.output_port):
        parser.error("Use separate JSONL recordings, or distinct destination ports in a shared pcap/pcapng")
    if args.report and args.report.resolve() in (args.source.resolve(), args.output.resolve()):
        parser.error("The report must not overwrite either capture")
    try:
        report = compare(args.source, args.output, args.input_port, args.output_port)
        text = json.dumps(report, indent=2) + "\n"
        if args.report:
            args.report.write_text(text)
        print(text, end="")
        return {"passed": 0, "failed": 1, "inconclusive": 2}[report["status"]]
    except (OSError, ValueError, KeyError, IndexError, struct.error) as error:
        print(f"Weather comparison could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
