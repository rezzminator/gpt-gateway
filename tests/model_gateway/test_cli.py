"""Tests for model_gateway.cli: serve's strict start, check, mw, and the usage built from COMMANDS.

Each runs gateway.py as a process from a staged install, as run.sh does.
"""
import os
import io
from contextlib import redirect_stdout
from unittest.mock import patch
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

    def test_timed_lines_add_p50_and_p95_per_slug_after_the_total(self):
        import contextlib
        import io
        d = jail.scratch(self)
        path = os.path.join(d, "gw.log")
        tail = " out=8 reasoning=- ttfb_ms=%d upstream_ms=1 total_ms=%d reused=0 stale=0\n"
        with open(path, "w") as f:
            f.write("stamp    gpt lane=a slug=gpt-test status=200 input=20 cached=0 ratio=0.000 attempts=1\n")  # untimed
            for n in (4, 1, 3, 2):
                f.write("stamp    gpt lane=a slug=gpt-test status=200 input=20 cached=0 ratio=0.000 attempts=1" +
                        tail % (n * 100, n * 1000))
            f.write("stamp    gpt lane=b slug=gpt-other status=200 input=10 cached=5 ratio=0.500 attempts=2" +
                    (tail % (50, 700)).replace("\n", " later_total_ms=9\n"))  # fields only ever grow at the end
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["chatgpt-stats", path], d, {}), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "a responses=5 input=100 cached=0 hit=0.000", "b responses=1 input=10 cached=5 hit=0.500",
            "total responses=6 input=110 cached=5 hit=0.045",
            "slug=gpt-other timed=1 ttfb_ms p50=50 p95=50 total_ms p50=700 p95=700",
            "slug=gpt-test timed=4 ttfb_ms p50=200 p95=400 total_ms p50=2000 p95=4000"])


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


class CodexCliTest(unittest.TestCase):
    def test_k10_status_and_login_explain_codex_precedence(self):
        from model_gateway import clock, env
        from model_gateway.chatgpt import auth
        from tests.model_gateway.chatgpt.test_auth import tokens
        home = jail.scratch(self)
        mapping = jail.clean_env(home, MODEL_GATEWAY_CHATGPT_AUTH_STORE="file")
        path = os.path.join(home, ".codex", "auth.json")
        os.makedirs(os.path.dirname(path))
        expiry = 1800000000
        jail.write_json(path, {"tokens": dict(tokens(expiry), account_id="acct-test-0001")})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["chatgpt-status"], home, mapping), 0)
        self.assertEqual(out.getvalue().strip(),
            "ChatGPT: Codex login (~/.codex/auth.json); account ...0001; access expires " +
            clock.stamp("%Y-%m-%d %H:%M:%S %z", expiry))
        for args, name in (([], "browser_login"), (["--device"], "device_login")):
            out = io.StringIO()
            with patch("model_gateway.chatgpt.login." + name) as login, redirect_stdout(out):
                self.assertEqual(cli.main(["chatgpt-login"] + args, home, mapping), 0)
            self.assertIsInstance(login.call_args.args[0], auth.Store)
            self.assertEqual(out.getvalue().splitlines()[0],
                "ChatGPT: a Codex login at ~/.codex/auth.json is used first; this login serves only without it")
        os.unlink(path)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["chatgpt-status"], home, mapping), 0)
        self.assertEqual(out.getvalue().strip(),
                        "ChatGPT: not logged in; run codex login, or gateway.py chatgpt-login")
        store = auth.Store(env.ChatGPTEnv.parse(mapping))
        store.save(tokens(expiry))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["chatgpt-status"], home, mapping), 0)
        self.assertEqual(out.getvalue().strip(), store.status())


if __name__ == "__main__":
    unittest.main()
