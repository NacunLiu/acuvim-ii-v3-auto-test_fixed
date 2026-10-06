"""Poll the Acuvim II V3 BACnet test meters and record which ones answered.

The meters sit on MS/TP network 203 behind the KMC BAC-5051E router at
20.20.20.34.  A plain broadcast Who-Is does not reach them reliably, so this
sends a Who-Is addressed to DNET 203 *unicast to the router*, which forwards it
onto the MS/TP trunk, and collects the I-Ams that come back.  MS/TP is slow --
a full sweep needs several seconds per round, hence LISTEN_SECS/ROUNDS.

Writes one JSON line per run to test_logs/bacnet_monitor.jsonl and prints a
one-line summary.  Exit code is always 0: this is a monitor, not a test.
"""
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime

ROUTER_IP = '20.20.20.34'
DNET = 203
EXPECTED = list(range(2, 11))          # device instances 2..10
BACNET_PORT = 47808
LISTEN_SECS = 12
ROUNDS = 3

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'test_logs', 'bacnet_monitor.jsonl')


def _parse_iam(d):
    """Return (instance, snet, sadr) for an I-Am, else None."""
    if not d or d[0] != 0x81:
        return None
    off = 4
    if d[1] == 0x04:                   # forwarded-NPDU carries a 6-byte origin
        off = 4 + 6
    ctl = d[off + 1]
    p = off + 2
    snet = sadr = None
    if ctl & 0x08:                     # SNET/SADR present
        snet = struct.unpack('>H', d[p:p + 2])[0]
        p += 2
        ln = d[p]
        p += 1
        sadr = d[p:p + ln].hex()
        p += ln
    if ctl & 0x20:                     # DNET/DADR present
        p += 2
        ln = d[p]
        p += 1 + ln
        p += 1                         # hop count
    if ctl & 0x80:                     # network-layer message, not an APDU
        return None
    a = d[p:]
    if len(a) < 7 or a[0] != 0x10 or a[1] != 0x00 or a[2] != 0xC4:
        return None
    return struct.unpack('>I', a[3:7])[0] & 0x3FFFFF, snet, sadr


def sweep():
    """Who-Is to DNET, unicast to the router. Returns {instance: mstp_mac}."""
    npdu = bytes([0x01, 0x24]) + struct.pack('>H', DNET) + bytes([0x00, 0xFF])
    body = npdu + bytes([0x10, 0x08])          # unconstrained Who-Is
    pkt = bytes([0x81, 0x0a]) + struct.pack('>H', len(body) + 4) + body

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(('0.0.0.0', BACNET_PORT))
    s.settimeout(0.8)
    found = {}
    try:
        for _ in range(ROUNDS):
            s.sendto(pkt, (ROUTER_IP, BACNET_PORT))
            t0 = time.time()
            while time.time() - t0 < LISTEN_SECS:
                try:
                    d, _ = s.recvfrom(1500)
                except socket.timeout:
                    continue
                r = _parse_iam(d)
                if r and r[1] == DNET:
                    found[r[0]] = int(r[2], 16) if r[2] else None
    finally:
        s.close()
    return found


def previous():
    """Last recorded online set, for transition detection."""
    try:
        with open(LOG, encoding='utf-8') as fh:
            last = None
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        return set(json.loads(last)['online']) if last else None
    except (OSError, ValueError, KeyError):
        return None


def main():
    prev = previous()
    found = sweep()
    online = sorted(found)
    offline = [i for i in EXPECTED if i not in found]
    extra = [i for i in online if i not in EXPECTED]

    record = {
        'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
        'router': ROUTER_IP,
        'dnet': DNET,
        'expected': EXPECTED,
        'online': online,
        'offline': offline,
        'unexpected': extra,
        'macs': {str(k): v for k, v in sorted(found.items())},
    }
    if prev is not None:
        record['went_offline'] = sorted(prev - set(online))
        record['came_online'] = sorted(set(online) - prev)

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record) + '\n')

    print('{}  online {}/{}  offline={}  {}'.format(
        record['ts'], len(online), len(EXPECTED),
        offline or 'none',
        'changed: -{} +{}'.format(record.get('went_offline') or [],
                                  record.get('came_online') or [])
        if prev is not None and (record.get('went_offline') or record.get('came_online'))
        else 'no change'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
