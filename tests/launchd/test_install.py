"""Tests for launchd/install.sh, into a scratch HOME with a stub launchctl that reports the agent off."""
import json
import os
import subprocess
import unittest

from tests import jail
from tests.launchd.stub import Host

FIRST_SETTINGS = {"capture_dir": None, "middleware": []}


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

    def test_an_update_keeps_a_valid_settings_file_with_its_middleware(self):
        h = Host(self)
        self.assertEqual(h.install()[0], 0)
        with open(os.path.join(h.dest, "middleware", "model_note.py"), "w") as f:
            f.write('NAME = "model-note"\n')
        mine = {"capture_dir": None, "middleware": [{"module": "model_note"}]}
        with open(os.path.join(h.dest, "settings.json"), "w") as f:
            json.dump(mine, f)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        with open(os.path.join(h.dest, "settings.json")) as f:
            self.assertEqual(json.load(f), mine)
        self.assertTrue(os.path.isfile(os.path.join(h.dest, "middleware", "model_note.py")))

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

    def test_bad_options_are_refused(self):
        h = Host(self)
        self.assertEqual(h.run("install.sh", "--port", "x")[0], 2)
        self.assertEqual(h.run("install.sh", "--ttl", "1h")[0], 2)


if __name__ == "__main__":
    unittest.main()
