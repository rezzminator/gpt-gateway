"""A source tree to install from, a scratch HOME, and stub service managers for the service script tests.

The stubs stand first on PATH, record calls and keep lifecycle operations inside the jail.
launchctl reports the agent unloaded; systemctl manages only scratch supervisors.
"""
import json
import os
import shutil
import subprocess

PLUTIL = '''#!/usr/bin/python3 -I
import plistlib
import sys
with open(sys.argv[-1], 'rb') as source:
    value = plistlib.load(source)
if sys.argv[1] == '-extract':
    for key in sys.argv[2].split('.'):
        value = value[key]
    print(value)
'''

SYSTEMCTL = '''#!/usr/bin/python3 -I
import fcntl
import json
import os
import signal
import shlex
import subprocess
import sys
import time
args = sys.argv[1:]
if args == ['--version']:
    print('systemd ' + os.environ.get('SYSTEMCTL_STUB_VERSION', '255'))
    sys.exit(0)
assert args.pop(0) == '--user'
with open(os.environ['SYSTEMCTL_STUB_LOG'], 'a') as output:
    output.write('--user ' + ' '.join(args) + '\\n')
command = args.pop(0)
if os.environ.get('SYSTEMCTL_STUB_BUS_DOWN'):
    sys.exit('Failed to connect to bus: No medium found')
if os.environ.get('SYSTEMCTL_STUB_FAIL') == command:
    sys.exit(1)
state_file = os.environ['SYSTEMCTL_STUB_STATE']
lock = open(state_file + '.lock', 'a')
fcntl.flock(lock, fcntl.LOCK_EX)
state = {}
if os.path.exists(state_file):
    with open(state_file) as source:
        state = json.load(source)
def alive(pid):
    if not pid:
        return False
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() and not result.stdout.strip().startswith('Z')
def hup_default():
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
if command == 'is-enabled':
    sys.exit(0 if state.get('enabled') else 1)
elif command == 'is-active':
    activity = os.environ.get('SYSTEMCTL_STUB_ACTIVITY') or ('active' if alive(state.get('pid')) else 'inactive')
    sys.exit(0 if activity == 'active' else 3)
elif command == 'show':
    print('LoadState=loaded')
    print('ActiveState=' + (os.environ.get('SYSTEMCTL_STUB_ACTIVITY') or
                            ('active' if alive(state.get('pid')) else 'inactive')))
    print('UnitFileState=' + ('enabled' if state.get('enabled') else 'disabled'))
    sys.exit(0)
elif command == 'enable':
    state['enabled'] = True
elif command == 'disable':
    state['enabled'] = False
elif command == 'start':
    if not alive(state.get('pid')):
        dest = os.path.join(os.environ['HOME'], '.local', 'share', 'gpt-gateway')
        unit = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.environ['HOME'], '.config'),
                            'systemd', 'user', 'gpt-gateway.service')
        values = {}
        environment = dict(os.environ)
        with open(unit) as source:
            for line in source:
                key, separator, value = line.strip().partition('=')
                if not separator:
                    continue
                if key == 'Environment':
                    name, _, setting = json.loads(value).replace('%%', '%').partition('=')
                    environment[name] = setting
                else:
                    values[key] = value
        command_line = [item.replace('%%', '%') for item in shlex.split(values['ExecStart'].lstrip(':'))]
        stderr = values.get('StandardError', '')
        stderr = stderr[len('append:'):].replace('%%', '%') if stderr.startswith('append:') else state_file + '.journal'
        with open(stderr, 'ab') as errors:
            process = subprocess.Popen(command_line, env=environment,
                                       stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True,
                                       preexec_fn=hup_default)
        state['pid'] = process.pid
elif command == 'stop':
    pid = state.get('pid')
    if alive(pid):
        os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 25
        while alive(pid) and time.monotonic() < end:
            time.sleep(0.05)
        if alive(pid):
            sys.exit('stub systemctl: supervisor did not stop')
    state.pop('pid', None)
elif command == 'kill':
    assert args[:2] == ['-s', 'HUP'], args
    if '--kill-whom=main' in args:
        os.kill(state['pid'], signal.SIGHUP)
    else:
        os.killpg(state['pid'], signal.SIGHUP)
elif command not in ('daemon-reload', 'reset-failed', 'show-environment'):
    sys.exit('stub systemctl: unexpected command ' + command)
with open(state_file, 'w') as output:
    json.dump(state, output)
'''

from tests import jail

LAUNCHCTL = """#!/bin/sh
echo "$*" >> "$LAUNCHCTL_STUB_LOG"
case "$1" in
  print) exit 1 ;;
  print-disabled) [ -n "${LAUNCHCTL_STUB_DISABLED:-}" ] && printf '\\t"%s" => disabled\\n' "$LAUNCHCTL_STUB_DISABLED"; exit 0 ;;
  *) exit 0 ;;
esac
"""
PASSING = "import unittest\n\n\nclass Ok(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
FAILING = "import unittest\n\n\nclass Bad(unittest.TestCase):\n    def test_bad(self):\n        self.fail('no')\n"
HANGING = ("import threading\nimport unittest\n\n\nclass Stuck(unittest.TestCase):\n"
           "    def test_stuck(self):\n        threading.Event().wait()\n")


class Host:
    """A scratch HOME with Claude Code settings, a source tree with a one-test suite, and the stub on PATH."""

    def __init__(self, test, tests_pass=True, hangs=False, platform='Darwin'):
        self.platform = platform
        self.root = jail.scratch(test)
        self.home = os.path.join(self.root, "home")
        self.src = os.path.join(self.root, "src")
        self.claude = os.path.join(self.home, ".claude", "settings.json")
        self.dest = os.path.join(self.home, ".local", "share", "gpt-gateway")
        self.plist = os.path.join(self.home, "Library", "LaunchAgents", "local.gpt-gateway.plist")
        self.calls = os.path.join(self.root, "launchctl.calls")
        self.systemctl_log = os.path.join(self.root, 'systemctl.calls')
        self.systemctl_state = os.path.join(self.root, 'systemctl.state')
        self.unit = os.path.join(self.home, '.config', 'systemd', 'user', 'gpt-gateway.service')
        self.port = jail.free_port()
        assert self.port != 18787, 'the jail must never bind the live port'
        self.env = {}
        os.makedirs(os.path.dirname(self.claude))
        self.write_claude({"env": {"KEEP": "1"}})
        for item in dict.fromkeys(jail.ship_list() + ["middleware", "service"]):
            src, target = os.path.join(jail.ROOT, item), os.path.join(self.src, item)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, target)
        os.makedirs(os.path.join(self.src, "tests"))
        open(os.path.join(self.src, "tests", "__init__.py"), "w").close()
        with open(os.path.join(self.src, "tests", "test_one.py"), "w") as f:
            f.write(HANGING if hangs else PASSING if tests_pass else FAILING)
        bin_dir = os.path.join(self.root, "bin")
        os.makedirs(bin_dir)
        for name, source in {'launchctl': LAUNCHCTL, 'systemctl': SYSTEMCTL, 'plutil': PLUTIL,
                             'uname': '#!/bin/sh\nprintf "%s\\n" ' + platform + '\n',
                             'xcode-select': '#!/bin/sh\nexit 0\n'}.items():
            with open(os.path.join(bin_dir, name), 'w') as f:
                f.write(source)
            os.chmod(os.path.join(bin_dir, name), 0o755)
        self.path = bin_dir + ":/usr/bin:/bin:/usr/sbin:/sbin"
        found = subprocess.run(["/bin/sh", "-c", "command -v launchctl"], env={"PATH": self.path},
                               capture_output=True, text=True).stdout.strip()
        assert found == os.path.join(bin_dir, "launchctl"), "the stub launchctl is not first on PATH: %s" % found
        found = shutil.which('systemctl', path=self.path)
        assert found == os.path.join(bin_dir, 'systemctl'), 'the stub systemctl must be first on PATH'
        test.addCleanup(self.cleanup)

    def cleanup(self):
        if os.path.exists(self.systemctl_state):
            with open(self.systemctl_state) as source:
                pid = json.load(source).get('pid')
            if pid:
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    pass

    def environment(self, disabled=True):
        return jail.clean_env(self.home, PATH=self.path, LAUNCHCTL_STUB_LOG=self.calls,
                              LAUNCHCTL_STUB_DISABLED='local.gpt-gateway' if disabled else '',
                              SYSTEMCTL_STUB_LOG=self.systemctl_log, SYSTEMCTL_STUB_STATE=self.systemctl_state,
                              GPT_GATEWAY_RETRY_SECONDS='0', **self.env)

    def write_claude(self, data):
        with open(self.claude, "w") as f:
            json.dump(data, f)

    def read_claude(self):
        with open(self.claude) as f:
            return json.load(f)

    def run(self, script, *args, disabled=True):
        env = self.environment(disabled)
        p = subprocess.run(["/bin/sh", os.path.join(self.src, "service", script)] + list(args), env=env,
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def install(self, *args):
        return self.run("install.sh", "--port", str(self.port), *args)

    def launchctl_calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    def plist_value(self, key):
        return subprocess.run([os.path.join(self.root, 'bin', 'plutil'), "-extract", key, "raw", self.plist],
                              capture_output=True, text=True).stdout.strip()

    def ctl(self, verb):
        result = subprocess.run(['/bin/sh', os.path.join(self.dest, 'ctl.sh'), verb], env=self.environment(),
                                capture_output=True, text=True, timeout=40)
        return result.returncode, result.stdout + result.stderr

    def systemctl_calls(self):
        if not os.path.exists(self.systemctl_log):
            return []
        with open(self.systemctl_log) as source:
            return source.read().splitlines()
