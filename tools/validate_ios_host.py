"""Compile and exercise the actual Swift/C worker on macOS; does not validate UIKit or BGTask."""
from pathlib import Path
import argparse
import socket
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import sim

def http_stop(binary,port,output):
    """Stop/restart while a slow receiver web server holds the first status request."""
    import http.server,threading
    import gdl90
    requests=[];lock=threading.Lock();held=threading.Event()
    class Receiver(http.server.BaseHTTPRequestHandler):
        def handle_request_line(self):
            with lock:requests.append(self.command);first=self.command=='GET' and requests.count('GET')==1
            if first:held.set();time.sleep(3)  # the stopped worker must cancel this request, not wait for it
            try:self.send_response(200);self.end_headers()
            except OSError:pass
        def do_GET(self):self.handle_request_line()
        def do_POST(self):self.handle_request_line()
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Receiver);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    stop=threading.Event()
    def encrypted():
        # Encrypted input (and so the queued POST) only after the first worker's GET is held.
        if not held.wait(10):return
        frame=gdl90.frame(bytes((0x26,0x00))+bytes(18))
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            while not stop.is_set():sender.sendto(frame,('127.0.0.1',port));time.sleep(.1)
    threading.Thread(target=encrypted,daemon=True).start()
    try:
        subprocess.run([str(binary),str(port),str(output),'http-stop',str(server.server_address[1])],check=True,timeout=20)
        time.sleep(3.5)  # longer than the held request: a queued POST would have arrived by now
    finally:
        stop.set();server.shutdown();server.server_close()
    with lock:seen=list(requests)
    if 'POST' in seen or seen.count('GET')<2:raise RuntimeError(f'http-stop: receiver saw {seen}')
    print(f'PASS: http-stop: receiver saw {seen}; no Compatibility Mode POST after Stop')

def readings(binary,port,output):
    """The worker's statistics reader against a loopback server playing the receiver's /stats WebSocket."""
    import base64,hashlib,json,threading
    message=json.dumps(json.loads((ROOT/'tests/data/stats-cases.json').read_text())[0]['message']).encode()
    server=socket.socket();server.bind(('127.0.0.1',0));server.listen(4);server.settimeout(.2)
    stop=threading.Event();requests=[]
    def serve():
        while not stop.is_set():
            try:connection,_=server.accept()
            except OSError:continue
            with connection:
                try:
                    request=b''
                    while b'\r\n\r\n' not in request:
                        chunk=connection.recv(4096)
                        if not chunk:break
                        request+=chunk
                    requests.append(request.split(b'\r\n')[0])
                    key=[l.split(b':',1)[1].strip() for l in request.split(b'\r\n') if l.lower().startswith(b'sec-websocket-key:')][0]
                    accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                    connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
                    while not stop.is_set():
                        connection.sendall(bytes([0x81,126])+len(message).to_bytes(2,'big')+message);time.sleep(.5)
                except OSError:pass
    threading.Thread(target=serve,daemon=True).start()
    try:subprocess.run([str(binary),str(port),str(output),'readings',str(server.getsockname()[1])],check=True,timeout=20)
    finally:stop.set();server.close()
    if not requests or requests[0]!=b'GET /stats HTTP/1.1':raise RuntimeError(f'readings: receiver saw {requests}')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thread-sanitizer',action='store_true',help='Instrument the C adapter/core and Swift worker for data races')
    args=parser.parse_args()
    build=ROOT/('build/ios-host-tsan' if args.thread_sanitizer else 'build/ios-host');build.mkdir(parents=True,exist_ok=True)
    cflags=['-fsanitize=thread','-g'] if args.thread_sanitizer else []
    swiftflags=['-sanitize=thread','-g'] if args.thread_sanitizer else []
    for name,path in [('sbcore','core/sbcore.c'),('io','ios/SentryBridge/BridgeIO.c')]:
        subprocess.run(['clang',*cflags,'-O2','-Wall','-Wextra','-Werror','-I'+str(ROOT/'core'),'-c',str(ROOT/path),'-o',str(build/(name+'.o'))],check=True)
    binary=build/'worker-smoke'
    subprocess.run(['swiftc',*swiftflags,'-D','SBIO_TESTING','-swift-version','5','-module-cache-path',str(build/'modules'),'-import-objc-header',str(ROOT/'ios/SentryBridge/Bridge-Bridging-Header.h'),'-I'+str(ROOT/'core'),str(ROOT/'ios/SentryBridge/BridgeWorker.swift'),str(ROOT/'ios/SentryBridge/EFBDiscovery.swift'),str(ROOT/'ios/SentryBridge/ReceiverReadings.swift'),str(ROOT/'ios/tests/WorkerSmoke.swift'),str(build/'io.o'),str(build/'sbcore.o'),'-o',str(binary)],check=True)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as reservation, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sink:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1];reservation.close()
        sink.bind(('127.0.0.1',0));sink.settimeout(.05)
        process=subprocess.Popen([str(binary),str(port),str(sink.getsockname()[1]),'replay'])
        try:
            generator=sim.SimSentry(fisb=True)
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                # Keep replay alive until worker completion, including cold Swift startup.
                end=time.monotonic()+15;t=0
                while time.monotonic()<end and process.poll() is None:
                    for data in generator.datagrams(t):sender.sendto(data,('127.0.0.1',port))
                    try:sink.recv(65536)
                    except socket.timeout:pass
                    time.sleep(.1);t+=1
            if process.wait(timeout=10):raise RuntimeError('Swift worker failed')
        finally:
            if process.poll() is None:process.kill();process.wait()
        for mode in ['cancel','prestop','invalid','discovery','discovery-restart']:
            subprocess.run([str(binary),str(port),str(sink.getsockname()[1]),mode],check=True,timeout=5)
        subprocess.run([str(binary),str(port),str(sink.getsockname()[1]),'rules',str(ROOT/'tests/data/stats-cases.json')],check=True,timeout=10)
        subprocess.run([str(binary),str(port),str(sink.getsockname()[1]),'follow'],check=True,timeout=20)
        subprocess.run([str(binary),str(port),str(sink.getsockname()[1]),'sweep'],check=True,timeout=10)
        subprocess.run([str(binary),str(port),str(sink.getsockname()[1]),'follow-end'],check=True,timeout=15)
        readings(binary,port,sink.getsockname()[1])
        subprocess.run([str(binary),str(port),str(sink.getsockname()[1]),'lifecycle'],check=True,timeout=30)
        http_stop(binary,port,sink.getsockname()[1])
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            # UDP connect performs a route lookup, sending no probe or receiver command.
            try:
                sender.connect(('192.0.2.1',9));local=sender.getsockname()[0]
            except OSError:
                print('SKIP: UDP discovery receive needs a non-loopback IPv4 interface');return
        if local.startswith('127.'):
            print('SKIP: UDP discovery receive needs a non-loopback IPv4 interface');return
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            sender.bind((local,0))
            process=subprocess.Popen([str(binary),str(port),str(sink.getsockname()[1]),'discovery-receive'])
            try:
                end=time.monotonic()+15
                while time.monotonic()<end and process.poll() is None:
                    sender.sendto(b'{"App":"Synthetic EFB","GDL90":{"port":4001}}',(local,port))
                    time.sleep(.1)
                if process.wait(timeout=5):raise RuntimeError('UDP discovery receive failed')
            finally:
                if process.poll() is None:process.kill();process.wait()
if __name__=='__main__':main()
