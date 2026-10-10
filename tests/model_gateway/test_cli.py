"""Tests for model_gateway.cli: serve's strict start, check, mw, and the usage built from COMMANDS.

Each runs gateway.py as a process from a staged install, as run.sh does.
"""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
import subprocess
import unittest

from model_gateway import cli, clock, db
from tests import jail


class ChatGPTStatsTest(unittest.TestCase):
    def seed(self, path, *rows):
        """A database at `path` holding `rows`: ChatGPT-route rows unless a row says otherwise."""
        store = db.Db(path, [].append)
        store.start()
        for row in rows:
            store.request(dict({"started": clock.now(), "route": "chatgpt", "method": "POST", "path": "/v1/messages",
                                "slug": "gpt-test"}, **row))
        self.assertTrue(store.close())

    def stats(self, d, path, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["chatgpt-stats"] + list(args), d, {"MODEL_GATEWAY_DB": path})
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_lane_totals_empty_unreadable_and_a_window(self):
        d = jail.scratch(self)
        path = os.path.join(d, "gateway.db")
        self.seed(path, {"lane": "b", "input_tokens": 100, "cache_read_tokens": 60},
                  {"lane": "a", "input_tokens": 20, "cache_read_tokens": 0},
                  {"lane": "b", "input_tokens": 50, "cache_read_tokens": 40, "started": clock.now() - 7200},
                  {"lane": "c", "input_tokens": None},  # a failed exchange: no usage
                  {"lane": None, "route": "anthropic", "input_tokens": 900, "cache_read_tokens": 900})
        self.assertEqual(self.stats(d, path), (0, ["a responses=1 input=20 cached=0 hit=0.000",
                                                   "b responses=2 input=150 cached=100 hit=0.667",
                                                   "total responses=3 input=170 cached=100 hit=0.588"], ""))
        self.assertEqual(self.stats(d, path, "--since", "1h")[1][:2], ["a responses=1 input=20 cached=0 hit=0.000",
                                                                      "b responses=1 input=100 cached=60 hit=0.600"])
        empty = os.path.join(d, "empty.db")
        self.seed(empty, {"route": "anthropic", "input_tokens": 5})
        self.assertEqual(self.stats(d, empty), (0, ["no gpt responses in " + empty], ""))
        self.seed(empty, {"lane": "a", "input_tokens": 5, "started": clock.now() - 7200})
        self.assertEqual(self.stats(d, empty, "--since", "30m"), (0, ["no gpt responses in %s in the last 30m" % empty], ""))
        code, out, err = self.stats(d, path + ".missing")
        self.assertEqual((code, out), (1, []))
        self.assertIn(path + ".missing: no database", err)
        for args in (["gw.log"], ["--since"], ["--since", "0h"], ["--since", "5w"], ["--since", "1h", "x"]):
            with self.subTest(args=args):
                self.assertEqual(self.stats(d, path, *args)[0], 2)

    def test_timed_rows_add_p50_and_p95_per_slug_after_the_total(self):
        d = jail.scratch(self)
        path = os.path.join(d, "gateway.db")
        self.seed(path, {"lane": "a", "input_tokens": 20, "cache_read_tokens": 0},  # untimed
                  *[{"lane": "a", "input_tokens": 20, "cache_read_tokens": 0, "ttfb_ms": n * 100, "total_ms": n * 1000}
                    for n in (4, 1, 3, 2)],
                  {"lane": "b", "slug": "gpt-other", "input_tokens": 10, "cache_read_tokens": 5, "ttfb_ms": 50,
                   "total_ms": 700})
        self.assertEqual(self.stats(d, path), (0, [
            "a responses=5 input=100 cached=0 hit=0.000", "b responses=1 input=10 cached=5 hit=0.500",
            "total responses=6 input=110 cached=5 hit=0.045",
            "slug=gpt-other timed=1 ttfb_ms p50=50 p95=50 total_ms p50=700 p95=700",
            "slug=gpt-test timed=4 ttfb_ms p50=200 p95=400 total_ms p50=2000 p95=4000"], ""))


    def test_claude_route_timings_follow_the_chatgpt_lines_and_stand_alone_without_them(self):
        d = jail.scratch(self)
        path = os.path.join(d, "gateway.db")
        claude = {"route": "anthropic", "kind": "main", "slug": None}
        self.seed(path, {"lane": "a", "input_tokens": 20, "cache_read_tokens": 0, "ttfb_ms": 50, "total_ms": 700},
                  *[dict(claude, model="claude-x", ttfb_ms=n * 100, upstream_ms=n * 1000, total_ms=n * 1000 + 5)
                    for n in (4, 1, 3, 2)],
                  dict(claude, model="claude-y", ttfb_ms=70, upstream_ms=900, total_ms=910),
                  dict(claude, model="claude-x"),  # no timings: not counted
                  dict(claude, model="claude-x", ttfb_ms=9000, upstream_ms=9000, total_ms=9005, started=clock.now() - 7200))
        lines = self.stats(d, path, "--since", "1h")[1]
        self.assertEqual(lines[-2:], [
            "anthropic model=claude-x timed=4 ttfb_ms p50=200 p95=400 upstream_ms p50=2000 p95=4000 total_ms p50=2005 p95=4005",
            "anthropic model=claude-y timed=1 ttfb_ms p50=70 p95=70 upstream_ms p50=900 p95=900 total_ms p50=910 p95=910"])
        self.assertEqual(len(lines), 5, lines)  # the lane, the total and the slug line, then the two models
        only = os.path.join(d, "only.db")
        self.seed(only, dict(claude, model="claude-x", ttfb_ms=100, upstream_ms=300, total_ms=310))
        self.assertEqual(self.stats(d, only), (0, [
            "no gpt responses in " + only,
            "anthropic model=claude-x timed=1 ttfb_ms p50=100 p95=100 upstream_ms p50=300 p95=300 total_ms p50=310 p95=310"],
            ""))


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

    def recorded(self, kind):
        """The fields of the events of `kind` in the jail's database."""
        rows = db.query(jail.clean_env(self.tmp)["MODEL_GATEWAY_DB"], "SELECT fields FROM events WHERE kind = ?", (kind,))
        return [json.loads(r["fields"]) for r in rows]


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
        code, out, _ = self.run_gateway("mw", "tool", MODEL_GATEWAY_TOOL_GREETING="env-hi")
        self.assertEqual((code, out), (0, "env-hi  log=%s\n" % self.log))
        self.assertEqual(self.recorded("middleware_log"), [{"line": "tool ran 3 x"}, {"line": "tool ran "}])
        self.assertNotIn("tool ran", self.logged() if os.path.exists(self.log) else "")

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
        self.assertEqual([(e["module"], e["hook"], e["error"]) for e in self.recorded("middleware_failed")],
                         [("crash", "cli", "RuntimeError('bad')")])


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
