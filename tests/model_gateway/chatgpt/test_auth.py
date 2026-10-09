"""Jailed token persistence, rotation and inter-process single-flight tests."""
import base64
import importlib
import io
import json
import multiprocessing
import os
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from model_gateway import cli, clock, env
from tests import jail


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


def tokens(exp=1, suffix="old"):
    return {"access_token": jwt({"exp": exp, "generation": suffix}),
            "refresh_token": "invented-refresh-" + suffix,
            "id_token": jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "invented-1234"}})}


class OAuthFake:
    def __init__(self, test):
        self.calls = []
        self.reply = (200, tokens(clock.now() + 3600, "new"))
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                fake.calls.append((self.path, self.headers, raw))
                status, data = fake.reply
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", jail.free_port()), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        test.addCleanup(self.close)
        self.origin = "http://127.0.0.1:%d" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def process_refresh(knobs, ready, go, out, force):
    from model_gateway.chatgpt.auth import Store
    store = Store(knobs)
    if force:
        store.credentials()
    ready.put(True)
    go.wait(10)
    try:
        out.put(store.credentials(force_refresh=force).access_token)
    except Exception as e:
        out.put(type(e).__name__)


class AuthTest(unittest.TestCase):
    def setUp(self):
        try:
            self.auth = importlib.import_module("model_gateway.chatgpt.auth")
        except ImportError:
            self.fail("A3-A6: auth store is not implemented")
        self.home = jail.scratch(self)
        self.fake = OAuthFake(self)
        self.mapping = jail.clean_env(self.home, MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN=self.fake.origin,
                                      MODEL_GATEWAY_CHATGPT_AUTH_STORE="file")
        self.knobs = env.ChatGPTEnv.parse(self.mapping)
        self.store = self.auth.Store(self.knobs)

    def test_file_mode_account_fallback_and_status_never_refreshes(self):
        self.store.save(tokens())
        self.assertEqual(os.stat(self.knobs.auth_path).st_mode & 0o777, 0o600)
        with open(self.knobs.auth_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["account_id"], "invented-1234")
        self.assertGreater(saved["last_refresh"], 0)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["chatgpt-status"], self.home, self.mapping), 0)
        self.assertIn("1234", out.getvalue())
        self.assertIn("1970-01-01", out.getvalue())
        self.assertNotIn("invented-", out.getvalue())
        self.assertNotIn(saved["access_token"], out.getvalue())
        self.assertEqual(self.fake.calls, [])

    def test_missing_login_and_bad_storage_raise_sanitized_errors(self):
        with self.assertRaises(self.auth.LoginRequired):
            self.store.credentials()
        os.makedirs(os.path.dirname(self.knobs.auth_path), exist_ok=True)
        jail.write_json(self.knobs.auth_path, "invented-secret-malformed")
        with self.assertRaises(self.auth.AuthError) as caught:
            self.store.credentials()
        self.assertNotIn("invented-secret", str(caught.exception))

    def test_refresh_expiry_and_exact_json_body(self):
        self.store.save(tokens(clock.now() + 299))
        got = self.store.credentials()
        self.assertEqual(got, self.auth.Credentials(self.fake.reply[1]["access_token"], "invented-1234"))
        self.assertEqual(self.store.credentials(), got)
        self.assertEqual(len(self.fake.calls), 1)
        path, headers, raw = self.fake.calls[0]
        self.assertEqual(path, "/oauth/token")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(raw), {"client_id": self.auth.CLIENT_ID, "grant_type": "refresh_token",
                                          "refresh_token": "invented-refresh-old"})

    def test_account_id_prefers_tokens_then_top_level_and_namespaced_claim(self):
        for body, expected in ((dict(tokens(), account_id="explicit-5678"), "explicit-5678"),
                               (dict(tokens(), id_token=jwt({"chatgpt_account_id": "top-1234"})), "top-1234"),
                               (tokens(), "invented-1234")):
            with self.subTest(expected=expected):
                body["access_token"] = jwt({"exp": clock.now() + 3600})
                self.store.save(body)
                self.assertEqual(self.store.credentials().account_id, expected)

    def test_refresh_preserves_optional_tokens(self):
        self.store.save(tokens())
        self.fake.reply = (200, {"access_token": jwt({"exp": clock.now() + 3600})})
        self.assertEqual(self.store.credentials().account_id, "invented-1234")
        with open(self.knobs.auth_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["refresh_token"], "invented-refresh-old")
        self.assertEqual(saved["id_token"], tokens()["id_token"])

    def test_permanent_refusal_is_persisted_and_never_retried(self):
        cases = ((401, {}),
                 (400, {"error": "INVALID_GRANT"}),
                 (403, {"error": {"code": "Refresh_Token_Expired"}}),
                 (500, {"error": {"type": "REFRESH_TOKEN_REUSED"}}),
                 (400, {"error": "Refresh_Token_Invalidated"}))
        for status, reply in cases:
            with self.subTest(status=status, reply=reply):
                self.store.save(tokens())
                self.fake.reply = (status, reply)
                before = len(self.fake.calls)
                for _ in range(2):
                    with self.assertRaises(self.auth.AuthError) as caught:
                        self.auth.Store(self.knobs).credentials(force_refresh=True)
                    self.assertIsInstance(caught.exception, self.auth.LoginRequired)
                    self.assertIn("log in again", str(caught.exception))
                    with open(self.knobs.auth_path) as stream:
                        self.assertEqual(json.load(stream), {"login_required": True})
                self.assertEqual(len(self.fake.calls) - before, 1)

    def test_transient_failure_is_reported_without_retry_or_secret(self):
        for status, error in ((500, "invalid_grant"), (500, "invented-secret"), (400, "unknown")):
            with self.subTest(status=status, error=error):
                self.store.save(tokens())
                with open(self.knobs.auth_path, "rb") as stream:
                    stored = stream.read()
                self.fake.reply = (status, {"error": error})
                before = len(self.fake.calls)
                with self.assertRaises(self.auth.AuthError) as caught:
                    self.store.credentials()
                self.assertIs(type(caught.exception), self.auth.AuthError)
                self.assertNotIn("invented-secret", str(caught.exception))
                self.assertEqual(len(self.fake.calls) - before, 1)
                with open(self.knobs.auth_path, "rb") as stream:
                    self.assertEqual(stream.read(), stored)

    def test_threads_single_flight_expiry_and_forced_401(self):
        for force in (False, True):
            self.store.save(tokens(clock.now() + 3600 if force else 1))
            barrier = threading.Barrier(3)
            results = []
            errors = []
            before = len(self.fake.calls)

            def run():
                try:
                    store = self.auth.Store(self.knobs)
                    if force:
                        store.credentials()
                    barrier.wait(5)
                    results.append(store.credentials(force_refresh=force))
                except Exception as e:
                    errors.append(e)

            workers = [threading.Thread(target=run) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait(5)
            for worker in workers:
                worker.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertEqual(len(self.fake.calls) - before, 1)

    def test_processes_single_flight_expiry_and_forced_401(self):
        context = multiprocessing.get_context("spawn")
        for force in (False, True):
            self.store.save(tokens(clock.now() + 3600 if force else 1))
            ready, out, go = context.Queue(), context.Queue(), context.Event()
            before = len(self.fake.calls)
            workers = [context.Process(target=process_refresh, args=(self.knobs, ready, go, out, force))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
                self.addCleanup(lambda p=worker: p.terminate() if p.is_alive() else None)
            self.assertTrue(ready.get(timeout=10))
            self.assertTrue(ready.get(timeout=10))
            go.set()
            results = [out.get(timeout=10) for _ in workers]
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
            ready.close()
            out.close()
            self.assertEqual(results, [self.fake.reply[1]["access_token"]] * 2)
            self.assertEqual(len(self.fake.calls) - before, 1)

    def test_force_refresh_uses_the_token_this_thread_received(self):
        self.store.save(tokens(clock.now() + 3600))
        original = self.store.credentials()
        changed = self.store.credentials(force_refresh=True)
        self.assertNotEqual(original, changed)
        self.assertEqual(self.store.credentials(), changed)
        self.assertEqual(len(self.fake.calls), 1)

    def test_malformed_token_response_cannot_replace_stored_login(self):
        self.store.save(tokens())
        self.fake.reply = (200, {"access_token": 123})
        with self.assertRaises(self.auth.AuthError):
            self.store.credentials()
        with open(self.knobs.auth_path) as f:
            self.assertEqual(json.load(f)["refresh_token"], "invented-refresh-old")

    def test_refresh_rejection_shapes_and_invalid_success_are_sanitized(self):
        for status, reply in ((400, {"error": {"type": "invalid_grant"}}),
                              (401, {"error": "invalid_grant"})):
            self.store.save(tokens())
            self.fake.reply = (status, reply)
            with self.assertRaises(self.auth.LoginRequired):
                self.store.credentials()
        self.store.save(tokens())
        self.fake.reply = (200, ["invented-secret"])
        with self.assertRaisesRegex(self.auth.AuthError, "Invalid ChatGPT auth response"):
            self.store.credentials()

    def test_keychain_store_uses_same_refresh_lock_and_never_writes_fallback(self):
        data = {}
        with patch.object(self.auth.Keychain, "available", return_value=True), \
                patch.object(self.auth.Keychain, "read", side_effect=lambda: data.get("tokens")), \
                patch.object(self.auth.Keychain, "write", side_effect=lambda value: data.update(tokens=value)):
            self.knobs.auth_store = "auto"
            store = self.auth.Store(self.knobs)
            store.save(tokens())
            self.assertEqual(store.credentials().account_id, "invented-1234")
            self.assertEqual(data["tokens"]["refresh_token"], "invented-refresh-new")
            self.assertFalse(os.path.exists(self.knobs.auth_path))
            self.assertEqual(os.stat(self.knobs.auth_path + ".lock").st_mode & 0o777, 0o600)

    def test_forced_refresh_coalesces_even_when_access_token_does_not_change(self):
        self.store.save(tokens(clock.now() + 3600))
        other = self.auth.Store(self.knobs)
        first = self.store.credentials()
        other.credentials()
        self.fake.reply = (200, {"access_token": first.access_token,
                                 "refresh_token": "invented-refresh-new"})
        self.store.credentials(force_refresh=True)
        self.assertEqual(other.credentials(force_refresh=True), first)
        self.assertEqual(len(self.fake.calls), 1)
