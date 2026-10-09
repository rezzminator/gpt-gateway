"""Tests for model_gateway.runtime: one object wires env, log, settings and the hook chain."""
import os
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

    def test_a_bad_env_stops_it(self):
        with self.assertRaises(EnvError):
            Runtime({"MODEL_GATEWAY_PORT": "x"}, "/nonexistent/settings.json")


if __name__ == "__main__":
    unittest.main()
