"""The jail strips inherited credential discovery paths and keeps the database inside it."""
import os
import unittest
from unittest.mock import patch

from gpt_gateway import env
from tests import jail


class JailTest(unittest.TestCase):
    def test_k12_clean_env_drops_codex_home(self):
        home = jail.scratch(self)
        with patch.dict(os.environ, {"CODEX_HOME": "/invented-external-home",
                                    "GPT_GATEWAY_CHATGPT_AUTH_PATH": "/invented-external-auth"}, clear=True):
            mapping = jail.clean_env(home)
        self.assertNotIn("CODEX_HOME", mapping)
        self.assertNotIn("GPT_GATEWAY_CHATGPT_AUTH_PATH", mapping)
        self.assertEqual(mapping["HOME"], home)

    def test_the_database_stays_inside_the_jail(self):
        home = jail.scratch(self)
        with patch.dict(os.environ, {"HOME": "/invented-real-home", "GPT_GATEWAY_DB": "/invented-real.db"}):
            mapping = jail.clean_env(home)
        self.assertEqual(os.path.dirname(mapping.get("GPT_GATEWAY_DB") or ""), os.path.join(home, "state"))
        self.assertEqual(env.CoreEnv.parse(mapping).db_path, os.path.join(home, "state", "gateway.db"))
