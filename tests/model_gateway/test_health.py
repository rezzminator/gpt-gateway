"""Tests for model_gateway.health: the exact health line, with and without middleware, and its database fields."""
import os
import re
import resource
import unittest

from model_gateway import db
from tests import fakes, jail
from tests.fakes import RECORDER, calls

FAIL = 'NAME = "fail"\ndef on_request(ctx, req):\n    raise RuntimeError("x")\n'
SAYS = 'NAME = "says"\nSETTINGS = {"v": (7, int, None)}\ndef health(ctx):\n    return "v=%d" % ctx.settings["v"]\n'
FDS = re.compile(rb" fds=\d+/\d+ ")
with open(os.path.join(jail.ROOT, "examples", "middleware", "model_note.py")) as f:
    NOTE = f.read()


def health(port):
    """The health line with its `fds=` field replaced by a fixed one: the open count moves with every thread."""
    return FDS.sub(b" fds=N/L ", fakes.request(port, "GET", "/__gateway/health")[1])


def db_part(rt):
    """The health line's database fields, its rows counted from the file once the queue is written."""
    assert rt.db.flush(), "the db writer did not flush"
    rows = sum(len(db.query(rt.db.path, "SELECT id FROM %s" % table)) for table in ("requests", "events"))
    return ("db=%s db_rows=%d db_drops=0 db_errors=0" % (rt.db.path, rows)).encode()


class HealthTest(unittest.TestCase):
    def test_chatgpt_health_uses_only_the_memory_count(self):
        from unittest.mock import Mock
        from model_gateway.health import health_line
        rt, _ = fakes.start_gateway(self)
        rt.chatgpt = route = Mock()
        for count, expected, state in [(None, "none", "off"), (0, "0 models", "24h"), (2, "2 models", "24h(rejected)")]:
            route.catalog.count.return_value = count
            route.retention_state.return_value = state
            line = health_line(rt.settings.snapshot, rt)
            self.assertIn(" chatgpt=on catalog=%s cache_retention=%s middleware=" % (expected, state), line)
            route.retention_state.assert_called_with(rt.settings.snapshot)
        route.catalog.models.assert_not_called()

    def test_no_settings_no_middleware(self):
        rt, port = fakes.start_gateway(self)
        part = db_part(rt)
        self.assertEqual(health(port), b"ok pid=%d fds=N/L capture=off settings=missing %s middleware=[]\n" % (os.getpid(), part))

    def test_the_example_middleware_and_capture_on(self):
        rt, port = fakes.start_gateway(self, settings={"capture_dir": "/tmp/unused-capture",
                                                       "middleware": [{"module": "model_note"}]},
                                       middleware={"model_note": NOTE})
        part = db_part(rt)
        self.assertEqual(health(port), b"ok pid=%d fds=N/L capture=on settings=ok %s middleware=[model-note errors=0]\n"
                         % (os.getpid(), part))

    def test_health_text_and_errors(self):
        up = fakes.start_upstream(self)
        rt, port = fakes.start_gateway(self, up, settings={"middleware": [{"module": m} for m in ("says", "fail")]},
                                       middleware={"says": SAYS, "fail": FAIL})
        fakes.request(port, "POST", "/v1/messages", b'{"model":"m"}')
        part = db_part(rt)
        self.assertNotIn(b" db_rows=0 ", part + b" ")  # the request's row and its failure's event
        self.assertEqual(health(port), b"ok pid=%d fds=N/L capture=off settings=ok %s middleware=[says v=7 errors=0, fail errors=1]\n"
                         % (os.getpid(), part))

    def test_a_refused_edit_shows_as_a_settings_error(self):
        rt, port = fakes.start_gateway(self, settings={})
        jail.write_json(rt.settings.path, {"capture_dir": "relative"}, bump=1)
        self.assertRegex(health(port), rb"^ok pid=\d+ fds=N/L capture=off settings=error\(.*capture_dir must be an absolute path.*\) "
                                       rb"db=\S+ db_rows=\d+ db_drops=0 db_errors=0 middleware=\[\]\n$")

    def test_the_line_reports_open_handles_over_the_soft_limit(self):
        rt, port = fakes.start_gateway(self)
        raw = fakes.request(port, "GET", "/__gateway/health")[1]
        found = re.match(rb"^ok pid=\d+ fds=(\d+)/(\d+) capture=", raw)
        self.assertIsNotNone(found, raw)
        self.assertEqual(int(found.group(2)), resource.getrlimit(resource.RLIMIT_NOFILE)[0])
        self.assertGreater(int(found.group(1)), 0)
        self.assertLess(int(found.group(1)), int(found.group(2)))

    def test_health_runs_no_hook(self):
        rt, port = fakes.start_gateway(self, settings={"middleware": [{"module": "rec"}]},
                                       middleware={"rec": RECORDER.format(name="rec")})
        health(port)
        self.assertEqual(calls("rec"), [])


if __name__ == "__main__":
    unittest.main()
