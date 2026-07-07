"""Low-level Modbus access layer: client factories and register read/write."""
import asyncio
from time import sleep

from pymodbus.client import ModbusSerialClient, AsyncModbusSerialClient, ModbusTcpClient
from pymodbus.transaction import ModbusRtuFramer
from pymodbus.payload import BinaryPayloadBuilder
from pymodbus.constants import Endian

from acuvim_test import registers as reg
from acuvim_test.log import logger


# ---- Modbus client factories -------------------------------------------------
# Single source of truth for the meter's RS485 line settings. If the pymodbus
# API changes (e.g. a 3.13 migration), update only these two functions.
def make_serial_client(port, baudrate, timeout=1):
    """Sync Modbus-RTU client with the project's standard line settings.

    timeout (seconds) is overridable for the packet-loss test, which uses a
    short per-request timeout (50 ms) to count unanswered requests.
    """
    return ModbusSerialClient(method='rtu', port=port, baudrate=baudrate, parity='N',
                              stopbits=1, bytesize=8, timeout=timeout, framer=ModbusRtuFramer)


def make_async_serial_client(port, baudrate):
    """Async Modbus-RTU client with the project's standard line settings."""
    return AsyncModbusSerialClient(method='rtu', port=port, baudrate=baudrate, parity='N',
                                   stopbits=1, bytesize=8, timeout=1, framer=ModbusRtuFramer)


MODBUS_TCP_PORT = 502   # AXM-WEB2 / AXM-WEB-PUSH Modbus TCP gateway default port


def tcp_set_registers(ip, writes, port=MODBUS_TCP_PORT, unit=1, timeout=3):
    """Write config registers over Modbus TCP (via the Web module's gateway) and
    verify the readback. `writes` is a list of (address, [values]).

    This reaches the meter's register map over the internal bus, independently of
    the RS485 channel-1 protocol -- so it works even when channel 1 is in BACnet
    and can't be reached over serial. Config registers reject write-single (0x06,
    IllegalFunction), so this uses write-multiple (0x10). Returns True on success.
    """
    client = ModbusTcpClient(ip, port=port, timeout=timeout)
    if not client.connect():
        logger.warning('Modbus TCP connect to {}:{} failed'.format(ip, port))
        return False
    try:
        for addr, vals in writes:
            wrr = client.write_registers(addr, list(vals), slave=unit)
            if wrr is None or wrr.isError():
                logger.warning('Modbus TCP write addr {} <- {} failed: {}'.format(addr, vals, wrr))
                return False
            rr = client.read_holding_registers(addr, count=len(vals), slave=unit)
            if rr is None or rr.isError() or not hasattr(rr, 'registers') or rr.registers != list(vals):
                logger.warning('Modbus TCP readback addr {} expected {} got {}'
                               .format(addr, vals, getattr(rr, 'registers', rr)))
                return False
        return True
    finally:
        client.close()


async def connect_with_retry(client, port, attempts=3, delay=2):
    """Open `client`, retrying if the COM port is briefly unavailable.

    On Windows the OS may not have released the serial handle yet from a prior
    close(), so a fresh connect() can fail with 'Access is denied' or just leave
    client.connected False. We retry with a short delay. Returns True if the
    connection is actually up.
    """
    for i in range(1, attempts + 1):
        try:
            await client.connect()
        except Exception as e:
            logger.warning('connect {} attempt {}/{} raised: {}'.format(port, i, attempts, e))
        await asyncio.sleep(1)
        if client.connected:
            return True
        logger.warning('{} not ready (attempt {}/{}), retrying...'.format(port, i, attempts))
        await asyncio.sleep(delay)
    logger.error('Could not open {} after {} attempts'.format(port, attempts))
    return False


def sync_connect_with_retry(client, port, attempts=3, delay=2):
    """Sync counterpart of connect_with_retry. On Windows the OS may not have
    released the serial handle yet from a prior client's close() (e.g. the async
    serial client used to read the serial number), so a fresh open() can fail with
    'Access is denied'. Retry with a short delay. Returns True if the port opened.
    """
    for i in range(1, attempts + 1):
        try:
            if client.connect():
                return True
        except Exception as e:
            logger.warning('connect {} attempt {}/{} raised: {}'.format(port, i, attempts, e))
        if getattr(client, 'connected', False):
            return True
        logger.warning('{} not ready (attempt {}/{}), retrying in {}s...'.format(port, i, attempts, delay))
        sleep(delay)
    logger.error('Could not open {} after {} attempts (port busy? held by another program?)'
                 .format(port, attempts))
    return False


###########################################
# Purpose:
# synchronous connect and write through modbus rtu, allow changing protocol 1 from Modbus to Bacnet; NO NEED TO REBOOT
def syncConnectWrite(old_baudrate, Port, Address, Value, promptEnable: bool = False):
    client = make_serial_client(Port, old_baudrate)
    client.connect()
    sleep(1)
    if (promptEnable):
        logger.info('Sync Connection Status: {}'.format(client.connected))
    SyncModbusWriteRegisters(client, Address, Value)
    client.close()
    sleep(2)


############################################
# Purpose:
# synchronous write to target register
# inputs: ModbusSerialClient, destination address, and list of values
def SyncModbusWriteRegisters(client, Address, Value):
    # write_registers(address: int, values: List[int] | int, slave: int = 0, **kwargs: Any) ModbusResponse #0x10
    builder = BinaryPayloadBuilder(byteorder=Endian.Big)
    for value in Value:
        assert (value <= 65535 and value >= 0), "Input overflow~"
        builder.add_16bit_uint(value)
    client.write_registers(Address, builder.to_registers(), slave=1)


##########################################
async def asyncReadRegisters(client, Address: int, Size: int, Slave: int = 1):
    rr = await client.read_holding_registers(address=Address, count=Size, slave=Slave)
    await asyncio.sleep(2)
    return rr


# Check if the custom register has default value of 0
async def AsyncModbusCheckReadRegisters(acuClass, readAddress=reg.CUSTOM_REG_DEFAULT):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    await client.connect()
    await asyncio.sleep(1)
    RR = await asyncReadRegisters(client, readAddress, 1)

    try:
        assert len(RR.registers) == 1

    except AssertionError as e:
        logger.warning(e)
        acuClass.failCount += 1
        acuClass.failTest.append(e)
    client.close()

    if (readAddress != reg.CUSTOM_REG_DEFAULT):
        return RR.registers[-1]


##########################################
# Purpose: Async Connect and write to MULTIPLE registers
# recommended asynchronous connect and write through modbus rtu
async def asyncConnectWriteMultipleRegisters(acuClass, Address: list, \
                                             Value: list, prompt: str = None):
    if (prompt):
        logger.info(prompt)

    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    try:
        if not await connect_with_retry(client, acuClass.COM):
            raise ConnectionError('could not open {}'.format(acuClass.COM))
        logger.info('RTU Connection Status: {}'.format(client.connected))
        for index, address in enumerate(Address):
            builder = BinaryPayloadBuilder(byteorder=Endian.Big)
            localAddress = address
            for node in Value[index]:
                logger.info('writing {} to {}'.format(node, localAddress))
                localAddress += 1
                builder.add_16bit_uint(node)
            await client.write_registers(address, builder.to_registers(), slave=1)
            await asyncio.sleep(1)

    except Exception as e:
        logger.warning('Unable to write {}'.format(e))
        acuClass.fail('asyncConnectWrite function failed address: {}'.format(Address))
    client.close()
    await asyncio.sleep(2)
    return


##########################################
# Purpose: Async Connect and write to register
# recommended asynchronous connect and write through modbus rtu
async def asyncConnectWrite(acuClass, Address: int, \
                            Value: list, prompt: str = None):
    if (prompt):
        logger.info(prompt)
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    try:
        if not await connect_with_retry(client, acuClass.COM):
            raise ConnectionError('could not open {}'.format(acuClass.COM))
        logger.debug('RTU Connection Status: {}'.format(client.connected))
        address = Address
        builder = BinaryPayloadBuilder(byteorder=Endian.Big)
        for value in Value:
            value = int(value)
            logger.debug('writing {} to {}'.format(value, address))
            address += 1
            builder.add_16bit_uint(value)
        await client.write_registers(Address, builder.to_registers(), slave=1)
        await asyncio.sleep(1)

    except Exception as e:
        logger.warning('Unable to write {}'.format(e))
        acuClass.fail('asyncConnectWrite function failed address: {}'.format(Address))
    client.close()
    await asyncio.sleep(2)


async def _write_one(client, address, values):
    """Write one contiguous block on an already-connected client."""
    builder = BinaryPayloadBuilder(byteorder=Endian.Big)
    addr = address
    for v in values:
        v = int(v)
        logger.debug('writing {} to {}'.format(v, addr))
        addr += 1
        builder.add_16bit_uint(v)
    await client.write_registers(address, builder.to_registers(), slave=1)


async def write_blocks(acuClass, blocks, reset=False):
    """Open ONE connection, optionally clear energy, write each (address, values)
    block, then close. Holding a single connection for the whole write avoids the
    per-write reconnect contention ('Access is denied' when reopening the same COM
    port back-to-back) and is much faster than connect-per-register-block.
    """
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    try:
        if not await connect_with_retry(client, acuClass.COM):
            raise ConnectionError('could not open {}'.format(acuClass.COM))
        logger.debug('RTU Connection Status: {} (single-connection write)'.format(client.connected))
        if reset:
            await _write_one(client, reg.ENERGY_RESET, [1])
            await asyncio.sleep(3)
        for address, values in blocks:
            await _write_one(client, address, values)
            await asyncio.sleep(1)
    except Exception as e:
        logger.warning('Unable to write energy blocks: {}'.format(e))
        acuClass.fail('energy write failed: {}'.format(e))
    finally:
        client.close()
    await asyncio.sleep(2)


# return the serial number string
async def AsyncReadSerialId(acuClass, slaveId):
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    await client.connect()
    await asyncio.sleep(1)
    try:
        SR = await client.read_holding_registers(reg.SERIAL_NUMBER, 6, slaveId)
        SerialNumber = ''
        for reading in SR.registers:
            ascii_hex = format(int(reading), '02X')

            hex_bytes = bytes.fromhex(ascii_hex)
            SerialNumber += hex_bytes.decode('ascii')
            client.close()
        return SerialNumber[:-1]
    except Exception:
        acuClass.fail(acuClass.serialNum)
        client.close()
        return ''
