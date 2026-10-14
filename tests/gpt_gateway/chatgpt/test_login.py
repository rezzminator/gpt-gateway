"""Browser PKCE and device-code flows against loopback and replaceable browser opening."""
import base64
import errno
import hashlib
import importlib
import io
import json
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from http.client import HTTPConnection
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from gpt_gateway import cli, clock, env
from tests import jail
from tests.gpt_gateway.chatgpt.test_auth import OAuthFake, tokens


class LoginTest(unittest.TestCase):
    def setUp(self):
        try:
            self.login = importlib.import_module("gpt_gateway.chatgpt.login")
            self.auth = importlib.import_module("gpt_gateway.chatgpt.auth")
        except ImportError:
            self.fail("A1-A2: login flows are not implemented")
        self.home = jail.scratch(self)
        self.fake = OAuthFake(self)
        self.mapping = jail.clean_env(self.home, GPT_GATEWAY_CHATGPT_AUTH_ORIGIN=self.fake.origin,
                                      GPT_GATEWAY_CHATGPT_AUTH_STORE="file")
        self.store = self.auth.Store(env.ChatGPTEnv.parse(self.mapping))

    def browser_callback(self, url, state_ok=True):
        self.authorize = parse_qs(urlsplit(url).query)
        callback = urlsplit(self.authorize["redirect_uri"][0])
        conn = HTTPConnection("127.0.0.1", callback.port, timeout=5)
        state = self.authorize["state"][0] if state_ok else "wrong"
        conn.request("GET", "/auth/callback?" + urlencode({"state": state, "code": "invented-code"}))
        reply = conn.getresponse()
        reply.read()
        self.callback_status = reply.status
        conn.close()

    def test_browser_checks_state_s256_and_exchanges_form(self):
        port = jail.free_port()
        with patch.object(self.login, "CALLBACK_PORTS", (port,)), \
                patch.object(self.login, "open_browser", side_effect=self.browser_callback):
            self.login.browser_login(self.store, output=lambda _: None)
        query = self.authorize
        self.assertEqual(query["client_id"], [self.auth.CLIENT_ID])
        self.assertEqual(query["scope"], ["openid profile email offline_access"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], ["http://localhost:%d/auth/callback" % port])
        self.assertEqual(query["originator"], ["codex_cli_rs"])
        self.assertEqual(self.callback_status, 200)
        path, headers, raw = self.fake.calls[0]
        body = parse_qs(raw.decode())
        self.assertEqual(path, "/oauth/token")
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"][0].encode()).digest()).decode().rstrip("=")
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(body["redirect_uri"], query["redirect_uri"])
        self.assertEqual(body["code"], ["invented-code"])
        self.assertEqual(self.store.credentials().account_id, "invented-1234")

    def test_wrong_state_does_not_end_a_valid_login(self):
        statuses = []

        def browser(url):
            self.browser_callback(url, False)
            statuses.append(self.callback_status)
            self.browser_callback(url)
            statuses.append(self.callback_status)

        with patch.object(self.login, "CALLBACK_PORTS", (jail.free_port(),)), \
                patch.object(self.login, "open_browser", side_effect=browser), \
                patch.object(self.login, "LOGIN_SECONDS", 0.2):
            self.login.browser_login(self.store, output=lambda _: None)
        self.assertEqual(statuses, [400, 200])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.store.credentials().account_id, "invented-1234")

    def test_wrong_state_only_times_out_without_exchange(self):
        with patch.object(self.login, "CALLBACK_PORTS", (jail.free_port(),)), \
                patch.object(self.login, "open_browser", side_effect=lambda url: self.browser_callback(url, False)), \
                patch.object(self.login, "LOGIN_SECONDS", 0.05):
            with self.assertRaisesRegex(self.auth.AuthError, "timed out"):
                self.login.browser_login(self.store, output=lambda _: None)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.callback_status, 400)

    def test_valid_state_provider_error_ends_login(self):
        def browser(url):
            authorize = parse_qs(urlsplit(url).query)
            callback = urlsplit(authorize["redirect_uri"][0])
            conn = HTTPConnection("127.0.0.1", callback.port, timeout=5)
            try:
                conn.request("GET", "/auth/callback?" + urlencode({
                    "state": authorize["state"][0], "error": "access_denied"}))
                reply = conn.getresponse()
                reply.read()
                self.assertEqual(reply.status, 400)
            finally:
                conn.close()

        with patch.object(self.login, "CALLBACK_PORTS", (jail.free_port(),)), \
                patch.object(self.login, "open_browser", side_effect=browser), \
                patch.object(self.login, "LOGIN_SECONDS", 0.2):
            with self.assertRaisesRegex(self.auth.AuthError, "authorization refused"):
                self.login.browser_login(self.store, output=lambda _: None)
        self.assertEqual(self.fake.calls, [])

    def test_port_fallback_only_on_address_in_use(self):
        real_bind = self.login.bind_callback
        ports = []

        def binding(port, handler):
            ports.append(port)
            if len(ports) == 1:
                raise OSError(errno.EADDRINUSE, "busy")
            return real_bind(0, handler)

        with patch.object(self.login, "bind_callback", side_effect=binding), \
                patch.object(self.login, "open_browser", side_effect=self.browser_callback):
            self.login.browser_login(self.store, output=lambda _: None)
        self.assertEqual(ports, [1455, 1457])
        with patch.object(self.login, "bind_callback", side_effect=OSError(errno.EACCES, "denied")) as bind:
            with self.assertRaises(self.auth.AuthError):
                self.login.browser_login(self.store, output=lambda _: None)
            self.assertEqual(bind.call_count, 1)

    def test_device_shape_alias_poll_exchange_and_origin(self):
        for name in ("user_code", "usercode"):
            responses = [(200, {"device_auth_id": "invented-device", name: "ABCD-EFGH", "interval": "1"}),
                         (403, {}), (404, {}),
                         (200, {"authorization_code": "invented-code", "code_verifier": "invented-verifier"}),
                         (200, tokens(clock.now() + 3600))]
            calls = []

            def post(path, body, form=False, timeout=30):
                calls.append((path, body, form))
                self.fake.reply = responses.pop(0)
                return original(path, body, form=form, timeout=timeout)

            original = self.store.post
            output = []
            with patch.object(self.store, "post", side_effect=post), patch.object(clock, "sleep"):
                self.login.device_login(self.store, output=output.append)
            self.assertIn(self.fake.origin + "/codex/device", output[0])
            self.assertIn("ABCD-EFGH", output[0])
            self.assertEqual(calls[0][0], "/api/accounts/deviceauth/usercode")
            self.assertEqual(calls[1][1], {"device_auth_id": "invented-device", "user_code": "ABCD-EFGH"})
            self.assertEqual(calls[-1][1]["redirect_uri"], self.fake.origin + "/deviceauth/callback")
            self.assertEqual(calls[-1][1]["code_verifier"], "invented-verifier")
            self.assertEqual(self.store.credentials().account_id, "invented-1234")

    def test_device_timeout_and_non_pending_error(self):
        usercode = (200, {"device_auth_id": "invented-device", "user_code": "ABCD", "interval": 1})
        with patch.object(self.store, "post", side_effect=[usercode, (403, {})]) as post, \
                patch.object(clock, "monotonic", side_effect=[0, 0, 0, 901]), patch.object(clock, "sleep"):
            with self.assertRaisesRegex(self.auth.AuthError, "15 minutes"):
                self.login.device_login(self.store, output=lambda _: None)
            self.assertEqual(post.call_count, 2)
        with patch.object(self.store, "post", side_effect=[usercode, (500, {"error": "secret"})]):
            with self.assertRaisesRegex(self.auth.AuthError, "HTTP 500"):
                self.login.device_login(self.store, output=lambda _: None)

    def test_cli_login_dispatch_status_missing_and_bad_args(self):
        with patch.object(self.login, "device_login") as device, patch.object(self.login, "browser_login") as browser:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["chatgpt-login", "--device"], self.home, self.mapping), 0)
                self.assertEqual(cli.main(["chatgpt-login"], self.home, self.mapping), 0)
            self.assertEqual(device.call_count, 1)
            self.assertEqual(browser.call_count, 1)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["chatgpt-status"], self.home, self.mapping), 0)
        self.assertIn("not logged in", out.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["chatgpt-login", "bad"], self.home, self.mapping), 2)
            self.assertEqual(cli.main(["chatgpt-status", "bad"], self.home, self.mapping), 2)

    def test_non_ascii_state_is_rejected_until_timeout(self):

        def browser(url):
            self.authorize = parse_qs(urlsplit(url).query)
            destination = urlsplit(self.authorize["redirect_uri"][0])
            conn = HTTPConnection("127.0.0.1", destination.port, timeout=2)
            conn.request("GET", "/auth/callback?" + urlencode({"state": "\u2603", "code": "invented"}))
            try:
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 400)
            finally:
                conn.close()

        with patch.object(self.login, "CALLBACK_PORTS", (jail.free_port(),)), \
                patch.object(self.login, "open_browser", side_effect=browser), \
                patch.object(self.login, "LOGIN_SECONDS", 0.05):
            with self.assertRaisesRegex(self.auth.AuthError, "timed out"):
                self.login.browser_login(self.store, output=lambda _: None)
        self.assertEqual(self.fake.calls, [])
