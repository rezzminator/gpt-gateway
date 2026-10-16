"""Tests for gpt_gateway.runtime: one object wires env, log, settings and the hook chain."""
import os
import subprocess
import unittest

from gpt_gateway.env import EnvError
from gpt_gateway.runtime import Runtime
from tests import jail


class RuntimeTest(unittest.TestCase):
    def test_load_builds_the_chain_from_the_settings(self):
        d = jail.scratch(self)
        jail.write_middleware(os.path.join(d, "middleware"), "a", 'NAME = "a"\n')
        jail.write_json(os.path.join(d, "settings.json"), {"middleware": [{"module": "a"}]})
        rt = Runtime({"GPT_GATEWAY_LOG": os.path.join(d, "gw.log"), "GPT_GATEWAY_DB": os.path.join(d, "gateway.db")},
                     os.path.join(d, "settings.json"))
        self.assertEqual(rt.chain.modules, ())
        rt.load()
        self.addCleanup(rt.db.close)
        self.assertEqual([m.NAME for m in rt.chain.modules], ["a"])
        self.assertEqual((rt.log.path, rt.db.path), (os.path.join(d, "gw.log"), os.path.join(d, "gateway.db")))
        self.assertEqual((rt.db.thread is not None and rt.db.thread.is_alive(), rt.db.flush()), (True, True))
        from unittest import mock
        with mock.patch.object(rt.settings, "current", side_effect=AssertionError("a settings reload on the writer")):
            self.assertEqual(rt.db.retention(), 30)  # the writer reads the snapshot the requests keep current

    def test_chatgpt_is_lazy_and_knobs_are_conditional(self):
        from unittest.mock import patch
        import builtins
        d = jail.scratch(self)
        env = {"HOME": d, "GPT_GATEWAY_LOG": os.path.join(d, "gw.log"),  # the database under this HOME
               "GPT_GATEWAY_CHATGPT_ORIGIN": "invalid", "GPT_GATEWAY_CHATGPT_AUTH_STORE": "file"}
        rt = Runtime(env, os.path.join(d, "settings.json"))
        with patch("builtins.__import__", wraps=builtins.__import__) as imports:
            rt.load()
        self.assertIsNone(rt.chatgpt)
        self.assertFalse(any("chatgpt" in call.args[0] for call in imports.call_args_list))
        probe = ("import sys; sys.path.insert(0, %r); from gpt_gateway import cli, server; "
                 "from gpt_gateway.runtime import Runtime; Runtime(%r, %r).load(); "
                 "print([m for m in sys.modules if m.startswith('gpt_gateway.chatgpt')])"
                 % (jail.ROOT, env, os.path.join(d, "settings.json")))
        fresh = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", probe], capture_output=True, text=True,
                               env={"HOME": d, "PATH": "/usr/bin:/bin"})
        self.assertEqual((fresh.returncode, fresh.stdout), (0, "[]\n"), fresh.stderr)
        env["GPT_GATEWAY_CHATGPT"] = "on"
        with self.assertRaisesRegex(EnvError, "GPT_GATEWAY_CHATGPT_ORIGIN"):
            Runtime(env, os.path.join(d, "settings.json")).load()
        env["GPT_GATEWAY_CHATGPT_ORIGIN"] = "http://127.0.0.1:1"
        with self.assertRaisesRegex(EnvError, "GPT_GATEWAY_ROUTE_SECRET_PATH"):
            Runtime(env, os.path.join(d, "settings.json")).load()
        secret = jail.write_secret(os.path.join(d, "route-secret"), "a" * 64)
        env["GPT_GATEWAY_ROUTE_SECRET_PATH"] = secret
        for mode, text, message in ((0o644, "a" * 64, "regular 0600"), (0o600, "A" * 64, "64 lowercase hex"),
                                    (0o600, "a" * 63, "64 lowercase hex")):
            with self.subTest(mode=oct(mode), text=text):
                jail.write_json(secret, text)
                os.chmod(secret, mode)
                with self.assertRaisesRegex(EnvError, message):
                    Runtime(env, os.path.join(d, "settings.json")).load()
        jail.write_json(secret, "a" * 64)
        os.chmod(secret, 0o600)
        rt = Runtime(env, os.path.join(d, "settings.json"))
        rt.load()
        self.assertEqual(rt.route_secret, "a" * 64)
        self.assertIsNotNone(rt.chatgpt)
        self.assertIsNone(rt.chatgpt.catalog.count())
        from gpt_gateway.chatgpt.auth import AuthError
        env["GPT_GATEWAY_CHATGPT_AUTH_STORE"] = "keychain"
        with patch("gpt_gateway.chatgpt.keychain.Keychain.available", return_value=False) as probe:
            rt = Runtime(env, os.path.join(d, "settings.json"))
            rt.load()
            probe.assert_not_called()
            self.assertIsNotNone(rt.chatgpt)
            with self.assertRaises(AuthError) as caught:
                rt.chatgpt.store.credentials()
            probe.assert_called_once_with()
        self.assertEqual(str(caught.exception), "ChatGPT Keychain unavailable")

    def test_a_bad_env_stops_it(self):
        for mapping in ({"GPT_GATEWAY_PORT": "x"}, {}):  # {}: no HOME to place the database under
            with self.subTest(mapping=mapping), self.assertRaises(EnvError):
                Runtime(mapping, "/nonexistent/settings.json")


if __name__ == "__main__":
    unittest.main()
