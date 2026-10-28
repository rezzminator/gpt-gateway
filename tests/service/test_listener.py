"""The listening-descriptor entry preserves its socket across exec without requiring a service manager."""
import json
import os
import subprocess
import signal
import unittest

from tests import jail


class ListenerTest(unittest.TestCase):
    def setUp(self):
        self.home = jail.scratch(self)
        self.app = jail.stage_install(os.path.join(self.home, 'app'))
        self.settings = os.path.join(self.home, 'claude.json')
        self.port = jail.free_port()
        self.url = 'http://127.0.0.1:%d' % self.port
        jail.write_json(self.settings, {'env': {'ANTHROPIC_BASE_URL': self.url, 'KEEP': '1'}})
        self.env = jail.clean_env(self.home, GPT_GATEWAY_PORT=str(self.port), GPT_GATEWAY_JQ='/usr/bin/jq',
                                  GPT_GATEWAY_CLAUDE_SETTINGS=self.settings, GPT_GATEWAY_STOP_SECONDS='1',
                                  GPT_GATEWAY_RETRY_SECONDS='0', GPT_GATEWAY_LISTENER_FD='3')
        self.log = os.path.join(self.home, '.local', 'state', 'gpt-gateway', 'logs', 'gpt-gateway.log')

    def run_fault(self, code, args=None):
        launcher = os.path.join(self.home, 'launch.py')
        with open(launcher, 'w') as output:
            output.write('import os,runpy,socket,sys\nsys.path.insert(0,' + repr(self.app) + ')\n'
                         'import gpt_gateway.server\n' + code + '\n'
                         'sys.argv=sys.argv[1:]\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
        command = ['/usr/bin/python3', '-I', launcher, os.path.join(self.app, 'listener.py')]
        command += args if args is not None else [str(self.port), '/bin/sh', os.path.join(self.app, 'run.sh'), 'serve']
        return subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)

    def assert_unlinked(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        with open(self.settings) as source:
            self.assertEqual(json.load(source)['env'], {'KEEP': '1'}, result.stderr)

    def test_socket_creation_failure_unlinks(self):
        self.assert_unlinked(self.run_fault('def failed(*a,**k): raise OSError("socket creation refused")\n'
                                           'socket.socket=failed'))

    def test_socket_import_failure_unlinks(self):
        self.assert_unlinked(self.run_fault('sys.modules["socket"]=None'))

    def test_short_arguments_unlink(self):
        self.assert_unlinked(self.run_fault('', args=[]))

    def test_exec_failure_unlinks(self):
        self.assert_unlinked(self.run_fault('original=os.execv\n'
                                           'def failed_once(*a):\n'
                                           '    os.execv=original\n'
                                           '    raise OSError("exec refused")\n'
                                           'os.execv=failed_once'))

    def test_bind_failure_reports_the_real_cause_without_a_bad_descriptor(self):
        import socket
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.bind(('127.0.0.1', self.port))
        blocker.listen(1)
        result = self.run_fault('')
        self.assert_unlinked(result)
        self.assertIn('Address already in use', result.stderr)
        diagnosis = result.stderr
        if os.path.exists(self.log):
            with open(self.log) as source:
                diagnosis += source.read()
        self.assertNotIn('Bad file descriptor', diagnosis)
        self.assertNotIn('Socket operation on non-socket', diagnosis)

    def signal_during_bind(self, signum):
        ready, release = os.path.join(self.home, 'ready'), os.path.join(self.home, 'release')
        wrapper = os.path.join(self.home, 'paused.py')
        with open(wrapper, 'w') as output:
            output.write('import os,runpy,socket,sys,time\nsys.path.insert(0,' + repr(self.app) + ')\n'
                         'import gpt_gateway.server\nready,release=sys.argv[1:3]\noriginal=socket.socket\n'
                         'def paused(*a,**k):\n'
                         '    s=original(*a,**k)\n'
                         '    open(ready,"w").close()\n'
                         '    while not os.path.exists(release): time.sleep(0.01)\n'
                         '    return s\n'
                         'socket.socket=paused\n'
                         'sys.argv=sys.argv[3:]\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
        command = ['/usr/bin/python3', '-I', wrapper, ready, release, os.path.join(self.app, 'listener.py'),
                   str(self.port), '/usr/bin/python3', '-I', '-c', 'print("handed over")']
        def restore():
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        process = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, preexec_fn=restore)
        def reap():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(reap)
        self.assertTrue(jail.wait_for(lambda: os.path.exists(ready)), 'listener never reached socket creation')
        process.send_signal(signum)
        if signum == signal.SIGHUP:
            # SIGSTOP waits behind the HUP; observing it stopped proves the HUP was handled, not merely queued.
            process.send_signal(signal.SIGSTOP)
            stopped = lambda: 'T' in subprocess.run(['ps', '-o', 'stat=', '-p', str(process.pid)],
                                                    capture_output=True, text=True).stdout
            survived = jail.wait_for(stopped, 2)
            open(release, 'w').close()
            if survived:
                process.send_signal(signal.SIGCONT)
            out, err = process.communicate(timeout=10)
            self.assertTrue(survived, 'HUP killed the listener before shell handover: %r' % err)
            self.assertEqual((process.returncode, out), (0, b'handed over\n'), err)
        else:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
            with open(self.settings) as source:
                self.assertEqual(json.load(source)['env'], {'KEEP': '1'}, err)

    def test_hup_during_socket_creation_does_not_kill_the_supervisor(self):
        self.signal_during_bind(signal.SIGHUP)

    def test_term_during_socket_creation_unlinks(self):
        self.signal_during_bind(signal.SIGTERM)

    def test_the_child_inherits_the_shared_listening_socket(self):
        home = jail.scratch(self)
        app = jail.stage_install(os.path.join(home, 'app'))
        port = jail.free_port()
        child = ('import json,socket,os; blocking=os.get_blocking(3); s=socket.socket(fileno=3); '
                 'print(json.dumps({"blocking":blocking,"port":s.getsockname()[1],'
                 '"reuse":s.getsockopt(socket.SOL_SOCKET,socket.SO_REUSEPORT)}))')
        result = subprocess.run(['/usr/bin/python3', '-I', os.path.join(app, 'listener.py'), str(port),
                                 '/usr/bin/python3', '-I', '-c', child],
                                env=jail.clean_env(home), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = json.loads(result.stdout)
        self.assertGreater(shared.pop('reuse'), 0)  # nonzero means set: Linux reports 1, macOS 512
        self.assertEqual(shared, {'blocking': False, 'port': port})


if __name__ == '__main__':
    unittest.main()
