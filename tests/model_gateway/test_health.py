"""Tests for model_gateway.health: the exact health line, with and without middleware."""
import os
import unittest

from tests import fakes, jail
from tests.fakes import RECORDER, calls

FAIL = 'NAME = "fail"\ndef on_request(ctx, req):\n    raise RuntimeError("x")\n'
SAYS = 'NAME = "says"\nSETTINGS = {"v": (7, int, None)}\ndef health(ctx):\n    return "v=%d" % ctx.settings["v"]\n'
with open(os.path.join(jail.ROOT, "examples", "middleware", "model_note.py")) as f:
    NOTE = f.read()


def health(port):
    return fakes.request(port, "GET", "/__gateway/health")[1]


class HealthTest(unittest.TestCase):
    def test_chatgpt_health_uses_only_the_memory_count(self):
        from unittest.mock import Mock
        from model_gateway.health import health_line
        rt, _ = fakes.start_gateway(self)
        route = Mock()
        for count, expected in [(None, "none"), (0, "0 models"), (2, "2 models")]:
            route.catalog.count.return_value = count
            line = health_line(rt.settings.snapshot, rt.chain, rt.log, route)
            self.assertIn(" chatgpt=on catalog=" + expected + " middleware=", line)
        route.catalog.models.assert_not_called()

    def test_no_settings_no_middleware(self):
        rt, port = fakes.start_gateway(self)
        self.assertEqual(health(port), b"ok pid=%d capture=off settings=missing middleware=[]\n" % os.getpid())

    def test_the_example_middleware_and_capture_on(self):
        rt, port = fakes.start_gateway(self, settings={"capture_dir": "/tmp/unused-capture",
                                                       "middleware": [{"module": "model_note"}]},
                                       middleware={"model_note": NOTE})
        self.assertEqual(health(port), b"ok pid=%d capture=on settings=ok middleware=[model-note errors=0]\n" % os.getpid())

    def test_health_text_and_errors(self):
        up = fakes.start_upstream(self)
        rt, port = fakes.start_gateway(self, up, settings={"middleware": [{"module": m} for m in ("says", "fail")]},
                                       middleware={"says": SAYS, "fail": FAIL})
        fakes.request(port, "POST", "/v1/messages", b'{"model":"m"}')
        line = health(port)
        self.assertEqual(line, b"ok pid=%d capture=off settings=ok middleware=[says v=7 errors=0, fail errors=1]\n"
                         % os.getpid())

    def test_a_refused_edit_shows_as_a_settings_error(self):
        rt, port = fakes.start_gateway(self, settings={})
        jail.write_json(rt.settings.path, {"capture_dir": "relative"}, bump=1)
        self.assertRegex(health(port), rb"^ok pid=\d+ capture=off settings=error\(.*capture_dir must be an absolute path.*\) middleware=\[\]\n$")

    def test_health_runs_no_hook(self):
        rt, port = fakes.start_gateway(self, settings={"middleware": [{"module": "rec"}]},
                                       middleware={"rec": RECORDER.format(name="rec")})
        health(port)
        self.assertEqual(calls("rec"), [])


if __name__ == "__main__":
    unittest.main()
