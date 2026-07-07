"""Data packet-loss stress test + final meter restore (runs last, segment S6).

Flow (per the agreed sequence, after the Web Push/BACnet segment):
  1. Set channel 1 protocol to Modbus.
  2. Reboot the meter so the protocol change takes effect.
  3. After boot, raise channel 1 to 115200 baud.
  4. Hammer one holding register (default 38144 / reboot counter) with a short
     50 ms timeout for N reads and report how many requests went unanswered.
  5. Restore the meter to shipping config: Modbus @ 19200, channel 2 Web2.

Register values are per the Acuvim IIX (ABB Class S) address table:
  4094 channel-1 protocol -> 0 = Modbus, 2 = BACnet MS/TP
  4152 channel-2 protocol -> 4 = Web2
  4098 channel-1 baud      -> 2400..115200 (default 19200)
  38144 = Normal reboot counter (R/W)
"""
import asyncio
from time import sleep

from acuvim_test import registers as reg
from acuvim_test.log import logger
from acuvim_test.modbus_client import make_serial_client, syncConnectWrite
from acuvim_test.reboot import reboot_meter

# Channel-1 protocol enum (register 4094): 0 = Modbus-RTU, 2 = BACnet MS/TP.
MODBUS_CH1 = 0
# Channel-2 protocol enum (register 4152): 0 Other, 1 BACnet, 2 MESH, 3 WIFI, 4 Web2, 5 Profibus.
WEB2_CH2 = 4

# Actual baud rate -> value written to BAUD_CH1 (mirrors meter_tests.syncChangeBaudRate).
BAUD_CODE = {2400: 2400, 4800: 4800, 9600: 9600, 19200: 19200,
             38400: 38400, 57600: 57600, 76800: 7680, 115200: 11520}

DEFAULT_BAUD = 19200    # shipping / restore baud
STRESS_BAUD = 115200    # baud used for the packet-loss test
PACKET_LOSS_READS = 5000
PACKET_LOSS_TIMEOUT = 0.05  # 50 ms per request
PACKET_LOSS_FAIL_PCT = 10   # fail only if loss rate exceeds this (%); <= 10% passes


def packet_loss_test(acuClass, address=None,
                     reads=PACKET_LOSS_READS, timeout=PACKET_LOSS_TIMEOUT):
    """Channel 1 -> Modbus, reboot, raise to 115200, then read `address` `reads`
    times with a short timeout and count unanswered requests. Returns loss % (or None).

    `address` defaults to the reboot-counter register, which is relocated on ABB
    families (54528) vs the others (38144)."""
    # 1) Channel 1 -> Modbus, then 2) reboot to apply. Do this FIRST so any read
    #    (incl. model/family below) happens on a Modbus line. In the normal flow
    #    S5 already restored channel 1 to Modbus over TCP, so this is a no-op
    #    safety net for resume-at-S6 / standalone runs. (If channel 1 is stuck in
    #    BACnet it can't be switched back over serial -- that recovery lives in S5.)
    syncConnectWrite(acuClass.BR, acuClass.COM, reg.PROTOCOL_CH1, [MODBUS_CH1])
    logger.info('{} channel 1 set to Modbus; rebooting to apply'.format(acuClass.serialNum))
    asyncio.run(reboot_meter(acuClass, boot_wait=90,
                             reason='apply Modbus on channel 1 for packet-loss test'))

    # Meter now speaks Modbus @ 19200 -> pick the reboot-counter register (relocated
    # on ABB). is_abb() reads the model; safe here and no longer crashes on failure.
    if address is None:
        address = reg.REBOOT_COUNTER_ABB if acuClass.is_abb() else reg.REBOOT_COUNTER
    logger.info('{} ===== Data packet loss test ===== Modbus 115200, {} reads @ {:.0f} ms timeout, address {}'
                .format(acuClass.serialNum, reads, timeout * 1000, address))

    # 3) Raise the line to 115200 (write at 19200, then talk at 115200).
    syncConnectWrite(acuClass.BR, acuClass.COM, reg.BAUD_CH1, [BAUD_CODE[STRESS_BAUD]])

    client = make_serial_client(acuClass.COM, STRESS_BAUD, timeout=timeout)
    if not client.connect():
        logger.error('{} packet loss test: could not open {} at {} baud'
                     .format(acuClass.serialNum, acuClass.COM, STRESS_BAUD))
        acuClass.fail('Packet loss test: no connection at 115200')
        return None
    sleep(1)

    # 4) Stress read loop.
    lost = 0
    for i in range(reads):
        try:
            rr = client.read_holding_registers(address=address, count=1, slave=1)
            if rr is None or rr.isError():
                lost += 1
        except Exception:
            lost += 1
        if (i + 1) % 1000 == 0:
            logger.info('{} packet loss progress: {}/{} (lost so far {})'
                        .format(acuClass.serialNum, i + 1, reads, lost))
    client.close()
    sleep(1)

    rate = lost / reads * 100 if reads else 0
    if rate > PACKET_LOSS_FAIL_PCT:
        logger.error('{} Data packet loss result: {}/{} unanswered, loss rate {:.2f}% (FAIL, > {}%)'
                     .format(acuClass.serialNum, lost, reads, rate, PACKET_LOSS_FAIL_PCT))
        acuClass.fail('Packet loss {:.2f}% (> {}%) at 115200'.format(rate, PACKET_LOSS_FAIL_PCT))
    else:
        logger.info('{} Data packet loss result: {}/{} unanswered, loss rate {:.2f}% (PASS, <= {}%)'
                    .format(acuClass.serialNum, lost, reads, rate, PACKET_LOSS_FAIL_PCT))
    return rate


def restore_meter_config(acuClass):
    """Final restore: channel 1 Modbus @ 19200, channel 2 Web2. Nothing else."""
    logger.info('{} Restoring meter config: Modbus @ 19200, channel 2 Web2'.format(acuClass.serialNum))
    # Line may be at 115200 from the packet-loss test; set baud back to 19200.
    # If the test never raised the speed, this write at 115200 is a no-op against a
    # 19200 line, so swallow a connection failure here.
    try:
        syncConnectWrite(STRESS_BAUD, acuClass.COM, reg.BAUD_CH1, [BAUD_CODE[DEFAULT_BAUD]])
    except Exception as e:
        logger.warning('{} baud reset from 115200 skipped ({})'.format(acuClass.serialNum, e))

    # Now communicate at 19200 for the remaining writes.
    syncConnectWrite(DEFAULT_BAUD, acuClass.COM, reg.PROTOCOL_CH1, [MODBUS_CH1])
    syncConnectWrite(DEFAULT_BAUD, acuClass.COM, reg.PROTOCOL_CH2, [WEB2_CH2])
    logger.info('{} Restore complete'.format(acuClass.serialNum))


def run_packet_loss_and_restore(acuClass):
    """Run the packet-loss test, then always restore the meter config."""
    try:
        packet_loss_test(acuClass)
    finally:
        restore_meter_config(acuClass)
