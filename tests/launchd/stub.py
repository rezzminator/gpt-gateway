"""A source tree to install from (the shipped middleware included), a scratch HOME, and a stub launchctl,
for the launchd script tests.

The stub stands first on PATH: it records each call and reports the agent as never loaded, and
disabled when asked, so no test reaches the real launchd or the real home.
"""
import json
import os
import shutil
import subprocess

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


class Host:
    """A scratch HOME with Claude Code settings, a source tree with a one-test suite, and the stub on PATH."""

    def __init__(self, test, tests_pass=True):
        self.root = jail.scratch(test)
        self.home = os.path.join(self.root, "home")
        self.src = os.path.join(self.root, "src")
        self.claude = os.path.join(self.home, ".claude", "settings.json")
        self.dest = os.path.join(self.home, ".local", "share", "model-gateway")
        self.plist = os.path.join(self.home, "Library", "LaunchAgents", "local.model-gateway.plist")
        self.calls = os.path.join(self.root, "launchctl.calls")
        self.port = jail.free_port()
        os.makedirs(os.path.dirname(self.claude))
        self.write_claude({"env": {"KEEP": "1"}})
        for item in jail.ship_list() + ["middleware", "launchd/install.sh", "launchd/uninstall.sh"]:
            src, target = os.path.join(jail.ROOT, item), os.path.join(self.src, item)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, target)
        os.makedirs(os.path.join(self.src, "tests"))
        open(os.path.join(self.src, "tests", "__init__.py"), "w").close()
        with open(os.path.join(self.src, "tests", "test_one.py"), "w") as f:
            f.write(PASSING if tests_pass else FAILING)
        bin_dir = os.path.join(self.root, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "launchctl"), "w") as f:
            f.write(LAUNCHCTL)
        os.chmod(os.path.join(bin_dir, "launchctl"), 0o755)
        self.path = bin_dir + ":/usr/bin:/bin:/usr/sbin:/sbin"
        found = subprocess.run(["/bin/sh", "-c", "command -v launchctl"], env={"PATH": self.path},
                               capture_output=True, text=True).stdout.strip()
        assert found == os.path.join(bin_dir, "launchctl"), "the stub launchctl is not first on PATH: %s" % found

    def write_claude(self, data):
        with open(self.claude, "w") as f:
            json.dump(data, f)

    def read_claude(self):
        with open(self.claude) as f:
            return json.load(f)

    def run(self, script, *args, disabled=True):
        env = jail.clean_env(self.home, PATH=self.path, LAUNCHCTL_STUB_LOG=self.calls,
                             LAUNCHCTL_STUB_DISABLED="local.model-gateway" if disabled else "")
        p = subprocess.run(["/bin/sh", os.path.join(self.src, "launchd", script)] + list(args), env=env,
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
        return subprocess.run(["plutil", "-extract", key, "raw", self.plist], capture_output=True, text=True).stdout.strip()
