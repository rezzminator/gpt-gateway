"""Tests for gpt_gateway.capture: off by default, on by setting or env, one folder per exchange, secrets redacted."""
import errno
import http.client
import json
import os
import stat
import unittest
from unittest import mock

from gpt_gateway import capture, clock, handles
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
        rt, port = fakes.start_gateway(self, self.up, env={"GPT_GATEWAY_CAPTURE_DIR": self.cap})
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

    def test_capture_dirs_and_files_are_private_under_permissive_umask(self):
        now = clock.now()
        previous_umask = os.umask(0)
        try:
            for existing in (True, False):
                with self.subTest(existing=existing):
                    root = os.path.join(jail.scratch(self), "existing" if existing else "fresh")
                    if existing:
                        os.mkdir(root, 0o777)
                        os.mkdir(os.path.join(root, clock.stamp("%Y-%m-%d", now)), 0o777)
                    with mock.patch.object(clock, "now", return_value=now):
                        exchange = capture.Capture(root, 1, "POST", "/v1/messages", {}, "main", lambda *a, **k: None)
                    exchange.request_body(b"request")
                    exchange.sent_body(b"sent")
                    exchange.response(200, "OK", [])
                    exchange.chunk(b"response")
                    exchange.close()
                    self.assertIsNotNone(exchange.dir)
                    day = os.path.dirname(exchange.dir)
                    for directory in (root, day, exchange.dir):
                        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700, directory)
                    for name in (capture.REQUEST, capture.REQUEST_BODY, capture.SENT_BODY,
                                 capture.RESPONSE, capture.RESPONSE_BODY):
                        path = os.path.join(exchange.dir, name)
                        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
                    request_path = os.path.join(exchange.dir, capture.REQUEST_BODY)
                    os.chmod(request_path, 0o666)
                    exchange.request_body(b"replacement")
                    self.assertEqual(stat.S_IMODE(os.stat(request_path).st_mode), 0o600, request_path)
                    with open(request_path, "rb") as f:
                        self.assertEqual(f.read(), b"replacement")
        finally:
            os.umask(previous_umask)

    def test_a_symlinked_capture_root_is_used_without_changing_its_target(self):
        base = jail.scratch(self)
        target, root = os.path.join(base, "shared"), os.path.join(base, "capture")
        os.mkdir(target)
        os.chmod(target, 0o755)
        os.symlink(target, root)
        events = []
        exchange = capture.Capture(root, 1, "POST", "/v1/messages", {}, "main", lambda *a, **k: events.append(a))
        exchange.close()
        self.assertEqual(events, [])
        self.assertIsNotNone(exchange.dir)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(exchange.dir)).st_mode), 0o700)

    def test_capture_rejects_existing_symlink_without_touching_target(self):
        root = os.path.join(jail.scratch(self), "private-capture")
        events = []
        exchange = capture.Capture(root, 1, "POST", "/v1/messages", {}, "main",
                                   lambda name, **fields: events.append(name))
        self.assertIsNotNone(exchange.dir)
        target = os.path.join(jail.scratch(self), "other-file")
        with open(target, "wb") as outside:
            outside.write(b"keep this")
        os.symlink(target, os.path.join(exchange.dir, capture.REQUEST_BODY))
        exchange.request_body(b"private conversation")
        exchange.response(200, "OK", [])  # the refused file costs only itself
        with open(target, "rb") as outside:
            self.assertEqual(outside.read(), b"keep this")
        self.assertEqual((events, sorted(os.listdir(exchange.dir))),
                         (["capture_error"], ["1-request.json", "2-request.body", "4-response.json", "5-response.body"]))

    def test_a_capture_that_cannot_write_is_recorded_and_the_request_goes_on(self):
        blocker = os.path.join(jail.scratch(self), "a-file")
        open(blocker, "w").close()
        rt, port = fakes.start_gateway(self, self.up, settings={"capture_dir": blocker})
        resp, _ = fakes.request(port, "POST", "/v1/messages", b'{"model":"m"}')
        self.assertEqual(resp.status, 200)
        self.assertEqual([(e["request_id"], e["fields"]["error"].startswith("FileExistsError"))
                          for e in fakes.events(rt, "capture_error")], [(1, True)])


class CaptureShortageTest(unittest.TestCase):
    """A capture that runs the process out of handles counts it, says so once per window, and never raises."""

    def setUp(self):
        handles.THROTTLE.clear()
        self.root = os.path.join(jail.scratch(self), "cap")
        self.events, self.now = [], [1000.0]
        patcher = mock.patch.object(clock, "monotonic", lambda: self.now[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def exchange(self, seq, **failing):
        """One exchange, every step run, in which the named `os` call fails with EMFILE."""
        short = OSError(errno.EMFILE, "Too many open files")
        with mock.patch("gpt_gateway.capture.os.makedirs", side_effect=short if "makedirs" in failing else os.makedirs), \
                mock.patch("gpt_gateway.capture.os.open", side_effect=short if "open" in failing else os.open):
            cap = capture.Capture(self.root, seq, "POST", "/v1/messages", {"x": "1"}, "main",
                                  lambda kind, **fields: self.events.append((kind, fields)))
            cap.request_body(b"{}")
            cap.response(200, "OK", [("a", "b")])
            cap.chunk(b"data")
            cap.close()

    def test_running_out_of_handles_is_one_event_per_window_with_the_held_back_count(self):
        for seq, failing in enumerate(("makedirs", "makedirs", "open", "makedirs"), 1):
            self.exchange(seq, **{failing: True})
        self.assertEqual([k for k, _ in self.events], ["fd_exhausted"])
        fields = self.events[0][1]
        self.assertEqual((fields["where"], fields["suppressed"]), ("capture", 0))
        self.assertIn("Too many open files", fields["error"])
        self.now[0] += handles.EVENT_SECONDS + 1
        self.exchange(5, makedirs=True)
        # held back: the second and fourth exchanges' folder, and each of the third's four files
        self.assertEqual([(k, f["suppressed"]) for k, f in self.events], [("fd_exhausted", 0), ("fd_exhausted", 6)])

    def test_a_file_that_cannot_be_written_costs_only_itself(self):
        real = os.open

        def refuse(path, *args):
            if path.endswith(capture.REQUEST_BODY):
                raise PermissionError(errno.EACCES, "refused")
            return real(path, *args)
        with mock.patch("gpt_gateway.capture.os.open", side_effect=refuse):
            cap = capture.Capture(self.root, 1, "POST", "/v1/messages", {"x": "1"}, "main",
                                  lambda kind, **fields: self.events.append((kind, fields)))
            cap.request_body(b"{}")
            cap.response(200, "OK", [("a", "b")])
            cap.chunk(b"data")
            cap.close()
        self.assertEqual(([k for k, _ in self.events], sorted(os.listdir(cap.dir))),
                         (["capture_error"], ["1-request.json", "4-response.json", "5-response.body"]))

    def test_any_other_failure_stays_a_capture_error(self):
        blocker = os.path.join(jail.scratch(self), "a-file")
        open(blocker, "w").close()
        self.root = blocker
        self.exchange(1)
        self.assertEqual([k for k, _ in self.events], ["capture_error"])


if __name__ == "__main__":
    unittest.main()
