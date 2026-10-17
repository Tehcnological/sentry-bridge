"""Exercise the exact iOS POSIX worker adapter over real local UDP, without Xcode."""
import ctypes as C
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import gdl90
import sim

class Status(C.Structure):
    _fields_=[('received',C.c_uint64),('forwarded',C.c_uint64),('send_errors',C.c_uint64),('rejected_sources',C.c_uint64),
              ('traffic',C.c_uint32),('weather',C.c_uint32),('heartbeat',C.c_uint32),('crc_bad',C.c_uint32),
              ('gps_valid',C.c_int),('mode',C.c_int),('age',C.c_double),('elapsed',C.c_double),('max_gap',C.c_double),('detail',C.c_char*256)]

@unittest.skipUnless(sys.platform=='darwin','Darwin/iOS adapter validation runs on Mac')
class IOSAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); library=Path(cls.tmp.name)/'bridge.dylib'
        subprocess.run(['clang','-shared','-Wall','-Wextra','-Werror','-std=c11','-O2','-I'+str(ROOT/'core'),
                        str(ROOT/'core/sbcore.c'),str(ROOT/'ios/SentryBridge/BridgeIO.c'),'-DSBIO_TESTING','-lm','-o',str(library)],check=True)
        cls.lib=C.CDLL(str(library)); cls.lib.sbio_open.restype=C.c_void_p
        cls.lib.sbio_open.argtypes=[C.c_char_p,C.c_uint16,C.c_char_p,C.c_uint16,C.c_char_p,C.c_size_t]
        cls.lib.sbio_step.argtypes=[C.c_void_p,C.c_int];cls.lib.sbio_snapshot.argtypes=[C.c_void_p,C.POINTER(Status)]
        cls.lib.sbio_close.argtypes=[C.c_void_p]
        cls.lib.sbio_observe.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p]
        cls.lib.sbio_wants_enable.argtypes=[C.c_void_p];cls.lib.sbio_refresh_shared_input.argtypes=[C.c_void_p]
        cls.lib.sbio_test_set_shared_local.argtypes=[C.c_void_p,C.c_uint32]
        cls.lib.sbio_open_shared.restype=C.c_void_p
        cls.lib.sbio_open_shared.argtypes=[C.c_char_p,C.c_uint16,C.c_char_p,C.c_uint16,C.c_int,C.c_char_p,C.c_size_t]
        cls.lib.sbio_request_compatibility.argtypes=[C.c_char_p,C.c_uint16,C.c_int,C.c_void_p,C.c_char_p,C.c_size_t]
        cls.lib.sbio_request_status.argtypes=[C.c_char_p,C.c_uint16,C.c_int,C.c_void_p,C.c_char_p,C.c_size_t]
        cls.lib.sbio_cancel_create.restype=C.c_void_p
        cls.lib.sbio_cancel_set.argtypes=[C.c_void_p];cls.lib.sbio_cancel_destroy.argtypes=[C.c_void_p]
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def setUp(self):
        self.sink=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.sink.bind(('127.0.0.1',0));self.sink.setblocking(False)
        self.sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
        self.handles=[]
    def tearDown(self):
        for h in self.handles:self.lib.sbio_close(h)
        self.sink.close();self.sender.close()
    def open(self,source=b'127.0.0.1',port=None,dest=b'127.0.0.1',output=None):
        error=C.create_string_buffer(512)
        h=self.lib.sbio_open(source,port or self.port,dest,output or self.sink.getsockname()[1],error,512)
        if h:self.handles.append(h)
        return h,error.value.decode()
    def send(self,data):self.sender.sendto(data,('127.0.0.1',self.port))
    def step(self,h):self.assertEqual(self.lib.sbio_step(h,10),0)
    def frames(self):
        out=[]
        while True:
            try:out.append(self.sink.recv(65536))
            except BlockingIOError:return out
    def wait_frames(self,h,count=1):
        # UDP delivery and worker polling are asynchronous; a single 10 ms poll is not a deadline.
        end=time.monotonic()+.5;out=[]
        while True:
            out.extend(self.frames())
            if len(out)>=count or time.monotonic()>=end:return out
            self.step(h)
    def status(self,h):
        s=Status();self.lib.sbio_snapshot(h,C.byref(s));return s
    def test_demo_decodes_without_sending_to_an_efb_listener(self):
        self.sink.close()
        self.sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sink.bind(('127.0.0.1', 14501)); self.sink.setblocking(False)
        self.lib.sbio_open_demo.restype = C.c_void_p
        self.lib.sbio_open_demo.argtypes = [C.c_uint16, C.c_char_p, C.c_size_t]
        error = C.create_string_buffer(512)
        h = self.lib.sbio_open_demo(self.port, error, 512)
        self.assertTrue(h, error.value); self.handles.append(h)
        data = (ROOT / 'ios/SentryBridge/DemoReceiver.bin').read_bytes()
        import struct
        index = 0
        while index < len(data):
            _, size = struct.unpack_from('>IH', data, index); index += 6
            self.send(data[index:index + size]); index += size
            self.step(h)
        deadline = time.monotonic() + 1.5
        while self.status(h).traffic == 0 and time.monotonic() < deadline:
            self.step(h)
        status = self.status(h)
        self.assertGreater(status.received, 0)
        self.assertGreater(status.traffic, 0)
        self.assertEqual(status.forwarded, 0)
        self.assertEqual(status.send_errors, 0)
        self.assertEqual(self.frames(), [], 'demo must not reach another local app')

    def test_refresh_ignores_an_exclusive_input(self):
        h,e=self.open();self.assertTrue(h,e)
        self.assertEqual(self.lib.sbio_refresh_shared_input(h),0)
    def test_exclusive_port_and_invalid_routes(self):
        h,e=self.open();self.assertTrue(h,e)
        other,e=self.open();self.assertFalse(other)
        self.assertIn(f'Receiver UDP port {self.port} is already in use',e)
        self.assertIn('Close the EFB',e)
        for dest in [b'255.255.255.255',b'224.0.0.1',b'invalid',b'0.0.0.0',b'255.255.255.255']:
            h,e=self.open(dest=dest);self.assertFalse(h,e)
        h,e=self.open(output=self.port);self.assertFalse(h);self.assertIn('ports must differ',e)
    def test_shared_input_observes_announcements_without_forwarding_them(self):
        h,e=self.open(source=b'127.0.0.2');self.assertTrue(h,e)
        observed=[]
        callback=C.CFUNCTYPE(None,C.c_void_p,C.POINTER(C.c_uint8),C.c_size_t,C.c_uint32)(
            lambda _,data,length,source: observed.append((C.string_at(data,length),source)))
        payload=b'{"App":"Synthetic EFB","GDL90":{"port":4001}}'
        self.lib.sbio_observe(h,C.cast(callback,C.c_void_p),None)
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                sender.bind(('127.0.0.1',0));sender.sendto(payload,('127.0.0.1',self.port))
            end=time.monotonic()+1
            while not observed and time.monotonic()<end:self.step(h)
            self.assertEqual(observed,[(payload,0x7f000001)])
            self.assertEqual(self.frames(),[])
            status=self.status(h)
            self.assertEqual(status.forwarded,0);self.assertEqual(status.received,0)
            self.assertEqual(status.rejected_sources,1)
        finally:self.lib.sbio_observe(h,None,None)
    def test_relay_full_stuffed_weather_gps_heartbeat_and_crc(self):
        h,e=self.open();self.assertTrue(h,e)
        weather=gdl90.frame(b'\x07'+b'\x7e\x7d'*220)
        own=gdl90.encode_report(gdl90.Report(address=0,lat=40.0,lon=-100.0,alt_ft=2000),gdl90.MSG_OWNSHIP)
        heartbeat=gdl90.encode_heartbeat(True,1234)
        for frame in [weather,own,heartbeat]:self.send(frame);self.step(h)
        self.assertEqual(self.wait_frames(h,3),[weather,own,heartbeat])
        self.send(b'\x7e\x07\x00\x00\x7e');self.step(h);self.assertEqual(self.frames(),[])
        s=self.status(h);self.assertEqual(s.weather,1);self.assertEqual(s.heartbeat,1);self.assertTrue(s.gps_valid);self.assertGreater(s.crc_bad,0)
    def test_interface_routes_and_failed_output(self):
        fixture = r"""
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <stdio.h>
static int fixture_interfaces(struct ifaddrs **result) {
    static struct ifaddrs entry;
    static struct sockaddr_in local, broadcast;
    local.sin_family = broadcast.sin_family = AF_INET;
    local.sin_addr.s_addr = inet_addr("10.0.0.1");
    broadcast.sin_addr.s_addr = inet_addr("10.0.0.127");
    entry.ifa_addr = (struct sockaddr *)&local;
    entry.ifa_broadaddr = (struct sockaddr *)&broadcast;
    entry.ifa_flags = IFF_BROADCAST;
    *result = &entry; return 0;
}
static void fixture_free(struct ifaddrs *value) { (void)value; }
#define getifaddrs fixture_interfaces
#define freeifaddrs fixture_free
#include "BridgeIO.c"
int main(void) {
    char error[512];
    if (sbio_efb_candidate("10.0.0.127", 4001, "192.0.2.1", 4000)) return 10;
    if (sbio_efb_candidate("10.0.0.1", 4000, "192.0.2.1", 4000)) return 11;
    if (sbio_efb_candidate("127.0.0.1", 4001, "192.0.2.1", 4000)) return 12;
    if (sbio_efb_candidate("192.0.2.1", 4001, "192.0.2.1", 4000)) return 13;
    if (!sbio_efb_candidate("10.0.0.255", 4001, "192.0.2.1", 4000)) return 14;
    if (!sbio_efb_candidate("10.0.0.1", 4001, "192.0.2.1", 4000)) return 15;
    sbio *io = sbio_open("127.0.0.1", 14000, "10.0.0.127", 14001, error, sizeof error);
    if (io) { sbio_close(io); return 1; }
    io = sbio_open("127.0.0.1", 0, "10.0.0.255", 14001, error, sizeof error);
    if (io) { sbio_close(io); return 4; }
    /* Unlike .127 on this /25 interface, .255 is an ordinary unicast elsewhere. */
    io = sbio_open("127.0.0.1", 14000, "10.0.0.255", 14001, error, sizeof error);
    if (!io) return 5;
    close(io->output); io->output = -1; /* deterministic output failure, no network changes */
    uint8_t heartbeat[7] = {0, 0x80, 0, 0, 0, 0, 0}, frame[32];
    size_t count = sb_frame(heartbeat, sizeof heartbeat, frame, sizeof frame);
    sb_feed(&io->bridge, frame, count, 0x7f000001, sbio_now(), emit, io);
    sbio_status status; sbio_snapshot(io, &status);
    if (status.forwarded != 0 || status.send_errors != 1 || !status.gps_valid) return 6;
    /* Lost positions remain in core memory but must not be reported or counted. */
    sb_target *target = &io->bridge.targets[0];
    target->used = target->has_pos = target->lost = 1;
    target->icao = 0xabc123; target->last_seen = target->pos_t = sbio_now();
    if (sb_tick(&io->bridge, sbio_now(), emit, io) != 0) return 7;
    sbio_snapshot(io, &status);
    if (status.traffic != 0) return 8;
    target->lost = 0;
    if (sb_tick(&io->bridge, sbio_now(), emit, io) != 1) return 9;
    sbio_close(io);
    io = sbio_open("127.0.0.1", 14000, "10.0.0.1", 14000, error, sizeof error);
    if (io) { sbio_close(io); return 2; }
    if (!strstr(error, "ports must differ")) return 3;
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'fixture.c';source.write_text(fixture)
            binary=Path(directory)/'fixture'
            subprocess.run(['clang','-Wall','-Wextra','-Werror','-std=c11',
                '-I'+str(ROOT/'core'),'-I'+str(ROOT/'ios/SentryBridge'),str(source),
                str(ROOT/'core/sbcore.c'),'-lm','-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True)

    def test_weather_cannot_keep_an_old_gps_fix_fresh(self):
        h,e=self.open();self.assertTrue(h,e)
        self.send(gdl90.encode_heartbeat(True,1234));self.step(h)
        self.assertTrue(self.status(h).gps_valid)
        weather=gdl90.frame(b'\x07'+b'\x00'*20)
        end=time.monotonic()+3.2
        while time.monotonic()<end:
            self.send(weather);self.step(h);time.sleep(.05)
        s=self.status(h);self.assertLess(s.age,1);self.assertFalse(s.gps_valid)
        # A CRC-valid but truncated heartbeat must not refresh the adapter heartbeat timestamp.
        self.send(gdl90.frame(b"\x00\x80"));self.step(h)
        self.assertFalse(self.status(h).gps_valid)
        self.send(gdl90.encode_heartbeat(True,1235));self.step(h)
        self.assertTrue(self.status(h).gps_valid)

    def test_bad_or_escaped_short_heartbeat_cannot_refresh_gps(self):
        h,e=self.open();self.assertTrue(h,e)
        self.send(gdl90.encode_heartbeat(True,1234));self.step(h)
        self.assertTrue(self.status(h).gps_valid)
        end=time.monotonic()+3.2
        while time.monotonic()<end:
            # Its wire length exceeds a full heartbeat, but the decoded message is short.
            self.send(gdl90.frame(b"\x00\x80"+b"\x7e"*4));self.step(h)
            # A damaged full heartbeat must not refresh it either.
            bad=bytearray(gdl90.encode_heartbeat(True,1234));bad[2]^=1
            self.send(bad);self.step(h);time.sleep(.05)
        self.assertFalse(self.status(h).gps_valid)
        self.send(gdl90.encode_heartbeat(False,1235));self.step(h)
        self.assertFalse(self.status(h).gps_valid)
        self.send(gdl90.encode_heartbeat(True,1236));self.step(h)
        self.assertTrue(self.status(h).gps_valid)

    def test_1090_and_uat_produce_valid_traffic(self):
        h,e=self.open();self.assertTrue(h,e)
        receiver=sim.SimSentry(tracks=sim.default_tracks()+sim.uat_tracks(),gps_fix=True,fisb=True)
        addresses=set();ids=set();deadline=time.monotonic()+2.2;t=0
        while time.monotonic()<deadline:
            for data in receiver.datagrams(t):self.send(data);self.step(h)
            self.step(h)
            for frame in self.frames():
                parsed=gdl90.parse(frame);self.assertTrue(all(f.crc_ok for f in parsed))
                for f in parsed:
                    ids.add(f.msg_id)
                    if f.msg_id==gdl90.MSG_TRAFFIC:addresses.add(gdl90.decode_report(f.msg).address)
            time.sleep(.05);t+=1
        expected={track.icao for track in sim.default_tracks()+sim.uat_tracks()}
        self.assertEqual(addresses,expected);self.assertTrue({0,7,10,20}.issubset(ids))
    def test_wrong_source_silence_restart_and_suspend_queue(self):
        h,e=self.open(source=b'192.168.4.1');self.assertTrue(h,e)
        heartbeat=gdl90.encode_heartbeat(True,1234);self.send(heartbeat);self.step(h)
        self.assertEqual(self.frames(),[]);self.assertEqual(self.status(h).rejected_sources,1)
        self.lib.sbio_close(h);self.handles.remove(h)
        h,e=self.open();self.assertTrue(h,e)
        self.send(heartbeat);self.step(h);self.assertEqual(self.wait_frames(h),[heartbeat])
        # Queue the stale heartbeat while the worker is "suspended" (no step for over 3 s), so it is
        # already readable when the gap drain runs. Sending it after the pause raced loopback delivery:
        # a datagram arriving just after the drain is new input and was forwarded with the next one.
        self.send(heartbeat);time.sleep(3.1);self.step(h)
        self.assertEqual(self.frames(),[]);self.assertFalse(self.status(h).gps_valid)
        self.assertIn('Execution gap',self.status(h).detail.decode())
        self.send(heartbeat);self.step(h);self.assertEqual(self.wait_frames(h),[heartbeat])
    def test_shared_port_beside_an_efb_wildcard_listener(self):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
            try:probe.connect(('192.0.2.1',9));lan=probe.getsockname()[0]
            except OSError:self.skipTest('needs a non-loopback IPv4 interface')
        if lan.startswith('127.'):self.skipTest('needs a non-loopback IPv4 interface')
        efb=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);efb.bind(('0.0.0.0',self.port));efb.settimeout(.5)  # EFB first, no reuse flags
        error=C.create_string_buffer(512)
        try:
            self.assertFalse(self.lib.sbio_open(lan.encode(),self.port,b'127.0.0.1',self.port,error,512))  # default stays exclusive
            self.assertFalse(self.lib.sbio_open_shared(lan.encode(),self.port,b'10.0.0.1',self.port,1,error,512))
            self.assertIn('127.0.0.1',error.value.decode())
            # A receiver reached through a router (here 8.8.8.8, off the LAN subnet) is refused, as
            # for the wrong WiFi network or cellular; connect() only looks up the route.
            self.assertFalse(self.lib.sbio_open_shared(b'8.8.8.8',self.port,b'127.0.0.1',self.port,1,error,512))
            self.assertIn('Join its WiFi',error.value.decode())
            h=self.lib.sbio_open_shared(lan.encode(),self.port,b'127.0.0.1',self.port,1,error,512)
            self.assertTrue(h,error.value.decode());self.handles.append(h)
            self.assertEqual(self.lib.sbio_refresh_shared_input(h),0)  # same routed address: keep the socket
            heartbeat=gdl90.encode_heartbeat(True,1234)
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sentry:
                sentry.bind((lan,0));sentry.sendto(heartbeat,(lan,self.port))
                end=time.monotonic()+.5
                while self.status(h).received==0 and time.monotonic()<end:self.step(h)
            self.assertEqual(self.status(h).received,1)  # receiver unicast reached the bridge, not the EFB
            end=time.monotonic()+.5;got=None
            while got is None and time.monotonic()<end:
                self.step(h)
                try:got=efb.recv(65536)
                except socket.timeout:pass
            self.assertEqual(got,heartbeat)  # loopback output reached the EFB's wildcard listener
            # Changed route whose new address/port is occupied (here by this input itself): the
            # rebind fails, keeps the old socket and reports an actionable reason.
            self.lib.sbio_test_set_shared_local(h,0x0a0000fe)
            self.assertEqual(self.lib.sbio_refresh_shared_input(h),-1)
            detail=self.status(h).detail.decode()
            self.assertIn('WiFi address changed to '+lan,detail);self.assertIn('cannot be opened there',detail)
            before=self.status(h).received
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sentry:  # old socket still receives
                sentry.bind((lan,0));sentry.sendto(gdl90.encode_heartbeat(True,1),(lan,self.port))
                end=time.monotonic()+.5
                while self.status(h).received==before and time.monotonic()<end:self.step(h)
            self.assertEqual(self.status(h).received,before+1)
        finally:efb.close()
    def test_encrypted_start_schedules_compatibility_request(self):
        encrypted=gdl90.frame(bytes((0x26,0x00))+bytes(18))
        h,e=self.open();self.assertTrue(h,e)
        self.send(gdl90.encode_heartbeat(True,1234));self.wait_frames(h)
        self.assertEqual(self.lib.sbio_wants_enable(h),0)  # public stream: never request
        self.lib.sbio_close(h);self.handles.remove(h)
        h,e=self.open();self.assertTrue(h,e)
        self.send(encrypted);end=time.monotonic()+.5
        while self.status(h).mode!=2 and time.monotonic()<end:self.step(h)
        self.assertEqual(self.status(h).mode,2)
        self.assertEqual(self.lib.sbio_wants_enable(h),1)  # encrypted at bridge start
        self.assertEqual(self.lib.sbio_wants_enable(h),0)  # next try waits 20 s
    def request(self,port):
        error=C.create_string_buffer(256)
        return self.lib.sbio_request_compatibility(b'127.0.0.1',port,2000,None,error,256),error.value.decode()
    def test_compatibility_request_matches_core_and_never_follows_redirects(self):
        import http.server,threading
        seen=[]
        class Fake(http.server.BaseHTTPRequestHandler):
            status=200
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                seen.append((self.path,self.headers['Content-Type'],body))
                self.send_response(Fake.status)
                if Fake.status==302:self.send_header('Location','http://192.0.2.1/')
                self.end_headers()
            def do_GET(self):
                seen.append((self.command,self.path,self.headers['Content-Length']))
                self.send_response(200);self.end_headers()
            def log_message(self,*args):pass
        server=http.server.HTTPServer(('127.0.0.1',0),Fake);port=server.server_address[1]
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            self.assertEqual(self.request(port),(200,''))
            self.assertEqual(seen,[('/settings/?action=set','application/json',b'{"publicGDL90":true}')])
            Fake.status=302
            self.assertEqual(self.request(port)[0],302);self.assertEqual(len(seen),2)
            error=C.create_string_buffer(256)  # keepalive: read-only status page, no body
            self.assertEqual(self.lib.sbio_request_status(b'127.0.0.1',port,2000,None,error,256),200)
            self.assertEqual(seen[2],('GET','/?action=get',None))
        finally:server.shutdown();server.server_close()
        status,error=self.request(port)
        self.assertEqual(status,-1);self.assertIn('127.0.0.1:%d'%port,error)
        error=C.create_string_buffer(256)
        self.assertEqual(self.lib.sbio_request_compatibility(b'not an address',80,100,None,error,256),-1)
    def slow_server(self,reply):
        """Accepts one connection and runs reply(connection) on it; returns (port, accepted-event)."""
        import threading
        server=socket.socket(socket.AF_INET,socket.SOCK_STREAM);server.bind(('127.0.0.1',0));server.listen(1)
        accepted=threading.Event()
        def serve():
            try:
                connection,_=server.accept();accepted.set()
                with connection:reply(connection)
            except OSError:pass
            finally:server.close()
        threading.Thread(target=serve,daemon=True).start()
        self.addCleanup(server.close)
        return server.getsockname()[1],accepted
    def test_http_deadline_bounds_a_dribbling_status_line(self):
        def dribble(connection):
            connection.recv(512)
            for byte in b'HTTP/1.0 200 OK\r\n':
                try:connection.send(bytes([byte]))
                except OSError:return
                time.sleep(.07)
        port,_=self.slow_server(dribble)
        error=C.create_string_buffer(256);started=time.monotonic()
        status=self.lib.sbio_request_status(b'127.0.0.1',port,150,None,error,256)
        elapsed=time.monotonic()-started
        self.assertEqual(status,-1);self.assertIn('within 150 ms',error.value.decode())
        self.assertLess(elapsed,.4)  # was 1.257 s: per-receive timeouts restarted on every byte
    def test_cancel_stops_an_active_request_and_prevents_a_queued_one(self):
        import threading
        port,accepted=self.slow_server(lambda connection:time.sleep(3))  # accepts, never answers
        cancel=self.lib.sbio_cancel_create();self.addCleanup(self.lib.sbio_cancel_destroy,cancel)
        result={}
        def request():
            error=C.create_string_buffer(256);started=time.monotonic()
            result['status']=self.lib.sbio_request_compatibility(b'127.0.0.1',port,5000,cancel,error,256)
            result['elapsed']=time.monotonic()-started
        worker=threading.Thread(target=request);worker.start()
        self.assertTrue(accepted.wait(2))
        time.sleep(.1);self.lib.sbio_cancel_set(cancel);worker.join(2)
        self.assertEqual(result['status'],-2);self.assertLess(result['elapsed'],.5)
        # A request made after cancellation never connects.
        port,accepted=self.slow_server(lambda connection:connection.recv(512))
        error=C.create_string_buffer(256)
        self.assertEqual(self.lib.sbio_request_compatibility(b'127.0.0.1',port,2000,cancel,error,256),-2)
        self.assertFalse(accepted.wait(.3))
class IOSVersion(unittest.TestCase):
    def test_app_version_is_explicit_and_not_older_than_core(self):
        import plistlib,re
        app=plistlib.loads((ROOT/'ios/SentryBridge/Info.plist').read_bytes())['CFBundleShortVersionString']
        core=re.search(r'#define SB_VERSION "([0-9.]+)"',(ROOT/'core/sbcore.h').read_text()).group(1)
        self.assertRegex(app,r'^\d+\.\d+\.\d+$')
        # The iOS product may lead the shared core; Diagnostics labels both values separately.
        self.assertGreaterEqual(tuple(map(int,app.split('.'))),tuple(map(int,core.split('.'))))

if __name__=='__main__':unittest.main()
