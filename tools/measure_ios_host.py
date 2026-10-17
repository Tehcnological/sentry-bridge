"""30-second busy-replay baseline for the Swift/C worker, excluding the iOS UI/scheduler.

Run tools/validate_ios_host.py first to compile the harness. Writes a local JSON report in build/.
This is a Mac baseline, never an older-iPhone/iPad energy or background-reliability claim.
"""
from pathlib import Path
import json
import platform
import socket
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import sim

def main():
    binary=ROOT/'build/ios-host/worker-smoke'
    if not binary.exists():raise SystemExit('Run tools/validate_ios_host.py first')
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as reservation, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sink:
        reservation.bind(('127.0.0.1',0)); port=reservation.getsockname()[1];reservation.close()
        sink.bind(('127.0.0.1',0));sink.setblocking(False)
        process=subprocess.Popen([str(binary),str(port),str(sink.getsockname()[1]),'bench'],stdout=subprocess.PIPE,text=True)
        started=time.monotonic();next_sample=0;cpu=[];memory=[];sent=received=0
        tracks=sim.busy_tracks()+sim.uat_tracks()
        generator=sim.SimSentry(tracks=tracks,gps_fix=True,fisb=True)
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                t=0
                while process.poll() is None:
                    for data in generator.datagrams(t):sender.sendto(data,('127.0.0.1',port));sent+=1
                    while True:
                        try:sink.recv(65536);received+=1
                        except BlockingIOError:break
                    elapsed=time.monotonic()-started
                    if elapsed>=next_sample:
                        values=subprocess.run(['ps','-p',str(process.pid),'-o','%cpu=,rss='],capture_output=True,text=True,check=True).stdout.split()
                        if len(values)==2:cpu.append(float(values[0]));memory.append(int(values[1]))
                        next_sample=elapsed+1
                    if elapsed>40:raise RuntimeError('Worker timed out')
                    time.sleep(.1);t+=1
            output=process.communicate(timeout=5)[0]
            if process.returncode:raise RuntimeError(output)
        finally:
            if process.poll() is None:process.kill();process.wait()
    report={'scope':'Mac Swift/C worker only; no UI, iOS or background scheduler',
            'platform':platform.platform(),'architecture':platform.machine(),
            'durationSeconds':round(time.monotonic()-started,2),'inputDatagramsSent':sent,
            'outputDatagramsObserved':received,'psAverageCpuPercent':round(sum(cpu)/len(cpu),2),
            'psPeakCpuPercent':max(cpu),'peakRssMiB':round(max(memory)/1024,2),
            'method':f'{len(tracks)} simulated targets; replay every 100 ms; ps %cpu/RSS sampled once per second; no per-frame latency claim',
            'workerResult':output.strip()}
    path=ROOT/'build/ios-host/performance.json';path.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
