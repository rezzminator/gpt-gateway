"""Tests for gpt_gateway.middleware: every load error stops the start, naming the module and the cause."""
import os
import sys
import unittest

from gpt_gateway.middleware import LOADER_PREFIX, MiddlewareError, load_module, load_modules
from tests import jail


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(jail.scratch(self), "middleware")

    def write(self, module, source):
        jail.write_middleware(self.dir, module, source)

    def refused(self, names, message):
        with self.assertRaises(MiddlewareError) as cm:
            load_modules(names, self.dir)
        self.assertIn(message, str(cm.exception))
        return str(cm.exception)

    def test_loads_under_the_contract_name_with_only_NAME(self):
        self.write("plain", 'NAME = "plain"\n')
        (m,) = load_modules(["plain"], self.dir)
        self.assertEqual((m.module, m.NAME, m.spec, m.check, m.on_request, m.errors), ("plain", "plain", {}, None, None, 0))
        self.assertEqual(sys.modules[LOADER_PREFIX + "plain"].NAME, "plain")
        self.assertEqual(LOADER_PREFIX, "gpt_gateway_mw_")

    def test_a_missing_file(self):
        self.assertIn("middleware ghost: no module file", self.refused(["ghost"], "ghost.py"))

    def test_an_import_error(self):
        self.write("broken", 'NAME = "broken"\nimport no_such_module_anywhere\n')
        self.refused(["broken"], "middleware broken: import of")
        self.assertNotIn(LOADER_PREFIX + "broken", sys.modules)
        self.write("exits", "import sys\nsys.exit(3)\n")
        self.refused(["exits"], "SystemExit(3)")

    def test_a_missing_or_bad_NAME(self):
        self.write("nameless", "X = 1\n")
        self.refused(["nameless"], "middleware nameless: NAME must be a string")
        self.write("spaced", 'NAME = "has space"\n')
        self.refused(["spaced"], "NAME must be a string")

    def test_a_duplicate_NAME(self):
        self.write("one", 'NAME = "same"\n')
        self.write("two", 'NAME = "same"\n')
        self.refused(["one", "two"], "middleware one and two: duplicate NAME 'same'")

    def test_a_hook_that_is_not_callable(self):
        self.write("odd", 'NAME = "odd"\non_request = 3\n')
        self.refused(["odd"], "middleware odd: on_request must be callable")
        self.write("oddcheck", 'NAME = "oddcheck"\ncheck = "x"\n')
        self.refused(["oddcheck"], "check must be callable")

    def test_a_bad_SETTINGS_shape(self):
        for source, message in (('SETTINGS = []', "SETTINGS must be a dict"),
                                ('SETTINGS = {"a": (1, int)}', "must be (default, validator, env_var_or_None)"),
                                ('SETTINGS = {"a": (1, 2, None)}', "validator is not callable"),
                                ('SETTINGS = {"a": (1, int, "")}', "env variable must be a non-empty string"),
                                ('SETTINGS = {3: (1, int, None)}', "must be a non-empty string")):
            self.write("shape", 'NAME = "shape"\n' + source + "\n")
            self.refused(["shape"], message)

    def test_hooks_are_picked_up(self):
        self.write("full", 'NAME = "full"\nSETTINGS = {"a": (1, int, "GPT_GATEWAY_FULL_A")}\n'
                           "def check(s): pass\ndef on_request(ctx, req): pass\ndef on_event(ctx, e): pass\n"
                           "def on_response_end(ctx, r): pass\ndef health(ctx): return 'h'\ndef cli(argv, ctx): return 0\n")
        m = load_module("full", self.dir)
        for hook in ("check", "on_request", "on_event", "on_response_end", "health", "cli"):
            self.assertTrue(callable(getattr(m, hook)), hook)
        self.assertEqual(m.spec["a"][2], "GPT_GATEWAY_FULL_A")
        m.failed()
        self.assertEqual(m.errors, 1)


if __name__ == "__main__":
    unittest.main()
