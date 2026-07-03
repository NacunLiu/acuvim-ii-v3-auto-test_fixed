"""ABB (CS0/CS2) energy edit/retention test.

ABB meters store editable energy in the Energy_1Cycle region (start 50412 /
0xC4EC), 96 values, R/W:
  - M4M40 (new):      float64 / 'double'  -> 4 registers per value (384 total)
  - Acuvim IIX (old): float32 / 'float'   -> 2 registers per value (192 total)

This differs from the Accuenergy/Eaton/DEIF meters (uint16 fixed-point at
16456/17952/...), so ABB gets its own test. Other families are unaffected.

Both sizes exceed the Modbus single-frame limit (123 regs write / 125 read), so
writes and reads are chunked.

HARDWARE NOTES (validate on a real meter, tweak the constants below if needed):
  - ABB_FLOAT_WORD_SWAP: 16-bit word order within a float. Default big-endian,
    high word first (ABCD). If read-back is garbage, flip this to True (CDAB).
  - ABB_TOLERANCE: read-back compare tolerance.
"""
import asyncio
import struct

from acuvim_test import registers as reg
from acuvim_test.log import logger
from acuvim_test.modbus_client import make_async_serial_client, asyncReadRegisters, write_blocks
from acuvim_test.reboot import reboot_meter

ABB_FLOAT_WORD_SWAP = False   # flip to True if read-back floats are garbage
ABB_TOLERANCE = 0.05          # allowed abs difference on read-back
_MAX_REGS_PER_FRAME = 120     # stay under the 123/125 Modbus limits

_PHASES = ('A', 'B', 'C', 'System')


def _value_names():
    """96 names in the exact register order (Active/Reactive/Apparent x
    Import/Export/Total/Net, then the four quadrants), for clear log messages."""
    names = []
    for grp in ('Active', 'Reactive', 'Apparent'):
        for sub in ('Import', 'Export', 'Total', 'Net'):
            for p in _PHASES:
                names.append('{} Energy {} -- {}'.format(grp, sub, p))
    for grp in ('Active', 'Reactive', 'Apparent'):
        for q in ('First', 'Second', 'Third', 'Forth'):
            for p in _PHASES:
                names.append('{} Energy {} -- {}'.format(grp, q, p))
    return names


_NAMES = _value_names()


def _float_to_regs(value, is_double, swap=ABB_FLOAT_WORD_SWAP):
    raw = struct.pack('>d' if is_double else '>f', value)
    regs = [int.from_bytes(raw[i:i + 2], 'big') for i in range(0, len(raw), 2)]
    return regs[::-1] if swap else regs


def _regs_to_float(regs, is_double, swap=ABB_FLOAT_WORD_SWAP):
    if swap:
        regs = list(regs)[::-1]
    raw = b''.join(int(r).to_bytes(2, 'big') for r in regs)
    return struct.unpack('>d' if is_double else '>f', raw)[0]


def _test_values():
    """One distinct, fractional value per energy item (so a swapped/misaligned
    read-back is obvious and mismatches are easy to pinpoint)."""
    return [round(12.34 + i, 2) for i in range(reg.ABB_ENERGY_COUNT)]


def _encode_blocks(values, is_double):
    """Encode values to (address, register-list) chunks under the frame limit."""
    regs_per_value = 4 if is_double else 2
    per_chunk = (_MAX_REGS_PER_FRAME // regs_per_value)  # whole values per frame
    blocks = []
    for i in range(0, len(values), per_chunk):
        chunk = values[i:i + per_chunk]
        regs = []
        for v in chunk:
            regs += _float_to_regs(v, is_double)
        address = reg.ABB_ENERGY_1CYCLE + i * regs_per_value
        blocks.append((address, regs))
    return blocks


async def _read_all(acuClass, total_regs):
    """Read the whole energy region in chunks; return the flat register list."""
    client = make_async_serial_client(acuClass.COM, acuClass.BR)
    await client.connect()
    await asyncio.sleep(1)
    out = []
    off = 0
    while off < total_regs:
        n = min(_MAX_REGS_PER_FRAME, total_regs - off)
        rr = await asyncReadRegisters(client, reg.ABB_ENERGY_1CYCLE + off, n)
        out += list(rr.registers)
        off += n
    client.close()
    await asyncio.sleep(1)
    return out


def _decode_all(regs, is_double):
    step = 4 if is_double else 2
    return [_regs_to_float(regs[i:i + step], is_double) for i in range(0, len(regs), step)]


async def _verify(acuClass, expected, is_double, phase):
    """Read the region back and compare to expected; log per-item mismatches."""
    total_regs = len(expected) * (4 if is_double else 2)
    regs = await _read_all(acuClass, total_regs)
    got = _decode_all(regs, is_double)
    mismatches = []
    for i, exp in enumerate(expected):
        if i >= len(got) or abs(got[i] - exp) > ABB_TOLERANCE:
            read = got[i] if i < len(got) else 'missing'
            mismatches.append('{} (addr {}): wrote {}, read {}'.format(
                _NAMES[i], reg.ABB_ENERGY_1CYCLE + i * (4 if is_double else 2), exp, read))
    if not mismatches:
        logger.info('{} ABB energy {} read-back MATCHES all {} values'
                    .format(acuClass.serialNum, phase, len(expected)))
        return True
    logger.error('{} ABB energy {} MISMATCH ({} of {} differ): {}'.format(
        acuClass.serialNum, phase, len(mismatches), len(expected), '; '.join(mismatches[:15])))
    acuClass.fail('ABB energy {} mismatch ({} items)'.format(phase, len(mismatches)))
    return False


async def abb_energy_check(acuClass, is_double):
    """Write float energy values to the ABB Energy_1Cycle region, verify the
    read-back, then verify they survive a power cycle (retention)."""
    fmt = 'float64' if is_double else 'float32'
    logger.info('{} ABB energy test ({}), {} values at {}'
                .format(acuClass.serialNum, fmt, reg.ABB_ENERGY_COUNT, reg.ABB_ENERGY_1CYCLE))
    values = _test_values()

    # Write (chunked, single connection) then verify.
    await write_blocks(acuClass, _encode_blocks(values, is_double))
    await asyncio.sleep(3)
    await _verify(acuClass, values, is_double, 'edit')

    # Retention: power cycle, then verify the values persisted.
    await reboot_meter(acuClass, store_wait=130, reason='ABB energy retention')
    await asyncio.sleep(30)
    await _verify(acuClass, values, is_double, 'retention')
