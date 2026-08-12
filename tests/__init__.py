"""Test suite for fand.

Attaches a NullHandler to the root logger so warnings a test deliberately
provokes are not printed to stderr. `assertLogs`/`assertNoLogs` are unaffected:
they install their own handler on the logger under test.
"""

import logging

logging.getLogger().addHandler(logging.NullHandler())
