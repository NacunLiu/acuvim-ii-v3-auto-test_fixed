# Entry point for the Acuvim II v3 communication test suite.
# Automates section 2 (Modbus communication) plus Web push & BACnet tests.
# Run: python run.py
import sys
import time
import multiprocessing

from acuvim_test.log import logger
from acuvim_test.ui import starter, allPassed, testFail
from acuvim_test.notify import run
from acuvim_test.modbus_client import close_all_serial_clients
from acuvim_test.runner import (
    ask_use_switch, ask_static_ip, ask_skip_energy, collect_switch_configs, collect_manual_config,
    run_single_meter, run_multi,
)


if __name__ == '__main__':
    print(starter)  # title banner
    print('Total core number {}'.format(multiprocessing.cpu_count()), flush=True)

    # Interactive setup: one question at a time (see runner._ask).
    use_switch = ask_use_switch()
    static_ip = ask_static_ip()  # None => reuse the meter's DHCP-assigned address
    skip_energy = ask_skip_energy()  # skip the S2 energy edit/retention test (all meter types)
    if use_switch:
        configs = collect_switch_configs()
    else:
        print('\nManual reboot mode: you will be prompted to power-cycle the meter by hand.', flush=True)
        configs = collect_manual_config()

    browser_lock = multiprocessing.Lock()
    yabe_lock = multiprocessing.Lock()

    start_time = time.time()
    try:
        if len(configs) == 1:
            # Single meter: main process -> interactive retry + resumable test.
            errors = run_single_meter(configs[0], use_switch, static_ip, skip_energy, browser_lock, yabe_lock)
        else:
            # Multiple meters: one process each, non-interactive, no resume.
            # (each meter reuses its own DHCP address as static; a single entered IP can't apply to all)
            if static_ip:
                print('Note: multiple meters -> ignoring the entered static IP; each reuses its own DHCP address.', flush=True)
            errors = run_multi(configs, use_switch, skip_energy, browser_lock, yabe_lock)
    except KeyboardInterrupt:
        # Force-release the COM port so the next run isn't locked out, and exit
        # cleanly instead of dumping a traceback.
        close_all_serial_clients()
        print('\nInterrupted (Ctrl+C). Serial port released; exiting.', flush=True)
        logger.warning('Run interrupted by Ctrl+C; serial port released')
        sys.exit(130)
    finally:
        close_all_serial_clients()  # belt-and-suspenders: never leave the port locked
    runtime = time.time() - start_time

    if errors == 0:
        print(allPassed)
        run('Test finished successfully, total runtime of {}m{}s'
            .format(int(runtime // 60), int(runtime % 60)))
    else:
        print(testFail)
        run('Test failed ({} meter(s) with errors), total runtime of {}m{}s'
            .format(errors, int(runtime // 60), int(runtime % 60)))

    logger.info("Test finished, total runtime: {} minutes {} seconds"
                .format(int(runtime // 60), int(runtime % 60)))
