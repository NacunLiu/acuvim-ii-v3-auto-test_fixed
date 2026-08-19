"""Upload test results to the internal R&D Panel checklist (rd-panel).

After a test run, this module slices the per-meter log (test_logs/<SN>.log) into
per-checklist-item evidence, renders each slice to a PNG, uploads it to the
matching item on the panel's manual checklist, and sets the item's Pass/Fail.

Which checklist: meter family 'E' (Eaton / PXE / EPH4) -> "Eaton Checklist",
family 'A' (Accuenergy) -> "Accuenergy Checklist". Only the communication-test
subtree is touched -- Jira tickets / Internal Release items are never modified.

Run selection: test runs are created manually per internal release, so the run
id changes every time. The script lists the panel's runs and defaults to the
newest in-progress AcuvimIIV3 run; the operator confirms with Enter.

Item mapping: checklist item ids change when the template is edited, so the
"item name -> refId" map is built dynamically from GET /runs/{id} on every
upload -- nothing is hardcoded.

Credentials: NEVER in source control. Put them in panel_config.ini (gitignored,
see panel_config.ini.example) at the repo root, or in the environment as
ACU_PANEL_EMAIL / ACU_PANEL_PASSWORD (+ optional ACU_PANEL_URL).

Usage:
    python -m acuvim_test.report                # interactive: pick log + run
    python -m acuvim_test.report --serial AHB54040487
    python -m acuvim_test.report --serial AHB54040487 --dry-run   # no writes
It is also offered automatically at the end of a single-meter test run.
"""
import os
import re
import glob
import time
import argparse
import configparser

import requests
from PIL import Image, ImageDraw, ImageFont

from acuvim_test.log import logger

DEFAULT_URL = 'https://rd-panel.simonrd.ca'
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(REPO_ROOT, 'panel_config.ini')
LOG_DIR = os.path.join(REPO_ROOT, 'test_logs')
PNG_DIR = os.path.join(LOG_DIR, 'report_png')

# A line containing any of these marks its item as Fail.
FAIL_MARKERS = ['ERROR', 'FAILED', 'FAIL TO', 'Alert!', 'MISMATCH', 'Timed Out',
                'has failed', 'Test Failed', 'could not read', 'could not open']

# Log slices, cut on the runner's segment markers ('===== START <name> =====').
SEG_KEYS = {
    'S1': 'S1 Baud rate & latency',
    'S2': 'S2 Energy edit & retention',
    'S3': 'S3 Network (static IP / DHCP / Web2 / Modbus TCP)',
    'S4': 'S4 Reboot counter',
    'S5': 'S5 Web Push & BACnet',
    'S6': 'S6 Data packet loss & restore',
}


def slice_log(text):
    """Split the log into named slices: PRE (before S1), S1..S6, S3A/S3B, ALL."""
    lines = text.splitlines()
    slices = {'ALL': lines}
    starts = {}
    for i, ln in enumerate(lines):
        m = re.search(r'===== START (S\d) ', ln)
        if m and m.group(1) not in starts:
            starts[m.group(1)] = i
    order = sorted(starts.items(), key=lambda kv: kv[1])
    slices['PRE'] = lines[:order[0][1]] if order else lines
    for n, (seg, begin) in enumerate(order):
        end = order[n + 1][1] if n + 1 < len(order) else len(lines)
        slices[seg] = lines[begin:end]
    # S3 runs the 'Others' phase first, then Web2 -- split for the duplicated
    # Static IP / DHCP checklist items.
    s3 = slices.get('S3', [])
    cut = next((i for i, ln in enumerate(s3) if 'switching channel 2 to WEB2' in ln), len(s3))
    slices['S3A'], slices['S3B'] = s3[:cut], s3[cut:]
    return slices


# Checklist rules: level5 item name (as generated for the panel template) ->
# (slice key, [substrings to collect]). Duplicated names (Static IP / DHCP) are
# listed twice IN CHECKLIST ORDER and consumed per occurrence. Alternate names
# cover the small Accuenergy-vs-Eaton wording differences.
RULES = {
    'Serial Number / Log': [('PRE', ['testing meter'])],
    'Reset Latency': [('S1', ['Latency Register Reading'])],
    'Protocol 1 Baud Sweep': [('S1', ['baud rate'])],
    'Post Baud Latency': [('S1', ['Latency Register Reading', 'Alert!'])],
    'Default Register Read': [('S1', [])],  # no distinct marker -> whole S1
    'Meter Family': [('PRE', ['model code', 'meter family'])],
    'Max Ep/q/s': [('S2', ['max Ep/q/s'])],
    'Negative Ep/q': [('S2', ['negative Ep/q'])],
    'Max Import/Export Ep/q/s': [('S2', ['max import/export (2)'])],
    'Apparent Energy': [('S2', ['max import/export Es'])],
    'Four-Quadrant Reactive': [('S2', ['reactive 4-Q'])],
    'Independent Energy Skip Rules': [('S2', ['independent energy tests skipped',
                                              'Independent channel'])],
    'Independent Energy Not Supported': [('S2', ['independent energy tests skipped'])],
    'Channel Max Energy': [('S2', ['Independent channel (9472) - max'])],
    'Independent Max Energy': [('S2', ['Independent channel (9472) - max'])],
    'Channel Min Energy': [('S2', ['Independent channel (9472) - min'])],
    'Independent Min Energy': [('S2', ['Independent channel (9472) - min'])],
    'Unwritten Section Check': [('S2', ['empty'])],
    'Power Cycle Retention': [('S2', ['storing readings to FeRAM', 'Energy in meter',
                                      'memory retention'])],
    'Static IP': [('S3A', ['Static IP test', 'Ping Test']),
                  ('S3B', ['Static IP test', 'Ping Test'])],
    'DHCP': [('S3A', ['DHCP enabled', 'ip address is']),
             ('S3B', ['DHCP enabled', 'ip address is'])],
    'Switch to WEB2': [('S3B', ['switching channel 2 to WEB2'])],
    'IP Verification': [('S3', ['Modbus TCP'])],
    'Counter Reset': [('S4', ['reboot', 'Reboot counter', 'RESET'])],
    'Power Off': [('ALL', ['Restore complete', 'Test finished'])],
    'Power On / Log': [('S5', ['===== START S5'])],
    'Other Static/DHCP': [('S5', ['Static IP test', 'DHCP enabled', 'ip address is'])],
    'Profibus Mode': [('S5', ['Profibus', 'protocol 2'])],
    'Profibus Default ID': [('S5', ['Profibus', 'protocol 2'])],
    'Channel 1 BACnet ID 4': [('S5', ['BACnet id: 4', 'non-display meter'])],
    # MS/TP is logged in S5 (Web Push fitted) while BACnet/IP is logged in S3
    # (Web2 fitted), so collect this item's evidence from the whole log rather
    # than one segment -- keeps the panel template unchanged.
    'BACnet Connection': [('ALL', ['BACnet MS/TP', 'BACnet/IP', 'readings match the source'])],
    'Failure Notification': [('ALL', ['Test finished'])],
    'Packet Loss Stress Test': [('S6', ['packet loss', 'Data packet loss'])],
}

# Items that are N/A (not Fail) when the log shows independents were skipped.
NA_WHEN_INDEP_SKIPPED = {'Independent Energy Not Supported', 'Independent Max Energy',
                         'Independent Min Energy', 'Channel Max Energy', 'Channel Min Energy'}


def collect_evidence(slices, level5, occurrence):
    """Return (lines, status) for one checklist item, or (None, None) to skip."""
    rules = RULES.get(level5)
    if not rules:
        return None, None
    slice_key, patterns = rules[min(occurrence, len(rules) - 1)]
    body = slices.get(slice_key)
    if not body:
        return None, None  # segment never ran -> leave the item untouched
    if patterns:
        lines = [ln for ln in body if any(p in ln for p in patterns)]
    else:
        lines = list(body)
    if not lines and level5 in NA_WHEN_INDEP_SKIPPED:
        skip = [ln for ln in slices.get('S2', []) if 'independent energy tests skipped' in ln]
        if skip:
            return skip, 'N/A'
    if not lines:
        return None, None
    lines = lines[:60]  # keep screenshots readable
    status = 'Fail' if any(m in ln for ln in lines for m in FAIL_MARKERS) else 'Pass'
    return lines, status


def render_png(title, lines, out_path):
    """Render log lines to a PNG (white background, monospace)."""
    try:
        font = ImageFont.truetype('C:\\Windows\\Fonts\\consola.ttf', 16)
    except Exception:
        font = ImageFont.load_default()
    wrapped = [title, '=' * min(len(title), 120)]
    for ln in lines:
        while len(ln) > 150:
            wrapped.append(ln[:150])
            ln = ln[150:]
        wrapped.append(ln)
    line_h = 20
    width = min(1500, 20 + 9 * max(len(x) for x in wrapped))
    img = Image.new('RGB', (width, 20 + line_h * len(wrapped)), 'white')
    draw = ImageDraw.Draw(img)
    for i, ln in enumerate(wrapped):
        draw.text((10, 10 + i * line_h), ln, fill='black', font=font)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


class Panel:
    """Thin client for the R&D Panel REST API (session-cookie auth)."""

    def __init__(self, url=DEFAULT_URL):
        self.url = url.rstrip('/')
        self.s = requests.Session()

    def login(self, email, password):
        r = self.s.post(self.url + '/api/auth/login',
                        json={'email': email, 'password': password}, timeout=15)
        r.raise_for_status()
        me = self.s.get(self.url + '/api/auth/me', timeout=15).json()
        logger.info('Panel login OK as {}'.format(me.get('name') or me.get('email')))
        return me

    def runs(self):
        r = self.s.get(self.url + '/api/rtest/runs', timeout=15)
        r.raise_for_status()
        return r.json()

    def run(self, run_id):
        r = self.s.get(self.url + '/api/rtest/runs/{}'.format(run_id), timeout=15)
        r.raise_for_status()
        return r.json()

    def set_result(self, run_id, ref_id, status, comment=''):
        r = self.s.put(self.url + '/api/rtest/runs/{}/result'.format(run_id),
                       json={'refKind': 'manual', 'refId': str(ref_id),
                             'status': status, 'comment': comment}, timeout=15)
        r.raise_for_status()

    def upload(self, run_id, ref_id, path):
        with open(path, 'rb') as fh:
            r = self.s.post(self.url + '/api/rtest/runs/{}/upload'.format(run_id),
                            files={'file': (os.path.basename(path), fh, 'image/png')},
                            data={'refKind': 'manual', 'refId': str(ref_id)}, timeout=60)
        r.raise_for_status()


def load_credentials():
    """Credentials from env or panel_config.ini. Returns (url, email, password)."""
    url = os.environ.get('ACU_PANEL_URL')
    email = os.environ.get('ACU_PANEL_EMAIL')
    password = os.environ.get('ACU_PANEL_PASSWORD')
    if not (email and password) and os.path.exists(CONFIG_FILE):
        cp = configparser.ConfigParser()
        cp.read(CONFIG_FILE, encoding='utf-8')
        sec = cp['panel'] if cp.has_section('panel') else {}
        url = url or sec.get('url')
        email = email or sec.get('email')
        password = password or sec.get('password')
    if not (email and password):
        raise SystemExit(
            'Panel credentials not found.\n'
            'Create {} (see panel_config.ini.example) or set ACU_PANEL_EMAIL / '
            'ACU_PANEL_PASSWORD.'.format(CONFIG_FILE))
    return (url or DEFAULT_URL), email, password


def parse_family_from_log(text):
    m = re.search(r'meter family (?:auto-detected|set by operator): (\S+)', text)
    return m.group(1) if m else None


def _ask(question, options=None):
    print('\n' + '=' * 56, flush=True)
    print(question, flush=True)
    for opt in (options or []):
        print('   ' + opt, flush=True)
    return input('Answer: ').strip()


def pick_log(serial=None):
    if serial:
        path = os.path.join(LOG_DIR, serial + '.log')
        if not os.path.exists(path):
            raise SystemExit('Log not found: {}'.format(path))
        return path
    logs = sorted(glob.glob(os.path.join(LOG_DIR, '*.log')),
                  key=os.path.getmtime, reverse=True)
    if not logs:
        raise SystemExit('No logs found in {}'.format(LOG_DIR))
    opts = ['{} = {} ({})'.format(i + 1, os.path.basename(p),
                                  time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(p))))
            for i, p in enumerate(logs[:8])]
    ans = _ask('Which meter log to upload? (Enter = 1 = newest)', options=opts)
    idx = int(ans) - 1 if ans.isdigit() else 0
    return logs[max(0, min(idx, len(logs) - 1))]


def pick_run(panel):
    runs = [r for r in panel.runs() if r.get('state') == 'in_progress']
    if not runs:
        raise SystemExit('No in-progress runs on the panel.')
    # Newest AcuvimIIV3 run first (runs come newest-first from the API).
    default = next((i for i, r in enumerate(runs) if 'AcuvimIIV3' in (r.get('name') or '')), 0)
    opts = ['{} = [{}] {}'.format(i + 1, r['id'], r['name']) for i, r in enumerate(runs[:8])]
    ans = _ask('Which test run? (Enter = {} = default)'.format(default + 1), options=opts)
    idx = int(ans) - 1 if ans.isdigit() else default
    return runs[max(0, min(idx, len(runs) - 1))]['id']


def upload_for_meter(serial=None, family=None, run_id=None, dry_run=False, assume_yes=False):
    """Main flow. Returns the number of items updated."""
    log_path = pick_log(serial)
    sn = os.path.splitext(os.path.basename(log_path))[0]
    with open(log_path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    slices = slice_log(text)

    family = family or parse_family_from_log(text)
    if family == 'E':
        checklist = 'Eaton Checklist'
    elif family == 'A':
        checklist = 'Accuenergy Checklist'
    else:
        ans = _ask('Meter family unknown ({}). Which checklist?'.format(family),
                   options=['A = Accuenergy Checklist', 'E = Eaton Checklist']).upper()
        checklist = 'Eaton Checklist' if ans == 'E' else 'Accuenergy Checklist'

    url, email, password = load_credentials()
    panel = Panel(url)
    panel.login(email, password)
    run_id = run_id or pick_run(panel)
    run = panel.run(run_id)
    items = [t for t in run.get('templateItems', []) if t.get('level1') == checklist]
    if not items:
        raise SystemExit('Run {} has no "{}" items'.format(run_id, checklist))

    # Build the plan: one entry per checklist item that has evidence in the log.
    plan, seen = [], {}
    for t in items:
        name = t.get('level5') or t.get('item_text', '')
        occ = seen.get(name, 0)
        seen[name] = occ + 1
        lines, status = collect_evidence(slices, name, occ)
        if lines:
            plan.append((t, name, occ, lines, status))

    print('\nRun [{}] {} -> {}'.format(run_id, run.get('name'), checklist), flush=True)
    print('{} of {} checklist items have log evidence for meter {}:'.format(
        len(plan), len(items), sn), flush=True)
    for t, name, occ, lines, status in plan:
        print('  {:<34} {:>4}  ({} line(s))'.format(
            name + (' #%d' % (occ + 1) if seen[name] > 1 else ''), status, len(lines)), flush=True)
    if dry_run:
        print('\nDRY RUN: rendering PNGs only, no upload / no status change.', flush=True)
    elif not assume_yes:
        if _ask('Upload these {} results?'.format(len(plan)),
                options=['Y = upload', 'N = cancel']).upper() not in ('Y', 'YES'):
            print('Cancelled.', flush=True)
            return 0

    done = 0
    for t, name, occ, lines, status in plan:
        slug = re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_')
        png = os.path.join(PNG_DIR, sn, '{}_{}{}.png'.format(sn, slug, occ + 1 if seen[name] > 1 else ''))
        render_png('{}  |  {}  |  {}'.format(sn, name, status), lines, png)
        if dry_run:
            done += 1
            continue
        try:
            panel.upload(run_id, t['id'], png)
            panel.set_result(run_id, t['id'], status,
                             'Auto-uploaded from {}.log (acuvim_test.report)'.format(sn))
            logger.info('panel: {} -> {} ({} lines) uploaded'.format(name, status, len(lines)))
            done += 1
        except Exception as e:
            logger.error('panel: {} upload failed: {}'.format(name, e))
    print('\n{}{} of {} items {}.'.format('DRY RUN: ' if dry_run else '', done, len(plan),
                                          'rendered' if dry_run else 'uploaded'), flush=True)
    return done


def offer_upload(serial, family):
    """End-of-test hook (called from the runner; never raises)."""
    try:
        ans = _ask('Upload results to the R&D Panel checklist?',
                   options=['Y = upload now',
                            'N = skip (later: python -m acuvim_test.report --serial {})'.format(serial)])
        if ans.upper() in ('Y', 'YES'):
            upload_for_meter(serial=serial, family=family)
    except (EOFError, RuntimeError):
        pass  # no console
    except SystemExit as e:
        print(e, flush=True)
    except Exception as e:
        logger.warning('Panel upload skipped due to error: {}'.format(e))


def main():
    ap = argparse.ArgumentParser(description='Upload acuvim test results to the R&D Panel checklist')
    ap.add_argument('--serial', help='meter serial number (log = test_logs/<SN>.log)')
    ap.add_argument('--family', help="meter family: A / E (default: parsed from the log)")
    ap.add_argument('--run', type=int, help='panel run id (default: pick from list)')
    ap.add_argument('--dry-run', action='store_true', help='render PNGs only, no uploads')
    ap.add_argument('--yes', action='store_true', help='skip the confirmation prompt')
    a = ap.parse_args()
    upload_for_meter(serial=a.serial, family=a.family, run_id=a.run,
                     dry_run=a.dry_run, assume_yes=a.yes)


if __name__ == '__main__':
    main()
