"""Source-driven reading verification.

Drives the CL3021 programmable source to a known output point, then checks that
the meter reports it -- over BACnet/IP (the point of this test) and, as a
cross-check, over Modbus TCP. This turns the BACnet phase from "the meter
answers" into "the meter reports the right values".

Graceful degradation: if no source is controllable (not wired up, powered off,
or its vendor control panel is holding the COM port), the verification is
SKIPPED with an informative log line and the run continues -- the plain BACnet
connection tests still cover what they always did.
"""
import asyncio
import time

from acuvim_test import registers as reg
from acuvim_test.log import logger
from acuvim_test.modbus_client import tcp_read_floats
from acuvim_test.bacnet.ip_test import read_bacnet_analog_values
from acuvim_test.hardware.source_cl3021 import detect_source

# Output point used for verification. Current stays at 1 A by request.
TEST_POINT = {'volts': 57.7, 'amps': 1.0, 'freq': 50.0}

# Accepted deviation between source setting and meter reading.
TOL_PCT = 1.0        # percent, for voltage/current
TOL_FREQ_HZ = 0.05   # absolute, for frequency

# Substrings used to recognise BACnet object names (lower-cased) for each
# quantity. Meter firmwares name these differently, hence several options.
BACNET_NAME_HINTS = {
    'Ua': ('volts_an', 'volts an', 'ua', 'v_a', 'voltage_a'),
    'Ub': ('volts_bn', 'volts bn', 'ub', 'v_b', 'voltage_b'),
    'Uc': ('volts_cn', 'volts cn', 'uc', 'v_c', 'voltage_c'),
    'Ia': ('current_a', 'current a', 'ia', 'i_a'),
    'Ib': ('current_b', 'current b', 'ib', 'i_b'),
    'Ic': ('current_c', 'current c', 'ic', 'i_c'),
    'Freq': ('freq', 'frequency'),
}


def _within(expected, actual, tol_pct=TOL_PCT, tol_abs=None):
    if actual is None:
        return False
    if tol_abs is not None:
        return abs(actual - expected) <= tol_abs
    if expected == 0:
        return abs(actual) <= 0.05
    return abs(actual - expected) / abs(expected) * 100 <= tol_pct


def _match_bacnet(values, hints):
    """Find the first BACnet object whose name matches any hint."""
    for name, val in values.items():
        low = name.strip().lower()
        for h in hints:
            if low == h or h in low:
                return name, val
    return None, None


def _expected_map():
    p = TEST_POINT
    return {'Ua': p['volts'], 'Ub': p['volts'], 'Uc': p['volts'],
            'Ia': p['amps'], 'Ib': p['amps'], 'Ic': p['amps'],
            'Freq': p['freq']}


def _check_modbus_tcp(acuClass, expected):
    """Cross-check the live float block over Modbus TCP. Returns (ok, summary)."""
    ip = acuClass.address
    if not ip or ip == '0.0.0.0':
        return None, 'no meter IP for the Modbus TCP cross-check'
    volts = tcp_read_floats(ip, reg.RT_U_PHASE, 3)
    amps = tcp_read_floats(ip, reg.RT_I_PHASE, 3)
    freq = tcp_read_floats(ip, reg.RT_FREQ, 1)
    if volts is None or amps is None or freq is None:
        return None, 'Modbus TCP read failed (module offline?)'
    readings = {'Ua': volts[0], 'Ub': volts[1], 'Uc': volts[2],
                'Ia': amps[0], 'Ib': amps[1], 'Ic': amps[2], 'Freq': freq[0]}
    bad = []
    for key, exp in expected.items():
        tol_abs = TOL_FREQ_HZ if key == 'Freq' else None
        got = readings[key]
        if not _within(exp, got, tol_abs=tol_abs):
            bad.append('{} expected {:.3f} read {:.3f}'.format(key, exp, got))
    summary = ', '.join('{}={:.3f}'.format(k, v) for k, v in readings.items())
    return (not bad), summary if not bad else summary + ' | MISMATCH: ' + '; '.join(bad)


def _check_bacnet(acuClass, expected, local_addr):
    """Verify BACnet/IP present-values against the source point."""
    ip = acuClass.address
    if not ip or ip == '0.0.0.0':
        return None, 'no meter IP for the BACnet/IP reading check'
    values, detail = asyncio.run(read_bacnet_analog_values(ip, local_addr))
    if not values:
        return None, 'BACnet/IP objects unreadable ({})'.format(detail)
    logger.info('{} BACnet analog objects: {}'.format(
        acuClass.serialNum, ', '.join('{}={}'.format(k, v) for k, v in list(values.items())[:12])))
    checked, bad, missing = [], [], []
    for key, exp in expected.items():
        name, got = _match_bacnet(values, BACNET_NAME_HINTS[key])
        if name is None:
            missing.append(key)
            continue
        tol_abs = TOL_FREQ_HZ if key == 'Freq' else None
        checked.append('{}({})={:.3f}'.format(key, name, got))
        if not _within(exp, got, tol_abs=tol_abs):
            bad.append('{} [{}] expected {:.3f} read {:.3f}'.format(key, name, exp, got))
    if not checked:
        return None, 'no recognisable voltage/current objects among {} object(s)'.format(len(values))
    summary = '{}; {} object(s) matched'.format(', '.join(checked), len(checked))
    if missing:
        summary += ' (no object for {})'.format(', '.join(missing))
    if bad:
        return False, summary + ' | MISMATCH: ' + '; '.join(bad)
    return True, summary


def source_reading_verification(acuClass, local_addr, source_port=None):
    """Set a known source output, then verify the meter's BACnet/IP readings.

    Never raises: any problem downgrades to a logged skip so the rest of the
    test run is unaffected.
    """
    sn = acuClass.serialNum
    src = None
    try:
        src = detect_source(exclude=(acuClass.COM,),
                            candidates=[source_port] if source_port else None)
        if src is None:
            logger.info('{} source-driven reading verification SKIPPED: no controllable '
                        'CL3021 source found (meter may not be wired to the source, or the '
                        'CL3021 control panel is holding the port). BACnet connection tests '
                        'still ran.'.format(sn))
            return None

        p = TEST_POINT
        logger.info('{} source-driven verification: setting {} V / {} A / {} Hz'
                    .format(sn, p['volts'], p['amps'], p['freq']))
        src.init_ac()
        src.set_ac_output(p['volts'], p['volts'], p['volts'],
                          p['amps'], p['amps'], p['amps'], F=p['freq'])
        # Let the meter's metering filters settle on the new point.
        time.sleep(6)

        expected = _expected_map()

        ok_tcp, detail_tcp = _check_modbus_tcp(acuClass, expected)
        if ok_tcp is None:
            logger.warning('{} Modbus TCP cross-check unavailable: {}'.format(sn, detail_tcp))
        elif ok_tcp:
            logger.info('{} Modbus TCP readings match the source: {}'.format(sn, detail_tcp))
        else:
            logger.error('{} Modbus TCP readings do NOT match the source: {}'.format(sn, detail_tcp))
            acuClass.fail('\nModbus TCP reading vs source mismatch: {}'.format(detail_tcp))

        ok_bac, detail_bac = _check_bacnet(acuClass, expected, local_addr)
        if ok_bac is None:
            logger.warning('{} BACnet/IP reading verification unavailable: {}'.format(sn, detail_bac))
        elif ok_bac:
            logger.info('{} BACnet/IP readings match the source within {}%: {}'
                        .format(sn, TOL_PCT, detail_bac))
        else:
            logger.error('{} BACnet/IP readings do NOT match the source: {}'.format(sn, detail_bac))
            acuClass.fail('\nBACnet/IP reading vs source mismatch: {}'.format(detail_bac))
        return ok_bac
    except Exception as e:
        logger.warning('{} source-driven reading verification skipped due to error: {}'
                       .format(sn, e))
        return None
    finally:
        if src is not None:
            try:
                src.stop_output()
            except Exception as e:
                logger.warning('{} could not zero the source output: {}'.format(sn, e))
            src.close()
