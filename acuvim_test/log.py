"""Shared logger for the test suite.

All modules import `logger` from here so per-meter FileHandlers (added in
runner.TestRunner.wrapper) and the colored console handler attach to one place.
"""
import logging

import coloredlogs

logger = logging.getLogger('acuvim_test')
coloredlogs.install(level='INFO', logger=logger,
                    fmt='%(asctime)s %(hostname)s %(levelname)s %(message)s')
