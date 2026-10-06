"""Render the BACnet monitor log into a PNG for attaching to the Jira ticket.

The JACE's own Device Manager cannot be screenshotted automatically (it is
served over HTTPS with a self-signed certificate on a bare IP, which the
browser refuses, and an unattended job has no session there anyway), so the
evidence we can actually automate is our own Who-Is sweep.

    python scripts/monitor_report.py [--out path.png] [--samples N]
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from acuvim_test.report import render_png            # noqa: E402  same look as panel evidence
from scripts.bacnet_monitor import LOG, EXPECTED_MACS, ROUTER_IP, DNET   # noqa: E402


def records(limit=None):
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding='utf-8') as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    return rows[-limit:] if limit else rows


def build_lines(samples):
    rows = records()
    if not rows:
        return ['no samples in {}'.format(LOG)]
    last = rows[-1]
    online = set(last['online'])
    inst = last.get('instances', {})

    out = [
        'Router : KMC BAC-5051E  {}   (BACnet MS/TP network {})'.format(ROUTER_IP, DNET),
        'Probe  : Who-Is addressed to DNET {}, unicast to the router'.format(DNET),
        'Sample : {}'.format(last['ts']),
        '',
        '  MS/TP MAC   Device ID   Status',
        '  ---------   ---------   ------',
    ]
    for mac in EXPECTED_MACS:
        out.append('  {:<9}   {:<9}   {}'.format(
            mac, inst.get(str(mac), '-'), 'OK' if mac in online else 'OFFLINE'))
    out += ['', '  {} of {} meters responding.'.format(
        len(online & set(EXPECTED_MACS)), len(EXPECTED_MACS))]

    recent = records(samples)
    if len(recent) > 1:
        out += ['', 'Recent samples:']
        for r in recent:
            out.append('  {}   JACE {:<12}  online {}/{}   offline {}'.format(
                r['ts'], r.get('jace', '-'), len(r['online']),
                len(r.get('expected_macs') or r.get('expected', [])),
                r['offline'] or 'none'))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default=os.path.join('test_logs', 'bacnet_status.png'))
    ap.add_argument('--samples', type=int, default=10)
    args = ap.parse_args()
    title = 'Acuvim II V3 BACnet meter status - {}'.format(
        datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z'))
    path = render_png(title, build_lines(args.samples), args.out)
    print(path)


if __name__ == '__main__':
    main()
