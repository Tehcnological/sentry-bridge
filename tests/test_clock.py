"""Run the Python network loop while the wall clock jumps in either direction.

The fake transport feeds actual traffic/encrypted frames through the real Bridge. Only
sockets, HTTP and the clocks are replaced; staleness and retry decisions remain real.
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge
import gdl90
from test_traffic_semantics import position, SENTRY


class TestClockCorrections(unittest.TestCase):
    def test_wall_clock_jumps_do_not_extend_targets_or_change_retries(self):
        for jump in (-3600, 3600):
            with self.subTest(jump=jump):
                current = {"mono": 0.0}
                times = iter((1.0, 10.0, 17.0, 21.0, 22.0, 41.0))
                packet = position() + gdl90.frame(b"\x26\x00" + bytes(18))
                b = bridge.Bridge(ref=(40.0, -100.0), log=lambda m: None)
                rx, tx = MagicMock(), MagicMock()
                rx.recvfrom.return_value = (packet, (SENTRY, 4000))
                retries = []
                raw = io.StringIO()
                clocks = SimpleNamespace(
                    monotonic=lambda: current["mono"],
                    time=lambda: 1700000000 + current["mono"] + (jump if current["mono"] > 1 else 0),
                    strftime=lambda fmt: "12:00:00 ")

                def select(*args):
                    try:
                        current["mono"] = next(times)
                    except StopIteration:
                        raise KeyboardInterrupt
                    return ([rx] if current["mono"] == 1 else [], [], [])

                def enable(*args):
                    retries.append(current["mono"])
                    return True, "ok"

                args = SimpleNamespace(ref=None, sentry_ip=SENTRY, ownship_icao=None,
                                       stale=15, relay=False, listen_port=4000,
                                       dest=["127.0.0.1:4001"], log_raw="capture.jsonl",
                                       announce_port=0, interval=1, http_port=80,
                                       stats_every=60, auto_enable=True)
                with patch.object(bridge, "time", clocks), \
                     patch.object(bridge, "Bridge", return_value=b), \
                     patch.object(bridge.socket, "socket", side_effect=[rx, tx]), \
                     patch.object(bridge.select, "select", side_effect=select), \
                     patch.object(bridge, "check_power_savings"), \
                     patch.object(bridge.threading, "Thread"), \
                     patch.object(bridge.queue, "Queue", return_value=SimpleNamespace(put=lambda request: request())), \
                     patch.object(bridge, "enable_public", side_effect=enable), \
                     patch("builtins.open", return_value=raw), redirect_stdout(io.StringIO()):
                    # Sentry web requests run inline here, so retry times are the loop's decisions.
                    self.assertEqual(bridge.run(args), 0)
                # Heard at t=1; only t=1 and t=10 are within the 15-second lifetime.
                self.assertEqual(tx.sendto.call_count, 2)
                self.assertEqual(retries, [1, 21, 41])
                self.assertEqual(b.tick(41), [])
                self.assertIn('"t": 1700000001.0', raw.getvalue())


if __name__ == "__main__":
    unittest.main()
