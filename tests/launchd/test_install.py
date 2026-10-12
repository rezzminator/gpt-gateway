"""Tests for launchd/install.sh, into a scratch HOME with a stub launchctl that reports the agent off."""
import json
import os
import subprocess
import unittest

from tests import jail
from tests.launchd.stub import Host

FIRST_SETTINGS = {"capture_dir": None, "middleware": [{"module": "status_label"}]}
# The former product name, which the install migrates from.
LEGACY_NAME = "model-gateway"


class InstallTest(unittest.TestCase):
    def test_a_first_install_lays_out_the_copy_settings_and_agent(self):
        h = Host(self)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        self.assertIn("installed; the gateway is off", out)
        for name in ("gateway.py", "run.sh", "ctl.sh", "README.md", "LICENSE", "gpt_gateway/proxy.py",
                     "gpt_gateway/harness.py", "gpt_gateway/chatgpt/stream.py", "THIRD_PARTY_NOTICES.md"):
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(h.dest, name)), name)
        self.assertFalse(os.path.exists(os.path.join(h.dest, "gpt_gateway", "__pycache__")))
        self.assertTrue(os.access(os.path.join(h.dest, "run.sh"), os.X_OK))
        self.assertTrue(os.path.isdir(os.path.join(h.dest, "middleware")))
        with open(os.path.join(h.dest, "settings.json")) as f:
            self.assertEqual(json.load(f), FIRST_SETTINGS)
        self.assertEqual(self.shipped(h), self.source("status_label.py"))
        self.assertEqual(h.plist_value("Label"), "local.gpt-gateway")
        self.assertEqual(h.plist_value("EnvironmentVariables.GPT_GATEWAY_PORT"), str(h.port))
        self.assertEqual(h.plist_value("EnvironmentVariables.GPT_GATEWAY_LOG"),
                         os.path.join(h.home, "Library", "Logs", "gpt-gateway.log"))
        self.assertEqual(h.plist_value("EnvironmentVariables.GPT_GATEWAY_CLAUDE_SETTINGS"), os.path.realpath(h.claude))
        self.assertEqual(h.plist_value("StandardErrorPath"),
                         os.path.join(h.home, "Library", "Logs", "gpt-gateway.stderr.log"))
        self.assertFalse([c for c in h.launchctl_calls() if c.startswith(("bootstrap", "kill", "bootout"))])
        p = subprocess.run(["/usr/bin/python3", "-I", "-B", os.path.join(h.dest, "gateway.py"), "check"],
                           capture_output=True, text=True, env={"HOME": h.home, "PATH": "/usr/bin:/bin"})
        self.assertEqual(p.stdout, "ok: %s\n" % os.path.join(h.dest, "settings.json"))

    def test_the_advisor_middleware_ships_with_every_install_but_a_fresh_one_does_not_list_it(self):
        h = Host(self)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        installed = os.path.join(h.dest, "middleware", "advisor_cache.py")
        self.assertTrue(os.path.isfile(installed), installed)
        self.assertEqual(self.shipped(h, "advisor_cache.py"), self.source("advisor_cache.py"))
        with open(os.path.join(h.dest, "settings.json")) as f:
            self.assertEqual([entry["module"] for entry in json.load(f)["middleware"]], ["status_label"])
        listed = {"capture_dir": None, "middleware": [{"module": "status_label"},
                                                      {"module": "advisor_cache", "settings": {"ttl": "1h"}}]}
        with open(os.path.join(h.dest, "settings.json"), "w") as f:
            json.dump(listed, f)
        p = subprocess.run(["/usr/bin/python3", "-I", "-B", os.path.join(h.dest, "gateway.py"), "check"],
                           capture_output=True, text=True, env={"HOME": h.home, "PATH": "/usr/bin:/bin"})
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "ok: %s\n" % os.path.join(h.dest, "settings.json"), ""))

    def test_jail_stages_the_package_tree_and_notices(self):
        dest = jail.stage_install(os.path.join(jail.scratch(self), "app"))
        for name in ("gpt_gateway/chatgpt/__init__.py", "gpt_gateway/chatgpt/stream.py",
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
        log = os.path.join(h.home, "Library", "Logs", "gpt-gateway-install-tests.log")
        self.assertIn(log, out)
        with open(log) as f:
            self.assertIn("AssertionError: no", f.read())
        self.assertFalse(os.path.exists(h.dest))

    def test_a_hung_test_run_is_a_failure_that_names_the_test_and_keeps_the_stacks(self):
        h = Host(self, hangs=True)
        h.env = {"GPT_GATEWAY_TEST_SECONDS": "3"}
        code, out = h.install()
        self.assertEqual(code, 1)
        self.assertIn("install.sh: tests hung past 3s", out)
        self.assertIn("test_stuck", out)
        log = os.path.join(h.home, "Library", "Logs", "gpt-gateway-install-tests.log")
        with open(log) as f:
            self.assertIn("most recent call first", f.read())
        self.assertFalse(os.path.exists(h.dest))

    def test_another_base_url_is_removed_with_a_backup(self):
        h = Host(self)
        h.write_claude({"env": {"KEEP": "1", "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ENABLE_TOOL_SEARCH": "true"}})
        code, out = h.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(h.read_claude()["env"], {"KEEP": "1"})
        backups = os.path.join(h.home, ".local", "state", "gpt-gateway", "backups")
        (backup,) = os.listdir(backups)
        self.assertTrue(backup.startswith("settings.json.pre-install-"))
        chosen = Host(self)
        chosen.write_claude({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ENABLE_TOOL_SEARCH": "auto:5"}})
        code, out = chosen.install()
        self.assertEqual(code, 0, out)
        self.assertEqual(chosen.read_claude()["env"], {"ENABLE_TOOL_SEARCH": "auto:5"})

    def former(self, h):
        """Plants a former install in h's HOME: its agent, its copy with the user's settings and module, its state."""
        share = os.path.join(h.home, ".local", "share", LEGACY_NAME)
        state = os.path.join(h.home, ".local", "state", LEGACY_NAME)
        plist = os.path.join(h.home, "Library", "LaunchAgents", "local.%s.plist" % LEGACY_NAME)
        os.makedirs(os.path.join(share, "middleware"))
        os.makedirs(state)
        os.makedirs(os.path.dirname(plist))
        with open(os.path.join(share, "settings.json"), "w") as f:
            json.dump({"capture_dir": None, "middleware": [{"module": "mine"}]}, f)
        with open(os.path.join(share, "middleware", "mine.py"), "w") as f:
            f.write('NAME = "mine"\n')
        with open(os.path.join(state, "gateway.db"), "w") as f:
            f.write("former")
        with open(plist, "w") as f:
            f.write("<plist/>\n")
        return share, state, plist

    def test_an_install_migrates_the_former_install(self):
        h = Host(self)
        share, state, plist = self.former(h)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        self.assertIn("bootout gui/%d/local.%s" % (os.getuid(), LEGACY_NAME), h.launchctl_calls())
        for gone in (share, state, plist):
            with self.subTest(gone=gone):
                self.assertFalse(os.path.exists(gone), gone)
        with open(os.path.join(h.home, ".local", "state", "gpt-gateway", "gateway.db")) as f:
            self.assertEqual(f.read(), "former")
        with open(os.path.join(h.dest, "settings.json")) as f:
            self.assertEqual(json.load(f), {"capture_dir": None, "middleware": [{"module": "mine"}]})
        self.assertEqual(self.shipped(h, "mine.py"), b'NAME = "mine"\n')
        self.assertEqual(self.shipped(h), self.source("status_label.py"))
        self.assertEqual(h.plist_value("Label"), "local.gpt-gateway")

    def test_a_migration_keeps_an_existing_state_and_settings(self):
        h = Host(self)
        _, state, _ = self.former(h)
        self.assertEqual(h.install()[0], 0)
        os.makedirs(state)
        with open(os.path.join(state, "gateway.db"), "w") as f:
            f.write("former")
        with open(os.path.join(h.dest, "settings.json"), "w") as f:
            json.dump(FIRST_SETTINGS, f)
        self.former_settings_only(h)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        with open(os.path.join(h.home, ".local", "state", "gpt-gateway", "gateway.db")) as f:
            self.assertEqual(f.read(), "former", "the first migration's state is kept")
        self.assertTrue(os.path.isfile(os.path.join(state, "gateway.db")),
                        "a former state is left in place when the new one exists")
        with open(os.path.join(h.dest, "settings.json")) as f:
            self.assertEqual(json.load(f), FIRST_SETTINGS)
        self.assertFalse(os.path.exists(os.path.join(h.home, ".local", "share", LEGACY_NAME)))

    def former_settings_only(self, h):
        share = os.path.join(h.home, ".local", "share", LEGACY_NAME)
        os.makedirs(share)
        with open(os.path.join(share, "settings.json"), "w") as f:
            json.dump({"capture_dir": None, "middleware": [{"module": "ghost"}]}, f)

    def test_bad_options_are_refused(self):
        h = Host(self)
        self.assertEqual(h.run("install.sh", "--port", "x")[0], 2)
        self.assertEqual(h.run("install.sh", "--ttl", "1h")[0], 2)


if __name__ == "__main__":
    unittest.main()
