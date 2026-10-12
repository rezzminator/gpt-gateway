"""Invented Codex logins exercise selection, guarded rotation and safe failures in the jail."""
import copy
from datetime import datetime, timezone
import json
import os
import threading
import unittest
from unittest.mock import patch

from gpt_gateway import clock, env
from gpt_gateway.chatgpt import auth
from tests import jail
from tests.gpt_gateway.chatgpt.test_auth import OAuthFake, jwt, tokens

NOW = 1800000000


class CodexLoginTest(unittest.TestCase):
    def setUp(self):
        self.home = jail.scratch(self)
        self.fake = OAuthFake(self)
        self.mapping = jail.clean_env(self.home, GPT_GATEWAY_CHATGPT_AUTH_STORE="file",
                                      GPT_GATEWAY_CHATGPT_AUTH_ORIGIN=self.fake.origin)
        self.knobs = env.ChatGPTEnv.parse(self.mapping)
        self.path = os.path.join(self.home, ".codex", "auth.json")
        os.makedirs(os.path.dirname(self.path))
        self.data = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                     "tokens": dict(tokens(NOW + 3600), account_id="acct-test-0001"),
                     "last_refresh": "2020-01-01T00:00:00Z", "agent_identity": {"invented": True}}
        self.fake.reply = (200, tokens(NOW + 7200, "new"))
        self.timer = patch.object(clock, "now", return_value=NOW)
        self.timer.start()
        self.addCleanup(self.timer.stop)
        self.provider = auth.source(self.knobs)

    def write_login(self, data=None):
        jail.write_json(self.path, self.data if data is None else data)

    def read_login(self):
        with open(self.path) as stream:
            return json.load(stream)

    def get_credentials(self, force=False):
        try:
            return self.provider.credentials(force_refresh=force)
        except auth.AuthError as error:
            self.fail("Expected usable Codex credentials, got %s" % error)

    def test_k1_selection_and_fallback(self):
        own = auth.Store(self.knobs)
        own.save(tokens(NOW + 3600, "own"))
        self.write_login()
        self.assertEqual(self.get_credentials().account_id, "acct-test-0001")
        self.assertEqual(self.get_credentials().access_token, self.data["tokens"]["access_token"])
        for data in ({"auth_mode": "apikey", "tokens": self.data["tokens"]},
                     {"auth_mode": "other", "tokens": self.data["tokens"]}, {}, {"tokens": None}):
            self.write_login(data)
            self.assertEqual(self.get_credentials().access_token, tokens(NOW + 3600, "own")["access_token"])
        self.data.pop("auth_mode")
        self.write_login()
        self.assertEqual(self.get_credentials().account_id, "acct-test-0001")
        os.unlink(self.path)
        self.assertEqual(self.get_credentials().account_id, "invented-1234")
        self.write_login()
        disabled = env.ChatGPTEnv.parse(dict(self.mapping, GPT_GATEWAY_CHATGPT_CODEX_AUTH="off"))
        self.assertEqual(auth.source(disabled).credentials().account_id, "invented-1234")

    def test_k2_schedule_and_no_network_fast_path(self):
        for access, last, refresh in ((jwt({"exp": NOW + 301}), "2020-01-01T00:00:00Z", False),
                (jwt({"exp": NOW + 300}), None, True), ("opaque", None, False),
                ("opaque", "invalid", False), (jwt({"exp": "unknown"}), "2020-01-01T01:00:00+01:00", True),
                ("opaque", datetime.fromtimestamp(NOW - 8 * 86400, timezone.utc).isoformat(), False)):
            with self.subTest(access=access, last=last):
                self.data["tokens"]["access_token"] = access
                self.data["last_refresh"] = last
                self.write_login()
                before = os.stat(self.path)
                count = len(self.fake.calls)
                got = self.get_credentials()
                self.assertEqual(len(self.fake.calls) - count, int(refresh))
                self.assertEqual(got.access_token, self.fake.reply[1]["access_token"] if refresh else access)
                if not refresh:
                    self.assertEqual(os.stat(self.path).st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(self.read_login(), self.data)
        self.assertEqual(set(os.listdir(os.path.dirname(self.path))), {"auth.json"})

    def test_k3_guard_reloads_under_lock_and_posts_exact_body(self):
        self.write_login()
        self.get_credentials()
        from gpt_gateway.chatgpt.codex_login import CodexLogin
        original = CodexLogin._read
        count = []
        rotated = copy.deepcopy(self.data)
        rotated["tokens"].update(self.fake.reply[1])
        self.data["tokens"]["access_token"] = jwt({"exp": NOW})
        self.write_login()
        def change_at_guard(instance):
            count.append(1)
            if len(count) == 2:
                self.write_login(rotated)
            return original(instance)
        with patch.object(CodexLogin, "_read", change_at_guard):
            self.assertEqual(self.get_credentials().access_token, rotated["tokens"]["access_token"])
        self.assertEqual(self.fake.calls, [])
        self.write_login()
        self.get_credentials()
        path, headers, raw = self.fake.calls[0]
        self.assertEqual(path, "/oauth/token")
        self.assertEqual(json.loads(raw), {"client_id": auth.CLIENT_ID, "grant_type": "refresh_token",
                                          "refresh_token": self.data["tokens"]["refresh_token"]})
        lock = os.path.join(os.path.dirname(self.knobs.auth_path), "codex-auth.lock")
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(lock)).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["auth.json"])

    def test_k4_preserves_format_keys_symlink_and_optional_tokens(self):
        self.write_login()
        self.get_credentials()
        target = os.path.join(self.home, "real-auth.json")
        os.rename(self.path, target)
        os.symlink(target, self.path)
        # Parse after creating the symlink as well as before it.
        self.provider = auth.source(env.ChatGPTEnv.parse(self.mapping))
        self.data["tokens"]["access_token"] = jwt({"exp": NOW})
        self.data["tokens"]["extra"] = "invented café"
        self.write_login()
        self.fake.reply = (200, {"access_token": jwt({"exp": NOW + 9000})})
        self.assertEqual(self.get_credentials().account_id, "acct-test-0001")
        expected = copy.deepcopy(self.data)
        expected["tokens"]["access_token"] = self.fake.reply[1]["access_token"]
        expected["last_refresh"] = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertTrue(os.path.islink(self.path))
        with open(target) as stream:
            self.assertEqual(stream.read(), json.dumps(expected, indent=2, ensure_ascii=False))
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["auth.json"])
        for reply in ({}, {"access_token": ""}, {"access_token": "new", "refresh_token": ""},
                      {"access_token": "new", "id_token": 123}):
            self.write_login()
            self.fake.reply = (200, reply)
            with self.assertRaises(auth.AuthError):
                self.provider.credentials()
            self.assertEqual(self.read_login(), self.data)
        # The account may change during the token POST; never overwrite the new login.
        changed = copy.deepcopy(self.data)
        changed["tokens"].update(tokens(NOW + 7200, "external"), account_id="acct-test-9999")
        def external_login(*args, **kwargs):
            self.write_login(changed)
            return 200, tokens(NOW + 9000, "ours")
        self.write_login()
        with patch.object(auth, "post_token", side_effect=external_login):
            self.assertEqual(self.get_credentials().account_id, "acct-test-9999")
        self.assertEqual(self.read_login(), changed)

    def test_k4_failed_write_keeps_the_rotated_login_and_retries(self):
        self.data["tokens"]["access_token"] = jwt({"exp": NOW})
        self.write_login()
        rotated = self.fake.reply[1]
        from gpt_gateway.chatgpt import codex_login
        with patch.object(codex_login.os, "replace", side_effect=OSError("disk full")):
            self.assertEqual(self.get_credentials().access_token, rotated["access_token"])
            self.assertEqual(self.read_login(), self.data)
            # A later refresh sends the rotated refresh token held in memory, never the retired one.
            self.assertEqual(self.get_credentials(force=True).access_token, rotated["access_token"])
            sent = [json.loads(raw)["refresh_token"] for _, _, raw in self.fake.calls]
            self.assertEqual(sent, [self.data["tokens"]["refresh_token"], rotated["refresh_token"]])
            self.assertIn("rotated tokens held in memory", self.provider.status())
        self.get_credentials()
        self.assertEqual(self.read_login()["tokens"]["refresh_token"], rotated["refresh_token"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_k5_forced_refresh_reloads_the_threads_revision(self):
        self.write_login()
        first = self.get_credentials()
        self.data["tokens"]["refresh_token"] = "invented-rotated-by-codex"
        self.write_login()
        self.assertEqual(self.get_credentials(True), first)
        self.assertEqual(self.fake.calls, [])
        changed = self.get_credentials(True)
        self.assertEqual(changed.access_token, self.fake.reply[1]["access_token"])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.get_credentials(), changed)

    def test_k6_torn_reads_retry_and_fail_without_fallback(self):
        self.write_login()
        self.get_credentials()
        for broken in ("", '{"tokens":', "invented-secret"):
            self.write_login(broken)
            with patch.object(clock, "sleep", side_effect=lambda _: self.write_login()) as sleep:
                self.assertEqual(self.get_credentials().account_id, "acct-test-0001")
            sleep.assert_called_once_with(0.05)
            self.write_login(broken)
            with patch.object(clock, "sleep") as sleep:
                with self.assertRaisesRegex(auth.AuthError, r"^Cannot read the Codex login at ~/.codex/auth.json$"):
                    self.provider.credentials()
            self.assertEqual(sleep.call_args_list, [((0.05,),)] * 3)
            with open(self.path) as stream:
                self.assertEqual(stream.read(), broken)
        self.assertFalse(os.path.exists(self.knobs.auth_path))
        with patch("builtins.open", side_effect=PermissionError("invented-private-text")):
            with self.assertRaisesRegex(auth.AuthError, r"^Cannot read the Codex login at ~/.codex/auth.json$"):
                self.provider.credentials()
        self.write_login([])
        with self.assertRaises(auth.AuthError):
            self.provider.credentials()

    def test_k7_permanent_refusal_preserves_file_and_accepts_external_rotation(self):
        self.write_login()
        self.get_credentials()
        self.data["tokens"]["access_token"] = jwt({"exp": NOW})
        for status, reply in ((401, {}), (400, {"error": "INVALID_GRANT"}),
                (403, {"error": {"code": "Refresh_Token_Expired"}}),
                (500, {"error": {"type": "REFRESH_TOKEN_REUSED"}}),
                (400, {"error": "Refresh_Token_Invalidated"})):
            self.write_login()
            self.fake.reply = status, reply
            with self.assertRaisesRegex(auth.LoginRequired, r"Codex login expired; run `codex login`"):
                self.provider.credentials()
            self.assertEqual(self.read_login(), self.data)
        def rotate(*args, **kwargs):
            changed = copy.deepcopy(self.data)
            changed["tokens"].update(tokens(NOW + 3600, "external"))
            self.write_login(changed)
            return 401, {}
        with patch.object(auth, "post_token", side_effect=rotate):
            self.assertEqual(self.get_credentials().access_token, tokens(NOW + 3600, "external")["access_token"])

    def test_k8_transient_failure_keeps_valid_access_and_never_writes(self):
        self.write_login()
        self.get_credentials()
        for expiry in (NOW + 200, NOW):
            self.data["tokens"]["access_token"] = jwt({"exp": expiry})
            self.write_login()
            for reply in ((503, {"error": "invented-secret"}), (500, {"error": "invalid_grant"}),
                          auth.AuthError("ChatGPT auth connection failed")):
                with patch.object(auth, "post_token", side_effect=reply if isinstance(reply, Exception) else None,
                                  return_value=reply):
                    if expiry > NOW:
                        self.assertEqual(self.get_credentials().access_token, self.data["tokens"]["access_token"])
                    else:
                        with self.assertRaises(auth.AuthError) as caught:
                            self.provider.credentials()
                        self.assertIs(type(caught.exception), auth.AuthError)
                        self.assertNotIn("invented-secret", str(caught.exception))
                self.assertEqual(self.read_login(), self.data)
        # The gateway's state directory cannot hold the refresh lock: no POST, Codex's file untouched.
        blocked = os.path.join(self.home, "blocked")
        jail.write_json(blocked, {})
        knobs = env.ChatGPTEnv.parse(dict(self.mapping, GPT_GATEWAY_CHATGPT_AUTH_PATH=os.path.join(blocked, "a.json")))
        with self.assertRaises(Exception) as caught:
            auth.source(knobs).credentials()
        self.assertIsInstance(caught.exception, auth.AuthError)
        self.assertEqual(str(caught.exception), "Cannot lock the Codex login refresh")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.read_login(), self.data)

    def test_k9_eight_threads_share_one_refresh(self):
        self.write_login()
        self.get_credentials()
        self.data["tokens"]["access_token"] = jwt({"exp": NOW + 299})
        self.write_login()
        barrier = threading.Barrier(8)
        results, failures = [], []
        def run():
            try:
                barrier.wait(5)
                results.append(self.provider.credentials().access_token)
            except Exception as error:
                failures.append(type(error).__name__)
        workers = [threading.Thread(target=run) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [self.fake.reply[1]["access_token"]] * 8)
        self.assertEqual(len(self.fake.calls), 1)
        # Independent sources emulate separate gateway owners and each thread's 401 revision.
        self.fake.calls.clear()
        self.data["tokens"]["access_token"] = jwt({"exp": NOW + 3600})
        self.write_login()
        results.clear()
        failures.clear()
        def force():
            try:
                provider = auth.source(self.knobs)
                provider.credentials()
                barrier.wait(5)
                results.append(provider.credentials(force_refresh=True).access_token)
            except Exception as error:
                failures.append(type(error).__name__)
        workers = [threading.Thread(target=force) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [self.fake.reply[1]["access_token"]] * 8)
        self.assertEqual(len(self.fake.calls), 1)

    def test_k11_selector_is_dynamic_and_own_store_is_lazy(self):
        self.knobs.auth_store = "keychain"
        with patch.object(auth.Keychain, "available", return_value=False) as probe:
            self.provider = auth.source(self.knobs)
            self.write_login()
            self.assertEqual(self.get_credentials().account_id, "acct-test-0001")
            self.assertIn("Codex login", self.provider.status())
            probe.assert_not_called()
        self.knobs.auth_store = "file"
        os.unlink(self.path)
        with self.assertRaisesRegex(auth.LoginRequired,
                "ChatGPT login required: run codex login, or gateway.py chatgpt-login"):
            self.provider.credentials()
        auth.Store(self.knobs).save(tokens(NOW + 3600, "own"))
        self.assertEqual(self.get_credentials().account_id, "invented-1234")
        self.write_login()
        self.assertEqual(self.get_credentials().account_id, "acct-test-0001")
