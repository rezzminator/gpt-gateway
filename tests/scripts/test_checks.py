"""Tests for scripts/checks.sh: each check runs against a copy of this tree, clean and with one violation planted."""
import os
import shutil
import subprocess
import unittest

from tests import jail

# Spelled apart, so this file never matches the census it tests.
PREDECESSOR = "adv" + "isor"
OLD_NAME = "cache" + "-gateway"
PRODUCT_VARIANT = "GPT" + " Gateway"
FORMER_NAME = "model" + "-gateway"
IDS = ("size-src", "size-test", "negation-dirs", "module-purpose", "test-mirror", "test-jail", "stray-runners",
       "facade-env", "facade-clock", "facade-log", "facade-settings", "facade-upstream", "facade-capture", "facade-db",
       "spelling-predecessor", "spelling-old-name", "spelling-product", "spelling-env-prefix", "spelling-kind",
       "dup-functions", "registry-parity", "orientation-truth")
CTL_FALLBACK = '  *) echo "usage: ctl.sh'


def listed():
    """Every file git tracks or would add in this repository."""
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=jail.ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\n") if p and os.path.isfile(os.path.join(jail.ROOT, p))]


class Tree:
    """A scratch git work tree holding a copy of every listed file of this repository."""

    def __init__(self, test):
        self.test = test
        self.root = jail.scratch(test)
        for rel in listed():
            target = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(jail.ROOT, rel), target)
        subprocess.run(["git", "init", "-q"], cwd=self.root, env=jail.clean_env(self.root), check=True,
                       capture_output=True)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def read(self, rel):
        with open(self.path(rel)) as f:
            return f.read()

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "w") as f:
            f.write(text)

    def append(self, rel, text):
        self.write(rel, self.read(rel) + text)

    def replace(self, rel, old, new):
        text = self.read(rel)
        self.test.assertIn(old, text, rel)
        self.write(rel, text.replace(old, new))

    def remove(self, rel):
        p = self.path(rel)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)

    def lines_of(self, rel, n):
        """Pad `rel` with comment lines to exactly n lines."""
        have = self.read(rel).count("\n")
        self.test.assertLessEqual(have, n)
        self.append(rel, "#\n" * (n - have))

    def start(self, *args):
        return subprocess.Popen(["/bin/bash", self.path("scripts/checks.sh")] + list(args),
                                env=jail.clean_env(self.root, PATH="/usr/bin:/bin"),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def finish(proc):
    """(exit code, {id: (status, detail)}, the whole output) of a started run."""
    out, _ = proc.communicate(timeout=120)
    lines = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0] == "CHECK":
            lines[parts[1]] = (parts[2], parts[3] if len(parts) == 4 else "")
    return proc.returncode, lines, out


def run(tree, *args):
    return finish(tree.start(*args))


def plant_sh_without_purpose(t):
    t.write("launchd/bare.sh", "#!/bin/sh\necho hi\n")


def add_ctl_arm(t):
    t.replace("launchd/ctl.sh", CTL_FALLBACK, "  sideways) ;;\n" + CTL_FALLBACK)


def strip_usage(t):
    for name in ("ctl.sh", "install.sh", "uninstall.sh", "run.sh"):
        t.replace("launchd/" + name, "usage: ", "use: ")


def ignore_sources(t):
    t.write(".gitignore", "*.py\n*.sh\n")


def unreadable(t):
    os.chmod(t.path("gpt_gateway/clock.py"), 0)


# (check id, plant one violation, a fragment its FAIL line names)
VIOLATIONS = (
    ("size-src", lambda t: t.lines_of("gpt_gateway/clock.py", 401), "gpt_gateway/clock.py (new 401)"),
    ("size-test", lambda t: t.lines_of("tests/jail.py", 601), "tests/jail.py (new 601)"),
    ("negation-dirs", lambda t: t.write("docs/misc/note.md", "x\n"), "new: docs/misc"),
    ("module-purpose", lambda t: t.write("gpt_gateway/stray.py", "x = 1\n"), "new: gpt_gateway/stray.py"),
    ("module-purpose", plant_sh_without_purpose, "new: launchd/bare.sh"),
    ("test-mirror", lambda t: t.write("gpt_gateway/stray.py", '"""A stray."""\n'), "new: gpt_gateway/stray.py"),
    ("test-mirror", lambda t: t.remove("tests/test_gateway.py"), "new: gateway.py"),
    ("test-jail", lambda t: t.append("tests/fakes.py", "import " + "temp" + "file\n"), "tests/fakes.py (new 1)"),
    ("stray-runners", lambda t: t.write("tools/run.sh", "#!/bin/sh\n# runs\n"), "new: tools/run.sh"),
    ("facade-env", lambda t: t.append("gpt_gateway/proxy.py", "X = os.environ\n"), "gpt_gateway/proxy.py (new 1)"),
    ("facade-clock", lambda t: t.append("gpt_gateway/proxy.py", "X = time.time()\n"),
     "gpt_gateway/proxy.py (new 1)"),
    ("facade-log", lambda t: t.append("gpt_gateway/proxy.py", "import logging\n"), "gpt_gateway/proxy.py (new 1)"),
    ("facade-log", lambda t: t.append("gpt_gateway/capture.py", "X = open('x', 'a')\n"),
     "gpt_gateway/capture.py (new 1)"),
    ("facade-settings", lambda t: t.append("gpt_gateway/cli.py", 'X = "settings.json"\n'),
     "gpt_gateway/cli.py (new 1)"),
    ("facade-upstream", lambda t: t.append("gpt_gateway/proxy.py", "X = http.client.HTTPConnection('h')\n"),
     "gpt_gateway/proxy.py (new 1)"),
    ("facade-db", lambda t: t.append("gpt_gateway/proxy.py", "import sqlite3\n"), "gpt_gateway/proxy.py (new 1)"),
    ("facade-db", lambda t: t.append("gpt_gateway/cli.py", "X = sqlite3.connect(':memory:')\n"),
     "gpt_gateway/cli.py (new 1)"),
    ("facade-capture", lambda t: t.append("gpt_gateway/proxy.py", 'X = "5-response.body"\n'),
     "gpt_gateway/proxy.py (new 1)"),
    ("spelling-predecessor", lambda t: t.append("README.md", PREDECESSOR.upper() + "\n"), "README.md (new 1)"),
    ("spelling-predecessor", lambda t: t.append("tests/fakes.py", "# " + PREDECESSOR + "\n"), "tests/fakes.py (new 1)"),
    ("spelling-old-name", lambda t: t.append("README.md", OLD_NAME + "\n"), "README.md (new 1)"),
    ("spelling-product", lambda t: t.append("README.md", PRODUCT_VARIANT + "\n"), "README.md (new 1)"),
    ("spelling-product", lambda t: t.append("README.md", FORMER_NAME + "\n"), "README.md (new 1)"),
    ("spelling-product", lambda t: t.append("launchd/ctl.sh", "LEGACY_NAME=" + FORMER_NAME + "\n"), "launchd/ctl.sh (new 1)"),
    ("spelling-product", lambda t: t.append("launchd/install.sh", "# " + FORMER_NAME + "\n"), "launchd/install.sh (new 1)"),
    ("spelling-env-prefix", lambda t: t.append("launchd/ctl.sh", "X=${OTHER_KNOB:-1}\n"), "new: OTHER_KNOB"),
    ("spelling-env-prefix", lambda t: t.append("gpt_gateway/env.py", 'STRAY = "STRAY_KNOB"\n'), "new: STRAY_KNOB"),
    ("spelling-kind", lambda t: t.append("gpt_gateway/proxy.py", 'X = " conv=main"\n'),
     "gpt_gateway/proxy.py (new 1)"),
    ("dup-functions", lambda t: t.append("gpt_gateway/hooks.py", "def request_row():\n    pass\n"),
     "new: request_row: gpt_gateway/hooks.py gpt_gateway/proxy.py"),
    ("registry-parity", add_ctl_arm, "new: ctl.sh:sideways"),
    ("orientation-truth", lambda t: t.append("README.md", "`docs/gone.md`\n"), "new: docs/gone.md"),
    ("orientation-truth", lambda t: t.append("README.md", "`GPT_GATEWAY_NOWHERE`\n"), "new: GPT_GATEWAY_NOWHERE"),
    ("orientation-truth", lambda t: t.write("docs/CLAUDE.md", "`gone.sh`\n"), "new: gone.sh"),
)

# (check id, break its enumerator, a fragment its ERROR line names)
ERRORS = (
    ("facade-env", lambda t: t.remove("scripts/baselines/facade-env.txt"), "baseline"),
    ("dup-functions", lambda t: t.remove("scripts/baselines/dup-functions.txt"), "baseline"),
    ("orientation-truth", lambda t: t.remove("README.md"), "README.md missing"),
    ("orientation-truth", lambda t: (t.write("README.md", "no citations\n"), t.write("CLAUDE.md", "no citations\n")), "no backticked citation parsed"),
    ("registry-parity", lambda t: t.remove("launchd"), "no dispatched case arm parsed"),
    ("registry-parity", strip_usage, "no usage line parsed"),
    ("spelling-env-prefix", lambda t: (t.remove("launchd"), t.remove("gpt_gateway/env.py")), "no knob names parsed"),
    ("setup", ignore_sources, "no sources listed"),
    ("setup", unreadable, "unreadable: gpt_gateway/clock.py"),
    ("setup", lambda t: t.write("docs/a b.md", "x\n"), "a path holds a space: docs/a b.md"),
)


class ChecksTest(unittest.TestCase):
    def test_the_tree_passes_every_check(self):
        code, lines, out = run(Tree(self))
        self.assertEqual(code, 0, out)
        self.assertEqual(tuple(lines), IDS, out)
        self.assertEqual({status for status, _ in lines.values()}, {"PASS"}, out)

    def test_each_violation_fails_its_check(self):
        planted = []
        for check, plant, _ in VIOLATIONS:
            tree = Tree(self)
            plant(tree)
            planted.append(tree.start())
        for (check, _, fragment), proc in zip(VIOLATIONS, planted):
            code, lines, out = finish(proc)
            with self.subTest(check=check, fragment=fragment):
                self.assertEqual(code, 1, out)
                self.assertEqual(lines[check][0], "FAIL", out)
                self.assertIn(fragment, lines[check][1])

    def test_an_enumerator_that_cannot_run_is_an_error(self):
        broken = []
        for check, brk, _ in ERRORS:
            tree = Tree(self)
            brk(tree)
            broken.append(tree.start())
        for (check, _, fragment), proc in zip(ERRORS, broken):
            code, lines, out = finish(proc)
            with self.subTest(check=check, fragment=fragment):
                self.assertEqual(code, 2, out)
                self.assertEqual(lines[check][0], "ERROR", out)
                self.assertIn(fragment, lines[check][1])

    def test_measure_adds_no_entry_to_an_existing_baseline(self):
        tree = Tree(self)
        tree.append("gpt_gateway/proxy.py", "X = os.environ\n")
        code, lines, out = run(tree, "--measure")
        self.assertEqual((code, lines["facade-env"]), (0, ("MEASURE", "0 in 0 keys")), out)
        self.assertEqual(tree.read("scripts/baselines/facade-env.txt"), "")
        self.assertEqual(run(tree)[1]["facade-env"][0], "FAIL")

    def test_a_count_baseline_holds_then_shrinks_on_measure(self):
        tree = Tree(self)
        original = tree.read("gpt_gateway/proxy.py")
        tree.remove("scripts/baselines/facade-env.txt")
        tree.append("gpt_gateway/proxy.py", "X = os.environ\n")
        self.assertEqual(run(tree, "--measure")[1]["facade-env"], ("MEASURE", "1 in 1 keys"))
        self.assertEqual(tree.read("scripts/baselines/facade-env.txt"), "gpt_gateway/proxy.py 1\n")
        self.assertEqual(run(tree)[1]["facade-env"], ("PASS", "1 in 1 keys, none above baseline"))
        tree.append("gpt_gateway/proxy.py", "Y = os.environ\n")
        self.assertEqual(run(tree)[1]["facade-env"], ("FAIL", "gpt_gateway/proxy.py (1->2)"))
        tree.write("gpt_gateway/proxy.py", original)
        self.assertEqual(run(tree)[1]["facade-env"], ("PASS", "0 in 0 keys, none above baseline"))
        run(tree, "--measure")
        self.assertEqual(tree.read("scripts/baselines/facade-env.txt"), "")

    def test_a_list_baseline_names_the_fixed_entries_until_measure_locks_them(self):
        tree = Tree(self)
        tree.remove("scripts/baselines/untested-sources.txt")
        tree.write("gpt_gateway/stray.py", '"""A stray."""\n')
        run(tree, "--measure")
        self.assertEqual(tree.read("scripts/baselines/untested-sources.txt"), "gpt_gateway/stray.py\n")
        self.assertEqual(run(tree)[1]["test-mirror"], ("PASS", "1 baselined, 0 new"))
        tree.remove("gpt_gateway/stray.py")
        self.assertEqual(run(tree)[1]["test-mirror"],
                         ("PASS", "0 baselined, 0 new; 1 fixed — run --measure to lock the shrink"))
        run(tree, "--measure")
        self.assertEqual(tree.read("scripts/baselines/untested-sources.txt"), "")

    def test_the_size_slack_absorbs_five_lines_over_a_baselined_count(self):
        tree = Tree(self)
        tree.write("scripts/baselines/ceiling-src.txt", "gpt_gateway/clock.py 401\n")
        tree.lines_of("gpt_gateway/clock.py", 406)
        self.assertEqual(run(tree)[1]["size-src"][0], "PASS")
        tree.lines_of("gpt_gateway/clock.py", 407)
        self.assertEqual(run(tree)[1]["size-src"], ("FAIL", "gpt_gateway/clock.py (401->407)"))

    def test_an_unknown_mode_is_refused(self):
        code, _, out = run(Tree(self), "--bogus")
        self.assertEqual((code, out), (2, "usage: checks.sh [--measure]\n"))


if __name__ == "__main__":
    unittest.main()
