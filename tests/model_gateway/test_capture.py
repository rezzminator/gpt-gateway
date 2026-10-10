"""Tests for model_gateway.capture: off by default, on by setting or env, one folder per exchange, secrets redacted."""
import http.client
import json
import os
import unittest

from tests import fakes, jail
from tests.fakes import RECORDER, Upstream, calls

EDIT = 'NAME = "edit"\ndef on_request(ctx, req):\n    req.body["added"] = 1\n    req.mark_changed()\n'


class CaptureTest(unittest.TestCase):
    def setUp(self):
        Upstream.seen.clear()
        self.up = fakes.start_upstream(self)
        self.cap = os.path.join(jail.scratch(self), "cap")

    def test_off_by_default(self):
        rt, port = fakes.start_gateway(self, self.up, settings={"middleware": [{"module": "rec"}]},
                                       middleware={"rec": RECORDER.format(name="rec")})
        fakes.request(port, "POST", "/v1/messages", b'{"model":"m"}')
        self.assertIsNone(rt.settings.current().capture_dir)
        self.assertIsNone(calls("rec")[0][1]["capture_dir"])
        self.assertIn(b"capture=off", fakes.request(port, "GET", "/__gateway/health")[1])

    def test_on_by_env(self):
        rt, port = fakes.start_gateway(self, self.up, env={"MODEL_GATEWAY_CAPTURE_DIR": self.cap})
        fakes.request(port, "POST", "/v1/messages", b'{"model":"m"}')
        (day,) = os.listdir(self.cap)
        self.assertEqual(len(os.listdir(os.path.join(self.cap, day))), 1)

    def test_each_exchange_lands_in_one_folder_with_secrets_redacted(self):
        rt, port = fakes.start_gateway(self, self.up, settings={"capture_dir": self.cap, "middleware": [{"module": "edit"}]},
                                       middleware={"edit": EDIT})
        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/messages?beta=true", body=body, headers={"authorization": "Bearer secret-token"})
        streamed = conn.getresponse().read()
        conn.request("GET", "/v1/models")
        conn.getresponse().read()
        conn.close()
        fakes.recorded(rt)
        (day,) = os.listdir(self.cap)
        first, second = sorted(os.listdir(os.path.join(self.cap, day)))
        self.assertRegex(first, r"^\d{6}\.\d{3}-000001-main-POST-v1_messages$")
        self.assertRegex(second, r"^\d{6}\.\d{3}-000002-main-GET-v1_models$")
        d = os.path.join(self.cap, day, first)
        self.assertEqual(sorted(os.listdir(d)), ["1-request.json", "2-request.body", "3-request.sent.body",
                                                 "4-response.json", "5-response.body"])
        with open(os.path.join(d, "1-request.json")) as f:
            self.assertIn(["authorization", "<redacted>"], json.load(f)["headers"])
        with open(os.path.join(d, "2-request.body"), "rb") as f:
            self.assertEqual(f.read(), body)
        with open(os.path.join(d, "3-request.sent.body"), "rb") as f:
            self.assertEqual(f.read(), Upstream.seen[0]["body"])
        with open(os.path.join(d, "4-response.json")) as f:
            self.assertEqual(json.load(f)["status"], 200)
        with open(os.path.join(d, "5-response.body"), "rb") as f:
            self.assertEqual(f.read(), streamed)
        for root, _, files in os.walk(self.cap):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(b"secret-token", f.read())
        self.assertNotIn("3-request.sent.body", os.listdir(os.path.join(self.cap, day, second)))

    def test_a_capture_that_cannot_write_is_recorded_and_the_request_goes_on(self):
        blocker = os.path.join(jail.scratch(self), "a-file")
        open(blocker, "w").close()
        rt, port = fakes.start_gateway(self, self.up, settings={"capture_dir": blocker})
        resp, _ = fakes.request(port, "POST", "/v1/messages", b'{"model":"m"}')
        self.assertEqual(resp.status, 200)
        self.assertEqual([(e["request_id"], e["fields"]["error"].startswith("NotADirectoryError"))
                          for e in fakes.events(rt, "capture_error")], [(1, True)])


if __name__ == "__main__":
    unittest.main()
