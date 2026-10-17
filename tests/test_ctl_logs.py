"""sentry_ctl logs: list and download the Sentry's stored logs from a fake Mongoose /data/ index."""

import io
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentry_ctl  # noqa: E402

CSV = ("Host::positionAccuracy_mm,Host::UATLongPkts,Host::1090esPkts,Imu::rollDeg,Imu::pitchDeg,"
       "Sys::battery_mV,Co::ppm\n1,2,3,4,5,6,7\n")
# Shaped like Mongoose 6.x "Index of" output: sort links, parent link, then file rows
INDEX = """<html><head><title>Index of /data/</title></head><body><h1>Index of /data/</h1>
<table><tr><th><a href="?n=1">Name</a></th><th><a href="?s=1">Size</a></th></tr>
<tr><td><a href="../">..</a></td></tr>
<tr><td><a href="log_0001.csv">log_0001.csv</a></td><td>{n}</td></tr>
<tr><td><a href="big%20log.csv">big log.csv</a></td><td>200000</td></tr>
<tr><td><a href="%2e%2e">encoded parent</a></td><td>1</td></tr>
<tr><td><a href="sub%2flog.csv">encoded path</a></td><td>1</td></tr>
<tr><td><a href="log_12:00.csv">clock time</a></td><td>1</td></tr>
</table></body></html>""".format(n=len(CSV))


class Fake:
    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data, ctype="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/data":
                    self.send_response(301)
                    self.send_header("Location", "/data/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/data/":
                    self._send(200, INDEX.encode(), "text/html")
                elif self.path == "/data/log_0001.csv":
                    self._send(200, CSV.encode())
                elif self.path == "/data/big%20log.csv":
                    self._send(200, b"a,b\n" + b"1,2\n" * 50000)
                elif self.path == "/data/log_12%3A00.csv":
                    self._send(200, b"t,v\n1,2\n")
                else:
                    self._send(404, b"nope")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


class TestLogs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = Fake()

    @classmethod
    def tearDownClass(cls):
        cls.fake.srv.shutdown()
        cls.fake.srv.server_close()

    def run_ctl(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sentry_ctl.main(["--host", self.fake.host, *args])
        return rc, buf.getvalue()

    def test_list(self):
        self.assertEqual(sentry_ctl.list_logs(self.fake.host), ["log_0001.csv", "big%20log.csv", "log_12:00.csv"])
        rc, out = self.run_ctl("logs")
        self.assertEqual(rc, 0)
        self.assertIn("big log.csv", out)
        self.assertIn("--get all", out)

    def test_get_all_flags_columns_and_caps_size(self):
        d = tempfile.mkdtemp()
        rc, out = self.run_ctl("logs", "--get", "all", "--logdir", d, "--max-mb", "0.05")
        self.assertEqual(rc, 0)
        self.assertEqual((Path(d) / "log_0001.csv").read_text(), CSV)
        self.assertIn("7 columns", out)
        for col in ("Imu::rollDeg", "Imu::pitchDeg", "Sys::battery_mV", "Co::ppm"):
            self.assertIn(col, out)
        self.assertNotIn("Host::UATLongPkts", out.split("of interest:")[1].splitlines()[0])
        big = Path(d) / "big log.csv"
        self.assertEqual(big.stat().st_size, int(0.05 * 1024 * 1024))
        self.assertIn("TRUNCATED", out)
        # A clock time in the name is kept in the list; the saved file uses '_' (Windows drive/stream).
        self.assertEqual((Path(d) / "log_12_00.csv").read_text(), "t,v\n1,2\n")
        self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["big log.csv", "log_0001.csv", "log_12_00.csv"])

    def test_log_names_stay_in_the_download_folder(self):
        self.assertEqual(sentry_ctl.log_name("big%20log.csv"), "big log.csv")
        self.assertEqual(sentry_ctl.log_name("c:log.csv"), "c:log.csv")
        self.assertEqual(sentry_ctl.local_log_path(Path("logs"), "c:log.csv"), Path("logs") / "c_log.csv")
        for href in ("%2e%2e", "..", "a%2f..%2fb", "a%5cb", "log%0a.csv", ""):
            self.assertIsNone(sentry_ctl.log_name(href), href)

    def test_get_one_and_missing(self):
        d = tempfile.mkdtemp()
        rc, _ = self.run_ctl("logs", "--get", "log_0001.csv", "--logdir", d)
        self.assertEqual(rc, 0)
        self.assertEqual([p.name for p in Path(d).iterdir()], ["log_0001.csv"])
        rc, out = self.run_ctl("logs", "--get", "nope.csv", "--logdir", d)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
