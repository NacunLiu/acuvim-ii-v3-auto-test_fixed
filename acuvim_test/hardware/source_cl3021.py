"""CL3021 programmable AC source control over RS232/RS485.

Protocol recovered from the bench's "CL3021 控制面板" tool
(Downloads/CL3021_Control_V1 3/CL3021_Control_V1.exe, a PyInstaller-packed
Python script) so the test suite can drive the source directly instead of
automating that GUI.

Frame layout (all little-endian):
    [FRAME_HEAD][DEVICE_ID][PC_ID][LEN][...payload...][CHECKSUM]
    LEN      = len(payload) + 5   (head+ids+len+checksum)
    CHECKSUM = XOR of every byte from DEVICE_ID through the last payload byte

Value encoding:
    voltage : 40-bit little-endian int, value * 10000,  high byte forced 0xFC
    current : 40-bit little-endian int, value * 1000000, high byte forced 0xFA
    angle   : 32-bit little-endian int, degrees * 10000
    freq    : 32-bit little-endian int, Hz * 10000

Typical use (verify a meter reading against a known source output):
    src = CL3021Source('COM4')
    src.open()
    src.init_ac()                       # AC panel + 3P4W + auto range
    src.set_ac_output(57.7, 57.7, 57.7, 5, 5, 5)   # balanced, default angles
    ...read the meter / BACnet and compare...
    src.stop_output()
    src.close()
"""
from struct import pack
from time import sleep

import serial

from acuvim_test.log import logger

FRAME_HEAD = 0x81
DEVICE_ID = 0x01
PC_ID = 0x25

# Fixed command prefixes (payload headers) taken from the vendor tool.
_CMD_SHOW_AC_PANEL = (163, 0, 16, 128, 1)
_CMD_SHOW_HOME = (163, 0, 16, 128, 0)
_CMD_WIRING_3P4W = (163, 0, 1, 32, 8)
_CMD_COMM_CHANNEL = (163, 0, 2, 128, 0)
_CMD_AUTO_RANGE = (163, 5, 64, 4, 0)
_CMD_AMPLITUDE = (163, 5, 68, 63)
_CMD_ANGLE = (163, 5, 70, 63)
# Fixed tail the angle frame carries after the six encoded angles (verbatim from
# the vendor tool: per-channel range/marker bytes plus frame terminators).
_ANGLE_TAIL = (255, 0, 0, 0, 0, 252, 0, 0, 0, 0, 252, 0, 0, 0, 0, 252,
               0, 0, 0, 0, 250, 0, 0, 0, 0, 250, 0, 0, 0, 0, 250,
               32, 161, 7, 0, 7, 3, 63, 63)
_CMD_FREQ = (163, 5, 4, 192)
_LINK_PROBE = (129, 37, 1)

# Amplitude/angle scale factors and the "high byte" marker each field needs.
_V_SCALE, _V_HIGH = 10000, 0xFC
_I_SCALE, _I_HIGH = 1000000, 0xFA
_ANGLE_SCALE = 10000
_FREQ_SCALE = 10000

# Default balanced 3-phase angle set (Ua 0, Ub 240, Uc 120; currents in phase).
DEFAULT_ANGLES = (0, 240, 120, 0, 240, 120)
DEFAULT_FREQ = 50.0


class CL3021Source:
    def __init__(self, port='COM1', baudrate=9600, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None

    # ---- transport ----------------------------------------------------
    def open(self):
        self.ser = serial.Serial(port=self.port, baudrate=self.baudrate, bytesize=8,
                                 stopbits=1, parity=serial.PARITY_NONE, timeout=self.timeout)
        logger.info('CL3021 source opened on {} @ {}'.format(self.port, self.baudrate))
        return self.ser.is_open

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            logger.info('CL3021 source port closed')

    @staticmethod
    def _checksum(data):
        cs = 0
        for b in data:
            cs ^= b
        return cs & 0xFF

    @classmethod
    def build_frame(cls, payload):
        """Wrap a payload in the CL3021 frame (exposed for offline testing)."""
        body = [DEVICE_ID, PC_ID, len(payload) + 5] + list(payload)
        return bytes([FRAME_HEAD] + body + [cls._checksum(bytes(body))])

    def send_command(self, payload):
        if not (self.ser and self.ser.is_open):
            raise RuntimeError('CL3021 source port is not open')
        frame = self.build_frame(payload)
        self.ser.write(frame)
        try:
            return self.ser.read(1024)
        except serial.SerialTimeoutException:
            return b''

    def link(self):
        """Probe the source; True if it answers the link frame."""
        try:
            resp = self.send_command(_LINK_PROBE)
        except Exception as e:
            logger.warning('CL3021 link probe failed: {}'.format(e))
            return False
        ok = bool(resp)
        logger.info('CL3021 link probe: {}'.format('OK' if ok else 'no response'))
        return ok

    # ---- value encoders ------------------------------------------------
    @staticmethod
    def encode_40bit(val, scale, high_byte):
        raw = int(val * scale) & 0xFFFFFFFFFF
        b = list(raw.to_bytes(5, 'little'))
        b[4] = high_byte
        return b

    @staticmethod
    def encode_angle(degree):
        return list((int(degree * _ANGLE_SCALE) & 0xFFFFFFFF).to_bytes(4, 'little'))

    # ---- setup ---------------------------------------------------------
    def show_ac_panel(self):
        self.send_command(_CMD_SHOW_AC_PANEL)

    def show_home(self):
        self.send_command(_CMD_SHOW_HOME)

    def set_wiring_3p4w(self):
        self.send_command(_CMD_WIRING_3P4W)

    def set_comm_channel(self):
        self.send_command(_CMD_COMM_CHANNEL)

    def set_auto_range(self):
        self.send_command(_CMD_AUTO_RANGE)

    def init_ac(self):
        """Vendor init order: AC panel -> 3P4W wiring -> comm channel -> auto range."""
        self.show_ac_panel()
        sleep(1)
        self.set_wiring_3p4w()
        sleep(1)
        self.set_comm_channel()
        sleep(1)
        self.set_auto_range()
        sleep(1)
        logger.info('CL3021 initialised (AC panel, 3P4W, auto range)')

    # ---- output --------------------------------------------------------
    def set_ac_amplitude(self, Ua, Ub, Uc, Ia, Ib, Ic):
        # NOTE: the source expects phases in C, B, A order for both groups.
        data = list(_CMD_AMPLITUDE)
        for v in (Uc, Ub, Ua):
            data += self.encode_40bit(v, _V_SCALE, _V_HIGH)
        for i in (Ic, Ib, Ia):
            data += self.encode_40bit(i, _I_SCALE, _I_HIGH)
        data += [2, 63]
        self.send_command(data)

    def set_ac_angle(self, QUa, QUb, QUc, QIa, QIb, QIc):
        # Six 4-byte angles back to back (C, B, A for volts then amps), followed
        # by the vendor tool's fixed 39-byte tail. No per-angle marker byte.
        data = list(_CMD_ANGLE)
        for a in (QUc, QUb, QUa, QIc, QIb, QIa):
            data += self.encode_angle(a)
        data += list(_ANGLE_TAIL)
        self.send_command(data)

    def set_ac_freq(self, F):
        data = list(_CMD_FREQ) + list(pack('<I', int(F * _FREQ_SCALE))) + [7]
        self.send_command(data)

    def set_ac_output(self, Ua, Ub, Uc, Ia, Ib, Ic,
                      angles=DEFAULT_ANGLES, F=DEFAULT_FREQ):
        """Apply a full AC output point: angles, then amplitudes, then frequency
        (the vendor tool's order, with its settling delays)."""
        self.set_ac_angle(*angles)
        sleep(1)
        self.set_ac_amplitude(Ua, Ub, Uc, Ia, Ib, Ic)
        sleep(1)
        self.set_ac_freq(F)
        sleep(3)
        logger.info('CL3021 output set: U={:.3f}/{:.3f}/{:.3f} V  I={:.3f}/{:.3f}/{:.3f} A  {} Hz'
                    .format(Ua, Ub, Uc, Ia, Ib, Ic, F))

    def stop_output(self):
        """Zero every channel (the GUI's Stop button)."""
        self.set_ac_output(0, 0, 0, 0, 0, 0)
        logger.info('CL3021 output stopped (all channels zero)')


def list_ports():
    """Available COM ports, for the interactive source-port prompt."""
    from serial.tools import list_ports as lp
    return [p.device for p in lp.comports()]


def detect_source(exclude=(), candidates=None, baudrate=9600):
    """Find a CL3021 by sending the link frame to each candidate COM port.

    Returns an OPEN CL3021Source, or None when no source answers -- the caller
    then runs connection-only tests instead of source-driven verification. This
    is the normal case whenever the meter isn't wired to the source, the source
    is powered off, or its vendor control panel is running (it holds the port
    exclusively, so the port raises 'Access is denied' here).
    """
    ports = list(candidates) if candidates else list_ports()
    for port in ports:
        if port in exclude:
            continue
        src = CL3021Source(port, baudrate=baudrate)
        try:
            if not src.open():
                continue
        except Exception as e:
            logger.info('source probe: {} unavailable ({})'.format(port, e))
            continue
        try:
            if src.link():
                logger.info('CL3021 source detected on {}'.format(port))
                return src
        except Exception as e:
            logger.info('source probe: {} did not answer ({})'.format(port, e))
        src.close()
    logger.info('No CL3021 source detected on {} -- source-driven verification '
                'will be skipped'.format(', '.join(p for p in ports if p not in exclude) or 'any port'))
    return None
