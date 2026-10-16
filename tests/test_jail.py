"""The jail strips inherited credential discovery paths and keeps the database inside it."""
import os
import subprocess
import unittest
from unittest.mock import patch

from gpt_gateway import env
from tests import jail


class JailTest(unittest.TestCase):
    def test_k12_clean_env_drops_codex_home(self):
        home = jail.scratch(self)
        with patch.dict(os.environ, {"CODEX_HOME": "/invented-external-home",
                                    "GPT_GATEWAY_CHATGPT_AUTH_PATH": "/invented-external-auth",
                                    "PLUGIN_LOG_DIR": "/invented-external-logs"}, clear=True):
            mapping = jail.clean_env(home)
        self.assertNotIn("PLUGIN_LOG_DIR", mapping)
        self.assertNotIn("CODEX_HOME", mapping)
        self.assertNotIn("GPT_GATEWAY_CHATGPT_AUTH_PATH", mapping)
        self.assertEqual(mapping["HOME"], home)

    def test_the_database_stays_inside_the_jail(self):
        home = jail.scratch(self)
        with patch.dict(os.environ, {"HOME": "/invented-real-home", "GPT_GATEWAY_DB": "/invented-real.db"}):
            mapping = jail.clean_env(home)
        self.assertEqual(os.path.dirname(mapping.get("GPT_GATEWAY_DB") or ""), os.path.join(home, "state"))
        self.assertEqual(env.CoreEnv.parse(mapping).db_path, os.path.join(home, "state", "gateway.db"))

    def test_a_mode_zero_file_is_unreadable_to_the_demoted_run_even_under_root(self):
        path = jail.scratch(self)
        locked = os.path.join(path, "locked")
        with open(locked, "w") as f:
            f.write("x")
        os.chmod(locked, 0)
        demoted = jail.demote(self, path)
        done = subprocess.run(["/bin/cat", locked], capture_output=True, **demoted)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        own = subprocess.run(["/bin/cat", os.path.join(jail.ROOT, "tests", "jail.py")], capture_output=True, **demoted)
        self.assertEqual(own.returncode, 0, own.stderr)  # the checkout's code stays readable to the demoted child
