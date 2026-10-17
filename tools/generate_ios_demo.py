"""Write ios/SentryBridge/DemoReceiver.bin: 60 s of simulated Sentry Compatibility Mode datagrams.

Synthetic data only (sim.SimSentry: ownship and five traffic targets around an arbitrary point), for
the app's labelled demo mode. Format, repeated: big-endian uint32 offset in ms, uint16 length, payload.
Deterministic; rerun after changing sim.py.
"""
from pathlib import Path
import struct
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sim

def main():
    generator = sim.SimSentry()
    out = bytearray()
    for second in range(60):
        datagrams = generator.datagrams(second)
        for index, payload in enumerate(datagrams):
            offset = second * 1000 + index * 1000 // max(1, len(datagrams))
            out += struct.pack(">IH", offset, len(payload)) + payload
    target = ROOT / "ios/SentryBridge/DemoReceiver.bin"
    target.write_bytes(bytes(out))
    print(f"{target.relative_to(ROOT)}: {len(out)} bytes")

if __name__ == "__main__":
    main()
