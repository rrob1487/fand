"""Tests for lib/utils/logging.py.

Everything here mutates the root logger, which is process-global and shared with
every other test in the run. The base class saves and restores it, and that is
load-bearing rather than tidiness: `logging.basicConfig` is a no-op once the
root logger has a handler, and tests/__init__.py installs one at import time, so
without clearing them first `configure()` would do nothing and these tests would
assert nothing.
"""

from __future__ import annotations

import io
import logging
import unittest

from lib.utils.logging import configure, get_logger, set_level


class RootLoggerTestCase(unittest.TestCase):
    """Runs each test against a pristine root logger, and puts back the one the
    rest of the suite is relying on."""

    def setUp(self):
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        self.addCleanup(setattr, root, "handlers", list(root.handlers))
        root.handlers = []
        self.root = root

    def emit(self, name="lib.example", level="info", message="hello") -> str:
        """Log one record through a captured stream and return what was written."""
        stream = io.StringIO()
        configure_kwargs = getattr(self, "configure_kwargs", {})
        configure(stream=stream, **configure_kwargs)
        getattr(logging.getLogger(name), level)(message)
        return stream.getvalue()


class ConfigureTests(RootLoggerTestCase):
    def test_the_default_level_is_info(self):
        configure()
        self.assertEqual(self.root.level, logging.INFO)

    def test_verbose_selects_debug(self):
        configure(verbose=True)
        self.assertEqual(self.root.level, logging.DEBUG)

    def test_a_handler_is_installed(self):
        configure()
        self.assertTrue(self.root.handlers)

    def test_records_reach_the_given_stream(self):
        self.assertEqual(self.emit(), "lib.example: INFO: hello\n")

    def test_the_format_carries_the_logger_name_and_level(self):
        output = self.emit(name="lib.controller", level="warning", message="fans")
        self.assertEqual(output, "lib.controller: WARNING: fans\n")

    def test_the_format_has_no_timestamp_or_pid(self):
        # journald stamps its own; duplicating it makes every line noisier.
        output = self.emit()
        self.assertNotIn("20", output.split(":")[0])
        self.assertEqual(output.count(":"), 2)

    def test_debug_records_are_dropped_by_default(self):
        self.assertEqual(self.emit(level="debug"), "")

    def test_debug_records_are_kept_when_verbose(self):
        self.configure_kwargs = {"verbose": True}
        self.assertEqual(self.emit(level="debug"), "lib.example: DEBUG: hello\n")

    def test_the_stream_is_keyword_only(self):
        # Guards the signature the daemon and the tests both depend on.
        with self.assertRaises(TypeError):
            configure(False, io.StringIO())


class GetLoggerTests(unittest.TestCase):
    def test_the_named_logger_is_returned(self):
        self.assertEqual(get_logger("lib.controller").name, "lib.controller")

    def test_the_same_name_gives_the_same_logger(self):
        self.assertIs(get_logger("lib.controller"), get_logger("lib.controller"))

    def test_it_matches_the_stdlib_logger(self):
        # Modules that use logging.getLogger directly and modules that use this
        # helper have to land on the same object, or assertLogs would miss one.
        self.assertIs(get_logger("lib.daemon"), logging.getLogger("lib.daemon"))


class SetLevelTests(RootLoggerTestCase):
    """Called from Daemon._apply_log_level on every setup() and every reload,
    so a bad value in config.toml reaches here on a live daemon."""

    def test_a_level_name_is_applied(self):
        set_level("WARNING")
        self.assertEqual(self.root.level, logging.WARNING)

    def test_level_names_are_case_insensitive(self):
        for name in ("debug", "Debug", "DEBUG"):
            with self.subTest(name=name):
                set_level(name)
                self.assertEqual(self.root.level, logging.DEBUG)

    def test_every_standard_level_is_accepted(self):
        for name, expected in (
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ):
            with self.subTest(name=name):
                set_level(name)
                self.assertEqual(self.root.level, expected)

    def test_an_unknown_level_raises_value_error(self):
        # ValueError specifically: that is what Daemon._apply_log_level catches
        # to keep the current level instead of taking the daemon down.
        with self.assertRaises(ValueError):
            set_level("VERBOSE")

    def test_a_numeric_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            set_level("20")

    def test_an_empty_level_raises_value_error(self):
        with self.assertRaises(ValueError):
            set_level("")

    def test_the_bad_name_is_reported(self):
        with self.assertRaises(ValueError) as caught:
            set_level("VERBOSE")
        self.assertIn("VERBOSE", str(caught.exception))

    def test_a_rejected_level_leaves_the_current_one_alone(self):
        set_level("WARNING")
        with self.assertRaises(ValueError):
            set_level("VERBOSE")
        self.assertEqual(self.root.level, logging.WARNING)

    def test_it_overrides_a_previous_configure(self):
        configure(verbose=False)
        set_level("DEBUG")
        self.assertEqual(self.root.level, logging.DEBUG)


if __name__ == "__main__":
    unittest.main()
