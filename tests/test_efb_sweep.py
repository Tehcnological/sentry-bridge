"""Finding an EFB that does not announce itself: the device sweep and the follow rule (tools/discovery.py)."""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import discovery  # noqa: E402


class TestSweep(unittest.TestCase):
    def test_sweep_lists_answering_devices_except_this_device_and_the_receiver(self):
        up = {"192.168.4.1", "192.168.4.4", "192.168.4.6", "192.168.4.200"}
        found = discovery.sweep("192.168.4.4", exclude={"192.168.4.1"}, probe=lambda address: address in up)
        self.assertEqual(found, ["192.168.4.6", "192.168.4.200"])  # address order, never 1 (receiver) or 4 (us)

    def test_follow_switches_only_to_a_single_other_device(self):
        self.assertEqual(discovery.follow_choice(["192.168.4.6"], "192.168.4.19"), "192.168.4.6")
        self.assertIsNone(discovery.follow_choice([], "192.168.4.19"))
        self.assertIsNone(discovery.follow_choice(["192.168.4.6", "192.168.4.7"], "192.168.4.19"))  # the user chooses
        self.assertEqual(discovery.follow_choice(["192.168.4.19", "192.168.4.6"], "192.168.4.19"), "192.168.4.6")

    def test_a_device_answers_whether_it_accepts_or_refuses(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            self.assertTrue(discovery.answers("127.0.0.1", listener.getsockname()[1]))
            closed = listener.getsockname()[1]
        self.assertTrue(discovery.answers("127.0.0.1", closed))  # refused: still a device
        with patch.object(socket, "create_connection", side_effect=socket.timeout()):
            self.assertFalse(discovery.answers("192.0.2.1"))  # silence: nobody there


if __name__ == "__main__":
    unittest.main()
