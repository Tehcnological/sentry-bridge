#!/usr/bin/env python3
"""Unicast UDP sink for simulator/device tests; reports transport only, retains no location data."""
import argparse
from collections import Counter
from pathlib import Path
import socket
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gdl90

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='127.0.0.1'); p.add_argument('--port',type=int,default=14001)
    p.add_argument('--seconds',type=int,default=65); a=p.parse_args()
    counts=Counter(); bad=0; end=time.monotonic()+a.seconds
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
        s.bind((a.host,a.port)); s.settimeout(.5)
        while time.monotonic()<end:
            try: data,_=s.recvfrom(65536)
            except socket.timeout: continue
            for f in gdl90.parse(data):
                if f.crc_ok: counts[f.msg_id]+=1
                else: bad+=1
    print('Valid frames:', ', '.join(f'0x{k:02X}={v}' for k,v in sorted(counts.items())), 'bad CRC:',bad)
    if not counts: raise SystemExit('No data received')
if __name__=='__main__': main()
