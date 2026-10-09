"""The Keychain execution boundary is replaced in every test."""
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import jail


class KeychainTest(unittest.TestCase):
    def setUp(self):
        try:
            self.keychain = importlib.import_module("model_gateway.chatgpt.keychain")
        except ImportError:
            self.fail("A3: Keychain store is not implemented")
        self.home = jail.scratch(self)

    def test_service_is_ours_and_read_write_use_the_execution_facade(self):
        with patch.object(self.keychain, "security", return_value=SimpleNamespace(returncode=0, stdout='{"x":1}')) as run:
            store = self.keychain.Keychain()
            self.assertEqual(store.read(), {"x": 1})
            store.write({"x": 2})
        self.assertIn("model-gateway.chatgpt", run.call_args_list[0].args[0])
        self.assertIn("model-gateway.chatgpt", run.call_args_list[1].args[0])
        self.assertEqual(json.loads(run.call_args_list[1].args[0][-1]), {"x": 2})

    def test_missing_item_is_empty_but_denial_is_an_error(self):
        store = self.keychain.Keychain()
        with patch.object(self.keychain, "security", return_value=SimpleNamespace(returncode=44, stdout="")):
            self.assertIsNone(store.read())
        with patch.object(self.keychain, "security", return_value=SimpleNamespace(returncode=36, stdout="secret")):
            with self.assertRaisesRegex(self.keychain.KeychainError, "unavailable"):
                store.read()
            with self.assertRaises(self.keychain.KeychainError):
                store.write({"secret": "secret"})

    def test_file_selection_never_probes_keychain_and_auto_probes_once(self):
        from model_gateway import env
        from model_gateway.chatgpt.auth import Store
        with patch.object(self.keychain.Keychain, "available", return_value=False) as probe:
            knobs = env.ChatGPTEnv.parse(jail.clean_env(self.home, MODEL_GATEWAY_CHATGPT_AUTH_STORE="file"))
            Store(knobs)
            probe.assert_not_called()
            knobs.auth_store = "auto"
            Store(knobs)
            probe.assert_called_once_with()

    def test_security_binary_boundary_has_a_timeout_and_no_shell(self):
        with patch.object(self.keychain.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.keychain.security(["default-keychain", "-d", "user"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/security", "default-keychain", "-d", "user"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertNotIn("shell", run.call_args.kwargs)
