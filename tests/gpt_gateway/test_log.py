"""Tests for gpt_gateway.log: stamped lines, rotation, and a failing log reported, never silent."""
import contextlib
import io
import os
import json
import threading
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

    def test_level_refuses_aliased_endpoints_without_foreign_mutation(self):
        for leaf in ("directory", "errors.jsonl", "warnings.jsonl", "info.jsonl", "errors.jsonl.1", "warnings.jsonl.1", "info.jsonl.1"):
            with self.subTest(leaf=leaf):
                root = jail.scratch(self)
                outside = os.path.join(root, "outside")
                os.mkdir(outside, 0o755)
                sentinel = os.path.join(outside, "sentinel")
                with open(sentinel, "w") as source:
                    source.write("protected")
                os.chmod(sentinel, 0o644)
                directory = os.path.join(root, "gpt-gateway")
                if leaf == "directory":
                    os.symlink(outside, directory)
                else:
                    os.mkdir(directory)
                    os.symlink(sentinel, os.path.join(directory, leaf))
                logger = self.level_log(root, PLUGIN_LOG_DIR=root)
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    logger.record("error", "alias-test", "new", "test")
                self.assertIn("symlink", error.getvalue())
                with open(sentinel) as source:
                    self.assertEqual(source.read(), "protected")
                self.assertEqual(os.stat(sentinel).st_mode & 0o777, 0o644)
                self.assertEqual(os.stat(outside).st_mode & 0o777, 0o755)

    def test_level_tightens_dormant_backup_without_rotation(self):
        root = jail.scratch(self)
        directory = os.path.join(root, "gpt-gateway")
        os.mkdir(directory)
        backup = os.path.join(directory, "errors.jsonl.1")
        with open(backup, "w") as source:
            source.write("old")
        os.chmod(backup, 0o644)
        self.level_log(root, PLUGIN_LOG_DIR=root).record("error", "privacy", "new", "test")
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
        with open(backup) as source:
            self.assertEqual(source.read(), "old")

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
        absent.record("info", "no-home", "nothing to resolve", "gpt_gateway/log.py#record")

    def test_record_write_failure_is_one_stderr_line(self):
        home = jail.scratch(self)
        root = os.path.join(home, "blocked")
        with open(root, "w") as source:
            source.write("not a directory")
        log = self.level_log(home, PLUGIN_LOG_DIR=root)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            log.record("error", "forced-event", "first", "gpt_gateway/log.py#record")
            log.record("error", "forced-event", "second", "gpt_gateway/log.py#record")
            self.assertEqual(len(err.getvalue().splitlines()), 1)
            os.unlink(root)
            log.record("error", "recovered", "third", "gpt_gateway/log.py#record")
            with patch.object(logmod.os, "open", side_effect=OSError("disk full")):
                log.record("error", "forced-event", "fourth", "gpt_gateway/log.py#record")
                log.record("error", "forced-event", "fifth", "gpt_gateway/log.py#record")
        self.assertEqual(len(err.getvalue().splitlines()), 2)
        self.assertIn(os.path.join(root, "gpt-gateway"), err.getvalue())
        self.assertIn("disk full", err.getvalue())
        self.assertEqual(self.rows(os.path.join(root, "gpt-gateway"), "errors.jsonl")[0]["event"], "recovered")

    def test_record_cap_moves_file_aside(self):
        home = jail.scratch(self)
        log = self.level_log(home, PLUGIN_LOG_DIR=home)
        directory = os.path.join(home, "gpt-gateway")
        for level, filename in (("error", "errors.jsonl"), ("warning", "warnings.jsonl"), ("info", "info.jsonl")):
            with self.subTest(level=level):
                log.record(level, "before-cap", "old", "gpt_gateway/log.py#record")
                path = os.path.join(directory, filename)
                with open(path, "r+b") as source:
                    source.truncate(20971521)
                with open(path + ".1", "w") as source:
                    source.write("stale")
                log.record(level, "after-cap", "new", "gpt_gateway/log.py#record")
                self.assertEqual(os.path.getsize(path + ".1"), 20971521)
                self.assertEqual(self.rows(directory, filename)[-1]["event"], "after-cap")
                capped = self.rows(directory, "warnings.jsonl")
                self.assertEqual(capped[-1 if level != "warning" else -2]["event"], "log-capped")

    def test_record_bounds_rows_and_uses_one_append_syscall(self):
        home = jail.scratch(self)
        log = self.level_log(home, PLUGIN_LOG_DIR=home)
        directory = os.path.join(home, "gpt-gateway")
        for msg, detail in (("brief", {"error": "x" * 5000}), ("☃\udc80" * 5000, {"count": 1})):
            with self.subTest(kind=len(msg)), patch.object(logmod.os, "write", wraps=os.write) as writer:
                log.record("error", "large-error", msg, "gpt_gateway/log.py#record", detail)
                self.assertEqual(writer.call_count, 1)
                self.assertLessEqual(len(writer.call_args.args[1]), 4096)
                row = self.rows(directory, "errors.jsonl")[-1]
                self.assertTrue(row["detail"]["truncated"])
                self.assertGreater(row["detail"]["bytes"], 4096)
                self.assertTrue(msg.startswith(row["msg"]))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), patch.object(logmod.os, "write", return_value=1):
            log.record("error", "short-write", "failed", "gpt_gateway/log.py#record")
        self.assertIn("short", err.getvalue())

    def test_record_serializes_concurrent_writers(self):
        home = jail.scratch(self)
        log = self.level_log(home, PLUGIN_LOG_DIR=home)
        workers = [threading.Thread(target=log.record, args=("info", "concurrent", str(i), "gpt_gateway/log.py#record"))
                   for i in range(40)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        rows = self.rows(os.path.join(home, "gpt-gateway"), "info.jsonl")
        self.assertEqual(sorted(int(row["msg"]) for row in rows), list(range(40)))

    def test_ready_reports_fallback_without_a_text_line(self):
        home = jail.scratch(self)
        text = os.path.join(home, "gw.log")
        log = self.level_log(home, text)
        log.ready("digest123")
        row, = self.rows(os.path.join(home, ".local/state/gpt-gateway/logs"), "info.jsonl")
        self.assertEqual((row["event"], row["detail"]),
                         ("logger-ready", {"version": "digest123", "dir_source": "fallback"}))
        log.write("legacy")
        with open(text) as source:
            self.assertEqual(len(source.read().splitlines()), 1)

    def test_invalid_level_inputs_are_reported_without_interrupting_text(self):
        home = jail.scratch(self)
        text = os.path.join(home, "gw.log")
        log = self.level_log(home, text, PLUGIN_LOG_DIR="relative")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            log.record("error", "invalid-root", "legacy survives", "gpt_gateway/log.py#record")
        self.assertIn("PLUGIN_LOG_DIR", err.getvalue())
        with open(text) as source:
            self.assertIn("legacy survives", source.read())
        good = self.level_log(home, text, PLUGIN_LOG_DIR=home)
        with contextlib.redirect_stderr(err):
            good.record("error", "invalid-detail", "still survives", "gpt_gateway/log.py#record", {"bad": object()})
            good.record("info", "recovered", "valid", "gpt_gateway/log.py#record")
        self.assertIn("TypeError", err.getvalue())
        self.assertEqual(self.rows(os.path.join(home, "gpt-gateway"), "info.jsonl")[0]["event"], "recovered")

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

    def test_an_unwritable_log_is_reported_once_on_stderr(self):
        path = os.path.join(jail.scratch(self), "missing-dir", "gw.log")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            log = Log(path)
            log.write("a")
            log.write("b")
        self.assertEqual(err.getvalue().count("cannot write the log"), 1)

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
