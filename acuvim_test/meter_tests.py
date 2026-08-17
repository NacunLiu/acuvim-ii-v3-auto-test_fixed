"""Meter test flows: network/IP, baud rate, protocol, energy edit/retention."""
import asyncio
import subprocess
import webbrowser
from time import sleep
from collections import defaultdict

from pymodbus.client import AsyncModbusTcpClient

from acuvim_test import registers as reg
from acuvim_test.log import logger
from acuvim_test.modbus_client import (
    make_serial_client, make_async_serial_client, syncConnectWrite,
    asyncReadRegisters, AsyncModbusCheckReadRegisters,
    asyncConnectWrite, asyncConnectWriteMultipleRegisters, write_blocks,
    sync_connect_with_retry, connect_with_retry,
)
from acuvim_test.modbus_request import AccuenergyModbusRequest
from acuvim_test.reboot import reboot_meter


########################################
# Purpose: Open the target Ip in a separated browser
# Each process will first acquire a lock to prevent racing condition by allowing one browser to open at a time
# Will terminate the browser after testing
def openBrowser(acuClass, lock):
    if (not acuClass):
        logger.error('NO ADDRESS ERROR')
        pass
    else:
        with lock:
            pingTest(acuClass, True)


# Purpose: Set up connection to meter through Modbus TCP, read IP through Modbus TCP and verify correctness
async def AsyncModbusTCP(acuClass, Host):
    # Guard: 0.0.0.0 means the Ethernet module never reported a real address
    # (module still booting / no DHCP lease). Connecting to it raises WinError
    # 1214, so record a clear failure instead.
    if not Host or Host == '0.0.0.0':
        logger.error('{} Modbus TCP test skipped: no valid meter IP (got {!r}; Ethernet '
                     'module not booted or no address assigned)'.format(acuClass.serialNum, Host))
        acuClass.fail('\nModbus TCP Test Fail (no valid meter IP)')
        return
    logger.info('Modbus TCP Communication {} test in progress....'.format(Host))
    address = Host
    slaveId = await AsyncModbusCheckReadRegisters(acuClass, reg.SLAVE_ID)
    client = AsyncModbusTcpClient(Host)
    await client.connect()
    sleep(2)
    try:
        data = await asyncReadRegisters(client, reg.IP_ADDRESS, 2, slaveId)  # slave id == 1
        try:
            ip = ''
            for reading in data.registers:
                ip_hex = format(int(reading), '02X')
                if (reading > 255):
                    ip += str(int(ip_hex[:2], 16)) + '.' + str(int(ip_hex[2:], 16)) + '.'
                else:
                    ip += str(int('0x00', 16)) + '.' + str(int(ip_hex, 16)) + '.'
            AD = ip[:-1]
            assert (AD == address)
            logger.info('Complete Modbus TCP Test successfully')
        except AssertionError:
            acuClass.failTest.append('\nModbus TCP Test Fail')
            acuClass.failCount += 1
            logger.error('Error happened when comparing ip address')

    except Exception as e:
        acuClass.failTest.append('\nModbus TCP Test Fail')
        acuClass.failCount += 1
        logger.exception('Unable to connect through Modbus TCP {}'.format(e))
    client.close()


#########################################
# purpose: store ip address of the meter
async def asyncModbusCheckIp(acuClass, client, attempts=7, delay=15):
    """Read the meter's IP register (259) and update acuClass.address.

    The register is only populated once the Ethernet module finishes booting --
    an AXM-WEB2 can take 2-3 minutes after a power cycle, well past the meter
    itself answering Modbus. So retry while the read fails or returns 0.0.0.0,
    and NEVER overwrite a previously learned address with 0.0.0.0 (a bogus
    0.0.0.0 here used to propagate into the Modbus TCP test -> WinError 1214).
    """
    for attempt in range(1, attempts + 1):
        rr = await asyncReadRegisters(client, reg.IP_ADDRESS, 2)
        await asyncio.sleep(0.5)
        try:
            regs = rr.registers
            ip = '{}.{}.{}.{}'.format(regs[0] >> 8, regs[0] & 0xFF,
                                      regs[1] >> 8, regs[1] & 0xFF)
        except (AttributeError, IndexError):
            ip = None
        if ip and ip != '0.0.0.0':
            acuClass.address = ip
            logger.info('{} ip address is: {}'.format(acuClass.serialNum, ip))
            return
        logger.warning('{} IP register not ready ({}); Ethernet module may still be '
                       'booting -- attempt {}/{}, retrying in {}s'
                       .format(acuClass.serialNum, ip or 'read failed', attempt, attempts, delay))
        if attempt < attempts:
            await asyncio.sleep(delay)
    logger.error('{} could not learn a valid IP after {} attempts (module not booted / '
                 'no address assigned); keeping previous address {}'
                 .format(acuClass.serialNum, attempts, acuClass.address))


#######################################################################
# Purpose:
# recommended asynchronous connection through modbus rtu, avoid threads racing and lead to connection failure.
async def asyncConnectIp(acuClass):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    try:
        await client.connect()
        await asyncio.sleep(1)
        logger.debug('Async Connection Status: {}'.format(client.connected))
        await asyncModbusCheckIp(acuClass, client)

    except Exception as e:
        acuClass.failCount += 1
        acuClass.failTest.append('\nasyncConnectIp function Failed{}'.format(acuClass.serialNum))
        logger.warning(e)
        logger.error("COM Occupied during asyncConnectIp test")
    client.close()
    await asyncio.sleep(10)


def ip_to_registers(ip):
    """'192.168.61.42' -> [0xC0A8, 0x3D2A] = [49320, 15658] (two 16-bit words)."""
    a, b, c, d = (int(x) for x in ip.strip().split('.'))
    return [(a << 8) | b, (c << 8) | d]


################################################################################
# Purpose: Disable DHCP and write a static IP (acuClass.static_ip).
async def AsyncManualIpWrite(acuClass, Address=reg.IP_ADDRESS):
    ip = acuClass.static_ip
    if not ip:
        logger.error('{} static IP test skipped: no static IP resolved'.format(acuClass.serialNum))
        return
    logger.info('{} Static IP test: writing {}'.format(acuClass.serialNum, ip))
    await asyncConnectWrite(acuClass, reg.DHCP_ENABLE, [0], 'Disabling DHCP....')  # DHCP off
    await asyncConnectWrite(acuClass, Address, ip_to_registers(ip), 'Writing static IP {}'.format(ip))
    # 120s: the meter answers Modbus well before the Ethernet module (AXM-WEB2)
    # finishes booting; the IP readback additionally retries while it reads 0.0.0.0.
    await reboot_meter(acuClass, boot_wait=120, reason='apply manual IP / disable DHCP')
    await asyncConnectIp(acuClass)


################################################################################
# Write one energy block (optionally clearing all energy first), on a SINGLE
# connection that's held until the whole write is done, then released.
async def asyncManualEnergyWrite(acuClass, Address, Values: list, Reset):
    await write_blocks(acuClass, [(Address, Values)], reset=Reset)
    await asyncio.sleep(3)


#########################################
# Purpose: Generate energy readings across 4 regions in ONE connection.
async def AsyncManualEnergyWriteLegacy(acuClass):
    logger.debug('Generating manual Energy in progress...')
    await write_blocks(acuClass, [
        # Ep_imp, Ep_exp, Eq, Es, etc
        (16456, [20, 31679, 0, 21347, 0, 20528, 1, 57872, 20,
                 53026, 20, 10332, 2, 12865, 21, 62748, 21, 62748]),
        # Es_imp, Esa, Esb, phase-wise energy
        (18688, [21, 62748, 7, 18954, 7, 21871, 7, 21992, 0, 0, 0, 0, 0, 0, 0, 0]),
        # Epa, Epb, phase-wise energy
        (17952, [6, 47101, 0, 6775, 6, 52223, 0, 12060, 6, 63425, 0,
                 2511, 0, 3200, 0, 49436, 0, 11760, 0, 35876, 0, 5567,
                 0, 38094, 7, 18954, 7, 21871, 7, 21922]),
        # four-quad energy q
        (18704, [3, 61059, 0, 835, 0, 1430, 0, 4828, 0, 35539,
                 0, 2365, 0, 10330, 0, 739, 0, 10944, 0, 13722,
                 0, 860, 0, 3081, 3, 39377, 0, 35713, 0, 35016, 0, 35013]),
    ])


async def asyncDHCPEnablePowerCycle(acuClass):
    await asyncConnectWrite(acuClass, reg.DHCP_ENABLE, [1], '{} DHCP enabled'.format(acuClass.serialNum))  # Enabled DHCP
    await reboot_meter(acuClass, reason='enable DHCP')
    await asyncConnectIp(acuClass)


# Purpose: Enable DHCP
async def asyncDHCPEnable(acuClass):
    await asyncConnectWrite(acuClass, reg.DHCP_ENABLE, [1], '{} DHCP enabled'.format(acuClass.serialNum))  # Enabled DHCP
    await reboot_meter(acuClass, reason='enable DHCP')
    await asyncConnectIp(acuClass)


# Purpose: Change Baudrate for channel 2
async def asyncChangeBaudrate2(acuClass, newBaudrate):
    await asyncio.sleep(8)
    await asyncConnectWrite(acuClass, reg.BAUD_CH2, [newBaudrate], 'Changing baud rate on channel 2...')
    await asyncio.sleep(8)


# Purpose: Change protocol for channel 2
async def asyncChangeProtocol2(acuClass, Mode=None, lock=None):
    await asyncio.sleep(5)
    if (Mode == 'OTHER'):
        await asyncConnectWrite(acuClass, reg.PROTOCOL_CH2, [0], '{} Changing protocol 2 to Other'.format(acuClass.serialNum))
        await asyncChangeBaudrate2(acuClass, 38400)
        await AsyncManualIpWrite(acuClass)
        sleep(30)
        openBrowser(acuClass, lock)
        await asyncDHCPEnablePowerCycle(acuClass)
        sleep(30)
        openBrowser(acuClass, lock)

    elif (Mode == 'PROFIBUS'):
        MeterType = meterModelScan(acuClass)
        if (MeterType != 'E'):
            await asyncConnectWrite(acuClass, reg.PROFIBUS_ID, [5], '{} Setting Profibus Id-> 5'
                                    .format(acuClass.serialNum))
        else:
            logger.info('{} Default Profibus Id-> 2'.format(acuClass.serialNum))

        await asyncConnectWrite(acuClass, reg.PROTOCOL_CH2, [5], '{} Changing channel 2 to Profibus'
                                .format(acuClass.serialNum))
    else:
        await asyncConnectWrite(acuClass, reg.PROTOCOL_CH2, [4], 'Changing channel 2 to Web 2')
        await asyncChangeBaudrate2(acuClass, 11520)


## If possible, create an async version to prevent racing condition
def syncChangeBaudRate(acuClass):
    logger.info("{} Protocol 1 baud rate test in progress >>>".format(acuClass.serialNum))
    curRate = acuClass.BR
    dict = defaultdict(int)
    keys = [2400, 4800, 9600, 19200, 38400, 57600, 76800, 115200]
    values = [2400, 4800, 9600, 19200, 38400, 57600, 7680, 11520]
    for c, key in enumerate(keys):
        dict[key] = values[c]

    for rate in keys:
        syncConnectWrite(curRate, acuClass.COM, reg.BAUD_CH1, [dict[rate]])

        client = make_serial_client(acuClass.COM, rate)
        if not sync_connect_with_retry(client, acuClass.COM):
            client.close()
            raise RuntimeError('Could not open {} at {} baud for the baud-rate sweep '
                               '(port busy / handle not yet released?)'.format(acuClass.COM, rate))
        sleep(5)
        rr = client.read_holding_registers(address=reg.BAUD_CH1, count=1, slave=1)
        sleep(2)
        try:
            assert (rr.registers[-1] == dict[rate])
            logger.info('{} baud rate {} passed'.format(acuClass.serialNum, rate))
        except AttributeError as e:
            logger.warning(e)

        client.close()
        sleep(2)
        curRate = rate
    syncConnectWrite(curRate, acuClass.COM, reg.BAUD_CH1, [19200])


async def asyncFlagTest(acuClass):
    await asyncFlagChecking(acuClass, True)
    syncChangeBaudRate(acuClass)
    await asyncFlagChecking(acuClass, False)
    await AsyncModbusCheckReadRegisters(acuClass)


# asyncFlagChecking: read the latency register.
# Fail the test if latency > 160; only raise an alert (no fail) if > 140.
LATENCY_FAIL = 160


LATENCY_ALERT = 140


async def asyncFlagChecking(acuClass, resetEnable: bool):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    # ABB families (old ABB Class S + M4M40) use a relocated latency register.
    is_abb = acuClass.is_abb()
    latency_addr = reg.LATENCY_REG_ABB if is_abb else reg.LATENCY_REG
    latency = None
    if (resetEnable):
        try:
            logger.debug('Erasing Latency Register....')
            newTest = AccuenergyModbusRequest(acuClass.COM, acuClass.BR, is_abb=is_abb)
            await newTest.rebootLatency()
            await asyncio.sleep(1)
            await connect_with_retry(client, acuClass.COM)
            await asyncio.sleep(1)
            RR = await asyncReadRegisters(client, latency_addr, 1)
            latency = RR.registers[-1]
            logger.info('Latency Register Reading: {}'.format(latency))
        except asyncio.exceptions.CancelledError:
            logger.warning('Flag check ERROR')

    else:
        await connect_with_retry(client, acuClass.COM)
        await asyncio.sleep(1)
        RR = await asyncReadRegisters(client, latency_addr, 1)
        latency = RR.registers[-1]
        logger.info('Latency Register Reading: {}'.format(latency))
    client.close()
    await asyncio.sleep(1)

    if latency is None:
        return
    if latency > LATENCY_FAIL:
        logger.warning("Alert! {} Latency is too high: {}".format(acuClass.serialNum, latency))
        acuClass.fail("Meter {} latency reaches {}".format(acuClass.serialNum, latency))
    elif latency > LATENCY_ALERT:
        logger.warning("Alert! {} Latency is {}".format(acuClass.serialNum, latency))


# Purpose: This function will check the meter type (LCD or no LCD)
# For LCD type, set channel 1 to BACnet with id of 4
async def meterMountTypeScan(acuClass):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    await client.connect()
    await asyncio.sleep(1)
    RR = await asyncReadRegisters(client, reg.MOUNT_TYPE, 1)
    logger.info("{} MeterMountTest started".format(acuClass.serialNum))
    client.close()
    await asyncio.sleep(3)
    if (RR.registers[-1] == 0):
        # Set meter to BACnet with id=4
        await asyncConnectWriteMultipleRegisters(acuClass, [reg.BACNET_BAUD, reg.BACNET_ID, reg.PROTOCOL_CH1], [[38400], [0, 4], [2]])
        logger.info("{} is set to BACnet id: 4".format(acuClass.serialNum))
    else:
        logger.info("{} is a non-display meter; BACnet auto-config skipped".format(acuClass.serialNum))


# ABB families share the CS0/CS2 3-char prefix, so they're distinguished by the
# full 4-char model code: new M4M40 stores energy as float64, old Acuvim IIX as float32.
ABB_NEW_MODELS = {'CS07', 'CS08', 'CS09', 'CS0A', 'CS0B'}        # M4M40 / D4M40  -> 'B_NEW'
ABB_OLD_MODELS = {'CS06', 'CS26', 'CS46', 'CG06', 'CG26', 'CG46'}  # Acuvim IIX     -> 'B_OLD'


# Read the meter model; return a family code: 'A' Accuenergy, 'E' Eaton, 'D' DEIF,
# 'B_NEW' ABB M4M40 (float64), 'B_OLD' ABB Acuvim IIX (float32), or None if unknown.
def meterModelScan(acuClass) -> str:
    MeterFamily = defaultdict(list)
    MeterFamily['A'] = ['CU0', 'CP0', 'CP2', 'CP4', 'CU2', 'CV0', 'CM0']  # Accuenergy model
    MeterFamily['E'] = ['CRD', 'CPG', 'CXD', 'CPD', 'CUG', 'CUD', 'CPE', 'CPH', 'EPH']  # Eaton model (EPH = PXE / EPH4)
    MeterFamily['D'] = ['CPB', 'CUB'] # DEIF model
    client = make_serial_client(acuClass.COM, acuClass.BR)
    # Retry the open: right after the async serial-number read the OS may not have
    # released the COM handle yet, so a plain connect() can hit 'Access is denied'.
    if not sync_connect_with_retry(client, acuClass.COM):
        logger.warning('{} could not open {} to read the model (port busy / held by '
                       'another program?); family unknown'.format(acuClass.serialNum, acuClass.COM))
        client.close()
        return None
    sleep(1)
    RR = client.read_holding_registers(reg.MODEL, count=2, slave=1)
    sleep(1)
    client.close()
    # Guard against a failed read: pymodbus returns a ModbusIOException/error
    # object (no .registers) when the meter doesn't answer (e.g. wrong baud, or
    # channel 1 not in Modbus mode). Don't crash -> report unknown family.
    if RR is None or RR.isError() or not hasattr(RR, 'registers'):
        logger.warning('{} could not read model register (comms/baud/protocol issue); family unknown'
                       .format(acuClass.serialNum))
        return None
    Model = ''
    for reading in RR.registers:
        ascii_hex = format(int(reading), '02X')
        hex_bytes = bytes.fromhex(ascii_hex)
        Model += hex_bytes.decode('ascii')

    logger.info('{} model code: {}'.format(acuClass.serialNum, Model))
    acuClass.model_code = Model  # keep the raw code for the family prompt / diagnostics
    # ABB first (needs the full 4-char code; CS06 vs CS07 share the 'CS0' prefix).
    if Model[:4] in ABB_NEW_MODELS:
        return 'B_NEW'
    if Model[:4] in ABB_OLD_MODELS:
        return 'B_OLD'
    for key in MeterFamily:
        if (Model[:3] in MeterFamily[key]):
            return (key)
    logger.warning('{} model code {!r} not in any known family list; family unknown'
                   .format(acuClass.serialNum, Model))
    return None


# Purpose: async read energy values
async def checkEnergy(acuClass, Address, Size):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    await client.connect()
    await asyncio.sleep(1)
    RR = await asyncReadRegisters(client, Address, Size)
    await asyncio.sleep(1)
    client.close()
    return RR.registers


# New test should refer to checkEnergy function
async def checkEnergyLegacy(acuClass):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    await client.connect()
    await asyncio.sleep(1)
    RR = await asyncReadRegisters(client, 16456, 18)
    RR2 = await asyncReadRegisters(client, 18688, 16)
    RR3 = await asyncReadRegisters(client, 17952, 30)
    Energy = RR.registers
    Energy += RR2.registers
    Energy += RR3.registers
    client.close()
    logger.info('{} Energy in meter: {}'.format(acuClass.serialNum, Energy))
    return Energy


##########################################
# pingTest:
# This module will ping the ip address three times. If error, prompt Error, otherwise, active
def pingTest(acuClass, open_browser=False):
    logger.info("{} Ping Test in process...".format(acuClass.serialNum))

    try:
        # Use subprocess to run the ping command
        result = subprocess.run(['ping', '-n', '5', acuClass.address], \
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ping_status = "Network Active"
            if open_browser:
                webbrowser.open(acuClass.address)
                sleep(30)
                result = subprocess.run("taskkill /f /im msedge.exe" \
                                        , stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            ping_status = "{} Ping Test Failed, unable to communicate" \
                .format(acuClass.serialNum)
    except subprocess.TimeoutExpired:
        ping_status = "{} Ping Test Timed Out".format(acuClass.serialNum)
        acuClass.failCount += 1
        acuClass.failTest.append(ping_status)
        logger.error('{} Ping Test Timed Out'.format(acuClass.serialNum))
    except Exception as e:
        ping_status = f"Error: {str(e)}"
        acuClass.failCount += 1
        acuClass.failTest.append(ping_status)
        logger.error('Exception happend at ping test')


##########################################
# The bench source keeps metering while we verify, so energy registers keep
# accumulating between the write and the read-back -- with residual reactive
# power, Eq_exp/Eq_net drift by a count or two (seen live: write 51702, read
# 51701). Compare as 32-bit values with a small tolerance: genuine failures
# (block wiped to 0, wrong value) are thousands of counts away.
ENERGY_DRIFT_TOL = 3


def _energy_mismatches(wrote, read, start_address=None, tol=ENERGY_DRIFT_TOL):
    """Compare two register lists as uint32 pairs with +/-tol counts of slack.
    Returns a list of mismatch descriptions (empty list = match)."""
    if not isinstance(read, list) or len(read) != len(wrote):
        return ['length/read error: wrote {} regs, read {!r}'.format(len(wrote), read)]
    out = []
    for i in range(0, len(wrote) - 1, 2):
        w = (wrote[i] << 16) | wrote[i + 1]
        r = (read[i] << 16) | read[i + 1]
        if abs(r - w) > tol:
            where = 'reg {}'.format(start_address + i) if start_address is not None else 'offset {}'.format(i)
            out.append('{} wrote {} read {} (diff {})'.format(where, w, r, r - w))
    if len(wrote) % 2 and wrote[-1] != read[-1]:
        out.append('trailing reg wrote {} read {}'.format(wrote[-1], read[-1]))
    return out


# Compare energy readings with reference [contents]
async def ReadingComparator(acuClass, contents, start_address, size):
    logger.info('{} reading back {} registers from address {} to verify write...'
                .format(acuClass.serialNum, size, start_address))
    Energy = await checkEnergy(acuClass, start_address, size)
    mismatches = _energy_mismatches(contents, Energy, start_address)
    if not mismatches:
        logger.info('{} read-back MATCHES the written energy within +/-{} counts (address {})'
                    .format(acuClass.serialNum, ENERGY_DRIFT_TOL, start_address))
        return True
    logger.error('{} read-back MISMATCH at address {} ({} value(s) differ): {}'
                 .format(acuClass.serialNum, start_address, len(mismatches), '; '.join(mismatches[:20])))
    return False


# Energy edit/read sub-tests. Each is (label, start_address, contents). The
# common set runs on every meter model; the "independent input channel" set
# (was SequenceId 7/8) only runs on Accuenergy-family meters (not Eaton/DEIF).
# Max writable energy = raw 999,999,990 (= Acuview 2's input cap of
# 99,999,999.0 kWh at 0.1 resolution). Do NOT use raw 999,999,999: newer
# firmware treats it as the rollover threshold and silently zeroes the write
# (ACK but readback 0) on the Real Time Parameter energy blocks
# (0x4048/0x4620/0x4900/0x4910). Verified on fw under test, meter AHB22070458.
_MAX = [15258, 51702]   # +999,999,990  max positive Ep/q/s sample
_NEG = [50277, 13834]   # -999,999,990  negative sample

# Labels are "<region name> (<address>) - <pattern>" so logs say exactly which
# energy block (and register address) each sub-test covers.
ENERGY_TESTS_COMMON = [
    (reg.energy_label(reg.ENERGY_TOTAL, 'max Ep/q/s (1.1)'), reg.ENERGY_TOTAL, _MAX * 9),
    (reg.energy_label(reg.ENERGY_TOTAL, 'negative Ep/q (1.2)'), reg.ENERGY_TOTAL,
     _MAX * 5 + _NEG + _MAX + _NEG + _MAX),
    (reg.energy_label(reg.ENERGY_PHASE, 'max import/export (2)'), reg.ENERGY_PHASE, _MAX * 15),
    (reg.energy_label(reg.ENERGY_PHASE_APPARENT, 'max import/export Es (3)'), reg.ENERGY_PHASE_APPARENT, _MAX * 8),
    (reg.energy_label(reg.ENERGY_FOUR_QUADRANT, 'reactive 4-Q (4)'), reg.ENERGY_FOUR_QUADRANT, _MAX * 16),
]


ENERGY_TESTS_INDEPENDENT = [
    (reg.energy_label(reg.ENERGY_INDEP_CHANNEL, 'max (6a)'), reg.ENERGY_INDEP_CHANNEL, _MAX * 32),
    (reg.energy_label(reg.ENERGY_INDEP_CHANNEL, 'min (6b)'), reg.ENERGY_INDEP_CHANNEL,
     _MAX * 14 + (_NEG + _MAX * 3) * 2 + _MAX * 12),
]


DP_TYPE = {0: 'Primary 0.1', 1: 'Secondary 0.001', 2: 'Primary 0.001'}


# Purpose: verify energy readings can be edited/read back correctly for a given
# display (decimal-point) mode, then confirm they survive a power cycle.
async def energyLegitCheck(acuClass, Display, MeterModel):
    # Eaton meters only support Primary 0.1, so skip the other display modes.
    if MeterModel == 'E' and Display in (1, 2):
        return

    logger.info('{} Change to Display Mode : {}'.format(acuClass.serialNum, DP_TYPE[Display]))
    await asyncConnectWrite(acuClass, reg.DISPLAY_MODE, [Display])
    await asyncio.sleep(3)

    tests = list(ENERGY_TESTS_COMMON)
    if MeterModel in ('E', 'D'):  # Eaton/DEIF don't support independent input-channel energy
        logger.info('Meter model {}: independent energy tests skipped'.format(MeterModel))
    else:
        tests += ENERGY_TESTS_INDEPENDENT

    for label, start_address, contents in tests:
        await asyncManualEnergyWrite(acuClass, start_address, contents, True)
        await isMemorySectionEmpty(acuClass, start_address)
        if await ReadingComparator(acuClass, contents, start_address, len(contents)):
            logger.info('{} {} passed'.format(acuClass.serialNum, label))
        else:
            msg = '{} {} FAILED @ DP Mode {}'.format(acuClass.serialNum, label, DP_TYPE[Display])
            logger.error(msg)
            acuClass.fail(msg)

    # Verify stored energy survives a power cycle (was SequenceId 9).
    await EnergyMemoryRetention(acuClass, False)


# Purpose: confirm the OTHER energy regions are still zero (write didn't bleed over).
# Sizes are register counts read per region (kept as in the original test).
_MEMORY_REGION_SIZES = {
    reg.ENERGY_TOTAL: 9,
    reg.ENERGY_PHASE: 15,
    reg.ENERGY_PHASE_APPARENT: 8,
    reg.ENERGY_FOUR_QUADRANT: 16,
    reg.ENERGY_INDEP_CHANNEL: 32,
}


async def isMemorySectionEmpty(acuClass, StartAddress):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    await client.connect()
    await asyncio.sleep(1)
    all_clear = True
    for address, size in _MEMORY_REGION_SIZES.items():
        if address == StartAddress:
            continue
        readings = await asyncReadRegisters(client, address, size)
        if not all(x == 0 for x in readings.registers):
            all_clear = False
            name = reg.ENERGY_REGION_NAMES.get(address, 'region')
            msg = '{} {} ({}) should be empty but is not: {}'.format(
                acuClass.serialNum, name, address, readings.registers)
            logger.error(msg)
            acuClass.fail(msg)
    client.close()
    if all_clear:
        logger.info('{} other energy regions are empty as expected'.format(acuClass.serialNum))
    await asyncio.sleep(1)


# Expected read-back for AsyncManualEnergyWriteLegacy (16456 block + 18688 +
# 17952 as checkEnergyLegacy concatenates them). Compared with drift tolerance.
LEGACY_ENERGY_EXPECTED = [20, 31679, 0, 21347, 0, 20528, 1, 57872, 20, 53026, 20, 10332, 2, 12865, 21, 62748, 21,
                          62748, 21, 62748, 7, 18954, 7, 21871, 7, 21992, 0, 0, 0, 0, 0, 0, 0, 0, 6, 47101, 0, 6775, 6,
                          52223, 0, 12060,
                          6, 63425, 0, 2511, 0, 3200, 0, 49436, 0, 11760, 0, 35876, 0, 5567, 0, 38094, 7, 18954, 7,
                          21871, 7, 21922]


# Energy memory retention test
# Purpose:
async def EnergyMemoryRetention(acuClass, WaitControl):
    if (WaitControl):
        await asyncio.sleep(20)
    else:
        pass
    await AsyncManualEnergyWriteLegacy(acuClass)
    Energy = await checkEnergyLegacy(acuClass)
    mismatches = _energy_mismatches(LEGACY_ENERGY_EXPECTED, Energy)
    if mismatches:
        acuClass.failCount += 1
        acuClass.failTest.append('\nEnergy memory retention test 1 fails')
        logger.error('{} Energy memory retention test 1 has failed: {}'
                     .format(acuClass.serialNum, '; '.join(mismatches[:10])))
    else:
        logger.info('{} retention test 1: written energy verified (within +/-{} counts)'
                    .format(acuClass.serialNum, ENERGY_DRIFT_TOL))

    await reboot_meter(acuClass, store_wait=130, reason='energy memory retention')
    await asyncio.sleep(30)
    Energy = await checkEnergyLegacy(acuClass)
    # After ~4 min of store+reboot the live source drifts the reactive counters
    # a little further -- allow a bigger (still tiny) budget for the power-cycle leg.
    mismatches = _energy_mismatches(LEGACY_ENERGY_EXPECTED, Energy, tol=ENERGY_DRIFT_TOL * 3)
    if mismatches:
        acuClass.failCount += 1
        acuClass.failTest.append('\nEnergy memory retention test 2 fails')
        logger.error('{} Energy memory retention test 2 has failed: {}'
                     .format(acuClass.serialNum, '; '.join(mismatches[:10])))
    else:
        logger.info('{} retention test 2: energy survived the power cycle (within +/-{} counts)'
                    .format(acuClass.serialNum, ENERGY_DRIFT_TOL * 3))
