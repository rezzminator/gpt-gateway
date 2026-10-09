"""Tests for model_gateway.runtime: one object wires env, log, settings and the hook chain."""
import os
import subprocess
import unittest

from model_gateway.env import EnvError
from model_gateway.runtime import Runtime
from tests import jail


class RuntimeTest(unittest.TestCase):
    def test_load_builds_the_chain_from_the_settings(self):
        d = jail.scratch(self)
        jail.write_middleware(os.path.join(d, "middleware"), "a", 'NAME = "a"\n')
        jail.write_json(os.path.join(d, "settings.json"), {"middleware": [{"module": "a"}]})
        rt = Runtime({"MODEL_GATEWAY_LOG": os.path.join(d, "gw.log")}, os.path.join(d, "settings.json"))
        self.assertEqual(rt.chain.modules, ())
        rt.load()
        self.assertEqual([m.NAME for m in rt.chain.modules], ["a"])
        self.assertEqual(rt.log.path, os.path.join(d, "gw.log"))

    def test_chatgpt_is_lazy_and_knobs_are_conditional(self):
        from unittest.mock import patch
        import builtins
        d = jail.scratch(self)
        env = {"HOME": d, "MODEL_GATEWAY_LOG": os.path.join(d, "gw.log"),
               "MODEL_GATEWAY_CHATGPT_ORIGIN": "invalid", "MODEL_GATEWAY_CHATGPT_AUTH_STORE": "file"}
        rt = Runtime(env, os.path.join(d, "settings.json"))
        with patch("builtins.__import__", wraps=builtins.__import__) as imports:
            rt.load()
        self.assertIsNone(rt.chatgpt)
        self.assertFalse(any("chatgpt" in call.args[0] for call in imports.call_args_list))
        probe = ("import sys; sys.path.insert(0, %r); from model_gateway import cli, server; "
                 "from model_gateway.runtime import Runtime; Runtime(%r, %r).load(); "
                 "print([m for m in sys.modules if m.startswith('model_gateway.chatgpt')])"
                 % (jail.ROOT, env, os.path.join(d, "settings.json")))
        fresh = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", probe], capture_output=True, text=True,
                               env={"HOME": d, "PATH": "/usr/bin:/bin"})
        self.assertEqual((fresh.returncode, fresh.stdout), (0, "[]\n"), fresh.stderr)
        env["MODEL_GATEWAY_CHATGPT"] = "on"
        with self.assertRaisesRegex(EnvError, "MODEL_GATEWAY_CHATGPT_ORIGIN"):
            Runtime(env, os.path.join(d, "settings.json")).load()
        env["MODEL_GATEWAY_CHATGPT_ORIGIN"] = "http://127.0.0.1:1"
        rt = Runtime(env, os.path.join(d, "settings.json"))
        rt.load()
        self.assertIsNotNone(rt.chatgpt)
        self.assertIsNone(rt.chatgpt.catalog.count())
        env["MODEL_GATEWAY_CHATGPT_AUTH_STORE"] = "keychain"  # a start error, never a traceback out of serve
        with patch("model_gateway.chatgpt.keychain.Keychain.available", return_value=False), \
                self.assertRaises(Exception) as caught:
            Runtime(env, os.path.join(d, "settings.json")).load()
        self.assertIsInstance(caught.exception, EnvError)
        self.assertIn("MODEL_GATEWAY_CHATGPT_AUTH_STORE: ChatGPT Keychain unavailable", str(caught.exception))

    def test_a_bad_env_stops_it(self):
        with self.assertRaises(EnvError):
            Runtime({"MODEL_GATEWAY_PORT": "x"}, "/nonexistent/settings.json")


if __name__ == "__main__":
    unittest.main()
