"""Tests for model_gateway.cli: serve's strict start, check, mw, and the usage built from COMMANDS.

Each runs gateway.py as a process from a staged install, as run.sh does.
"""
import os
import subprocess
import unittest

from model_gateway import cli
from tests import jail


class ChatGPTStatsTest(unittest.TestCase):
    def test_lane_totals_empty_and_unreadable(self):
        import contextlib
        import io
        d = jail.scratch(self)
        path = os.path.join(d, "gw.log")
        with open(path, "w") as f:
            f.write("stamp    gpt lane=b slug=gpt-test status=200 input=100 cached=60 ratio=0.600 attempts=1\n")
            f.write("stamp    gpt lane=a slug=gpt-test status=200 input=20 cached=0 ratio=0.000 attempts=1\n")
        with open(path + ".1", "w") as f:
            f.write("stamp    gpt lane=b slug=gpt-test status=200 input=50 cached=40 ratio=0.800 attempts=1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["chatgpt-stats"], d, {"MODEL_GATEWAY_LOG": path})
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().splitlines(), ["a responses=1 input=20 cached=0 hit=0.000",
                         "b responses=2 input=150 cached=100 hit=0.667",
                         "total responses=3 input=170 cached=100 hit=0.588"])
        with open(path, "w"):
            pass
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["chatgpt-stats", path], d, {}), 0)
        self.assertIn("no gpt responses in " + path, out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["chatgpt-stats", path + ".missing"], d, {}), 1)
        self.assertIn(path + ".missing", err.getvalue())
        os.unlink(path + ".1")  # a log that has not rotated yet: the default reads what exists
        with open(path, "w") as f:
            f.write("stamp    gpt lane=a slug=gpt-test status=200 input=20 cached=5 ratio=0.250 attempts=1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["chatgpt-stats"], d, {"MODEL_GATEWAY_LOG": path}), 0)
        self.assertEqual(out.getvalue().splitlines()[0], "a responses=1 input=20 cached=5 hit=0.250")

MW_CLI = '''
NAME = "tool"
SETTINGS = {"greeting": ("hi", str, "MODEL_GATEWAY_TOOL_GREETING")}

def cli(argv, ctx):
    ctx.log("tool ran " + " ".join(argv))
    print("%s %s log=%s" % (ctx.settings["greeting"], ",".join(argv), ctx.log_path))
    return int(argv[0]) if argv else 0
'''


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = jail.scratch(self)
        self.app = jail.stage_install(os.path.join(self.tmp, "app"))
        self.settings = os.path.join(self.app, "settings.json")
        self.log = os.path.join(self.tmp, "gw.log")

    def run_gateway(self, *args, **env):
        env = jail.clean_env(self.tmp, MODEL_GATEWAY_PORT=str(jail.free_port()), MODEL_GATEWAY_LOG=self.log, **env)
        p = subprocess.run(["/usr/bin/python3", "-I", "-B", os.path.join(self.app, "gateway.py")] + list(args),
                           env=env, capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout, p.stderr

    def logged(self):
        with open(self.log) as f:
            return f.read()


class ServeTest(CliCase):
    def test_a_bad_file_at_start_exits_non_zero_and_says_why(self):
        jail.write_json(self.settings, {"capture_dir": "relative"})
        code, _, err = self.run_gateway()
        self.assertEqual(code, 1)
        self.assertIn("model-gateway: settings error: %s: capture_dir must be an absolute path" % self.settings, err)
        self.assertIn("start failed: settings error", self.logged())

    def test_a_middleware_that_cannot_load_stops_the_start_naming_it(self):
        jail.write_json(self.settings, {"middleware": [{"module": "broken"}]})
        jail.write_middleware(os.path.join(self.app, "middleware"), "broken", "raise ImportError('nope')\n")
        code, _, err = self.run_gateway("serve")
        self.assertEqual(code, 1)
        self.assertIn("model-gateway: middleware error: middleware broken: import of", err)
        self.assertIn("ImportError('nope')", self.logged())

    def test_a_failing_check_stops_the_start(self):
        jail.write_json(self.settings, {"middleware": [{"module": "strict"}]})
        jail.write_middleware(os.path.join(self.app, "middleware"), "strict",
                              'NAME = "strict"\ndef check(s):\n    raise ValueError("no key file")\n')
        code, _, err = self.run_gateway()
        self.assertEqual(code, 1)
        self.assertIn("middleware strict: check failed: no key file", err)

    def test_a_bad_env_value_stops_the_start(self):
        code, _, err = self.run_gateway(MODEL_GATEWAY_DRAIN_SECONDS="soon")
        self.assertEqual(code, 1)
        self.assertIn("env MODEL_GATEWAY_DRAIN_SECONDS", err)


class CheckTest(CliCase):
    def test_check_validates_a_file_and_its_middleware(self):
        jail.write_json(self.settings, {"capture_dir": None})
        self.assertEqual(self.run_gateway("check", self.settings)[:2], (0, "ok: %s\n" % self.settings))
        self.assertEqual(self.run_gateway("check")[:2], (0, "ok: %s\n" % self.settings))
        os.remove(self.settings)
        self.assertEqual(self.run_gateway("check")[:2], (0, "ok: %s missing, defaults apply\n" % self.settings))
        jail.write_json(self.settings, {"capture_dir": None, "extra": 1})
        code, _, err = self.run_gateway("check", self.settings)
        self.assertEqual((code, "unknown key(s) extra" in err), (1, True))
        jail.write_json(self.settings, {"middleware": [{"module": "ghost"}]})
        code, _, err = self.run_gateway("check")
        self.assertEqual(code, 1)
        self.assertIn("invalid: middleware ghost: no module file", err)

    def test_check_reports_a_bad_module_setting(self):
        jail.write_middleware(os.path.join(self.app, "middleware"), "tool", MW_CLI)
        jail.write_json(self.settings, {"middleware": [{"module": "tool", "settings": {"greting": "x"}}]})
        code, _, err = self.run_gateway("check")
        self.assertEqual(code, 1)
        self.assertIn("middleware tool: unknown setting(s) greting", err)


class MwTest(CliCase):
    def setUp(self):
        super().setUp()
        jail.write_middleware(os.path.join(self.app, "middleware"), "tool", MW_CLI)
        jail.write_json(self.settings, {"middleware": [{"module": "tool", "settings": {"greeting": "hello"}}]})

    def test_mw_runs_the_cli_and_exits_with_its_value(self):
        code, out, _ = self.run_gateway("mw", "tool", "3", "x")
        self.assertEqual((code, out), (3, "hello 3,x log=%s\n" % self.log))
        self.assertIn("tool ran 3 x", self.logged())
        code, out, _ = self.run_gateway("mw", "tool", MODEL_GATEWAY_TOOL_GREETING="env-hi")
        self.assertEqual((code, out), (0, "env-hi  log=%s\n" % self.log))

    def test_an_unknown_name_exits_2_listing_the_loaded_modules(self):
        code, _, err = self.run_gateway("mw", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown middleware 'nope'; loaded: tool", err)

    def test_a_module_without_cli_and_a_failing_cli(self):
        jail.write_middleware(os.path.join(self.app, "middleware"), "quiet", 'NAME = "quiet"\n')
        jail.write_middleware(os.path.join(self.app, "middleware"), "crash",
                              'NAME = "crash"\ndef cli(argv, ctx):\n    raise RuntimeError("bad")\n')
        jail.write_json(self.settings, {"middleware": [{"module": "quiet"}, {"module": "crash"}]})
        code, _, err = self.run_gateway("mw", "quiet")
        self.assertEqual((code, "middleware quiet has no cli" in err), (2, True))
        code, _, err = self.run_gateway("mw", "crash")
        self.assertEqual(code, 1)
        self.assertIn("middleware crash cli failed: RuntimeError('bad')", err)


class UsageTest(CliCase):
    def test_usage_lists_every_command_and_an_unknown_one_exits_2(self):
        code, _, err = self.run_gateway("nonsense")
        self.assertEqual(code, 2)
        for name in cli.COMMANDS:
            self.assertIn("gateway.py " + name, err)
        self.assertEqual(self.run_gateway("check", "a", "b")[0], 2)
        self.assertEqual(self.run_gateway("mw")[0], 2)


if __name__ == "__main__":
    unittest.main()
