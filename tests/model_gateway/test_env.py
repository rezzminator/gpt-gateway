"""Tests for model_gateway.env: the core knobs, their defaults, and refusal by name."""
import os
import unittest

from model_gateway import env


class CoreEnvTest(unittest.TestCase):
    def test_chatgpt_origins_store_and_paths_are_validated(self):
        for name, value in (("MODEL_GATEWAY_CHATGPT_ORIGIN", "ftp://invalid"),
                            ("MODEL_GATEWAY_CHATGPT_ORIGIN", "http://host/path"),
                            ("MODEL_GATEWAY_CHATGPT_MODELS_PATH", "relative"),
                            ("MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN", "ftp://example.test"),
                            ("MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN", "https://example.test/path"),
                            ("MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN", "http://u:p@example.test"),
                            ("MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN", "http://a:bad"),
                            ("MODEL_GATEWAY_CHATGPT_AUTH_STORE", "unknown")):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(env.EnvError, name):
                env.ChatGPTEnv.parse({"HOME": "/invented", name: value})
        knobs = env.ChatGPTEnv.parse({"HOME": "/invented"})
        self.assertEqual((knobs.origin, knobs.auth_origin, knobs.auth_store),
                         ("https://chatgpt.com", "https://auth.openai.com", "auto"))
        self.assertEqual(knobs.models_path, "/invented/.local/state/model-gateway/chatgpt-models.json")
        self.assertEqual(knobs.auth_path, "/invented/.local/state/model-gateway/chatgpt-auth.json")

    def test_defaults(self):
        core = env.CoreEnv.parse({})
        self.assertEqual((core.port, core.upstream, core.drain_seconds, core.debug),
                         (18787, "https://api.anthropic.com", 600.0, False))
        self.assertEqual(core.log_path, os.path.expanduser("~/Library/Logs/model-gateway.log"))

    def test_every_knob_is_read_under_its_prefix(self):
        core = env.CoreEnv.parse({"MODEL_GATEWAY_PORT": "1234", "MODEL_GATEWAY_UPSTREAM": "http://127.0.0.1:9",
                                  "MODEL_GATEWAY_LOG": "/x/gw.log", "MODEL_GATEWAY_DRAIN_SECONDS": "0.5",
                                  "MODEL_GATEWAY_DEBUG": "1"})
        self.assertEqual((core.port, core.upstream, core.log_path, core.drain_seconds, core.debug),
                         (1234, "http://127.0.0.1:9", "/x/gw.log", 0.5, True))

    def test_an_empty_value_means_unset(self):
        self.assertEqual(env.CoreEnv.parse({"MODEL_GATEWAY_PORT": "  "}).port, 18787)
        self.assertIsNone(env.value({"X": " "}, "X"))
        self.assertEqual(env.value({"X": " a "}, "X"), "a")

    def test_a_bad_value_is_refused_by_name(self):
        for name, raw in (("MODEL_GATEWAY_PORT", "abc"), ("MODEL_GATEWAY_PORT", "0"), ("MODEL_GATEWAY_PORT", "70000"),
                          ("MODEL_GATEWAY_UPSTREAM", "ftp://x"), ("MODEL_GATEWAY_UPSTREAM", "http://h:port"),
                          ("MODEL_GATEWAY_DRAIN_SECONDS", "-1"), ("MODEL_GATEWAY_DRAIN_SECONDS", "soon")):
            with self.assertRaises(env.EnvError) as cm:
                env.CoreEnv.parse({name: raw})
            self.assertIn("env " + name, str(cm.exception))

    def test_snapshot_is_a_copy(self):
        snap = env.snapshot()
        snap["MODEL_GATEWAY_TEST_ONLY"] = "1"
        self.assertNotIn("MODEL_GATEWAY_TEST_ONLY", env.snapshot())


if __name__ == "__main__":
    unittest.main()
