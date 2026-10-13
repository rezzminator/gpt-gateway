"""Tests for scripts/gate.sh, run against a scratch tree whose checks script, shellcheck and tests are stubs."""
import os
import shutil
import subprocess
import unittest

from tests import jail

PASSING = "import unittest\n\n\nclass Ok(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
FAILING = "import unittest\n\n\nclass Bad(unittest.TestCase):\n    def test_bad(self):\n        self.fail('no')\n"
SKIPPING = "import unittest\n\n\nclass Skip(unittest.TestCase):\n    def test_skip(self):\n        self.skipTest('later')\n"
EMPTY = "import unittest\n"
# Passes only in a clean environment: no inherited variable and an empty HOME.
CLEAN = """import os
import unittest


class Clean(unittest.TestCase):
    def test_clean(self):
        self.assertNotIn("GATE_TEST_PROBE", os.environ)
        self.assertEqual(os.listdir(os.environ["HOME"]), [])
"""
CHECKS_PASS = 'echo "CHECK stub-check PASS 0 new"\nexit 0\n'
SC_PASS = "exit 0\n"
CLEAN_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class Tree:
    """A scratch git work tree: this repository's gate.sh beside stub links."""

    def __init__(self, test, *, checks=CHECKS_PASS, shellcheck=SC_PASS, tests=PASSING, source="x = 1\n"):
        self.root = jail.scratch(test)
        os.makedirs(os.path.join(self.root, "scripts"))
        shutil.copy2(os.path.join(jail.ROOT, "scripts", "gate.sh"), os.path.join(self.root, "scripts", "gate.sh"))
        self.write("scripts/checks.sh", "#!/usr/bin/env bash\n# A stub checks script.\n" + checks)
        self.write("a.py", source)
        self.write("b.sh", "#!/bin/sh\n# A script.\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_one.py", tests)
        self.bin = os.path.join(self.root, 'bin')
        self.clean_path = test.clean_path
        if shellcheck is not None:
            self.write("bin/shellcheck", "#!/bin/sh\n" + shellcheck)
            os.chmod(os.path.join(self.bin, "shellcheck"), 0o755)
        self.write(".gitignore", "bin/\n")
        subprocess.run(['git', 'init', '-q'], cwd=self.root, env=jail.clean_env(self.root),
                       check=True, capture_output=True)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def gate(self, *args):
        env = jail.clean_env(self.root, PATH=self.bin + ':' + self.clean_path, GATE_TEST_PROBE="1")
        p = subprocess.run(["/bin/bash", os.path.join(self.root, "scripts", "gate.sh")] + list(args), env=env,
                           capture_output=True, text=True, timeout=120)
        lines = {}
        for line in p.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[0] == "GATE" and parts[2] in ("PASS", "FAIL", "ERROR"):
                lines[parts[1]] = (parts[2], parts[3] if len(parts) == 4 else "")
        last = p.stdout.splitlines()[-1] if p.stdout else ""
        return p.returncode, lines, last, p.stdout + p.stderr


class GateTest(unittest.TestCase):
    def setUp(self):
        self.clean_path = os.path.join(jail.scratch(self), 'commands')
        os.mkdir(self.clean_path)
        for command in ('bash', 'dirname', 'mktemp', 'rm', 'mkdir', 'grep', 'wc', 'tr', 'sed',
                        'sort', 'paste', 'awk', 'tail', 'cat', 'env', 'cut', 'git'):
            os.symlink(shutil.which(command, path=CLEAN_PATH), os.path.join(self.clean_path, command))
        self.assertIsNone(shutil.which('shellcheck', path=self.clean_path),
                          'a shellcheck outside the stub would hide it')

    def test_every_link_passing_is_a_pass(self):
        tree = Tree(self, tests=CLEAN)
        code, lines, last, out = tree.gate()
        self.assertEqual((code, last), (0, "GATE PASS"), out)
        self.assertEqual(lines["compile"], ("PASS", "3 files"))
        self.assertEqual(lines["shellcheck"], ("PASS", "3 scripts"))
        self.assertEqual(lines["checks"], ("PASS", "1 checks"))
        self.assertEqual(lines["tests"][0], "PASS")
        self.assertTrue(lines["tests"][1].startswith("1 test in "), lines["tests"])
        self.assertIn("CHECK stub-check PASS 0 new", out)
        self.assertFalse(os.path.exists(os.path.join(tree.root, "__pycache__")))

    def test_a_failing_link_fails_the_gate(self):
        cases = (
            ("compile", dict(source="x = (\n"), "a.py", "GATE FAIL compile"),
            ("shellcheck", dict(shellcheck='echo "In b.sh line 1:"\nexit 1\n'), "b.sh", "GATE FAIL shellcheck"),
            ("checks", dict(checks='echo "CHECK size-src FAIL new: x"\nexit 1\n'), "size-src", "GATE FAIL checks"),
            ("tests", dict(tests=FAILING), "1 of 1 failed", "GATE FAIL tests"),
            ("tests", dict(tests="import nowhere_at_all\n"), "1 of 1 failed", "GATE FAIL tests"),
        )
        for link, kwargs, fragment, verdict in cases:
            with self.subTest(link=link):
                code, lines, last, out = Tree(self, **kwargs).gate()
                self.assertEqual((code, last), (1, verdict), out)
                self.assertEqual(lines[link][0], "FAIL", out)
                self.assertIn(fragment, lines[link][1])

    def test_a_link_that_did_not_run_is_an_error(self):
        cases = (
            ("shellcheck", dict(shellcheck=None), "shellcheck not found on PATH — brew install shellcheck"),
            ("shellcheck", dict(shellcheck="exit 3\n"), "shellcheck exited 3"),
            ("checks", dict(checks="exit 0\n"), "no CHECK line printed (exit 0)"),
            ("checks", dict(checks='echo "CHECK facade-env ERROR baseline missing"\nexit 2\n'), "facade-env (exit 2)"),
            ("tests", dict(tests=EMPTY), "Ran 0 tests"),
            ("tests", dict(tests=SKIPPING), "1 of 1 skipped — a skipped test is a check that did not run"),
        )
        for link, kwargs, fragment in cases:
            with self.subTest(link=link, fragment=fragment):
                code, lines, last, out = Tree(self, **kwargs).gate()
                self.assertEqual(lines[link], ("ERROR", fragment), out)
                self.assertEqual(code, 2, out)

    def test_an_error_outranks_a_failure_and_names_both(self):
        code, lines, last, out = Tree(self, source="x = (\n", shellcheck=None).gate()
        self.assertEqual((code, last), (2, "GATE ERROR shellcheck · FAIL compile"), out)

    def test_extra_arguments_are_refused(self):
        code, _, _, out = Tree(self).gate("now")
        self.assertEqual((code, out), (2, "usage: gate.sh\n"))


if __name__ == "__main__":
    unittest.main()
