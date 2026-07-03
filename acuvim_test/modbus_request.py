"""AccuenergyModbusRequest: customized 0x6A command (reboot counter / latency)."""
from time import sleep

import serial
from pymodbus.utilities import computeCRC

from acuvim_test import registers as reg
from acuvim_test.log import logger
from acuvim_test.modbus_client import make_serial_client


class AccuenergyModbusRequest():
    def __init__(self, Port, Baudrate, is_abb=False):
        # ABB families (old ABB Class S + M4M40) relocate the reboot-counter and
        # latency registers (54528/54530 vs 38144/38146). The custom 0x6A reset
        # command embeds the target address, so build both from the addresses.
        self.address = reg.REBOOT_COUNTER_ABB if is_abb else reg.REBOOT_COUNTER
        latency_addr = reg.LATENCY_REG_ABB if is_abb else reg.LATENCY_REG
        self.count = 16
        self.Port = Port
        self.BR = Baudrate
        self.reset_counter = self._reset_cmd(self.address)
        self.reset_latency = self._reset_cmd(latency_addr)

    @staticmethod
    def _reset_cmd(address):
        """Custom 0x6A 'write zero' command for `address`:
        01 6A <addr_hi> <addr_lo> 00 01 02 00 00 (CRC appended by the caller)."""
        return bytearray([0x01, 0x6A, (address >> 8) & 0xFF, address & 0xFF,
                          0x00, 0x01, 0x02, 0x00, 0x00])

    # readCounter function
    # Usage: this function will read reboot counter
    def readCounter(self):
        client = make_serial_client(self.Port, self.BR)
        client.connect()
        rr = client.read_holding_registers(self.address, 1, slave=1)
        logger.info('Meter has a normal reboot time of {}'.format(rr.registers))
        sleep(1)
        client.close()
        sleep(1)
        return rr.registers[0]

    # Reboot Latency Register function
    # This function will use 6A command to clear out latency register
    async def rebootLatency(self):
        crc = computeCRC(self.reset_latency)
        self.reset_latency += crc.to_bytes(2, byteorder='big')
        ser = serial.Serial(port=self.Port, baudrate=self.BR, timeout=1)
        ser.write(self.reset_latency)
        ser.close()
        logger.debug('Cleaning Latency Register')

    # rebootcounter function
    # This function will reset the reboot register
    def rebootCounter(self, option):
        start = self.readCounter()
        crc = computeCRC(self.reset_counter)
        self.reset_counter += crc.to_bytes(2, byteorder='big')
        ser = serial.Serial(port=self.Port, baudrate=self.BR, timeout=1)
        ser.write(self.reset_counter)

        logger.debug('Cleaning reboot counter')
        ser.close()
        sleep(2)
        end = self.readCounter()
        if (end == 0 and start != 0):
            logger.info("Reboot counter reset test passed")
            pass
        elif (end == 0 and start == 0):
            pass
        else:
            logger.error("FAIL TO RESET REBOOT COUNTER")
            option.failCount += 1
            option.failTest.append('\nCounter Fail to reset')
