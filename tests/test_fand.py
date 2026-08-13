"""Tests for fand.py, the entry point.

load_env_file writes into os.environ, so every test that calls it is wrapped in
patch.dict and only ever points at a temporary file. Never at the repository
root: a real, gitignored .env lives there, and setdefault would pull genuine
notification credentials into the test process.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fand


def _recording_daemon(built: list, run_result=0, notify_result=0):
    """A stand-in for Daemon that records how it was built and called."""

    class Recording:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls: list[str] = []
            built.append(self)

        def run(self) -> int:
            self.calls.append("run")
            return run_result

        def run_notify_test(self) -> int:
            self.calls.append("run_notify_test")
            return notify_result

    return Recording


class ArgumentTests(unittest.TestCase):
    def test_the_defaults_are_the_deployed_paths(self):
        args = fand.parse_args([])
        self.assertEqual(args.config, Path("/opt/fand/config"))
        self.assertEqual(args.env_file, Path("/opt/fand/.env"))

    def test_the_flags_default_to_off(self):
        args = fand.parse_args([])
        self.assertFalse(args.dry_run)
        self.assertFalse(args.notify_test)
        self.assertFalse(args.verbose)

    def test_the_poll_interval_defaults_to_unset(self):
        # None, not a number: it means "use config.toml", and a default here
        # would silently override the configured value.
        self.assertIsNone(fand.parse_args([]).poll_interval)

    def test_the_config_directory_can_be_set(self):
        for argv in (["--config", "/etc/fand"], ["-c", "/etc/fand"]):
            with self.subTest(argv=argv):
                self.assertEqual(fand.parse_args(argv).config, Path("/etc/fand"))

    def test_the_config_directory_is_a_path(self):
        self.assertIsInstance(fand.parse_args(["-c", "/etc/fand"]).config, Path)

    def test_the_env_file_can_be_set(self):
        args = fand.parse_args(["--env-file", "/etc/fand.env"])
        self.assertEqual(args.env_file, Path("/etc/fand.env"))

    def test_dry_run_can_be_set(self):
        self.assertTrue(fand.parse_args(["--dry-run"]).dry_run)

    def test_notify_test_can_be_set(self):
        self.assertTrue(fand.parse_args(["--notify-test"]).notify_test)

    def test_verbose_can_be_set(self):
        for argv in (["--verbose"], ["-v"]):
            with self.subTest(argv=argv):
                self.assertTrue(fand.parse_args(argv).verbose)

    def test_the_poll_interval_is_a_float(self):
        self.assertEqual(fand.parse_args(["--poll-interval", "2.5"]).poll_interval, 2.5)

    def test_a_non_numeric_poll_interval_is_rejected(self):
        # argparse prints usage to stderr on its way out; captured so a
        # deliberate failure does not look like a broken test run.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                fand.parse_args(["--poll-interval", "soon"])

    def test_an_unknown_flag_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                fand.parse_args(["--overclock"])

    def test_flags_combine(self):
        args = fand.parse_args(["-v", "--dry-run", "-c", "/etc/fand", "--poll-interval", "1"])
        self.assertTrue(args.verbose)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.config, Path("/etc/fand"))
        self.assertEqual(args.poll_interval, 1.0)


class EnvFileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        # Every mutation is undone when the patch stops.
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def write(self, text: str) -> Path:
        path = self.dir / "test.env"
        path.write_text(text, encoding="utf-8")
        return path


class EnvFileTests(EnvFileTestCase):
    def test_a_pair_is_loaded(self):
        fand.load_env_file(self.write("FAND_TEST_TOKEN=secret\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "secret")

    def test_several_pairs_are_loaded(self):
        fand.load_env_file(self.write("FAND_TEST_A=1\nFAND_TEST_B=2\n"))
        self.assertEqual(os.environ["FAND_TEST_A"], "1")
        self.assertEqual(os.environ["FAND_TEST_B"], "2")

    def test_surrounding_whitespace_is_stripped(self):
        fand.load_env_file(self.write("  FAND_TEST_TOKEN  =  secret  \n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "secret")

    def test_comments_are_ignored(self):
        fand.load_env_file(self.write("# FAND_TEST_TOKEN=nope\nFAND_TEST_TOKEN=yes\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "yes")

    def test_blank_lines_are_ignored(self):
        fand.load_env_file(self.write("\n\nFAND_TEST_TOKEN=secret\n\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "secret")

    def test_lines_without_an_equals_sign_are_ignored(self):
        fand.load_env_file(self.write("this is not a pair\nFAND_TEST_TOKEN=secret\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "secret")
        self.assertNotIn("this is not a pair", os.environ)

    def test_a_value_containing_an_equals_sign_is_kept_whole(self):
        # Base64 tokens end in '='; splitting on every one would corrupt them.
        fand.load_env_file(self.write("FAND_TEST_TOKEN=abc=def==\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "abc=def==")

    def test_an_empty_value_is_allowed(self):
        fand.load_env_file(self.write("FAND_TEST_TOKEN=\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "")

    def test_the_real_environment_wins(self):
        # systemd's EnvironmentFile and a real export must both be able to
        # override the file, so the file is a fallback and never a mandate.
        os.environ["FAND_TEST_TOKEN"] = "from-the-environment"
        fand.load_env_file(self.write("FAND_TEST_TOKEN=from-the-file\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "from-the-environment")

    def test_an_empty_environment_value_still_wins(self):
        os.environ["FAND_TEST_TOKEN"] = ""
        fand.load_env_file(self.write("FAND_TEST_TOKEN=from-the-file\n"))
        self.assertEqual(os.environ["FAND_TEST_TOKEN"], "")

    def test_a_missing_file_is_a_silent_no_op(self):
        # fand.service marks the EnvironmentFile optional with a leading '-':
        # no credentials means no notifications, never no fan control.
        fand.load_env_file(self.dir / "absent.env")

    def test_a_directory_is_a_silent_no_op(self):
        fand.load_env_file(self.dir)

    def test_an_empty_file_is_a_silent_no_op(self):
        fand.load_env_file(self.write(""))


class MainTests(unittest.TestCase):
    """Daemon is replaced, so nothing here touches IPMI, systemd, or a socket."""

    def setUp(self):
        self.built: list = []
        # configure_logging would reconfigure the root logger for the rest of
        # the run; the entry point's job is to call it, not what it does.
        logging_patch = patch("fand.configure_logging")
        self.configure_logging = logging_patch.start()
        self.addCleanup(logging_patch.stop)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = str(Path(self.tmp.name) / "absent.env")

    def main(self, *argv, run_result=0, notify_result=0) -> int:
        args = ["fand.py", "--env-file", self.env_file, *argv]
        with patch("fand.Daemon", _recording_daemon(self.built, run_result, notify_result)):
            with patch.object(sys, "argv", args):
                return fand.main()

    def test_the_daemon_is_run(self):
        self.main()
        self.assertEqual(self.built[0].calls, ["run"])

    def test_the_run_result_becomes_the_exit_code(self):
        self.assertEqual(self.main(run_result=1), 1)

    def test_a_clean_run_exits_zero(self):
        self.assertEqual(self.main(), 0)

    def test_notify_test_runs_the_self_test_instead(self):
        self.main("--notify-test")
        self.assertEqual(self.built[0].calls, ["run_notify_test"])

    def test_notify_test_never_enters_the_control_loop(self):
        self.main("--notify-test")
        self.assertNotIn("run", self.built[0].calls)

    def test_the_notify_test_result_becomes_the_exit_code(self):
        self.assertEqual(self.main("--notify-test", notify_result=1), 1)

    def test_the_configured_directory_reaches_the_daemon(self):
        self.main("--config", "/etc/fand")
        self.assertEqual(self.built[0].kwargs["config_dir"], Path("/etc/fand"))

    def test_dry_run_reaches_the_daemon(self):
        self.main("--dry-run")
        self.assertTrue(self.built[0].kwargs["dry_run"])

    def test_verbose_reaches_the_daemon(self):
        self.main("-v")
        self.assertTrue(self.built[0].kwargs["verbose"])

    def test_the_poll_interval_override_reaches_the_daemon(self):
        self.main("--poll-interval", "2.5")
        self.assertEqual(self.built[0].kwargs["poll_interval"], 2.5)

    def test_an_absent_poll_interval_is_passed_as_none(self):
        # So the daemon knows to fall back to config.toml.
        self.main()
        self.assertIsNone(self.built[0].kwargs["poll_interval"])

    def test_logging_is_configured_before_the_daemon_is_built(self):
        self.main()
        self.assertEqual(self.configure_logging.call_count, 1)

    def test_verbose_is_passed_to_logging(self):
        self.main("-v")
        self.configure_logging.assert_called_once_with(True)

    def test_exactly_one_daemon_is_built(self):
        self.main()
        self.assertEqual(len(self.built), 1)


class EntryPointTests(unittest.TestCase):
    def test_importing_the_module_starts_nothing(self):
        # main() runs under an if __name__ guard, so importing fand for these
        # tests must not have taken over the machine's fans.
        self.assertTrue(callable(fand.main))

    def test_the_module_exposes_the_expected_entry_points(self):
        for name in ("main", "parse_args", "load_env_file"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(fand, name)))


if __name__ == "__main__":
    unittest.main()
