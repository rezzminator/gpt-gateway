"""Tests for gateway.py: it runs under -I from any directory and uses the package beside it."""
import os
import subprocess
import unittest

from tests import jail


class EntryTest(unittest.TestCase):
    def test_runs_isolated_from_another_directory_with_the_package_beside_it(self):
        tmp = jail.scratch(self)
        app = jail.stage_install(os.path.join(tmp, "app"))
        p = subprocess.run(["/usr/bin/python3", "-I", "-B", os.path.join(app, "gateway.py"), "check"],
                           cwd=tmp, env=jail.clean_env(tmp), capture_output=True, text=True, timeout=20)
        self.assertEqual((p.returncode, p.stdout), (0, "ok: %s missing, defaults apply\n" % os.path.join(app, "settings.json")))

    def test_importing_it_runs_nothing(self):
        p = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c",
                            "import importlib.util as u, sys; s = u.spec_from_file_location('g', sys.argv[1]); "
                            "m = u.module_from_spec(s); s.loader.exec_module(m); print(m.HERE)",
                            os.path.join(jail.ROOT, "gateway.py")],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual((p.returncode, p.stdout), (0, jail.ROOT + "\n"))


if __name__ == "__main__":
    unittest.main()
