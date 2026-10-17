#!/usr/bin/env python3
"""Synthetic Sentry UDP replay. Never requires or configures receiver hardware."""
import argparse
import ipaddress
from pathlib import Path
import socket
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=14000)
    p.add_argument('--seconds', type=int, default=60)
    p.add_argument('--no-gps', action='store_true')
    p.add_argument('--quiet-after', type=int, default=0, help='Stop input after this many seconds to measure silence')
    a = p.parse_args()
    address = ipaddress.IPv4Address(a.host)
    if address.is_multicast or address.is_unspecified or int(address) == 0xffffffff or not 1 <= a.port <= 65535 or a.seconds < 1:
        p.error('Use a unicast IPv4, port 1–65535 and positive duration')
    receiver = sim.SimSentry(tracks=sim.default_tracks() + sim.uat_tracks(), gps_fix=not a.no_gps, fisb=True)
    sent = 0; started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for t in range(a.seconds):
            if not a.quiet_after or t < a.quiet_after:
                for data in receiver.datagrams(t):
                    sock.sendto(data, (a.host, a.port)); sent += 1
            time.sleep(max(0, started + t + 1 - time.monotonic()))
    print(f'Sent {sent} synthetic datagrams: 1090/UAT traffic, heartbeat, ownship and weather transport.')
    print('Synthetic weather is not a displayable real weather product.')
if __name__ == '__main__': main()
