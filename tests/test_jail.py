"""The jail strips inherited credential discovery paths before code sees its environment."""
import os
import unittest
from unittest.mock import patch

from tests import jail


class JailTest(unittest.TestCase):
    def test_k12_clean_env_drops_codex_home(self):
        home = jail.scratch(self)
        with patch.dict(os.environ, {"CODEX_HOME": "/invented-external-home",
                                    "MODEL_GATEWAY_CHATGPT_AUTH_PATH": "/invented-external-auth"}, clear=True):
            mapping = jail.clean_env(home)
        self.assertNotIn("CODEX_HOME", mapping)
        self.assertNotIn("MODEL_GATEWAY_CHATGPT_AUTH_PATH", mapping)
        self.assertEqual(mapping["HOME"], home)
