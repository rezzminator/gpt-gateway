"""Fail-open settings errors are reported by both platform frontends and retried to the stop deadline."""
import json
import os
import subprocess
import unittest

from tests import jail
from tests.service.stub import Host


class SettingsTest(unittest.TestCase):
    def test_an_inaccessible_settings_directory_is_a_read_failure(self):
        home = jail.scratch(self)
        directory = os.path.join(home, 'private')
        os.mkdir(directory)
        settings = os.path.join(directory, 'settings.json')
        jail.write_json(settings, {})
        os.chmod(directory, 0)
        self.addCleanup(os.chmod, directory, 0o700)
        if os.access(directory, os.X_OK):
            self.skipTest('this user can search directories regardless of permission bits')
        env = jail.clean_env(home, SETTINGS=settings, URL='http://127.0.0.1:%d' % jail.free_port(), JQ='/usr/bin/jq')
        result = subprocess.run(['/bin/sh', '-c', '. "$1"; settings_linked; code=$?; [ "$code" -eq 2 ]',
                                 'test', os.path.join(jail.ROOT, 'service', 'settings.sh')],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, 'an inaccessible file was misreported as absent')

    def test_a_settings_rewrite_keeps_the_files_mode(self):
        home = jail.scratch(self)
        url = 'http://127.0.0.1:%d' % jail.free_port()
        for mode in (0o600, 0o640, 0o644):
            with self.subTest(mode=oct(mode)):
                settings = os.path.join(home, 'settings-%o.json' % mode)
                jail.write_json(settings, {'env': {'ANTHROPIC_BASE_URL': url, 'KEEP': '1'}})
                os.chmod(settings, mode)
                env = jail.clean_env(home, SETTINGS=settings, URL=url, JQ='/usr/bin/jq')
                result = subprocess.run(['/bin/sh', '-c', '. "$1"; settings_edit "del(.env.ANTHROPIC_BASE_URL)"',
                                         'test', os.path.join(jail.ROOT, 'service', 'settings.sh')],
                                        env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(os.stat(settings).st_mode & 0o777, mode)
                with open(settings) as source:
                    self.assertEqual(json.load(source), {'env': {'KEEP': '1'}})

    def test_missing_settings_are_definitely_not_linked(self):
        home = jail.scratch(self)
        env = jail.clean_env(home, SETTINGS=os.path.join(home, 'absent.json'),
                             URL='http://127.0.0.1:%d' % jail.free_port(), JQ='/usr/bin/jq')
        result = subprocess.run(['/bin/sh', '-c', '. "$1"; settings_linked; code=$?; '
                                 '[ "$code" -eq 1 ]', 'test',
                                 os.path.join(jail.ROOT, 'service', 'settings.sh')],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, 'missing settings were classified as a read error')

    def test_missing_settings_allow_status_stop_and_uninstall_on_both_platforms(self):
        for platform in ('Linux', 'Darwin'):
            for verb in ('status', 'stop', 'uninstall'):
                with self.subTest(platform=platform, verb=verb):
                    host = Host(self, platform=platform)
                    host.env['GPT_GATEWAY_STOP_SECONDS'] = '1'
                    code, out = host.install()
                    self.assertEqual(code, 0, out)
                    os.remove(host.claude)
                    code, out = host.run('uninstall.sh') if verb == 'uninstall' else host.ctl(verb)
                    self.assertEqual(code, 0, out)
                    self.assertNotIn('retrying', out)
                    if verb == 'status':
                        self.assertIn('settings file is missing', out)

    def test_ctl_off_leaves_a_linked_setting_to_the_supervisor(self):
        for platform in ('Linux', 'Darwin'):
            with self.subTest(platform=platform):
                host = Host(self, platform=platform)
                code, out = host.install()
                self.assertEqual(code, 0, out)
                if platform == 'Linux':
                    self.assertEqual(host.ctl('stop')[0], 0)
                linked = {'env': {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:%d' % host.port,
                                  'ENABLE_TOOL_SEARCH': 'true', 'KEEP': '1'}}
                host.write_claude(linked)
                code, out = host.ctl('stop')
                self.assertNotEqual(code, 0, out)
                self.assertIn('by hand', out)
                self.assertEqual(host.read_claude(), linked, 'ctl became a second settings writer')

    def test_ctl_and_uninstall_report_unreadable_settings_on_both_platforms(self):
        for platform in ('Linux', 'Darwin'):
            for verb in ('status', 'stop', 'uninstall'):
                with self.subTest(platform=platform, verb=verb):
                    host = Host(self, platform=platform)
                    host.env['GPT_GATEWAY_STOP_SECONDS'] = '1'
                    code, out = host.install()
                    self.assertEqual(code, 0, out)
                    if platform == 'Linux':
                        self.assertEqual(host.ctl('stop')[0], 0)
                    jail.write_json(host.claude, '{broken')
                    if verb == 'uninstall':
                        code, out = host.run('uninstall.sh')
                    else:
                        code, out = host.ctl(verb)
                    self.assertNotEqual(code, 0, out)
                    self.assertIn('could not read settings', out)
                    self.assertNotIn('Claude Code: straight to the API', out)


if __name__ == '__main__':
    unittest.main()
