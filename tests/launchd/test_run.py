"""Tests for launchd/run.sh: it links Claude Code's settings to the gateway only while the gateway answers."""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import unittest
import urllib.request

from tests import fakes, jail

JQ = shutil.which("jq") or "/usr/bin/jq"


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = jail.scratch(self)
        self.app = jail.stage_install(os.path.join(self.dir, "app"))
        self.settings = os.path.join(self.dir, "claude-settings.json")
        self.port = jail.free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.addCleanup(self.stop)

    def stop(self):
        """Kills run.sh's whole process group: run.sh, its gateways, serving, draining or starting, and their sleeps."""
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:  # every process of the group has already exited
            pass
        self.proc.wait()
        self.proc.stderr.close()

    def write_settings(self, env):
        with open(self.settings, "w") as f:
            json.dump({"model": "opus", "env": env}, f)

    def read_settings(self):
        with open(self.settings) as f:
            return json.load(f)

    def start(self, python="/usr/bin/python3", upstream=None):
        env = jail.clean_env(self.dir, MODEL_GATEWAY_PYTHON=python, MODEL_GATEWAY_JQ=JQ,
                             MODEL_GATEWAY_RETRY_SECONDS="0", MODEL_GATEWAY_CLAUDE_SETTINGS=self.settings,
                             MODEL_GATEWAY_PORT=str(self.port), MODEL_GATEWAY_LOG=os.path.join(self.dir, "gateway.log"),
                             MODEL_GATEWAY_UPSTREAM=upstream or "http://127.0.0.1:%d" % jail.free_port())
        self.proc = subprocess.Popen(["/bin/sh", os.path.join(self.app, "run.sh"), "serve"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)

    def healthy(self):
        try:
            with urllib.request.urlopen(self.url + "/__gateway/health", timeout=1) as r:
                return r.status == 200
        except OSError:
            return False

    def health_pid(self):
        try:
            with urllib.request.urlopen(self.url + "/__gateway/health", timeout=1) as r:
                return int(r.read().split(b"pid=")[1].split()[0])
        except (OSError, IndexError, ValueError):
            return None

    def linked(self):
        return self.read_settings()["env"].get("ANTHROPIC_BASE_URL") == self.url

    def stream(self, out):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("POST", "/v1/messages", body=b'{"model":"m","messages":[]}')
        out.append(conn.getresponse().read())
        conn.close()

    def test_links_once_healthy_and_unlinks_when_stopped(self):
        self.write_settings({"KEEP": "1"})
        self.start()
        self.assertTrue(jail.wait_for(self.linked), "never linked")
        self.assertTrue(self.healthy())
        self.assertEqual(self.read_settings()["env"]["ENABLE_TOOL_SEARCH"], "true")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)
        settings = self.read_settings()
        self.assertEqual((settings["env"], settings["model"]), ({"KEEP": "1"}, "opus"))
        self.assertFalse(self.healthy())

    def test_the_cleanup_stops_the_gateway_run_sh_started(self):
        self.write_settings({})
        self.start()
        self.assertTrue(jail.wait_for(self.linked), "never linked")
        gateway = self.health_pid()
        self.stop()  # the cleanup, run while run.sh still serves, as when an assertion fails mid-test
        self.assertTrue(jail.wait_for(lambda: self.health_pid() != gateway, 5), "gateway %s outlived its test" % gateway)

    def test_missing_python_fails_open(self):
        self.write_settings({"ANTHROPIC_BASE_URL": self.url, "ENABLE_TOOL_SEARCH": "true", "KEEP": "1"})
        self.start(python=os.path.join(self.dir, "no-such-python"))
        self.assertEqual(self.proc.wait(timeout=10), 1)
        self.assertEqual(self.read_settings()["env"], {"KEEP": "1"})
        self.assertIn(b"missing or older", self.proc.stderr.read())

    def test_gateway_that_dies_fails_open(self):
        self.write_settings({})
        self.start()
        self.assertTrue(jail.wait_for(self.linked), "never linked")
        child = subprocess.run(["pgrep", "-P", str(self.proc.pid)], capture_output=True, text=True).stdout.split()
        self.assertEqual(len(child), 1, child)
        os.kill(int(child[0]), signal.SIGKILL)
        self.assertEqual(self.proc.wait(timeout=10), 1)
        self.assertNotIn("ANTHROPIC_BASE_URL", self.read_settings()["env"])

    def test_port_taken_fails_open(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.port))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        self.write_settings({"ANTHROPIC_BASE_URL": self.url, "ENABLE_TOOL_SEARCH": "true"})
        self.start()
        self.assertEqual(self.proc.wait(timeout=20), 1)
        self.assertEqual(self.read_settings()["env"], {})

    def test_rejected_settings_fail_open(self):
        jail.write_json(os.path.join(self.app, "settings.json"), {"capture_dir": "relative"})
        self.write_settings({"ANTHROPIC_BASE_URL": self.url, "KEEP": "1"})
        self.start()
        self.assertEqual(self.proc.wait(timeout=20), 1)
        self.assertEqual(self.read_settings()["env"], {"KEEP": "1"})
        err = self.proc.stderr.read()
        self.assertIn(b"settings error", err)
        self.assertIn(b"exited with status 1 before answering", err)

    def test_a_middleware_that_fails_to_load_fails_open(self):
        jail.write_json(os.path.join(self.app, "settings.json"), {"middleware": [{"module": "broken"}]})
        jail.write_middleware(os.path.join(self.app, "middleware"), "broken", "NAME = 3\n")
        self.write_settings({"ANTHROPIC_BASE_URL": self.url})
        self.start()
        self.assertEqual(self.proc.wait(timeout=20), 1)
        self.assertNotIn("ANTHROPIC_BASE_URL", self.read_settings()["env"])
        self.assertIn(b"middleware error: middleware broken: NAME must be a string", self.proc.stderr.read())

    def test_a_stop_while_the_gateway_starts_stops_it_too(self):
        slow = os.path.join(self.dir, "slow-python")
        with open(slow, "w") as f:  # the version probe answers; the gateway itself never does
            f.write('#!/bin/sh\ncase "$1 $2" in "-I -c") exec /usr/bin/python3 "$@" ;; esac\nexec sleep 30\n')
        os.chmod(slow, 0o755)
        self.write_settings({"KEEP": "1"})
        self.start(python=slow)
        time.sleep(1.5)  # run.sh now polls the health of a gateway that has not answered
        self.assertIsNone(self.proc.poll())
        self.proc.send_signal(signal.SIGTERM)
        self.assertTrue(jail.wait_for(lambda: self.proc.poll() is not None, 8), "run.sh hung on the starting gateway")
        self.assertEqual(self.proc.returncode, 0)
        self.assertEqual(self.read_settings()["env"], {"KEEP": "1"})

    def test_a_gateway_linked_before_tool_search_existed_gets_it_on_start(self):
        self.write_settings({"ANTHROPIC_BASE_URL": self.url, "KEEP": "1"})
        self.start()
        self.assertTrue(jail.wait_for(lambda: self.read_settings()["env"].get("ENABLE_TOOL_SEARCH") == "true"),
                        "tool search never linked")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)
        self.assertEqual(self.read_settings()["env"], {"KEEP": "1"})

    def test_a_tool_search_value_the_user_chose_is_never_replaced_or_removed(self):
        self.write_settings({"ENABLE_TOOL_SEARCH": "auto:5"})
        self.start()
        self.assertTrue(jail.wait_for(self.linked), "never linked")
        self.assertEqual(self.read_settings()["env"]["ENABLE_TOOL_SEARCH"], "auto:5")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)
        self.assertEqual(self.read_settings()["env"], {"ENABLE_TOOL_SEARCH": "auto:5"})

    def test_other_url_is_left_alone(self):
        other = "http://127.0.0.1:1/other-proxy"
        self.write_settings({"ANTHROPIC_BASE_URL": other})
        self.start()
        self.assertTrue(jail.wait_for(self.healthy), "gateway never answered")
        time.sleep(0.5)
        self.assertEqual(self.read_settings()["env"], {"ANTHROPIC_BASE_URL": other})
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)
        self.assertEqual(self.read_settings()["env"]["ANTHROPIC_BASE_URL"], other)

    def test_hup_restarts_without_cutting_a_stream(self):
        self.write_settings({})
        self.start(upstream=fakes.start_upstream(self, fakes.SlowUpstream))
        self.assertTrue(jail.wait_for(self.linked), "never linked")
        first = self.health_pid()
        got = []
        t = threading.Thread(target=self.stream, args=(got,))
        t.start()
        time.sleep(0.3)
        self.proc.send_signal(signal.SIGHUP)
        self.assertTrue(jail.wait_for(lambda: self.health_pid() not in (None, first)), "no new gateway took over")
        t.join(20)
        self.assertIn(b"message_stop", got[0])
        self.assertTrue(self.linked())
        self.assertIsNone(self.proc.poll(), "run.sh exited on HUP")
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=15), 0)
        self.assertIn(b"restarted: gateway", self.proc.stderr.read())

    def test_stop_unlinks_first_then_drains_the_stream(self):
        self.write_settings({})
        self.start(upstream=fakes.start_upstream(self, fakes.SlowUpstream))
        self.assertTrue(jail.wait_for(self.linked), "never linked")
        got = []
        t = threading.Thread(target=self.stream, args=(got,))
        t.start()
        time.sleep(0.3)
        self.proc.send_signal(signal.SIGTERM)
        self.assertTrue(jail.wait_for(lambda: not self.linked(), timeout=1), "the URL stayed while draining")
        self.assertEqual(self.proc.wait(timeout=15), 0)
        t.join(20)
        self.assertIn(b"message_stop", got[0])


if __name__ == "__main__":
    unittest.main()
