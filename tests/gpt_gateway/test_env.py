"""Tests for gpt_gateway.env: the core knobs, their defaults, and refusal by name."""
import os
import unittest
from unittest.mock import patch

from gpt_gateway import env
from tests import jail


class CoreEnvTest(unittest.TestCase):
    def test_the_inherited_listener_descriptor_is_validated(self):
        core = env.CoreEnv.parse({'HOME': '/jail', 'GPT_GATEWAY_LISTENER_FD': '3'})
        self.assertEqual(core.listener_fd, 3)
        for value in ('0', '2', '-1', 'word'):
            with self.subTest(value=value), self.assertRaisesRegex(env.EnvError, 'GPT_GATEWAY_LISTENER_FD'):
                env.CoreEnv.parse({'HOME': '/jail', 'GPT_GATEWAY_LISTENER_FD': value})

    def test_chatgpt_origins_store_and_paths_are_validated(self):
        for name, value in (("GPT_GATEWAY_CHATGPT_ORIGIN", "ftp://invalid"),
                            ("GPT_GATEWAY_CHATGPT_ORIGIN", "http://host/path"),
                            ("GPT_GATEWAY_CHATGPT_MODELS_PATH", "relative"),
                            ("GPT_GATEWAY_CHATGPT_AUTH_ORIGIN", "ftp://example.test"),
                            ("GPT_GATEWAY_CHATGPT_AUTH_ORIGIN", "https://example.test/path"),
                            ("GPT_GATEWAY_CHATGPT_AUTH_ORIGIN", "http://u:p@example.test"),
                            ("GPT_GATEWAY_CHATGPT_AUTH_ORIGIN", "http://a:bad"),
                            ("GPT_GATEWAY_CHATGPT_AUTH_STORE", "unknown")):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(env.EnvError, name):
                env.ChatGPTEnv.parse({"HOME": "/invented", name: value})
        knobs = env.ChatGPTEnv.parse({"HOME": "/invented"})
        self.assertEqual((knobs.origin, knobs.auth_origin, knobs.auth_store),
                         ("https://chatgpt.com", "https://auth.openai.com", "auto"))
        self.assertEqual(knobs.models_path, "/invented/.local/state/gpt-gateway/chatgpt-models.json")
        self.assertEqual(knobs.auth_path, "/invented/.local/state/gpt-gateway/chatgpt-auth.json")

    def test_k1_codex_path_precedence_and_symlink(self):
        home = jail.scratch(self)
        cases = (({}, os.path.join(home, ".codex", "auth.json")),
                 ({"CODEX_HOME": os.path.join(home, "custom")}, os.path.join(home, "custom", "auth.json")),
                 ({"CODEX_HOME": ""}, os.path.join(home, ".codex", "auth.json")),
                 ({"CODEX_HOME": os.path.join(home, "custom"), "GPT_GATEWAY_CHATGPT_CODEX_AUTH": "~/chosen"},
                  os.path.join(home, "chosen")),
                 ({"GPT_GATEWAY_CHATGPT_CODEX_AUTH": "off"}, None))
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                knobs = env.ChatGPTEnv.parse(jail.clean_env(home, **overrides))
                self.assertEqual(knobs.codex_auth, expected)
        for name in ("GPT_GATEWAY_CHATGPT_CODEX_AUTH", "CODEX_HOME"):  # the error names the variable set
            with self.assertRaisesRegex(env.EnvError, "^env %s: must be an absolute path$" % name):
                env.ChatGPTEnv.parse(jail.clean_env(home, **{name: "relative"}))
        os.symlink(os.path.join(home, "target"), os.path.join(home, "chosen"))
        knobs = env.ChatGPTEnv.parse(jail.clean_env(home, GPT_GATEWAY_CHATGPT_CODEX_AUTH="~/chosen"))
        self.assertEqual(knobs.codex_auth, os.path.join(home, "chosen"))

    def test_defaults(self):
        core = env.CoreEnv.parse({"HOME": "/jail"})
        self.assertEqual((core.port, core.upstream, core.drain_seconds, core.debug),
                         (18787, "https://api.anthropic.com", 600.0, False))
        self.assertEqual(core.db_path, "/jail/.local/state/gpt-gateway/gateway.db")

    def test_the_database_resolves_against_the_mappings_home_never_the_process_home(self):
        with patch.dict(os.environ, {"HOME": "/process-home"}):
            self.assertEqual([env.CoreEnv.parse(m).db_path for m in (
                {"HOME": "/jail"}, {"HOME": "/jail", "GPT_GATEWAY_DB": "~/x/gw.db"},
                {"GPT_GATEWAY_DB": "/abs/gw.db"})],
                ["/jail/.local/state/gpt-gateway/gateway.db", "/jail/x/gw.db", "/abs/gw.db"])
            for mapping, named in (({}, "env HOME"), ({"HOME": "relative"}, "env HOME"),
                                   ({"HOME": "/jail", "GPT_GATEWAY_DB": "gw.db"}, "env GPT_GATEWAY_DB")):
                with self.subTest(mapping=mapping), self.assertRaises(env.EnvError) as cm:
                    env.CoreEnv.parse(mapping)
                self.assertIn(named, str(cm.exception))

    def test_every_knob_is_read_under_its_prefix(self):
        core = env.CoreEnv.parse({"GPT_GATEWAY_PORT": "1234", "GPT_GATEWAY_UPSTREAM": "http://127.0.0.1:9",
                                  "GPT_GATEWAY_DRAIN_SECONDS": "0.5",
                                  "GPT_GATEWAY_DEBUG": "1", "GPT_GATEWAY_DB": "/x/gw.db"})
        self.assertEqual((core.port, core.upstream, core.drain_seconds, core.debug, core.db_path),
                         (1234, "http://127.0.0.1:9", 0.5, True, "/x/gw.db"))

    def test_an_empty_value_means_unset(self):
        self.assertEqual(env.CoreEnv.parse({"GPT_GATEWAY_PORT": "  ", "HOME": "/jail"}).port, 18787)
        self.assertIsNone(env.value({"X": " "}, "X"))
        self.assertEqual(env.value({"X": " a "}, "X"), "a")

    def test_a_bad_value_is_refused_by_name(self):
        for name, raw in (("GPT_GATEWAY_PORT", "abc"), ("GPT_GATEWAY_PORT", "0"), ("GPT_GATEWAY_PORT", "70000"),
                          ("GPT_GATEWAY_UPSTREAM", "ftp://x"), ("GPT_GATEWAY_UPSTREAM", "http://h:port"),
                          ("GPT_GATEWAY_DRAIN_SECONDS", "-1"), ("GPT_GATEWAY_DRAIN_SECONDS", "soon")):
            with self.assertRaises(env.EnvError) as cm:
                env.CoreEnv.parse({"HOME": "/jail", name: raw})
            self.assertIn("env " + name, str(cm.exception))

    def test_the_chatgpt_transport_is_websocket_unless_set_to_http(self):
        home = jail.scratch(self)
        self.assertEqual(env.ChatGPTEnv.parse(jail.clean_env(home)).transport, "websocket")
        self.assertEqual(env.ChatGPTEnv.parse(jail.clean_env(home, GPT_GATEWAY_CHATGPT_TRANSPORT=" http ")).transport, "http")
        with self.assertRaisesRegex(env.EnvError, "^env GPT_GATEWAY_CHATGPT_TRANSPORT: must be websocket or http$"):
            env.ChatGPTEnv.parse(jail.clean_env(home, GPT_GATEWAY_CHATGPT_TRANSPORT="sse"))

    def test_the_chatgpt_agent_contract_is_on_unless_set_to_off(self):
        home = jail.scratch(self)
        self.assertTrue(env.ChatGPTEnv.parse(jail.clean_env(home)).agent_contract)
        self.assertFalse(env.ChatGPTEnv.parse(jail.clean_env(home, GPT_GATEWAY_CHATGPT_AGENT_CONTRACT=" off ")).agent_contract)
        with self.assertRaisesRegex(env.EnvError, "^env GPT_GATEWAY_CHATGPT_AGENT_CONTRACT: must be on or off$"):
            env.ChatGPTEnv.parse(jail.clean_env(home, GPT_GATEWAY_CHATGPT_AGENT_CONTRACT="yes"))

    def test_snapshot_is_a_copy(self):
        snap = env.snapshot()
        snap["GPT_GATEWAY_TEST_ONLY"] = "1"
        self.assertNotIn("GPT_GATEWAY_TEST_ONLY", env.snapshot())


if __name__ == "__main__":
    unittest.main()
