"""Tests for gpt_gateway.log: stamped lines, rotation, and a failing log reported, never silent."""
import contextlib
import io
import os
import unittest

from gpt_gateway.log import Log
from tests import jail


class LogTest(unittest.TestCase):
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
