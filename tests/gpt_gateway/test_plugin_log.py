"""Tests for gpt_gateway.plugin_log: directory resolution, the bounded row, rotation past the cap, and rows held across a failure."""
import io
import os
import unittest

from gpt_gateway import plugin_log
from tests import jail

NOW_MS = 1793509200000  # 2026-11-01T05:00:00.000Z


class PluginLogTest(unittest.TestCase):
    def test_log_dir_resolves_override_then_home_then_off(self):
        self.assertEqual(plugin_log.log_dir("/logs/", "/home/u", "p"), "/logs/p")
        self.assertEqual(plugin_log.log_dir("", "/home/u/", "p"), "/home/u/.local/state/p/logs")
        self.assertIsNone(plugin_log.log_dir("", "", "p"))

    def test_level_row_bounds_an_oversized_row_and_refuses_an_unknown_level(self):
        fields = {"plugin": "p", "level": "info", "event": "e", "msg": "m", "where": "w", "detail": "x" * 5000}
        self.assertEqual(
            plugin_log.level_row(fields, NOW_MS),
            b'{"ts":"2026-11-01T05:00:00.000Z","plugin":"p","level":"info","event":"e","msg":"m","where":"w",'
            b'"detail":{"truncated":true,"bytes":5108}}\n')
        with self.assertRaisesRegex(ValueError, "plugin log invalid level: 'loud'"):
            plugin_log.level_row({**fields, "level": "loud"}, NOW_MS)

    def test_record_appends_rotates_and_holds_rows_across_a_failure(self):
        root = jail.scratch(self)
        directory = os.path.join(root, "logs")
        screen = io.StringIO()
        log = plugin_log.PluginLog("p", directory, screen, lambda: NOW_MS)
        log.record("info", "e", "m", "w")
        info = os.path.join(directory, "info.jsonl")
        with open(info) as f:
            self.assertEqual(f.read(), '{"ts":"2026-11-01T05:00:00.000Z","plugin":"p","level":"info","event":"e","msg":"m","where":"w"}\n')
        # A full level file moves to .1 and a log-capped warning says so.
        os.truncate(info, plugin_log.CAP_BYTES + 1)
        log.record("info", "e2", "m", "w")
        self.assertEqual(os.stat(info + ".1").st_size, plugin_log.CAP_BYTES + 1)
        with open(info) as f:
            self.assertEqual(f.read(), '{"ts":"2026-11-01T05:00:00.000Z","plugin":"p","level":"info","event":"e2","msg":"m","where":"w"}\n')
        with open(os.path.join(directory, "warnings.jsonl")) as f:
            self.assertEqual(
                f.read(),
                '{"ts":"2026-11-01T05:00:00.000Z","plugin":"p","level":"warning","event":"log-capped",'
                '"msg":"level file exceeded 20971520 bytes; moved aside","where":"plugin_log.py#PluginLog._write",'
                '"detail":{"file":"info.jsonl"}}\n')
        # A directory that cannot be made holds both rows and says one screen line.
        blocker = os.path.join(root, "file")
        open(blocker, "w").close()
        screen = io.StringIO()
        failing = plugin_log.PluginLog("p", os.path.join(blocker, "logs"), screen, lambda: NOW_MS)
        failing.record("info", "a", "m", "w")
        failing.record("info", "b", "m", "w")
        self.assertEqual(failing.held, 2)
        self.assertEqual(screen.getvalue().count("\n"), 1)
        self.assertTrue(screen.getvalue().startswith("p: plugin log %s failed: " % os.path.join(blocker, "logs")))


if __name__ == "__main__":
    unittest.main()
