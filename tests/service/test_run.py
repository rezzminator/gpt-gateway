"""Tests for service/run.sh: it links Claude Code's settings to the gateway only while the gateway answers."""
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


def restore_hangup():
    """A shell cannot trap a signal it inherits ignored, and nohup, cron and many agent harnesses start the suite with
    HUP ignored: run.sh gets the default back, as launchd starts it."""
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = jail.scratch(self)
        self.app = jail.stage_install(os.path.join(self.dir, "app"))
        self.settings = os.path.join(self.dir, "claude-settings.json")
        self.port = jail.free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.stopped = False
        self.addCleanup(self.stop)

    def stop(self):
        """Kills run.sh's whole process group: run.sh, its gateways, serving, draining or starting, and their sleeps."""
        if self.proc is None or self.stopped:
            return
        self.stopped = True  # a test may stop early; the cleanup then finds the group gone, or only zombies
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # every process of the group has exited, or macOS refuses a
            pass  # group whose last members are zombies waiting to be reaped
        self.proc.wait()
        self.errors.close()

    def write_settings(self, env):
        with open(self.settings, "w") as f:
            json.dump({"model": "opus", "env": env}, f)

    def read_settings(self):
        with open(self.settings) as f:
            return json.load(f)

    def start(self, python="/usr/bin/python3", upstream=None, jq=JQ, stop_seconds='630', drain='600', retry='0'):
        env = jail.clean_env(self.dir, GPT_GATEWAY_PYTHON=python, GPT_GATEWAY_JQ=jq,
                             GPT_GATEWAY_STOP_SECONDS=stop_seconds,
                             GPT_GATEWAY_DRAIN_SECONDS=drain,
                             GPT_GATEWAY_RETRY_SECONDS=retry, GPT_GATEWAY_CLAUDE_SETTINGS=self.settings,
                             GPT_GATEWAY_PORT=str(self.port), GPT_GATEWAY_LOG=os.path.join(self.dir, "gateway.log"),
                             GPT_GATEWAY_UPSTREAM=upstream or "http://127.0.0.1:%d" % jail.free_port())
        # A file, not a pipe: a read to the end of a pipe waits for every gateway still draining to let go of it.
        self.errors = open(os.path.join(self.dir, "run.err"), "wb")
        self.proc = subprocess.Popen(["/bin/sh", os.path.join(self.app, "run.sh"), "serve"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.errors, start_new_session=True,
                                     preexec_fn=restore_hangup)

    def stderr(self):
        """What run.sh and its gateways have written to stderr so far."""
        with open(os.path.join(self.dir, "run.err"), "rb") as f:
            return f.read()

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
        self.assertIn(b"missing or older", self.stderr())

    def test_missing_settings_stop_succeeds_without_retrying(self):
        self.write_settings({})
        self.start(stop_seconds='4', drain='0')
        self.assertTrue(jail.wait_for(self.linked), self.stderr())
        os.remove(self.settings)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=8), 0, self.stderr())
        self.assertNotIn(b'retrying', self.stderr())

    def test_unlink_failure_leaves_time_for_the_full_stream_drain(self):
        entered, release = threading.Event(), threading.Event()
        class HeldUpstream(fakes.SlowUpstream):
            def do_POST(self):
                entered.set()
                release.wait(12)
                super().do_POST()
        self.addCleanup(release.set)
        self.write_settings({})
        self.start(upstream=fakes.start_upstream(self, HeldUpstream), stop_seconds='7', drain='4')
        self.assertTrue(jail.wait_for(self.linked), self.stderr())
        got, errors = [], []
        def stream():
            try:
                self.stream(got)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=stream)
        thread.start()
        self.assertTrue(entered.wait(5), 'the stream never reached upstream')
        jail.write_json(self.settings, '{broken')
        started = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.assertTrue(jail.wait_for(lambda: b'stopping: gateways draining' in self.stderr(), 3),
                            'unlink consumed the stream drain budget: %r' % self.stderr())
            release.set()
            thread.join(8)
            self.assertEqual(errors, [])
            self.assertIn(b'message_stop', got[0])
            self.assertEqual(self.proc.wait(timeout=5), 1, self.stderr())
            self.assertLess(time.monotonic() - started, 7, 'stop reached the manager kill deadline')
        finally:
            release.set()
            thread.join(10)

    def assert_refused(self):
        try:
            connection = socket.create_connection(('127.0.0.1', self.port), timeout=1)
        except ConnectionRefusedError:
            return
        connection.close()
        self.fail('a failed gateway still accepts TCP connections without serving them')

    def test_failed_start_closes_the_listener_before_the_retry_sleep(self):
        jail.write_json(os.path.join(self.app, 'settings.json'), {'capture_dir': 'relative'})
        self.write_settings({'ANTHROPIC_BASE_URL': self.url})
        self.start(retry='5')
        self.assertTrue(jail.wait_for(lambda: b'the gateway could not start' in self.stderr()), self.stderr())
        self.assertIsNone(self.proc.poll(), 'the retry window was not exercised')
        self.assert_refused()

    def test_a_crash_closes_the_listener_before_failed_settings_cleanup(self):
        self.write_settings({})
        self.start(stop_seconds='3')
        self.assertTrue(jail.wait_for(self.linked), self.stderr())
        child = self.health_pid()
        jail.write_json(self.settings, '{broken')
        os.kill(child, signal.SIGKILL)
        self.assertTrue(jail.wait_for(lambda: b'gateway exited with status' in self.stderr()), self.stderr())
        self.assert_refused()

    def test_a_broken_gateway_package_still_unlinks_on_linux(self):
        jail.write_json(os.path.join(self.app, 'gpt_gateway', 'server.py'), 'invalid python !')
        self.write_settings({'ANTHROPIC_BASE_URL': self.url, 'KEEP': '1'})
        self.start(stop_seconds='1')
        self.assertEqual(self.proc.wait(timeout=10), 1, self.stderr())
        self.assertEqual(self.read_settings()['env'], {'KEEP': '1'}, self.stderr())

    def test_hup_before_the_listener_interpreter_starts_is_safe(self):
        ready, release = os.path.join(self.dir, 'ready'), os.path.join(self.dir, 'release')
        wrapper = os.path.join(self.dir, 'paused-python')
        with open(wrapper, 'w') as output:
            output.write('#!/bin/sh\ncase "$2" in */listener.py)\n'
                         'touch "' + ready + '"\n'
                         'while [ ! -f "' + release + '" ]; do sleep 0.01; done ;;\nesac\n'
                         'exec /usr/bin/python3 "$@"\n')
        os.chmod(wrapper, 0o755)
        self.write_settings({})
        self.start(python=wrapper)
        self.assertTrue(jail.wait_for(lambda: os.path.exists(ready)), self.stderr())
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(0.1)
        open(release, 'w').close()
        self.assertTrue(jail.wait_for(self.linked), 'HUP killed the listener entry: %r' % self.stderr())


    def test_stop_retries_settings_that_are_temporarily_unreadable(self):
        self.write_settings({'KEEP': '1'})
        self.start(stop_seconds='4', drain='0')
        self.assertTrue(jail.wait_for(self.linked), self.stderr())
        saved = self.read_settings()
        jail.write_json(self.settings, '{broken')
        self.proc.send_signal(signal.SIGTERM)
        self.assertTrue(jail.wait_for(lambda: b'could not read settings' in self.stderr(), 3), self.stderr())
        jail.write_json(self.settings, saved)
        self.assertEqual(self.proc.wait(timeout=8), 0, self.stderr())
        self.assertEqual(self.read_settings()['env'], {'KEEP': '1'})
        self.assertIn(b'could not read settings', self.stderr())

    def test_stop_reports_settings_read_failure_at_its_deadline(self):
        self.write_settings({})
        self.start(stop_seconds='1')
        self.assertTrue(jail.wait_for(self.linked), self.stderr())
        jail.write_json(self.settings, '{broken')
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=8), 1, self.stderr())
        self.assertIn(b'could not read settings', self.stderr())
        self.assertIn(b'deadline', self.stderr())

    def test_stop_retries_a_settings_rewrite_failure(self):
        self.write_settings({'KEEP': '1'})
        marker = os.path.join(self.dir, 'reject-edit')
        wrapper = os.path.join(self.dir, 'retry-jq')
        with open(wrapper, 'w') as output:
            output.write('#!/bin/sh\ncase "$*" in *"del(.env.ANTHROPIC_BASE_URL)"*)\n'
                         '  [ -f "' + marker + '" ] && exit 3 ;;\nesac\nexec "' + JQ + '" "$@"\n')
        os.chmod(wrapper, 0o755)
        self.start(jq=wrapper, stop_seconds='4', drain='0')
        self.assertTrue(jail.wait_for(self.linked), self.stderr())
        open(marker, 'w').close()
        self.proc.send_signal(signal.SIGTERM)
        self.assertTrue(jail.wait_for(lambda: b'could not rewrite settings' in self.stderr(), 3), self.stderr())
        os.remove(marker)
        self.assertEqual(self.proc.wait(timeout=8), 0, self.stderr())
        self.assertEqual(self.read_settings()['env'], {'KEEP': '1'})
        self.assertIn(b'could not rewrite settings', self.stderr())

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
        err = self.stderr()
        self.assertIn(b"settings error", err)
        self.assertIn(b"exited with status 1 before answering", err)

    def test_a_middleware_that_fails_to_load_fails_open(self):
        jail.write_json(os.path.join(self.app, "settings.json"), {"middleware": [{"module": "broken"}]})
        jail.write_middleware(os.path.join(self.app, "middleware"), "broken", "NAME = 3\n")
        self.write_settings({"ANTHROPIC_BASE_URL": self.url})
        self.start()
        self.assertEqual(self.proc.wait(timeout=20), 1)
        self.assertNotIn("ANTHROPIC_BASE_URL", self.read_settings()["env"])
        self.assertIn(b"middleware error: middleware broken: NAME must be a string", self.stderr())

    def test_a_stop_while_the_gateway_starts_stops_it_too(self):
        slow = os.path.join(self.dir, "slow-python")
        with open(slow, "w") as f:  # the version probe answers; the gateway itself never does
            f.write('#!/bin/sh\ncase "$2" in */gateway.py) exec sleep 30 ;; esac\nexec /usr/bin/python3 "$@"\n')
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
        self.assertTrue(jail.wait_for(lambda: b"restarted: gateway" in self.stderr(), 30), self.stderr())
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=30), 0)

    def test_connections_opened_during_hup_keep_their_first_request(self):
        self.write_settings({})
        self.start()
        self.assertTrue(jail.wait_for(self.linked), self.stderr())
        before = self.health_pid()
        os.kill(before, signal.SIGSTOP)
        def resume():
            try:
                os.kill(before, signal.SIGCONT)
            except ProcessLookupError:
                pass
        self.addCleanup(resume)
        self.proc.send_signal(signal.SIGHUP)
        def successor_started():
            children = subprocess.run(['pgrep', '-P', str(self.proc.pid)],
                                      capture_output=True, text=True).stdout.split()
            return len(children) >= 2
        self.assertTrue(jail.wait_for(successor_started), self.stderr())
        connections = [socket.create_connection(('127.0.0.1', self.port), timeout=5) for _ in range(32)]
        for connection in connections:
            self.addCleanup(connection.close)
        self.assertTrue(jail.wait_for(lambda: b'restarted: gateway' in self.stderr(), 30), self.stderr())
        resume()
        failures = []
        for connection in connections:
            try:
                connection.sendall(b'GET /__gateway/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                response = http.client.HTTPResponse(connection)
                response.begin()
                body = response.read()
                if response.status != 200 or not body.startswith(b'ok pid='):
                    failures.append((response.status, body))
            except (OSError, http.client.HTTPException) as error:
                failures.append(str(error))
        self.assertEqual(failures, [], 'handover lost connections that had already reached a listener')
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=15), 0)

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
