"""Tests for service/ctl.sh, against an installed copy in a scratch HOME and the stub launchctl."""
import os
import subprocess
import unittest

from tests import jail
from tests.service.stub import Host


class CtlTest(unittest.TestCase):
    def ctl(self, h, *args, disabled=True):
        env = h.environment(disabled)
        p = subprocess.run(["/bin/sh", os.path.join(h.dest, "ctl.sh")] + list(args), env=env,
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout + p.stderr

    def test_status_of_an_installed_gateway_that_is_off(self):
        h = Host(self)
        self.assertEqual(h.install()[0], 0)
        code, out = self.ctl(h, "status")
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "state:  off\nhealth: not answering\nClaude Code: straight to the API\n")
        code, out = self.ctl(h, "status", disabled=False)
        self.assertIn("state:  off until the next login (not disabled)", out)

    def test_usage_and_not_installed(self):
        h = Host(self)
        self.assertEqual(h.install()[0], 0)
        code, out = self.ctl(h, "sideways")
        self.assertEqual((code, out), (2, "usage: ctl.sh start|stop|restart|status|on|off\n"))
        self.assertEqual(self.ctl(h, "restart")[0], 1)  # the stub says not loaded
        os.remove(h.plist)
        code, out = self.ctl(h, "status")
        self.assertEqual(code, 1)
        self.assertIn("ctl.sh: not installed", out)


if __name__ == "__main__":
    unittest.main()
