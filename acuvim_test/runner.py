"""TestRunner, BACnet wrappers, and phase orchestration."""
import os
import sys
import json
import asyncio
import logging
import multiprocessing
from time import sleep

from acuvim_test import registers as reg
from acuvim_test.log import logger
from acuvim_test.modbus_request import AccuenergyModbusRequest
from acuvim_test.modbus_client import AsyncReadSerialId, tcp_set_registers, close_all_serial_clients
from acuvim_test.reboot import reboot_meter
from acuvim_test.bacnet.ip_test import run_bacnet_ip_test
from acuvim_test.bacnet.mstp_yabe import Client, SSIM_THRESHOLD
from acuvim_test.hardware.kasa_plug import KasaSmartPlug
from acuvim_test.hardware.ip_tracker import get_target_ip_map
from acuvim_test.hardware.port_finder import serial_ports, can_open
from acuvim_test.meter_tests import (
    asyncFlagTest, meterModelScan, energyLegitCheck, AsyncManualIpWrite,
    openBrowser, asyncDHCPEnablePowerCycle, asyncChangeProtocol2, asyncDHCPEnable,
    AsyncModbusTCP, meterMountTypeScan,
)
from acuvim_test.abb_energy import abb_energy_check
from acuvim_test.packet_loss import run_packet_loss_and_restore


# Local NIC address (with CIDR) used by the BACnet/IP client. Override via env
# if the test PC isn't auto-binding correctly, e.g. ACU_BACNET_LOCAL=192.168.61.10/24
BACNET_LOCAL_ADDR = os.environ.get('ACU_BACNET_LOCAL', '0.0.0.0/24')


# Directory where per-meter .log files are written (instead of the repo root).
LOG_DIR = 'test_logs'


# Purpose: BACnet MS/TP test via YABE (relaxed screenshot smoke check).
# Prerequisite: YABE installed.
def BACnetConnectionTest(acuClass):
    client = Client(acuClass.serialNum, acuClass.processNum)
    passed = client.run()  # automated YABE flow (includes the ~120s scan wait)
    # YABE can be slow to load; if the comparison failed, let the operator fix
    # YABE by hand and re-check, instead of failing immediately.
    # Auto screenshot compare failed -> fall back to a MANUAL comparison: the
    # operator opens/connects the meter in YABE, eyeballs it, and reports the
    # verdict. Both verdicts continue the test (N just records the failure).
    while not passed:
        ssim = client.ssim_value if client.ssim_value is not None else 0.0
        try:
            ans = input('\n[BACnet MS/TP] Auto screenshot compare failed (SSIM={:.3f}, need >= {}).\n'
                        '  Do a MANUAL comparison: open YABE, scan and connect the meter, and check\n'
                        '  its data shows correctly. Then:\n'
                        '    P     = manual comparison PASSED -> continue\n'
                        '    N     = manual comparison FAILED -> record to log and continue\n'
                        '    Enter = re-run the automatic screenshot compare\n'
                        '  Choice: '
                        .format(ssim, SSIM_THRESHOLD)).strip().upper()
        except (EOFError, RuntimeError):
            break  # no console (multi-meter child process) -> record failure
        if ans in ('N', 'NO'):
            break
        if ans in ('P', 'PASS', 'Y', 'YES'):
            logger.info('{} BACnet MS/TP (YABE) PASSED by manual comparison (auto SSIM={:.3f})'
                        .format(acuClass.serialNum, ssim))
            passed = True
            break
        passed = client.recheck()
    if passed:
        logger.info('{} BACnet MS/TP (YABE) Test Passed'.format(acuClass.serialNum))
    else:
        logger.warning('{} BACnet MS/TP (YABE) Test Failed'.format(acuClass.serialNum))
        acuClass.fail('\nBACnet MS/TP (YABE) Test Failed')


# Purpose: deterministic BACnet/IP test via bacpypes3 against the meter's IP.
# Prerequisite: BACnet/IP enabled on the meter's Ethernet (AXM-WEB2) module.
def BACnetIpTest(acuClass):
    if not acuClass.address:
        logger.warning('{} BACnet/IP skipped: no IP address known'.format(acuClass.serialNum))
        return
    passed, detail = asyncio.run(run_bacnet_ip_test(acuClass.address, BACNET_LOCAL_ADDR))
    if passed:
        logger.info('{} BACnet/IP Test Passed ({})'.format(acuClass.serialNum, detail))
    else:
        logger.warning('{} BACnet/IP Test Failed ({})'.format(acuClass.serialNum, detail))
        acuClass.fail('\nBACnet/IP Test Failed: {}'.format(detail))


# ---- Test segmentation -------------------------------------------------------
# The full test is split into ordered segments so a run can resume from a
# checkpoint after an interruption. Each entry is (display name, TestRunner
# method name, pause_before). pause_before prompts the operator before that
# segment in interactive mode (e.g. to set up YABE for the BACnet segment).
SEGMENTS = [
    ('S1 Baud rate & latency', 'seg_baud', False),
    ('S2 Energy edit & retention', 'seg_energy', False),
    ('S3 Network (static IP / DHCP / Web2 / Modbus TCP)', 'seg_network', False),
    ('S4 Reboot counter', 'seg_reboot_counter', False),
    ('S5 Web Push & BACnet', 'seg_webpush', True),
    ('S6 Data packet loss & restore', 'seg_packet_loss', False),
]


def _progress_path(serial):
    return serial + '.progress'


def load_progress(serial):
    """Return the saved progress dict for a meter, or None if absent/unreadable."""
    if not serial:
        return None
    try:
        with open(_progress_path(serial), encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None


def save_progress(serial, completed_through, status):
    """Persist 'completed through segment index N' + status ('in_progress'/'done')."""
    if not serial:
        return
    data = {
        'serial': serial,
        'completed_through': completed_through,
        'status': status,
        'segments': [s[0] for s in SEGMENTS],
    }
    try:
        with open(_progress_path(serial), 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.warning('Could not write progress file for {}: {}'.format(serial, e))


# Main Class TestRunner for parallel testing
# Key inputs: processId, Com port, KasaPlug class, (Default baudrate: 19200)
class TestRunner:
    def __init__(self, pNum, plug, port, use_switch=True):
        self.BR = 19200
        self.COM = port
        self.failTest = []
        self.failCount = 0
        self.plug = plug
        self.use_switch = use_switch  # False => manual reboot mode, no Kasa plug
        self.processNum = pNum
        self.serialNum = None
        self.logFile = None
        self.pid = None
        self.address = None
        self.static_ip = None  # set by caller: user-entered IP, or None => reuse DHCP address
        self.skip_energy = False  # set by caller: skip the S2 energy edit/retention test
        self.meter_family = None  # cached result of meterModelScan
        self.model_code = None    # raw 4-char model code from the model register (for prompts)

    # Record a test failure: bump the count and keep the message for reporting.
    def fail(self, message):
        self.failCount += 1
        self.failTest.append(message)

    # Read (and cache) the meter family code: 'A'/'E'/'D'/'B_NEW'/'B_OLD'/None.
    def family(self):
        if self.meter_family is None:
            self.meter_family = meterModelScan(self)
        return self.meter_family

    def is_abb(self):
        return self.family() in ('B_NEW', 'B_OLD')

    def resolve_family(self, interactive=True):
        """Determine the meter family ONCE, up front. Try auto-detect (model
        register); if that fails or the model code isn't in the known lists, ask
        the operator (interactive) so a new/unlisted model doesn't get mis-routed
        to the wrong energy test (or crash on a None family). Sets self.meter_family."""
        fam = meterModelScan(self)
        if fam is not None:
            self.meter_family = fam
            logger.info('{} meter family auto-detected: {} (model {})'
                        .format(self.serialNum, fam, self.model_code))
            return fam
        if interactive:
            self.meter_family = self._prompt_meter_family()
            logger.info('{} meter family set by operator: {}'
                        .format(self.serialNum, self.meter_family))
        else:
            logger.warning('{} meter family unknown and no console to ask; energy routing '
                           'may be wrong'.format(self.serialNum))
        return self.meter_family

    def _prompt_meter_family(self):
        code = self.model_code
        note = ' (model code read: {})'.format(code) if code else ' (model register unreadable)'
        ans = _ask('Could not auto-detect the meter family{}. Which family is this meter?'.format(note),
                   options=['A = Accuenergy',
                            'E = Eaton (e.g. PXE / EPH4)',
                            'D = DEIF',
                            'N = ABB new  (M4M40, float64)',
                            'O = ABB old  (Acuvim IIX Class S, float32)']).strip().upper()
        fam = {'A': 'A', 'E': 'E', 'D': 'D', 'N': 'B_NEW', 'O': 'B_OLD'}.get(ans)
        if fam is None:
            logger.warning('unrecognized family choice {!r}; defaulting to Accuenergy (A)'.format(ans))
            fam = 'A'
        return fam

    # Mode-aware power control: use the Kasa plug when available, otherwise
    # just tell the operator (manual mode has no switch to drive).
    async def power_on(self):
        if self.use_switch:
            await self.plug.powerOn(1)
        else:
            logger.info('Manual mode: please make sure the meter is powered ON')

    async def power_off(self):
        if self.use_switch:
            await self.plug.powerOff()
        else:
            logger.info('Manual mode: tests done, you may power off the meter')

    # ---- Setup ----------------------------------------------------------
    def read_serial(self):
        """Read the meter serial number over RTU. Sets self.serialNum; returns it ('' if unread)."""
        self.pid = os.getpid()
        try:
            sin = asyncio.run(AsyncReadSerialId(self, 1))
        except asyncio.exceptions.CancelledError:
            sin = ''
        self.serialNum = sin or None
        return sin

    def open_log(self, append=False):
        """Attach a per-meter log file handler. append=True resumes (keeps prior results)."""
        base = self.serialNum or 'acuTestlog'
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, base + '.log')
        self.logFile = logging.FileHandler(log_path, mode='a' if append else 'w')
        self.logFile.setLevel(logging.DEBUG)
        logger.addHandler(self.logFile)

    def close_log(self):
        if self.logFile:
            logger.removeHandler(self.logFile)
            self.logFile.close()
            self.logFile = None

    # ---- Test segments (each is one resumable unit) ---------------------
    def seg_baud(self):
        asyncio.run(asyncFlagTest(self))  # connection on different baud rates + latency

    def seg_energy(self):
        if self.skip_energy:
            logger.info('{} Energy edit/retention test SKIPPED by user'.format(self.serialNum))
            return
        Model = self.family()
        if Model in ('B_NEW', 'B_OLD'):
            # ABB (CS0/CS2): float energy region, no display-mode loop, no independent channel.
            asyncio.run(abb_energy_check(self, is_double=(Model == 'B_NEW')))
        elif Model == 'E':
            asyncio.run(energyLegitCheck(self, 0, Model))
        else:
            for i in range(3):
                asyncio.run(energyLegitCheck(self, i, Model))

    def seg_network(self):
        # DHCP first (on 'Others') so we have a reachable address; in "reuse DHCP"
        # mode (static_ip is None) that address becomes the static IP to write.
        asyncio.run(asyncDHCPEnablePowerCycle(self))  # DHCP on 'Others' -> self.address
        openBrowser(self, self.browser_lock)
        if not self.static_ip:
            self.static_ip = self.address
            if self.static_ip:
                logger.info('{} reusing DHCP-assigned {} as the static IP'
                            .format(self.serialNum, self.static_ip))
            else:
                logger.error('{} could not learn a DHCP address for the static IP test'
                             .format(self.serialNum))

        asyncio.run(AsyncManualIpWrite(self))  # static IP on 'Others'
        sleep(60)
        openBrowser(self, self.browser_lock)
        logger.info("{} Static IP test on 'Others' finished".format(self.serialNum))

        logger.info('{} switching channel 2 to WEB2'.format(self.serialNum))
        asyncio.run(asyncChangeProtocol2(self))
        asyncio.run(asyncDHCPEnable(self))  # DHCP on Web2
        openBrowser(self, self.browser_lock)
        asyncio.run(AsyncManualIpWrite(self))  # static IP on Web2
        sleep(60)
        openBrowser(self, self.browser_lock)

        logger.info('Modbus TCP to {} test in progress'.format(self.address))
        asyncio.run(AsyncModbusTCP(self, self.address))

    def seg_reboot_counter(self):
        AccuenergyModbusRequest(self.COM, self.BR, is_abb=self.is_abb()).rebootCounter(self)

    def seg_webpush(self):
        asyncio.run(asyncChangeProtocol2(self, 'OTHER', self.browser_lock))
        asyncio.run(asyncChangeProtocol2(self, 'PROFIBUS'))  # channel 2 -> Profibus
        if self.is_abb():
            # ABB (CS0/CS2) meters are Modbus-only: no BACnet (MS/TP or IP).
            logger.info('{} BACnet tests SKIPPED: ABB (CS0/CS2) meters support Modbus only'
                        .format(self.serialNum))
            return
        asyncio.run(meterMountTypeScan(self))  # channel 1 -> BACnet
        with self.yabe_lock:
            BACnetConnectionTest(self)  # MS/TP via YABE (relaxed)
        BACnetIpTest(self)  # BACnet/IP via bacpypes3 (deterministic)
        # meterMountTypeScan switched channel 1 to BACnet MS/TP, and that takes
        # effect immediately -- once the RS485 line is BACnet it can NOT be
        # switched back over serial. The next segment (S6 packet loss) is
        # Modbus-only, so switch channel 1 back to Modbus over the Web module's
        # Modbus TCP gateway (which reaches the register map regardless of the
        # channel-1 protocol). Fall back to a manual prompt if TCP can't do it.
        if not self._restore_modbus_channel1_via_tcp():
            self._prompt_restore_modbus_channel1()

    def _restore_modbus_channel1_via_tcp(self):
        """Switch RS485 channel 1 back to Modbus @ 19200 over Modbus TCP, then
        reboot to apply. self.address is the Web module's current IP (re-learned
        at the start of this segment via the 'OTHER' DHCP cycle, and the same IP
        openBrowser last pinged). Returns True on success, False to fall back to
        the manual prompt."""
        ip = self.address
        if not ip:
            logger.warning('{} TCP channel-1 restore skipped: no meter IP known'
                           .format(self.serialNum))
            return False
        logger.info('{} switching channel 1 back to Modbus @ 19200 over Modbus TCP ({})'
                    .format(self.serialNum, ip))
        try:
            ok = tcp_set_registers(ip, [(reg.PROTOCOL_CH1, [0]),      # 0 = Modbus
                                        (reg.BAUD_CH1, [19200])])
        except Exception as e:
            logger.warning('{} TCP channel-1 restore to {} errored: {}'
                           .format(self.serialNum, ip, e))
            return False
        if not ok:
            logger.warning('{} TCP channel-1 restore to {} failed'.format(self.serialNum, ip))
            return False
        logger.info('{} channel 1 set to Modbus over TCP; rebooting to apply'
                    .format(self.serialNum))
        asyncio.run(reboot_meter(self, boot_wait=90,
                                 reason='apply Modbus on channel 1 (TCP restore after BACnet)'))
        return True

    def _prompt_restore_modbus_channel1(self):
        """Manual step: operator sets channel 1 back to Modbus @ 19200 on the meter
        display, since the BACnet switch can't be undone over Modbus. Falls back to
        a warning when there is no console (multi-meter child process)."""
        print('\n' + '=' * 60, flush=True)
        print('BACnet test done. Channel 1 is now in BACnet MS/TP mode and can', flush=True)
        print('NOT be switched back over Modbus.', flush=True)
        print('  -> On the METER, set Channel 1 protocol back to Modbus, baud 19200.', flush=True)
        print('  (If Channel 1 is already on Modbus, just press Enter.)', flush=True)
        try:
            input('Press Enter once Channel 1 is back on Modbus @ 19200 to continue... ')
            logger.info('{} operator confirmed Channel 1 restored to Modbus @ 19200'
                        .format(self.serialNum))
        except (EOFError, RuntimeError):
            logger.warning('{} no console to prompt for Channel 1 restore; if it is still '
                           'in BACnet the packet-loss segment will fail'.format(self.serialNum))

    def seg_packet_loss(self):
        # Final test: Modbus @ 115200 packet-loss stress read, then restore the
        # meter to Modbus @ 19200 / channel-2 Web2.
        run_packet_loss_and_restore(self)

    # ---- Segment driver -------------------------------------------------
    def run_step(self, name, method_name, interactive):
        """Run one segment. On uncaught error: log + record it, then either ask
        the operator to retry/abort (interactive) or auto-retry 3x then skip.
        Returns True if the segment completed, False if aborted/skipped."""
        attempt = 0
        while True:
            attempt += 1
            try:
                logger.info('===== START {} ====='.format(name))
                getattr(self, method_name)()
                logger.info('===== DONE  {} ====='.format(name))
                return True
            except Exception as e:
                logger.exception('Segment "{}" raised'.format(name))
                self.fail('Segment {} error: {}'.format(name, e))
                if interactive:
                    ans = _ask('Segment "{}" hit an error:\n    {}\nRetry this segment?'.format(name, e),
                               options=['Y = retry (fix the problem first)',
                                        'N = abort the test']).upper()
                    if ans in ('N', 'NO'):
                        logger.warning('User aborted at segment {}'.format(name))
                        return False
                    input('Fix the problem, then press Enter to retry this segment... ')
                else:
                    if attempt < 3:
                        logger.warning('Auto-retry "{}" ({}/3) in 5s...'.format(name, attempt))
                        sleep(5)
                        continue
                    logger.error('Segment "{}" failed after 3 auto-retries; continuing'.format(name))
                    return False

    def run_meter(self, browser_lock, yabe_lock, start_index=0, interactive=True):
        """Run SEGMENTS[start_index:], checkpointing after each completed segment.
        serialNum must already be read and the log opened. Returns failCount."""
        self.browser_lock = browser_lock
        self.yabe_lock = yabe_lock
        asyncio.run(self.power_on())
        completed_all = True
        try:
            for idx in range(start_index, len(SEGMENTS)):
                name, method_name, pause_before = SEGMENTS[idx]
                if pause_before and interactive:
                    print('\n' + '=' * 56, flush=True)
                    input('Next segment: {}\nPress Enter to start it (set up YABE if needed)... '.format(name))
                if self.run_step(name, method_name, interactive):
                    save_progress(self.serialNum, idx, 'in_progress')
                elif interactive:
                    completed_all = False  # operator aborted
                    break
            if completed_all:
                save_progress(self.serialNum, len(SEGMENTS) - 1, 'done')
        except KeyboardInterrupt:
            # Ctrl+C mid-run: release the COM port before anything else so the
            # next run isn't locked out ('Access is denied'), then re-raise.
            logger.warning('{} interrupted by Ctrl+C; releasing serial port'.format(self.serialNum))
            close_all_serial_clients()
            raise
        finally:
            # Always release the serial port (an interrupted segment leaves its
            # client open; on Windows that keeps a reader thread alive and locks
            # the port). Do this before power_off so the port frees promptly.
            close_all_serial_clients()
            try:
                asyncio.run(self.power_off())
            except Exception as e:
                logger.warning('{} power_off during cleanup failed: {}'.format(self.serialNum, e))
        return self.failCount


#############Config init#####################
# Interactive setup is strictly one-question-at-a-time: each _ask() prints a
# clear, flushed block (separator + question + options) and waits for one line.

def _ask(question, options=None):
    """Ask exactly one question and return the stripped answer.

    Prints a separator + the question + optional option lines, all flushed so
    the prompt always shows (a bare input() prompt can look like a hang).
    """
    print('\n' + '=' * 56, flush=True)
    print(question, flush=True)
    for opt in (options or []):
        print('   ' + opt, flush=True)
    return input('Answer: ').strip()


def _normalize_port(raw):
    """Accept '3', 'com3', 'COM3' and normalize to 'COM3'."""
    raw = raw.strip().upper()
    return raw if raw.startswith('COM') else 'COM' + raw


def _confirm_exit_or_continue(reason):
    """One question: exit (Y, terminates) or keep trying (N, returns)."""
    ans = _ask(reason, options=['Y = exit the program', 'N = keep trying']).upper()
    if ans in ('Y', 'YES'):
        print('Exiting at user request.', flush=True)
        sys.exit(1)


def _port_openable_with_retry(port, attempts=3):
    """Try to open `port` up to `attempts` times. True on success; on failure
    ask exit(Y)/keep-trying(N) and return False so the caller re-prompts."""
    for i in range(1, attempts + 1):
        if can_open(port):
            return True
        logger.warning('Cannot open {} (attempt {}/{})'.format(port, i, attempts))
        sleep(2)
    _confirm_exit_or_continue('Could not open {} after {} tries (port missing or busy).'.format(port, attempts))
    return False


def _ask_port(label):
    """One question: which COM port for `label`. Returns an openable 'COMx'."""
    while True:
        ports = serial_ports()
        detected = ', '.join(ports) if ports else 'none detected'
        port = _normalize_port(_ask(
            '{}: which COM port is it on?'.format(label),
            options=['Detected ports: ' + detected,
                     'Enter a number (e.g. 3) or full name (e.g. COM3)']))
        if _port_openable_with_retry(port):
            return port


def _valid_ip(ip):
    parts = ip.strip().split('.')
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def ask_static_ip():
    """Q: static IP for the meter. Returns an IP string (Y) or None (reuse DHCP address)."""
    ans = _ask('Set a custom static IP for the meter?',
               options=['Y = enter a static IP to use',
                        'N = reuse the DHCP-assigned address as the static IP']).upper()
    if ans not in ('Y', 'YES'):
        return None
    while True:
        ip = _ask('Enter the static IP (e.g. 172.27.25.50):')
        if _valid_ip(ip):
            return ip.strip()
        print('Not a valid IPv4 address, try again.', flush=True)


def ask_skip_energy():
    """Q: skip the energy edit/retention test (S2)? Returns True to skip. All meter types."""
    while True:
        ans = _ask('Skip the energy read/write (retention) test?',
                   options=['Y = skip it', 'N = run it']).upper()
        if ans in ('Y', 'YES'):
            return True
        if ans in ('N', 'NO'):
            return False
        print('Please answer Y or N.', flush=True)


def ask_use_switch():
    """Q: power-cycle mode. True = Kasa switch, False = manual. Requires explicit Y/N."""
    while True:
        ans = _ask('How should the meter be power-cycled during the test?',
                   options=['Y = use Kasa WiFi switch (automatic)',
                            'N = manual reboot (you power-cycle by hand)']).upper()
        if ans in ('Y', 'YES'):
            return True
        if ans in ('N', 'NO'):
            return False
        print('Please answer Y or N.', flush=True)


def collect_switch_configs():
    """Switch mode: pair COM ports with Kasa plugs, one question at a time."""
    # Discover plugs; if none found, retry (rescan) up to 3x then ask to exit.
    while True:
        plugMap = {}
        for attempt in range(1, 4):
            plugMap = get_target_ip_map(force_refresh=True)
            if plugMap:
                break
            logger.warning('No Kasa plug found (attempt {}/3)'.format(attempt))
            sleep(2)
        if plugMap:
            break
        _confirm_exit_or_continue('No Kasa plug found (check PLUG_IPS / WiFi / nmap in ip_tracker.py).')

    plugList = list(plugMap.keys())
    configs = []
    meter_no = 1
    while True:
        port = _ask_port('Meter #{}'.format(meter_no))
        while True:
            ans = _ask('Meter #{}: which Kasa plug id?'.format(meter_no),
                       options=['Available plug ids: ' + ', '.join(str(p) for p in plugList)])
            try:
                plugId = int(ans)
            except ValueError:
                print('Plug id must be a number.', flush=True)
                continue
            if plugId not in plugList:
                print('Plug id {} is not available.'.format(plugId), flush=True)
                continue
            break
        configs.append((port, plugMap[plugId][1]))
        plugList.remove(plugId)
        meter_no += 1
        if not plugList:
            break
        more = _ask('Add another meter?', options=['Y = yes', 'N = no, start testing']).upper()
        if more not in ('Y', 'YES'):
            break
    return configs


def collect_manual_config():
    """Manual mode: a single meter on one COM port, no plug."""
    port = _ask_port('Meter')
    return [(port, None)]


def _resume_decision(runner):
    """Ask which segment to start from. Always prompts (Enter / 1 = full run from
    the top). If an unfinished run exists for this meter, the default is the next
    unfinished segment and choosing it appends to the existing log.

    Returns (start_index, append_log). start_index=0 + append=False means a fresh
    run from the top (overwrites the log).
    """
    serial = runner.serialNum
    progress = load_progress(serial)
    unfinished = bool(progress and progress.get('status') != 'done')

    print('\n' + '=' * 56, flush=True)
    if unfinished:
        done_through = progress.get('completed_through', -1)
        default_idx = min(done_through + 1, len(SEGMENTS) - 1)
        last = SEGMENTS[done_through][0] if 0 <= done_through < len(SEGMENTS) else '(none)'
        print('Detected an UNFINISHED previous test for meter {}.'.format(serial), flush=True)
        print('  Last completed segment: {}'.format(last), flush=True)
    else:
        default_idx = 0
        print('Meter {}: choose the starting segment.'.format(serial), flush=True)
    print('  Segments:', flush=True)
    for i, seg in enumerate(SEGMENTS):
        print('    {} = {}'.format(i + 1, seg[0]), flush=True)

    pick = _ask('Start from which segment number? (1-{}; Enter = {} = "{}"; 1 = run everything)'
                .format(len(SEGMENTS), default_idx + 1, SEGMENTS[default_idx][0]))
    if pick == '':
        start = default_idx
    else:
        try:
            start = max(0, min(int(pick) - 1, len(SEGMENTS) - 1))
        except ValueError:
            start = default_idx

    # Append to the existing log only when continuing an unfinished run at a
    # later segment; a fresh run (or an explicit restart at segment 1) overwrites.
    append_log = unfinished and start > 0
    return start, append_log


def run_single_meter(config, use_switch, static_ip, skip_energy, browser_lock, yabe_lock):
    """Run one meter in the main process: interactive retry + resumable. Returns failCount."""
    port, plug_ip = config
    plug = KasaSmartPlug(plug_ip) if use_switch else None
    runner = TestRunner(1, plug, port, use_switch=use_switch)
    runner.static_ip = static_ip
    runner.skip_energy = skip_energy
    runner.read_serial()
    start_index, append_log = _resume_decision(runner)
    runner.open_log(append=append_log)
    runner.resolve_family(interactive=True)  # detect or ask up front, so segments route correctly
    logger.info('pid {} testing meter {} (starting at segment {})'
                .format(os.getpid(), runner.serialNum, start_index + 1))
    try:
        return runner.run_meter(browser_lock, yabe_lock, start_index=start_index, interactive=True)
    finally:
        runner.close_log()


def _run_meter_process(config, pnum, use_switch, skip_energy, shared_failCount, browser_lock, yabe_lock):
    """Child-process entry for multi-meter parallel runs (non-interactive, no resume).

    Multi-meter always reuses each meter's own DHCP address as its static IP
    (a single user-entered IP can't apply to several meters)."""
    port, plug_ip = config
    plug = KasaSmartPlug(plug_ip) if use_switch else None
    runner = TestRunner(pnum, plug, port, use_switch=use_switch)
    runner.static_ip = None  # reuse per-meter DHCP address
    runner.skip_energy = skip_energy
    runner.read_serial()
    runner.open_log(append=False)
    runner.resolve_family(interactive=False)  # auto-detect only (no console in child)
    try:
        runner.run_meter(browser_lock, yabe_lock, start_index=0, interactive=False)
    finally:
        runner.close_log()
    if runner.failCount:
        with shared_failCount.get_lock():
            shared_failCount.value += 1


def run_multi(configs, use_switch, skip_energy, browser_lock, yabe_lock):
    """Run multiple meters in parallel (one process each). Returns count of meters with failures."""
    shared_failCount = multiprocessing.Value('i', 0)
    procs = [
        multiprocessing.Process(target=_run_meter_process,
                                args=(c, i + 1, use_switch, skip_energy, shared_failCount, browser_lock, yabe_lock))
        for i, c in enumerate(configs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    return shared_failCount.value
