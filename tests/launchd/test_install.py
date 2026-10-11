"""Tests for launchd/install.sh, into a scratch HOME with a stub launchctl that reports the agent off."""
import json
import os
import subprocess
import unittest

from tests import jail
from tests.launchd.stub import Host

FIRST_SETTINGS = {"capture_dir": None, "middleware": [{"module": "status_label"}]}


class InstallTest(unittest.TestCase):
    def test_a_first_install_lays_out_the_copy_settings_and_agent(self):
        h = Host(self)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        self.assertIn("installed; the gateway is off", out)
        for name in ("gateway.py", "run.sh", "ctl.sh", "README.md", "LICENSE", "model_gateway/proxy.py",
                     "model_gateway/harness.py", "model_gateway/chatgpt/stream.py", "THIRD_PARTY_NOTICES.md"):
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(h.dest, name)), name)
        self.assertFalse(os.path.exists(os.path.join(h.dest, "model_gateway", "__pycache__")))
        self.assertTrue(os.access(os.path.join(h.dest, "run.sh"), os.X_OK))
        self.assertTrue(os.path.isdir(os.path.join(h.dest, "middleware")))
        with open(os.path.join(h.dest, "settings.json")) as f:
            self.assertEqual(json.load(f), FIRST_SETTINGS)
        self.assertEqual(self.shipped(h), self.source("status_label.py"))
        self.assertEqual(h.plist_value("Label"), "local.model-gateway")
        self.assertEqual(h.plist_value("EnvironmentVariables.MODEL_GATEWAY_PORT"), str(h.port))
        self.assertEqual(h.plist_value("EnvironmentVariables.MODEL_GATEWAY_LOG"),
                         os.path.join(h.home, "Library", "Logs", "model-gateway.log"))
        self.assertEqual(h.plist_value("EnvironmentVariables.MODEL_GATEWAY_CLAUDE_SETTINGS"), os.path.realpath(h.claude))
        self.assertEqual(h.plist_value("StandardErrorPath"),
                         os.path.join(h.home, "Library", "Logs", "model-gateway.stderr.log"))
        self.assertFalse([c for c in h.launchctl_calls() if c.startswith(("bootstrap", "kill", "bootout"))])
        p = subprocess.run(["/usr/bin/python3", "-I", "-B", os.path.join(h.dest, "gateway.py"), "check"],
                           capture_output=True, text=True, env={"HOME": h.home, "PATH": "/usr/bin:/bin"})
        self.assertEqual(p.stdout, "ok: %s\n" % os.path.join(h.dest, "settings.json"))

    def test_jail_stages_the_package_tree_and_notices(self):
        dest = jail.stage_install(os.path.join(jail.scratch(self), "app"))
        for name in ("model_gateway/chatgpt/__init__.py", "model_gateway/chatgpt/stream.py",
                     "THIRD_PARTY_NOTICES.md"):
            with self.subTest(name=name):
                installed = os.path.join(dest, name)
                self.assertTrue(os.path.isfile(installed), name)
                with open(installed, "rb") as actual, open(os.path.join(jail.ROOT, name), "rb") as source:
                    self.assertEqual(actual.read(), source.read())

    def shipped(self, h, name="status_label.py"):
        with open(os.path.join(h.dest, "middleware", name), "rb") as f:
            return f.read()

    def source(self, name):
        with open(os.path.join(jail.ROOT, "middleware", name), "rb") as f:
            return f.read()

    def test_an_update_keeps_a_valid_settings_file_with_its_middleware(self):
        h = Host(self)
        self.assertEqual(h.install()[0], 0)
        with open(os.path.join(h.dest, "middleware", "model_note.py"), "w") as f:
            f.write('NAME = "model-note"\n')
        with open(os.path.join(h.dest, "middleware", "status_label.py"), "w") as f:
            f.write('NAME = "status-label"\n# an older copy\n')
        mine = {"capture_dir": None, "middleware": [{"module": "model_note"}]}
        with open(os.path.join(h.dest, "settings.json"), "w") as f:
            json.dump(mine, f)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        with open(os.path.join(h.dest, "settings.json")) as f:
            self.assertEqual(json.load(f), mine)
        self.assertEqual(self.shipped(h, "model_note.py"), b'NAME = "model-note"\n')
        self.assertEqual(self.shipped(h), self.source("status_label.py"))

    def test_an_invalid_settings_file_stops_the_update(self):
        h = Host(self)
        self.assertEqual(h.install()[0], 0)
        with open(os.path.join(h.dest, "settings.json"), "w") as f:
            json.dump({"middleware": [{"module": "ghost"}]}, f)
        code, out = h.install()
        self.assertEqual(code, 1)
        self.assertIn("settings.json is invalid", out)

    def test_failing_tests_stop_the_install(self):
        h = Host(self, tests_pass=False)
        code, out = h.install()
        self.assertEqual(code, 1)
        self.assertIn("install.sh: tests fail", out)
        self.assertIn("FAIL: test_bad", out)
        log = os.path.join(h.home, "Library", "Logs", "model-gateway-install-tests.log")
        self.assertIn(log, out)
        with open(log) as f:
            self.assertIn("AssertionError: no", f.read())
        self.assertFalse(os.path.exists(h.dest))

    def test_a_hung_test_run_is_a_failure_that_names_the_test_and_keeps_the_stacks(self):
        h = Host(self, hangs=True)
        h.env = {"MODEL_GATEWAY_TEST_SECONDS": "3"}
        code, out = h.install()
        self.assertEqual(code, 1)
        self.assertIn("install.sh: tests hung past 3s", out)
        self.assertIn("test_stuck", out)
        log = os.path.join(h.home, "Library", "Logs", "model-gateway-install-tests.log")
        with open(log) as f:
            self.assertIn("most recent call first", f.read())
        self.assertFalse(os.path.exists(h.dest))

    def test_another_base_url_is_removed_with_a_backup(self):
        h = Host(self)
        h.write_claude({"env": {"KEEP": "1", "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ENABLE_TOOL_SEARCH": "true"}})
        code, out = h.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(h.read_claude()["env"], {"KEEP": "1"})
        backups = os.path.join(h.home, ".local", "state", "model-gateway", "backups")
        (backup,) = os.listdir(backups)
        self.assertTrue(backup.startswith("settings.json.pre-install-"))
        chosen = Host(self)
        chosen.write_claude({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ENABLE_TOOL_SEARCH": "auto:5"}})
        code, out = chosen.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(chosen.read_claude()["env"], {"ENABLE_TOOL_SEARCH": "auto:5"})

    def test_bad_options_are_refused(self):
        h = Host(self)
        self.assertEqual(h.run("install.sh", "--port", "x")[0], 2)
        self.assertEqual(h.run("install.sh", "--ttl", "1h")[0], 2)


if __name__ == "__main__":
    unittest.main()
