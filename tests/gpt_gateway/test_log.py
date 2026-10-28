"""Tests for gpt_gateway.log, the adapter over the vendored plugin-log: where the text and level logs go, stamped
lines, rotation, and what a failing log puts on the screen. The library itself is tested by its canonical source."""
import contextlib
import io
import os
import json
import unittest
from unittest.mock import patch

from gpt_gateway import env, log as logmod
from gpt_gateway.log import Log
from tests import jail


class LogTest(unittest.TestCase):
    def level_log(self, home, path=None, **values):
        with patch.object(env, "snapshot", return_value={"HOME": home, **values}):
            log = Log(path)
        return log

    def rows(self, directory, name):
        with open(os.path.join(directory, name), encoding="utf-8") as source:
            return [json.loads(line) for line in source]

    def test_record_rows_land_in_level_files(self):
        home = jail.scratch(self)
        root = os.path.join(home, "logs with ' quote")
        path = os.path.join(home, "gw.log")
        log = self.level_log(home, path, PLUGIN_LOG_DIR=root)
        directory = os.path.join(root, "gpt-gateway")
        with patch("gpt_gateway.clock.now", return_value=1791991231.123):
            for level, filename in (("error", "errors.jsonl"), ("warning", "warnings.jsonl"), ("info", "info.jsonl")):
                with self.subTest(level=level):
                    log.record(level, "forced-event", "one sentence", "gpt_gateway/log.py#record", {"count": 2})
                    self.assertEqual(self.rows(directory, filename), [{
                        "ts": "2026-10-14T15:20:31.123Z", "plugin": "gpt-gateway", "level": level,
                        "event": "forced-event", "msg": "one sentence", "where": "gpt_gateway/log.py#record",
                        "detail": {"count": 2}}])
                    self.assertEqual(os.stat(os.path.join(directory, filename)).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        with open(path) as source:
            self.assertEqual(len(source.read().splitlines()), 3)

    def test_record_falls_back_to_home_state(self):
        home = jail.scratch(self)
        log = self.level_log(home)
        log.record("warning", "fallback-test", "kept", "gpt_gateway/log.py#record")
        self.assertEqual(self.rows(os.path.join(home, ".local/state/gpt-gateway/logs"), "warnings.jsonl")[0]["event"],
                         "fallback-test")
        for mapping, expected in (({}, None), ({"HOME": home}, os.path.join(home, ".local/state/gpt-gateway/logs")),
                                  ({"PLUGIN_LOG_DIR": home}, os.path.join(home, "gpt-gateway"))):
            self.assertEqual(env.plugin_log_dir(mapping), expected)
        with self.assertRaisesRegex(env.EnvError, "PLUGIN_LOG_DIR"):
            env.plugin_log_dir({"PLUGIN_LOG_DIR": "relative"})
        with patch.object(env, "snapshot", return_value={}):
            absent = Log(None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            absent.record("info", "no-home", "nothing to resolve", "gpt_gateway/log.py#record")
            absent.record("info", "no-home", "said once", "gpt_gateway/log.py#record")
        self.assertEqual(err.getvalue(), "gpt-gateway: plugin log off: no PLUGIN_LOG_DIR and no HOME\n")

    def test_an_unusable_log_directory_is_one_line_and_the_text_still_writes(self):
        home = jail.scratch(self)
        text = os.path.join(home, "gw.log")
        log = self.level_log(home, text, PLUGIN_LOG_DIR="relative")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            log.record("error", "invalid-root", "legacy survives", "gpt_gateway/log.py#record")
            log.record("error", "invalid-root", "said once", "gpt_gateway/log.py#record")
        self.assertEqual(err.getvalue(), "gpt-gateway: plugin log off: env PLUGIN_LOG_DIR: must be an absolute path\n")
        with open(text) as source:
            self.assertIn("legacy survives", source.read())
        self.assertIsNone(logmod.text_path({"HOME": home, "PLUGIN_LOG_DIR": "relative"}))

    def test_writes_a_stamped_line(self):
        path = os.path.join(jail.scratch(self), "gw.log")
        Log(path).write("hello")
        with open(path) as f:
            self.assertRegex(f.read(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d hello\n$")

    def test_rotates_past_the_limit(self):
        path = os.path.join(jail.scratch(self), "gw.log")
        log = Log(path, max_bytes=10)
        log.write("first line, longer than ten bytes")
        log.write("second")
        with open(path + ".1") as f:
            self.assertIn("first line", f.read())
        with open(path) as f:
            self.assertIn("second", f.read())

    def test_the_text_log_makes_its_directory_and_is_owner_only(self):
        path = os.path.join(jail.scratch(self), "gpt-gateway", "gpt-gateway.log")
        Log(path).write("hello")
        self.assertEqual((os.stat(os.path.dirname(path)).st_mode & 0o777, os.stat(path).st_mode & 0o777), (0o700, 0o600))

    def test_a_failing_text_log_is_one_error_row_and_its_line(self):
        home = jail.scratch(self)
        blocked = os.path.join(home, "blocked")
        with open(blocked, "w") as source:
            source.write("not a directory")
        log = self.level_log(home, os.path.join(blocked, "gw.log"), PLUGIN_LOG_DIR=home)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            log.write("a")
            log.write("b")
        errors = os.path.join(home, "gpt-gateway", "errors.jsonl")
        self.assertTrue(os.path.isfile(errors), "a failing text log must leave an error row")
        row, = self.rows(os.path.join(home, "gpt-gateway"), "errors.jsonl")
        self.assertEqual((row["level"], row["event"]), ("error", "text-log-failed"))
        self.assertEqual(err.getvalue(), "gpt-gateway: %s\n" % row["msg"])

    def test_a_line_with_an_unencodable_character_is_written_escaped(self):
        path = os.path.join(jail.scratch(self), "gw.log")
        log = Log(path)
        log.write("POST /v1/messages -> 200 kind=main model-note=\udc80")
        log.write("next")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("model-note=\\udc80\n", text)
        self.assertIn(" next\n", text)

    def test_no_path_discards(self):
        Log(None).write("nothing")  # no file, no error


if __name__ == "__main__":
    unittest.main()
