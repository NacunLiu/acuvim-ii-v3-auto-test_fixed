"""Meter reboot control: Kasa switch power-cycle or manual-reboot prompt."""
import asyncio
from time import sleep

from acuvim_test.log import logger


# Seconds to wait after a *manual* reboot is confirmed, before resuming tests.
MANUAL_SETTLE = 30


def prompt_manual_reboot(acuClass, reason=''):
    """Block until the operator confirms they have physically rebooted the meter.

    Used both in manual mode (no WiFi switch) and as the fallback when a Kasa
    plug becomes unreachable mid-test. Requires two Enter presses so an
    accidental keystroke can't skip the reboot.
    """
    label = acuClass.serialNum or 'meter'
    print('\n' + '=' * 60)
    print(f'[MANUAL REBOOT] Please power-cycle {label} now.')
    if reason:
        print(f'  Reason: {reason}')
    print('=' * 60)
    try:
        input('  Done? Press Enter to confirm (1/2)... ')
        input('  Press Enter again to confirm reboot is complete (2/2)... ')
        logger.info(f'{label} manual reboot confirmed')
    except (EOFError, RuntimeError):
        # No console attached (e.g. a multi-meter switch-mode child process on
        # Windows). We can't prompt, so pause long enough for a manual reboot.
        logger.error(f'{label} cannot prompt for manual reboot here; '
                     f'pausing 120s, please reboot the meter now')
        sleep(120)


async def reboot_meter(acuClass, store_wait=0, boot_wait=50, reason=''):
    """Single entry point for rebooting a meter, in either switch or manual mode.

    Args:
        store_wait: seconds to wait *before* cutting power, so the meter can
            flush readings into FeRAM (only needed for energy-retention tests).
        boot_wait: seconds to wait after the switch restores power (switch mode
            only); manual mode always waits MANUAL_SETTLE instead.
        reason: short text shown to the operator on a manual reboot.
    """
    if store_wait:
        logger.info(f'{acuClass.serialNum} storing readings to FeRAM, wait {store_wait}s')
        await asyncio.sleep(store_wait)

    if acuClass.use_switch:
        if await _switch_cycle(acuClass):
            await asyncio.sleep(boot_wait)
            return
        logger.error('Kasa plug unreachable after retries, falling back to manual reboot')

    # Manual mode (chosen at startup) or switch fell through.
    prompt_manual_reboot(acuClass, reason)
    await asyncio.sleep(MANUAL_SETTLE)


async def _switch_cycle(acuClass, retries=5, delay=30):
    """Try to power-cycle the meter through the Kasa plug. Return True on success."""
    for attempt in range(1, retries + 1):
        try:
            await acuClass.plug.cycle()
            logger.info(f'Power cycle succeeded on attempt {attempt}')
            return True
        except Exception as e:
            logger.warning(f'Power cycle attempt {attempt} failed: {e}')
            if attempt < retries:
                logger.info(f'Retrying after {delay} seconds...')
                await asyncio.sleep(delay)
    return False
