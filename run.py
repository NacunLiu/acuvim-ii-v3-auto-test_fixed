# Entry point for the Acuvim II v3 communication test suite.
# Automates section 2 (Modbus communication) plus Web push & BACnet tests.
# Run: python run.py
import time
import multiprocessing

from acuvim_test.log import logger
from acuvim_test.ui import starter, allPassed, testFail, p1Fail
from acuvim_test.notify import run
from acuvim_test.runner import (
    ask_use_switch, collect_switch_configs, collect_manual_config, run_phase,
)


if __name__ == '__main__':
    print(starter)
    print('Total core number {}'.format(multiprocessing.cpu_count()))

    use_switch = ask_use_switch()
    if use_switch:
        configs = collect_switch_configs()
    else:
        print('Manual reboot mode: you will be prompted to power-cycle the meter by hand.')
        configs = collect_manual_config()

    openyabelock = multiprocessing.Lock()

    # ---- Phase 1: general / Modbus communication tests ----
    start_time = time.time()
    global_error = run_phase('run_tests', configs, use_switch)
    runtime = time.time() - start_time
    if global_error == 0:
        run('Connections test finished successfully, total runtime of {}m{}s'
            .format(int(runtime // 60), int(runtime % 60)))
    else:
        print(p1Fail)
        run('Some Meter fail to pass all tests, total runtime of {}m{}s'
            .format(int(runtime // 60), int(runtime % 60)))
    logger.info("Gen. tests finished, runtime: {} minutes {} seconds"
                .format(int(runtime // 60), int(runtime % 60)))

    # ---- Phase 2: Web push & BACnet tests ----
    input('Press Enter to continue WEB Push Test ')
    start_time2 = time.time()
    global_error += run_phase('run_webpush', configs, use_switch, extra_args=(openyabelock,))
    runtime2 = time.time() - start_time2

    if global_error == 0:
        print(allPassed)
    else:
        print(testFail)
        run('Test failed, total runtime of {}m{}s'
            .format(int(runtime2 // 60), int(runtime2 % 60)))

    logger.info("Test finished, total runtime: {} minutes {} seconds"
                .format(int(runtime2 // 60), int(runtime2 % 60)))
