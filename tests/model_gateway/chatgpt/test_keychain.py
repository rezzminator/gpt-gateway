"""The Keychain execution boundary is replaced in every test."""
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch



class KeychainTest(unittest.TestCase):
    def setUp(self):
        try:
            self.keychain = importlib.import_module("model_gateway.chatgpt.keychain")
        except ImportError:
            self.fail("A3: Keychain store is not implemented")

    def test_service_is_ours_and_read_write_use_the_execution_facade(self):
        secret = json.dumps({"x": 2}, separators=(",", ":"))
        encoded = secret.encode().hex()
        replies = [SimpleNamespace(returncode=0, stdout='{"x":1}'),
                   SimpleNamespace(returncode=0, stdout=""),
                   SimpleNamespace(returncode=0, stdout=secret)]
        with patch.object(self.keychain.subprocess, "run", side_effect=replies) as run:
            store = self.keychain.Keychain()
            self.assertEqual(store.read(), {"x": 1})
            store.write({"x": 2})
        self.assertEqual(run.call_count, 3)
        read, write, readback = run.call_args_list
        expected_read = ["/usr/bin/security", "find-generic-password", "-s", "model-gateway.chatgpt",
                         "-a", "tokens", "-w"]
        self.assertEqual(read.args[0], expected_read)
        self.assertEqual(readback.args[0], expected_read)
        self.assertEqual(write.args[0], ["/usr/bin/security", "-i"])
        self.assertEqual(write.kwargs["input"],
                         "add-generic-password -U -s model-gateway.chatgpt -a tokens -X " + encoded + "\n")
        self.assertEqual(json.loads(bytes.fromhex(write.kwargs["input"].split()[-1])), {"x": 2})
        for call in run.call_args_list:
            for argument in call.args[0]:
                self.assertNotIn(secret, argument)
                self.assertNotIn(encoded, argument)
        success = SimpleNamespace(returncode=0, stdout="")
        for value in ("{}", "invalid"):  # security -i can exit 0 on a failed command; the readback decides
            with patch.object(self.keychain.subprocess, "run",
                              side_effect=[success, SimpleNamespace(returncode=0, stdout=value)]):
                with self.assertRaisesRegex(self.keychain.KeychainError, "^ChatGPT Keychain write failed$"):
                    store.write({"x": 2})

    def test_a_command_line_security_would_split_never_reaches_it(self):
        # security -i reads 4095 bytes per line; the rest runs as further commands, so the first
        # fragment would store a truncated item over the old one. A real login's JWTs hex past 8 KB.
        store = self.keychain.Keychain()
        login = {"access_token": "a" * 1900, "refresh_token": "invented-refresh",
                 "id_token": "i" * 1900, "account_id": "acct-test-0001"}
        echo = SimpleNamespace(returncode=0, stdout=json.dumps(login, separators=(",", ":")))
        with patch.object(self.keychain.subprocess, "run", side_effect=[echo, echo]) as run:
            with self.assertRaisesRegex(self.keychain.KeychainError, "^ChatGPT Keychain write failed$"):
                store.write(login)
        self.assertFalse([call for call in run.call_args_list
                          if any(len(line) > 4095 for line in (call.kwargs.get("input") or "").splitlines(True))])
        small = {"access_token": "a" * 1900}
        echo = SimpleNamespace(returncode=0, stdout=json.dumps(small, separators=(",", ":")))
        with patch.object(self.keychain.subprocess, "run", side_effect=[echo, echo]) as run:
            store.write(small)
        self.assertLessEqual(len(run.call_args_list[0].kwargs["input"]), 4095)

    def test_missing_item_is_empty_but_denial_is_an_error(self):
        store = self.keychain.Keychain()
        with patch.object(self.keychain, "security", return_value=SimpleNamespace(returncode=44, stdout="")):
            self.assertIsNone(store.read())
        with patch.object(self.keychain, "security", return_value=SimpleNamespace(returncode=36, stdout="secret")):
            with self.assertRaisesRegex(self.keychain.KeychainError, "unavailable"):
                store.read()
            with self.assertRaises(self.keychain.KeychainError):
                store.write({"secret": "secret"})

    def test_security_binary_boundary_has_a_timeout_and_no_shell(self):
        with patch.object(self.keychain.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.keychain.security(["default-keychain", "-d", "user"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/security", "default-keychain", "-d", "user"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertNotIn("shell", run.call_args.kwargs)
