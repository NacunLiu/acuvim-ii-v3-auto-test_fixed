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
# The JACE itself. It does not answer Who-Is, but it does answer a directed
# ReadProperty -- which is the one check that catches the failure we actually
# hit: its BACnet/IP port left disabled, so the meters are fine on the wire but
# the station sees none of them.
JACE_IP = '20.20.20.25'
JACE_INSTANCE = 1001
# Track the meters by their MS/TP MAC, not by device instance: the MAC is the
# physical position on the trunk and does not move, while the instances have
# already been renumbered once (2-10 -> 1002-1010) to clear a collision with
# two foreign devices on the IP side.
EXPECTED_MACS = list(range(2, 11))
BACNET_PORT = 47808          # where we send; see _socket() for where we listen
LISTEN_SECS = 12
ROUNDS = 3

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'test_logs', 'bacnet_monitor.jsonl')


def _socket():
    """A UDP socket on an ephemeral port, not on 47808.

    Everything here is request/response and BACnet replies go back to the source
    port, so there is no need to own 47808 -- and owning it is actively harmful:
    YABE or Acuview binds it too, and with SO_REUSEADDR Windows hands each reply
    to only one of the sockets. Sharing the port made the monitor report every
    meter offline the moment YABE was opened.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(('0.0.0.0', 0))
    s.settimeout(0.8)
    return s


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


_SYSTEM_STATUS = {0: 'operational', 1: 'operational-read-only',
                  2: 'download-required', 3: 'download-in-progress',
                  4: 'non-operational', 5: 'backup-in-progress'}


def jace_status(timeout=5):
    """ReadProperty system-status from the JACE's device object.

    Returns one of the BACnet system-status names, 'unreachable' when it does
    not answer (its BACnet/IP port is disabled or the station is down), or
    'error' when it answers with an Error-PDU.
    """
    objid = (8 << 22) | JACE_INSTANCE                 # object type 8 = device
    apdu = bytes([0x00, 0x05, 0x01, 0x0C, 0x0C]) + struct.pack('>I', objid)         + bytes([0x19, 112])                          # property 112 = system-status
    body = bytes([0x01, 0x04]) + apdu
    pkt = bytes([0x81, 0x0a]) + struct.pack('>H', len(body) + 4) + body

    s = _socket()
    try:
        s.sendto(pkt, (JACE_IP, BACNET_PORT))
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                d, a = s.recvfrom(1500)
            except socket.timeout:
                continue
            if a[0] != JACE_IP or len(d) < 8:
                continue
            ctl = d[5]
            p = 6
            if ctl & 0x08:
                p += 2
                p += 1 + d[p]
            if ctl & 0x20:
                p += 2
                p += 1 + d[p]
                p += 1
            ap = d[p:]
            if len(ap) < 2 or ap[1] != 0x01:
                continue
            if ap[0] >> 4 == 5:
                return 'error'
            if ap[0] >> 4 == 3:
                # ctx0 objid, ctx1 propid, then opening tag 3 + an enumerated
                q = 3
                if ap[q] == 0x0C:
                    q += 5
                if ap[q] & 0xF8 == 0x18:
                    q += 1 + (ap[q] & 0x07)
                if ap[q] != 0x3E:
                    return 'unparsed'
                tag = ap[q + 1]
                val = int.from_bytes(ap[q + 2:q + 2 + (tag & 0x07)], 'big')
                return _SYSTEM_STATUS.get(val, 'status-{}'.format(val))
        return 'unreachable'
    finally:
        s.close()


def sweep():
    """Who-Is to DNET, unicast to the router. Returns {mstp_mac: instance}."""
    npdu = bytes([0x01, 0x24]) + struct.pack('>H', DNET) + bytes([0x00, 0xFF])
    body = npdu + bytes([0x10, 0x08])          # unconstrained Who-Is
    pkt = bytes([0x81, 0x0a]) + struct.pack('>H', len(body) + 4) + body

    s = _socket()
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
                if r and r[1] == DNET and r[2]:
                    found[int(r[2], 16)] = r[0]
    finally:
        s.close()
    return found


def previous():
    """Last recorded set of online MACs, for transition detection."""
    try:
        with open(LOG, encoding='utf-8') as fh:
            last = None
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if not last:
            return None
        rec = json.loads(last)
        # Records written before the switch to MAC tracking keyed on instance.
        return set(rec['online']) if 'expected_macs' in rec else None
    except (OSError, ValueError, KeyError):
        return None


def main():
    prev = previous()
    jace = jace_status()
    found = sweep()
    online = sorted(found)
    offline = [m for m in EXPECTED_MACS if m not in found]
    extra = [m for m in online if m not in EXPECTED_MACS]

    record = {
        'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
        'router': ROUTER_IP,
        'dnet': DNET,
        'jace': jace,
        'expected_macs': EXPECTED_MACS,
        'online': online,
        'offline': offline,
        'unexpected': extra,
        'instances': {str(k): v for k, v in sorted(found.items())},
    }
    if prev is not None:
        record['went_offline'] = sorted(prev - set(online))
        record['came_online'] = sorted(set(online) - prev)

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record) + '\n')

    print('{}  JACE={}  online {}/{}  offline MAC={}  {}'.format(
        record['ts'], jace, len(online), len(EXPECTED_MACS),
        offline or 'none',
        'changed: -{} +{}'.format(record.get('went_offline') or [],
                                  record.get('came_online') or [])
        if prev is not None and (record.get('went_offline') or record.get('came_online'))
        else 'no change'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
