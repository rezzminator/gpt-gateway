"""Tests for service/uninstall.sh, with a scratch HOME and the stub launchctl."""
import json
import os
import unittest

from tests.service.stub import Host


class UninstallTest(unittest.TestCase):
    def installed(self):
        h = Host(self)
        code, out = h.install()
        self.assertEqual(code, 0, out)
        with open(os.path.join(h.dest, "middleware", "mine.py"), "w") as f:
            f.write('NAME = "mine"\n')
        logs = os.path.join(h.home, "Library", "Logs")
        for name in ("gpt-gateway.log", "gpt-gateway.log.1", "gpt-gateway.stderr.log"):
            open(os.path.join(logs, name), "w").close()
        h.write_claude({"env": {"KEEP": "1", "ENABLE_TOOL_SEARCH": "true", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % h.port}})
        return h, logs

    def test_removes_the_agent_and_copy_unlinks_and_keeps_settings_and_middleware(self):
        h, logs = self.installed()
        code, out = h.run("uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertIn("gpt-gateway removed", out)
        self.assertFalse(os.path.exists(h.dest))
        self.assertFalse(os.path.exists(h.plist))
        self.assertEqual(h.read_claude()["env"], {"KEEP": "1"})
        backups = os.path.join(h.home, ".local", "state", "gpt-gateway", "backups")
        (kept,) = [d for d in os.listdir(backups) if d.startswith("uninstall-")]
        for name in ("mine.py", "status_label.py", "advisor_cache.py"):
            self.assertTrue(os.path.isfile(os.path.join(backups, kept, "middleware", name)), name)
        with open(os.path.join(backups, kept, "settings.json")) as f:
            self.assertEqual(json.load(f), {"capture_dir": None, "middleware": [{"module": "status_label"}]})
        self.assertTrue(any(d.startswith("settings.json.pre-uninstall-") for d in os.listdir(backups)))
        self.assertTrue(os.path.exists(os.path.join(logs, "gpt-gateway.log")))

    def test_a_tool_search_value_the_user_chose_stays(self):
        h, _ = self.installed()
        h.write_claude({"env": {"ENABLE_TOOL_SEARCH": "auto:5", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % h.port}})
        code, out = h.run("uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(h.read_claude()["env"], {"ENABLE_TOOL_SEARCH": "auto:5"})

    def test_the_jq_the_install_recorded_unlinks_whatever_jq_is_on_path(self):
        h, _ = self.installed()
        stub = os.path.join(h.root, "bin", "jq")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(stub, 0o755)
        code, out = h.run("uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertEqual(h.read_claude()["env"], {"KEEP": "1"})

    def test_purge_deletes_the_logs_and_the_database(self):
        h, logs = self.installed()
        state = os.path.join(h.home, ".local", "state", "gpt-gateway")
        os.makedirs(state, exist_ok=True)
        for name in ("gateway.db", "gateway.db-wal", "gateway.db-shm", "chatgpt-models.json"):
            open(os.path.join(state, name), "w").close()
        code, out = h.run("uninstall.sh", "--purge")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(logs), [])
        self.assertEqual(sorted(n for n in os.listdir(state) if n.startswith(("gateway", "chatgpt"))),
                         ["chatgpt-models.json"])  # the catalog is no log

    def test_an_unknown_option_is_refused(self):
        h = Host(self)
        code, out = h.run("uninstall.sh", "--all")
        self.assertEqual(code, 2)
        self.assertIn("usage: uninstall.sh [--purge]", out)


if __name__ == "__main__":
    unittest.main()
