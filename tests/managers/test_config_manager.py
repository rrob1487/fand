"""Tests for lib/managers/config_manager.py.

Driven against a real temporary config directory, because what is under test
is directory discovery.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lib.managers.config_manager import ConfigError, ConfigManager

_CONFIG_TOML = """
[daemon]
poll_interval = 5
log_level = "INFO"

[fan_curve]
points = [[40, 20], [85, 100]]

[safety]
max_temperature = 90

[watchdog]
enabled = true
"""

_VM_TOML = """
[name]
vm = "n8n"

[qga]
socket = "/run/qemu/qemu-n8n-ga.sock"

[gpu]
type = "nvidia"

[limits]
max_temperature = 85
"""


def _notifier_toml(name="Test Notifier", interval=60, endpoint="discord") -> str:
    return f"""
Name = "{name}"
EndpointType = "{endpoint}"
Interval = {interval}
QueueSize = 10

[Trigger]
Type = "threshold"
Temperature = 80

[Credentials]
Token = "FAND_TOKEN"
Channel = "FAND_CHANNEL"
"""


class ConfigDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.write("config.toml", _CONFIG_TOML)

    def write(self, relative: str, text: str) -> Path:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def manager(self) -> ConfigManager:
        return ConfigManager(self.dir)

    def loaded(self) -> ConfigManager:
        manager = self.manager()
        manager.load()
        return manager


class DiscoveryTests(ConfigDirTestCase):
    def test_no_notification_directory_is_not_an_error(self):
        self.assertEqual(self.loaded().notifiers, {})

    def test_empty_notification_directory(self):
        (self.dir / "notification").mkdir()
        self.assertEqual(self.loaded().notifiers, {})

    def test_loads_every_notifier(self):
        self.write("notification/discord.toml", _notifier_toml("Alerts"))
        self.write("notification/backup.toml", _notifier_toml("Backup"))
        self.assertEqual(sorted(self.loaded().notifiers), ["backup", "discord"])

    def test_keyed_by_file_stem_not_by_name(self):
        self.write("notification/rack1.toml", _notifier_toml("Discord Alerts"))
        notifiers = self.loaded().notifiers
        self.assertIn("rack1", notifiers)
        self.assertEqual(notifiers["rack1"].name, "Discord Alerts")

    def test_two_files_may_share_a_name(self):
        # notification.md: Name need not uniquely identify a notifier.
        self.write("notification/a.toml", _notifier_toml("Same Name"))
        self.write("notification/b.toml", _notifier_toml("Same Name"))
        notifiers = self.loaded().notifiers
        self.assertEqual(sorted(notifiers), ["a", "b"])
        self.assertEqual({n.name for n in notifiers.values()}, {"Same Name"})

    def test_non_toml_files_are_ignored(self):
        self.write("notification/discord.toml", _notifier_toml())
        self.write("notification/discord.toml.example", _notifier_toml())
        self.write("notification/README.md", "not a config")
        self.assertEqual(sorted(self.loaded().notifiers), ["discord"])


class LenientLoadingTests(ConfigDirTestCase):
    """The defining asymmetry: a bad notifier is skipped, a bad core config
    file is fatal."""

    def test_corrupt_toml_is_skipped_with_one_warning(self):
        self.write("notification/good.toml", _notifier_toml("Good"))
        self.write("notification/broken.toml", "this is not [ valid toml")
        with self.assertLogs("lib.managers.config_manager", level="WARNING") as logs:
            notifiers = self.loaded().notifiers
        self.assertEqual(sorted(notifiers), ["good"])
        warnings = [x for x in logs.output if "broken.toml" in x]
        self.assertEqual(len(warnings), 1)

    def test_invalid_configuration_is_skipped(self):
        self.write("notification/good.toml", _notifier_toml("Good"))
        self.write("notification/bad.toml", 'Name = "No other keys at all"')
        with self.assertLogs("lib.managers.config_manager", level="WARNING") as logs:
            notifiers = self.loaded().notifiers
        self.assertEqual(sorted(notifiers), ["good"])
        self.assertTrue([x for x in logs.output if "bad.toml" in x])

    def test_unknown_key_is_skipped(self):
        self.write("notification/typo.toml", _notifier_toml() + "\nMaxAttempt = 5\n")
        with self.assertLogs("lib.managers.config_manager", level="WARNING"):
            self.assertEqual(self.loaded().notifiers, {})

    def test_a_bad_notifier_does_not_affect_core_config(self):
        self.write("notification/broken.toml", "not [ valid")
        with self.assertLogs("lib.managers.config_manager", level="WARNING"):
            manager = self.loaded()
        self.assertIsNotNone(manager.config)
        self.assertEqual(manager.config.daemon.poll_interval, 5)

    def test_bad_config_toml_is_still_fatal(self):
        self.write("config.toml", "this is not [ valid toml")
        with self.assertRaises(ConfigError):
            self.loaded()

    def test_missing_config_toml_is_still_fatal(self):
        (self.dir / "config.toml").unlink()
        with self.assertRaises(ConfigError):
            self.loaded()

    def test_bad_vm_toml_is_still_fatal(self):
        self.write("vms/broken.toml", "not [ valid toml")
        with self.assertRaises(ConfigError):
            self.loaded()


class IntervalWarningTests(ConfigDirTestCase):
    def test_warns_when_interval_is_shorter_than_the_poll_interval(self):
        # poll_interval is 5; dispatch happens on the control loop, so a
        # 1-second interval cannot be honoured.
        self.write("notification/fast.toml", _notifier_toml(interval=1))
        with self.assertLogs("lib.managers.config_manager", level="WARNING") as logs:
            notifiers = self.loaded().notifiers
        self.assertTrue([x for x in logs.output if "polls every" in x])
        # It still loads: this is a warning, not a rejection.
        self.assertIn("fast", notifiers)

    def test_silent_when_interval_is_reachable(self):
        self.write("notification/slow.toml", _notifier_toml(interval=60))
        with self.assertNoLogs("lib.managers.config_manager", level="WARNING"):
            self.loaded()

    def test_silent_when_interval_equals_the_poll_interval(self):
        self.write("notification/exact.toml", _notifier_toml(interval=5))
        with self.assertNoLogs("lib.managers.config_manager", level="WARNING"):
            self.loaded()


class ReloadTests(ConfigDirTestCase):
    def test_reload_sees_an_added_file(self):
        manager = self.loaded()
        self.assertEqual(manager.notifiers, {})
        self.write("notification/new.toml", _notifier_toml("New"))
        manager.reload()
        self.assertIn("new", manager.notifiers)

    def test_reload_sees_an_edited_file(self):
        self.write("notification/a.toml", _notifier_toml("Before"))
        manager = self.loaded()
        self.assertEqual(manager.notifiers["a"].name, "Before")
        self.write("notification/a.toml", _notifier_toml("After"))
        manager.reload()
        self.assertEqual(manager.notifiers["a"].name, "After")

    def test_reload_sees_a_removed_file(self):
        self.write("notification/gone.toml", _notifier_toml())
        manager = self.loaded()
        self.assertIn("gone", manager.notifiers)
        (self.dir / "notification" / "gone.toml").unlink()
        manager.reload()
        self.assertEqual(manager.notifiers, {})


class AtomicityTests(ConfigDirTestCase):
    """A failed load must change nothing, so Daemon.reload_config's
    "keeping previous configuration" is actually true."""

    def test_failed_reload_leaves_everything_at_its_previous_value(self):
        self.write("vms/n8n.toml", _VM_TOML)
        self.write("notification/a.toml", _notifier_toml("Original"))
        manager = self.loaded()
        config, vms, notifiers = manager.config, manager.vms, manager.notifiers

        # A later stage now fails: vms/ is discovered after config.toml.
        self.write("config.toml", _CONFIG_TOML.replace("poll_interval = 5", "poll_interval = 9"))
        self.write("vms/broken.toml", "not [ valid toml")
        with self.assertRaises(ConfigError):
            manager.reload()

        self.assertIs(manager.config, config)
        self.assertIs(manager.vms, vms)
        self.assertIs(manager.notifiers, notifiers)
        self.assertEqual(manager.config.daemon.poll_interval, 5)


if __name__ == "__main__":
    unittest.main()
