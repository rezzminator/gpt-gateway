"""Tests for launchd/uninstall.sh, with a scratch HOME and the stub launchctl."""
import json
import os
import unittest

from tests.launchd.stub import Host


class UninstallTest(unittest.TestCase):
    def installed(self):
        h = Host(self)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        with open(os.path.join(h.dest, "middleware", "mine.py"), "w") as f:
            f.write('NAME = "mine"\n')
        logs = os.path.join(h.home, "Library", "Logs")
        for name in ("model-gateway.log", "model-gateway.log.1", "model-gateway.stderr.log"):
            open(os.path.join(logs, name), "w").close()
        h.write_claude({"env": {"KEEP": "1", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % h.port}})
        return h, logs

    def test_removes_the_agent_and_copy_unlinks_and_keeps_settings_and_middleware(self):
        h, logs = self.installed()
        code, out = h.run("uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertIn("model-gateway removed", out)
        self.assertFalse(os.path.exists(h.dest))
        self.assertFalse(os.path.exists(h.plist))
        self.assertEqual(h.read_claude()["env"], {"KEEP": "1"})
        backups = os.path.join(h.home, ".local", "state", "model-gateway", "backups")
        (kept,) = [d for d in os.listdir(backups) if d.startswith("uninstall-")]
        self.assertTrue(os.path.isfile(os.path.join(backups, kept, "middleware", "mine.py")))
        with open(os.path.join(backups, kept, "settings.json")) as f:
            self.assertEqual(json.load(f), {"capture_dir": None, "middleware": []})
        self.assertTrue(any(d.startswith("settings.json.pre-uninstall-") for d in os.listdir(backups)))
        self.assertTrue(os.path.exists(os.path.join(logs, "model-gateway.log")))

    def test_the_jq_the_install_recorded_unlinks_whatever_jq_is_on_path(self):
        h, _ = self.installed()
        stub = os.path.join(h.root, "bin", "jq")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(stub, 0o755)
        code, out = h.run("uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(h.read_claude()["env"], {"KEEP": "1"})

    def test_purge_deletes_the_logs(self):
        h, logs = self.installed()
        code, out = h.run("uninstall.sh", "--purge")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(logs), [])

    def test_an_unknown_option_is_refused(self):
        h = Host(self)
        code, out = h.run("uninstall.sh", "--all")
        self.assertEqual(code, 2)
        self.assertIn("usage: uninstall.sh [--purge]", out)


if __name__ == "__main__":
    unittest.main()
