"""Tests for gpt_gateway.cli: serve's strict start, check, mw, and the usage built from COMMANDS.

Each runs gateway.py as a process from a staged install, as run.sh does.
"""
import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
import subprocess
import unittest

from gpt_gateway import cli, clock, db
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
            code = cli.main(["chatgpt-stats"] + list(args), d, {"HOME": d, "GPT_GATEWAY_DB": path})
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

    def test_the_claude_route_transport_line_counts_reuse_retries_and_failures_by_class(self):
        d = jail.scratch(self)
        path = os.path.join(d, "gateway.db")
        claude = {"route": "anthropic", "kind": "main", "slug": None, "model": "claude-x"}
        self.seed(path, dict(claude, reused=0, stale=0, attempts=1), dict(claude, reused=1, stale=0, attempts=1),
                  dict(claude, reused=0, stale=1, attempts=2),
                  dict(claude, reused=0, stale=1, attempts=2, status=502, error_class="tls"),
                  dict(claude, reused=1, stale=0, attempts=1, status=200, error_class="reset"),
                  dict(claude, path="/__unsent"),  # answered by a hook: it never went upstream
                  dict(claude, reused=1, stale=0, attempts=1, error_class="tls", started=clock.now() - 7200))
        lines = self.stats(d, path, "--since", "1h")[1]
        self.assertEqual(lines[-1], "anthropic transport sent=5 reused=2 retried=2 failed=2 classes=reset:1,tls:1")
        before = self.stats(d, path)[1]
        self.assertEqual(before[-1], "anthropic transport sent=6 reused=3 retried=2 failed=3 classes=reset:1,tls:2")
        conn = sqlite3.connect(path)  # the file as an earlier gateway left it, before this one's first write
        conn.execute("ALTER TABLE requests DROP COLUMN error_class")
        conn.close()
        self.assertEqual(self.stats(d, path), (0, before[:-1] + [
            "anthropic transport unread: %s: no such column: error_class" % path], ""))


class BuildTest(unittest.TestCase):
    def test_the_build_names_the_package_sources_byte_for_byte(self):
        d = jail.scratch(self)
        a, b = (os.path.join(jail.stage_install(os.path.join(d, name)), "gpt_gateway") for name in "ab")
        self.assertRegex(str(cli.build(a)), "^[0-9a-f]{12}$")
        self.assertEqual(cli.build(a), cli.build(b))
        with open(os.path.join(b, "clock.py"), "a") as f:
            f.write("\n")
        self.assertNotEqual(cli.build(a), cli.build(b))


MW_CLI = '''
NAME = "tool"
SETTINGS = {"greeting": ("hi", str, "GPT_GATEWAY_TOOL_GREETING")}

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
        self.log = os.path.join(self.tmp, ".local", "state", "gpt-gateway", "logs", "gpt-gateway.log")

    def run_gateway(self, *args, **env):
        env = jail.clean_env(self.tmp, GPT_GATEWAY_PORT=str(jail.free_port()), **env)
        p = subprocess.run(["/usr/bin/python3", "-I", "-B", os.path.join(self.app, "gateway.py")] + list(args),
                           env=env, capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout, p.stderr

    def logged(self):
        with open(self.log) as f:
            return f.read()

    def recorded(self, kind):
        """The fields of the events of `kind` in the jail's database."""
        rows = db.query(jail.clean_env(self.tmp)["GPT_GATEWAY_DB"], "SELECT fields FROM events WHERE kind = ?", (kind,))
        return [json.loads(r["fields"]) for r in rows]


class ServeTest(CliCase):
    def test_other_cli_failures_emit_level_rows_without_changing_stderr(self):
        for command, changes, event, function in (("chatgpt-login", {"GPT_GATEWAY_CHATGPT_AUTH_STORE": "bad"}, "start-failed", "start_error"),
                                                  ("chatgpt-status", {"GPT_GATEWAY_CHATGPT_AUTH_STORE": "bad"}, "start-failed", "start_error"),
                                                  ("chatgpt-stats", {}, "stats-unreadable", "cmd_chatgpt_stats")):
            with self.subTest(command=command):
                root = os.path.join(self.tmp, command)
                code, _, err = self.run_gateway(command, PLUGIN_LOG_DIR=root, **changes)
                self.assertEqual(code, 1)
                self.assertTrue(err)
                path = os.path.join(root, "gpt-gateway", "errors.jsonl")
                self.assertTrue(os.path.isfile(path), "CLI diagnostic must reach errors.jsonl")
                with open(path) as source:
                    row, = [json.loads(line) for line in source]
                self.assertEqual((row["level"], row["event"], row["where"]),
                                 ("error", event, "gpt_gateway/cli.py#" + function))

    def test_stderr_failures_also_emit_classified_rows(self):
        for args, changes, event, where in ((["serve"], {"GPT_GATEWAY_DRAIN_SECONDS": "soon"},
                                            "start-failed", "gpt_gateway/cli.py#start_error"),
                                           (["check"], {}, "check-failed", "gpt_gateway/cli.py#cmd_check")):
            with self.subTest(args=args):
                home = os.path.join(self.tmp, args[0])
                os.makedirs(home)
                jail.write_json(self.settings, {"capture_dir": "relative"})
                code, _, err = self.run_gateway(*args, PLUGIN_LOG_DIR=home, **changes)
                self.assertEqual(code, 1)
                self.assertTrue(err)
                path = os.path.join(home, "gpt-gateway", "errors.jsonl")
                self.assertTrue(os.path.isfile(path), "stderr failure must also reach errors.jsonl")
                with open(path) as source:
                    row, = [json.loads(line) for line in source]
                self.assertEqual((row["event"], row["where"]), (event, where))

    def test_a_bad_file_at_start_exits_non_zero_and_says_why(self):
        jail.write_json(self.settings, {"capture_dir": "relative"})
        code, _, err = self.run_gateway()
        self.assertEqual(code, 1)
        self.assertIn("gpt-gateway: settings error: %s: capture_dir must be an absolute path" % self.settings, err)
        self.assertIn("start failed: settings error", self.logged())

    def test_a_middleware_that_cannot_load_stops_the_start_naming_it(self):
        jail.write_json(self.settings, {"middleware": [{"module": "broken"}]})
        jail.write_middleware(os.path.join(self.app, "middleware"), "broken", "raise ImportError('nope')\n")
        code, _, err = self.run_gateway("serve")
        self.assertEqual(code, 1)
        self.assertIn("gpt-gateway: middleware error: middleware broken: import of", err)
        self.assertIn("ImportError('nope')", self.logged())

    def test_a_failing_check_stops_the_start(self):
        jail.write_json(self.settings, {"middleware": [{"module": "strict"}]})
        jail.write_middleware(os.path.join(self.app, "middleware"), "strict",
                              'NAME = "strict"\ndef check(s):\n    raise ValueError("no key file")\n')
        code, _, err = self.run_gateway()
        self.assertEqual(code, 1)
        self.assertIn("middleware strict: check failed: no key file", err)

    def test_a_bad_env_value_stops_the_start(self):
        code, _, err = self.run_gateway(GPT_GATEWAY_DRAIN_SECONDS="soon")
        self.assertEqual(code, 1)
        self.assertIn("env GPT_GATEWAY_DRAIN_SECONDS", err)


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
    def test_stderr_only_middleware_sites_emit_their_classified_rows(self):
        cases = (("no-cli", 'NAME = "tool"\n', {}, ["tool"], 2, "error", "middleware-cli-unavailable", "invoke"),
                 ("crash", 'NAME = "tool"\ndef cli(argv, ctx):\n    raise RuntimeError("bad")\n', {},
                  ["tool"], 1, "error", "middleware-cli-failed", "invoke"),
                 ("exit", 'NAME = "tool"\ndef cli(argv, ctx):\n    raise SystemExit("refused")\n', {},
                  ["tool"], 1, "error", "middleware-cli-exit", "invoke"),
                 ("wrong-result", 'NAME = "tool"\ndef cli(argv, ctx):\n    return "bad"\n', {},
                  ["tool"], 1, "error", "middleware-cli-invalid-result", "invoke"),
                 ("unknown", MW_CLI, {}, ["missing"], 2, "error", "middleware-unknown", "cmd_mw"),
                 ("env", MW_CLI, {"GPT_GATEWAY_DRAIN_SECONDS": "soon"}, ["tool"], 1, "error", "env-invalid", "cmd_mw"),
                 ("settings", MW_CLI, {}, ["tool"], 1, "error", "settings-invalid", "cmd_mw"))
        for name, source, changes, args, expected_code, level, event, function in cases:
            with self.subTest(event=event):
                root = os.path.join(self.tmp, name)
                jail.write_middleware(os.path.join(self.app, "middleware"), "tool", source)
                body = {"middleware": [{"module": "tool"}]}
                if name == "settings":
                    body["capture_dir"] = "relative"
                jail.write_json(self.settings, body)
                code, _, err = self.run_gateway("mw", *args, PLUGIN_LOG_DIR=root, **changes)
                self.assertEqual(code, expected_code)
                self.assertTrue(err)
                path = os.path.join(root, "gpt-gateway", "warnings.jsonl" if level == "warning" else "errors.jsonl")
                self.assertTrue(os.path.isfile(path), "middleware CLI diagnostic must reach its level file")
                with open(path) as source:
                    row, = [json.loads(line) for line in source]
                self.assertEqual((row["level"], row["event"], row["where"]),
                                 (level, event, "gpt_gateway/cli.py#" + function))

    def setUp(self):
        super().setUp()
        jail.write_middleware(os.path.join(self.app, "middleware"), "tool", MW_CLI)
        jail.write_json(self.settings, {"middleware": [{"module": "tool", "settings": {"greeting": "hello"}}]})

    def test_mw_runs_the_cli_and_exits_with_its_value(self):
        code, out, _ = self.run_gateway("mw", "tool", "3", "x")
        self.assertEqual((code, out), (3, "hello 3,x log=%s\n" % self.log))
        code, out, _ = self.run_gateway("mw", "tool", GPT_GATEWAY_TOOL_GREETING="env-hi")
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
        from gpt_gateway import clock, env
        from gpt_gateway.chatgpt import auth
        from tests.gpt_gateway.chatgpt.test_auth import tokens
        home = jail.scratch(self)
        mapping = jail.clean_env(home, GPT_GATEWAY_CHATGPT_AUTH_STORE="file")
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
            with patch("gpt_gateway.chatgpt.login." + name) as login, redirect_stdout(out):
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
