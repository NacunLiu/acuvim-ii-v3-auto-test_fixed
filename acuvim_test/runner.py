"""TestRunner, BACnet wrappers, and phase orchestration."""
import os
import sys
import asyncio
import logging
import multiprocessing
from time import sleep

from acuvim_test.log import logger
from acuvim_test.ui import p1Passed, p1Fail, p2Passed, p2Fail
from acuvim_test.notify import run
from acuvim_test.modbus_request import AccuenergyModbusRequest
from acuvim_test.modbus_client import AsyncReadSerialId
from acuvim_test.bacnet.ip_test import run_bacnet_ip_test
from acuvim_test.bacnet.mstp_yabe import Client
from acuvim_test.hardware.kasa_plug import KasaSmartPlug
from acuvim_test.hardware.ip_tracker import get_target_ip_map
from acuvim_test.hardware.port_finder import serial_ports, can_open
from acuvim_test.meter_tests import (
    asyncFlagTest, meterModelScan, energyLegitCheck, AsyncManualIpWrite,
    openBrowser, asyncDHCPEnablePowerCycle, asyncChangeProtocol2, asyncDHCPEnable,
    AsyncModbusTCP, meterMountTypeScan,
)


# Local NIC address (with CIDR) used by the BACnet/IP client. Override via env
# if the test PC isn't auto-binding correctly, e.g. ACU_BACNET_LOCAL=192.168.61.10/24
BACNET_LOCAL_ADDR = os.environ.get('ACU_BACNET_LOCAL', '0.0.0.0/24')


# Purpose: BACnet MS/TP test via YABE (relaxed screenshot smoke check).
# Prerequisite: YABE installed.
def BACnetConnectionTest(acuClass):
    BACnetClient = Client(acuClass.serialNum, acuClass.processNum)
    if (BACnetClient.run()):
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

    # Record a test failure: bump the count and keep the message for reporting.
    def fail(self, message):
        self.failCount += 1
        self.failTest.append(message)

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

    # Purpose: generate a log file for each testing meter
    def wrapper(self, name=None):
        self.pid = os.getpid()
        try:
            SerialId = asyncio.run(AsyncReadSerialId(self, 1))
        except asyncio.exceptions.CancelledError:
            SerialId = 'acuTestlog'
        if (name):
            logFileName = SerialId + name + '.log'
        else:
            logFileName = SerialId + '.log'
        self.logFile = logging.FileHandler(logFileName, mode='w')
        self.logFile.setLevel(logging.DEBUG)
        logger.addHandler(self.logFile)
        self.serialNum = asyncio.run(AsyncReadSerialId(self, 1))

    # Frame for Web WebPush Test
    def run_webpush(self, shared_failCount, lock, OpenYabeLock):
        asyncio.run(self.power_on())
        self.wrapper('WebPush')
        logger.info('Process NO. {}, pid {}'.format(self.processNum, os.getpid()))
        asyncio.run(asyncChangeProtocol2(self, 'OTHER', lock))
        # After webpush test,
        asyncio.run(asyncChangeProtocol2(self, 'PROFIBUS'))  # change channel 2 to profibus

        asyncio.run(meterMountTypeScan(self))  # change channel 1 to BACnet
        with OpenYabeLock:
            BACnetConnectionTest(self)  # MS/TP via YABE (relaxed)
        BACnetIpTest(self)  # BACnet/IP via bacpypes3 (deterministic)
        if (self.failCount == 0):
            print(p2Passed)
        else:
            with shared_failCount.get_lock():  # only one process is allowed to increment fail counter
                value = shared_failCount.value
                value += 1
                shared_failCount.value = value
                print(p2Fail)

            parseError2 = ''
            for bugs in self.failTest:
                parseError2 += str(bugs) + ' '

            run('Meter {} Test fails, with {} Errors {}' \
                .format(self.serialNum, self.failCount, parseError2))
            logger.error("Fail {} tests: {}" \
                         .format(self.failCount, parseError2))
            logger.info('Test Failed {}'.format(parseError2))
            self.failTest.append(self.serialNum)

        logger.info('Phase 2 (Web push & BACnet) test completed')
        if self.logFile:
            logger.removeHandler(self.logFile)
            self.logFile.close()

    # main test framework, subject to add new tests/parameters
    # Inputs: shared_failCount -> global variable, need to acquire permit by calling share_failCount.get_lock()
    def run_tests(self, shared_failCount, lock):
        self.wrapper()
        logger.info('Process NO. {}, pid {}'.format(self.processNum, os.getpid()))
        asyncio.run(asyncFlagTest(self))  # test connection on different baud rate
        # Energy reading remain after power cycle; Max/Min editing range test
        Model = meterModelScan(self)
        if (Model == 'E'):
            asyncio.run(energyLegitCheck(self, 0, Model))
        else:
            for i in range(3):
                asyncio.run(energyLegitCheck(self, i, Model))

        logger.info('{} DHCP Disabled'.format(self.serialNum))
        asyncio.run(AsyncManualIpWrite(self))  # set manual ip to 192.168.61.42 MODBUS ADD:258->0
        sleep(60)
        openBrowser(self, lock)

        logger.info("{} Static Ip Test on protocol 'Others' Finished".format(self.serialNum))
        asyncio.run(asyncDHCPEnablePowerCycle(self))
        openBrowser(self, lock)

        logger.info("{} Finished DHCP Test on protocol 'Others'".format(self.serialNum))
        logger.info('Changing {} protocol 2 to WEB2 with baud rate of 115200'.format(self.serialNum))
        asyncio.run(asyncChangeProtocol2(self))

        # web2 fixed ip
        logger.info('{} DHCP disable'.format(self.serialNum))
        asyncio.run(AsyncManualIpWrite(self))  # set manual ip to 192.168.61.42/43 MODBUS ADD:258->0
        sleep(60)
        openBrowser(self, lock)

        # web 2 dhcp
        asyncio.run(asyncDHCPEnable(self))  # 4
        openBrowser(self, lock)

        # ############################Modbus TCP Test###########################################
        logger.info('Modbus TCP to {} Test in progress'.format(self.address))
        asyncio.run(AsyncModbusTCP(self, self.address))  # Modbus TCP Test

        ######################Read Register##############################################
        newTest = AccuenergyModbusRequest(self.COM, self.BR)
        newTest.rebootCounter(self)  # Check reboot counter

        if (self.failCount == 0):
            print(p1Passed)
        else:
            with shared_failCount.get_lock():
                value = shared_failCount.value
                value += 1
                shared_failCount.value = value
            print(p1Fail)
            parseError = ''
            for bugs in self.failTest:
                parseError += str(bugs) + ' '
            run('Meter {} Test fails, with {} Errors {}' \
                .format(self.serialNum, self.failCount, parseError))
            logger.error("Fail {} tests: {}" \
                         .format(self.failCount, parseError))
            # logger.info('Test Failed {}'.format(parseError2))
            self.failTest.append(self.serialNum)
        asyncio.run(self.power_off())
        logger.info('General(web2) test completed')
        if self.logFile:
            logger.removeHandler(self.logFile)
            self.logFile.close()


#############Config init#####################
def _normalize_port(raw):
    """Accept '3', 'com3', 'COM3' and normalize to 'COM3'."""
    raw = raw.strip().upper()
    return raw if raw.startswith('COM') else 'COM' + raw


def _ask_exit(prompt):
    """Ask whether to exit. Y -> terminate the program; N -> return (keep trying)."""
    ans = input('{} Exit? (Y to exit / N to keep trying) '.format(prompt)).strip().upper()
    if ans.startswith('Y'):
        print('Exiting at user request.')
        sys.exit(1)


def ask_use_switch():
    """Ask whether to drive a Kasa WiFi switch (Y) or use manual reboots (N)."""
    answer = input('Use Kasa WiFi switch for power cycling? (Y/N) ').strip().upper()
    return answer in ('Y', 'YES')


def collect_switch_configs():
    """Switch mode: pair each available COM port with a discovered Kasa plug.

    Returns a list of (port, plug_ip) tuples; supports multiple meters.
    Retries plug discovery and port opening instead of crashing.
    """
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
        _ask_exit('No Kasa plug found (check PLUG_IPS / WiFi / nmap / MAC in ip_tracker.py).')

    plugList = list(plugMap.keys())
    configs = []
    processID = 1
    continueAdding = True

    while continueAdding:
        portList = serial_ports()
        print('Available ports:', portList, 'available plug:', plugList)
        port = _normalize_port(input('Process {} will connect to com port (e.g. 3 or COM3) '.format(processID)))
        try:
            plugId = int(input('With Plug '))
        except ValueError:
            print('Plug id must be a number, try again.')
            continue
        if plugId not in plugList:
            print('Plug id {} not available, try again.'.format(plugId))
            continue
        # Retry opening the port up to 3x, then ask to exit (or re-enter on N).
        if not _ensure_port_openable(port):
            continue
        configs.append((port, plugMap[plugId][1]))
        processID += 1
        plugList.remove(plugId)
        if not plugList:
            break
        continueAdding = input('Enter y/Y to add another meter or none to continue ').strip().upper() == 'Y'
    return configs


def collect_manual_config():
    """Manual mode: a single meter on one COM port, no plug.

    Retries opening the port; on repeated failure asks whether to exit.
    """
    while True:
        portList = serial_ports()
        print('Available ports:', portList)
        port = _normalize_port(input('Meter will connect to com port (e.g. 3 or COM3) '))
        if _ensure_port_openable(port):
            return [(port, None)]
        # _ensure_port_openable returned False => user chose to keep trying; loop.


def _ensure_port_openable(port, attempts=3):
    """Try to open `port` up to `attempts` times. On success return True.
    On repeated failure, ask the user to exit (Y quits) or keep trying (N -> False)."""
    for i in range(1, attempts + 1):
        if can_open(port):
            return True
        logger.warning('Cannot open {} (attempt {}/{})'.format(port, i, attempts))
        sleep(2)
    _ask_exit('Could not open {} after {} attempts (port missing or busy).'.format(port, attempts))
    return False  # user chose N -> caller re-prompts / retries


def build_runners(configs, use_switch):
    """Build one TestRunner per config. plug is None in manual mode."""
    runners = []
    for idx, (port, plug_ip) in enumerate(configs):
        plug = KasaSmartPlug(plug_ip) if use_switch else None
        runners.append(TestRunner(idx + 1, plug, port, use_switch=use_switch))
    return runners


def run_phase(method_name, configs, use_switch, extra_args=()):
    """Run one test phase across all meters and return the phase fail count.

    Switch mode forks a process per meter (parallel). A single meter runs in
    the main process so its input() reboot prompts work.
    """
    shared_failCount = multiprocessing.Value('i', 0)
    browser_lock = multiprocessing.Lock()
    runners = build_runners(configs, use_switch)

    # Use multiprocessing only when genuinely parallel (switch mode, >1 meter).
    if use_switch and len(runners) > 1:
        procs = [
            multiprocessing.Process(target=getattr(r, method_name),
                                    args=(shared_failCount, browser_lock, *extra_args))
            for r in runners
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
    else:
        for r in runners:
            getattr(r, method_name)(shared_failCount, browser_lock, *extra_args)
    return shared_failCount.value
