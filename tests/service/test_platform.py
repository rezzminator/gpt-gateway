"""Linux user-service lifecycle through a jailed systemctl, with real supervisors on free ports."""
import http.client
import json
import os
import shutil
import re
import subprocess
import tarfile
import threading
import time
import unittest
import urllib.request

from tests import fakes, jail
from tests.service.stub import Host


class PlatformTest(unittest.TestCase):
    def rollback(self, fails=False):
        host = self.installed()
        backup = os.path.join(host.root, 'rollback')
        os.mkdir(backup)
        with tarfile.open(os.path.join(backup, 'install.tgz'), 'w:gz') as archive:
            archive.add(host.dest, arcname='gpt-gateway')
        shutil.copy2(host.unit, os.path.join(backup, 'gpt-gateway.service'))
        with open(os.path.join(backup, 'state'), 'w') as output:
            output.write('on\n')
        with open(os.path.join(backup, 'claude-settings.json'), 'w') as output:
            json.dump({'env': {'KEEP': 'old'}}, output)
        current = host.read_claude()
        current['env']['KEEP'] = 'new'
        current['env']['AFTER'] = 'preserve'
        current['plugins'] = {'changed': True}
        host.write_claude(current)
        if fails:
            host.env['SYSTEMCTL_STUB_FAIL'] = 'start'
        with open(os.path.join(jail.ROOT, 'docs', 'operations', 'linux-service.md')) as source:
            blocks = re.findall(r'```sh\n(.*?)\n```', source.read(), re.S)
        result = subprocess.run(['/bin/sh', '-c', blocks[-1]], cwd=host.src,
                                env=dict(host.environment(), gateway_backup=backup),
                                capture_output=True, text=True, timeout=40)
        return host, result

    def test_runbook_rollback_keeps_later_shared_settings_edits(self):
        host, result = self.rollback()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        settings = host.read_claude()
        self.assertEqual(settings['env'], {'KEEP': 'new', 'AFTER': 'preserve',
                                          'ANTHROPIC_BASE_URL': self.url(host), 'ENABLE_TOOL_SEARCH': 'true'})
        self.assertEqual(settings['plugins'], {'changed': True})

    def test_runbook_failed_restart_stops_with_the_gateway_keys_unlinked(self):
        host, result = self.rollback(fails=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Rollback completed', result.stdout)
        self.assertEqual(host.read_claude(), {'env': {'KEEP': 'new', 'AFTER': 'preserve'},
                                            'plugins': {'changed': True}})

    def test_ctl_uses_the_jq_found_on_path_to_read_the_service_record(self):
        host = Host(self, platform='Linux')
        actual = shutil.which('jq')
        wrapper = os.path.join(host.root, 'bin', 'jq')
        calls = os.path.join(host.root, 'jq.calls')
        with open(wrapper, 'w') as output:
            output.write('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + calls + '\nexec ' + actual + ' "$@"\n')
        os.chmod(wrapper, 0o755)
        code, out = host.install()
        self.assertEqual(code, 0, out)
        open(calls, 'w').close()
        code, out = host.ctl('status')
        self.assertEqual(code, 0, out)
        with open(calls) as source:
            self.assertIn(os.path.join(host.dest, 'service.json'), source.read())
        open(calls, 'w').close()
        code, out = host.run('uninstall.sh')
        self.assertEqual(code, 0, out)
        with open(calls) as source:
            self.assertIn(os.path.join(host.dest, 'service.json'), source.read())

    def test_uninstall_recovers_the_unit_environment_without_a_service_record(self):
        for broken in ('missing', 'invalid'):
            with self.subTest(record=broken):
                host = Host(self, platform='Linux')
                host.env['GPT_GATEWAY_CLAUDE_SETTINGS'] = os.path.join(host.home, 'other settings % space.json')
                jail.write_json(host.env['GPT_GATEWAY_CLAUDE_SETTINGS'], {'env': {'KEEP': '1'}})
                code, out = host.install()
                self.assertEqual(code, 0, out)
                self.assertEqual(host.ctl('stop')[0], 0)
                jail.write_json(host.env['GPT_GATEWAY_CLAUDE_SETTINGS'],
                                {'env': {'KEEP': 'new', 'ANTHROPIC_BASE_URL': self.url(host), 'ENABLE_TOOL_SEARCH': 'true'}})
                host.env.pop('GPT_GATEWAY_CLAUDE_SETTINGS')
                record = os.path.join(host.dest, 'service.json')
                if broken == 'missing':
                    os.remove(record)
                else:
                    jail.write_json(record, '{broken')
                code, out = host.run('uninstall.sh')
                self.assertEqual(code, 0, out)
                with open(os.path.join(host.home, 'other settings % space.json')) as source:
                    self.assertEqual(json.load(source)['env'], {'KEEP': 'new'})
                self.assertIn('--user disable gpt-gateway.service', host.systemctl_calls())

    def test_linux_status_reads_activity_and_preserves_unknown_bus_state(self):
        host = self.installed()
        self.assertEqual(host.ctl('stop')[0], 0)
        with open(host.systemctl_state) as source:
            state = json.load(source)
        state['enabled'] = True
        jail.write_json(host.systemctl_state, state)
        for activity in ('inactive', 'failed'):
            host.env['SYSTEMCTL_STUB_ACTIVITY'] = activity
            code, out = host.ctl('status')
            self.assertEqual(code, 0, out)
            self.assertIn('state:  off', out)
            self.assertIn('activity: ' + activity, out)
        host.env['SYSTEMCTL_STUB_BUS_DOWN'] = '1'
        code, out = host.ctl('status')
        self.assertNotEqual(code, 0, out)
        self.assertIn('state:  unknown', out)

    def test_install_checks_the_minimum_systemd_before_changing_the_install(self):
        host = Host(self, platform='Linux')
        host.env['SYSTEMCTL_STUB_VERSION'] = '251'
        code, out = host.install()
        self.assertEqual(code, 1, out)
        self.assertIn('systemd 252+', out)
        self.assertEqual(host.read_claude()['env'], {'KEEP': '1'})
        host.env['SYSTEMCTL_STUB_VERSION'] = '252'
        code, out = host.install()
        self.assertEqual(code, 0, out)
        self.assertIn('state:  on', host.ctl('status')[1])

    def installed(self, **kwargs):
        host = Host(self, platform='Linux', **kwargs)
        code, out = host.install()
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(host.unit), 'Linux must write a user unit')
        return host

    def url(self, host):
        return 'http://127.0.0.1:%d' % host.port

    def health_pid(self, host):
        try:
            with urllib.request.urlopen(self.url(host) + '/__gateway/health', timeout=1) as result:
                return int(result.read().split(b'pid=')[1].split()[0])
        except (OSError, ValueError):
            return None

    def test_fresh_install_writes_the_user_unit_and_enables_a_healthy_gateway(self):
        host = self.installed()
        self.assertEqual(host.ctl('status')[0], 0)
        self.assertIn('state:  on', host.ctl('status')[1])
        self.assertEqual(host.read_claude()['env'], {'KEEP': '1', 'ANTHROPIC_BASE_URL': self.url(host),
                                                    'ENABLE_TOOL_SEARCH': 'true'})
        with open(host.unit) as source:
            unit = source.read()
        for line in ('Restart=on-failure', 'KillMode=mixed', 'TimeoutStopSec=630', 'WantedBy=default.target'):
            self.assertIn(line, unit)
        self.assertIn('--user enable gpt-gateway.service', host.systemctl_calls())
        self.assertIn('--user start gpt-gateway.service', host.systemctl_calls())
        log = os.path.join(host.home, '.local', 'state', 'gpt-gateway', 'gpt-gateway-install-tests.log')
        self.assertTrue(os.path.isfile(log))
        stderr = os.path.join(host.home, '.local', 'state', 'gpt-gateway', 'gpt-gateway.stderr.log')
        messages = ''
        if os.path.isfile(stderr):
            with open(stderr) as source:
                messages = source.read()
        self.assertIn('linked: Claude Code uses ' + self.url(host) + ' with tool search on', messages)
        self.assertFalse(host.launchctl_calls(), 'Linux must use systemctl')

    def test_update_on_uses_hup_and_keeps_settings_and_user_middleware(self):
        host = self.installed()
        before = self.health_pid(host)
        own = os.path.join(host.dest, 'middleware', 'mine.py')
        with open(own, 'w') as output:
            output.write('NAME = "mine"\n')
        settings = os.path.join(host.dest, 'settings.json')
        jail.write_json(settings, {'middleware': [{'module': 'mine'}]})
        code, out = host.install()
        self.assertEqual(code, 0, out)
        self.assertTrue(jail.wait_for(lambda: self.health_pid(host) not in (None, before)), out)
        self.assertIn('--user kill -s HUP --kill-whom=main gpt-gateway.service', host.systemctl_calls())
        with open(settings) as source:
            self.assertEqual(json.load(source), {'middleware': [{'module': 'mine'}]})
        self.assertTrue(os.path.isfile(own))
        self.assertIn('state:  on', host.ctl('status')[1])

    def test_update_off_stays_off_and_start_stop_carry_the_state(self):
        host = self.installed()
        self.assertEqual(host.ctl('stop')[0], 0)
        self.assertEqual(host.read_claude()['env'], {'KEEP': '1'})
        calls = len(host.systemctl_calls())
        code, out = host.install()
        self.assertEqual(code, 0, out)
        self.assertIn('gateway is off', out)
        self.assertFalse(any(' start ' in call or ' kill ' in call for call in host.systemctl_calls()[calls:]))
        self.assertIn('state:  off', host.ctl('status')[1])
        self.assertEqual(host.ctl('restart')[0], 1)
        self.assertEqual(host.ctl('start')[0], 0)
        self.assertEqual(host.read_claude()['env']['ANTHROPIC_BASE_URL'], self.url(host))
        self.assertEqual(host.ctl('off')[0], 0)
        self.assertEqual(host.ctl('on')[0], 0)

    def test_supervisor_or_unit_changes_reload_without_changing_enabled_state(self):
        host = self.installed()
        before = self.health_pid(host)
        with open(os.path.join(host.src, 'service', 'run.sh'), 'a') as output:
            output.write('\n# refreshed supervisor\n')
        code, out = host.install()
        self.assertEqual(code, 0, out)
        self.assertTrue(jail.wait_for(lambda: self.health_pid(host) not in (None, before)), out)
        self.assertIn('--user stop gpt-gateway.service', host.systemctl_calls())
        self.assertEqual(host.read_claude()['env']['ANTHROPIC_BASE_URL'], self.url(host))
        self.assertEqual(host.ctl('stop')[0], 0)
        code, out = host.install('--port', str(jail.free_port()))
        self.assertEqual(code, 0, out)
        self.assertIn('state:  off', host.ctl('status')[1])

    def test_hup_through_systemctl_keeps_an_in_flight_stream(self):
        host = Host(self, platform='Linux')
        host.env['GPT_GATEWAY_UPSTREAM'] = fakes.start_upstream(self, fakes.SlowUpstream)
        code, out = host.install()
        self.assertEqual(code, 0, out)
        before = self.health_pid(host)
        got = []
        def stream():
            connection = http.client.HTTPConnection('127.0.0.1', host.port, timeout=20)
            try:
                connection.request('POST', '/v1/messages', body=b'{"model":"m","messages":[]}')
                got.append(connection.getresponse().read())
            except (OSError, http.client.HTTPException) as error:
                got.append(('stream failed: ' + str(error)).encode())
            finally:
                connection.close()
        thread = threading.Thread(target=stream)
        thread.start()
        time.sleep(0.3)
        code, out = host.ctl('restart')
        self.assertEqual(code, 0, out)
        self.assertTrue(jail.wait_for(lambda: self.health_pid(host) not in (None, before)), out)
        thread.join(20)
        self.assertIn(b'message_stop', got[0])
        self.assertEqual(host.read_claude()['env']['ANTHROPIC_BASE_URL'], self.url(host))

    def test_uninstall_stops_disables_unlinks_and_backs_up_user_files(self):
        host = self.installed()
        self.assertEqual(host.run('uninstall.sh')[0], 0)
        self.assertEqual(host.read_claude()['env'], {'KEEP': '1'})
        self.assertIn('--user disable gpt-gateway.service', host.systemctl_calls())
        self.assertIn('--user stop gpt-gateway.service', host.systemctl_calls())
        self.assertFalse(os.path.exists(host.unit))
        self.assertFalse(os.path.exists(host.dest))
        backups = os.path.join(host.home, '.local', 'state', 'gpt-gateway', 'backups')
        kept = [path for path in os.listdir(backups) if path.startswith('uninstall-')]
        self.assertEqual(len(kept), 1)
        self.assertTrue(os.path.isfile(os.path.join(backups, kept[0], 'settings.json')))

    def test_xdg_config_home_and_settings_symlink_are_recorded(self):
        host = Host(self, platform='Linux')
        host.env['XDG_CONFIG_HOME'] = os.path.join(host.home, 'custom config % $ space')
        actual = os.path.join(host.home, 'settings with % and space.json')
        os.rename(host.claude, actual)
        os.symlink(actual, host.claude)
        code, out = host.install()
        self.assertEqual(code, 0, out)
        host.unit = os.path.join(host.env['XDG_CONFIG_HOME'], 'systemd', 'user', 'gpt-gateway.service')
        self.assertTrue(os.path.isfile(host.unit))
        self.assertTrue(os.path.islink(host.claude))
        self.assertEqual(host.ctl('stop')[0], 0)
        self.assertEqual(host.read_claude()['env'], {'KEEP': '1'})

    def test_failing_suite_does_not_install_or_call_systemctl(self):
        host = Host(self, platform='Linux', tests_pass=False)
        code, out = host.install()
        self.assertEqual(code, 1)
        self.assertIn('FAIL: test_bad', out)
        self.assertEqual(host.systemctl_calls(), ['--user show-environment'])

    def test_uninstall_stale_settings_keeps_a_chosen_tool_search_value(self):
        host = self.installed()
        self.assertEqual(host.ctl('stop')[0], 0)
        host.write_claude({'env': {'ANTHROPIC_BASE_URL': self.url(host), 'ENABLE_TOOL_SEARCH': 'auto:5'}})
        code, out = host.run('uninstall.sh', '--purge')
        self.assertEqual(code, 0, out)
        self.assertEqual(host.read_claude()['env'], {'ENABLE_TOOL_SEARCH': 'auto:5'})
        self.assertFalse(os.path.exists(os.path.join(host.home, '.local', 'state', 'gpt-gateway', 'gpt-gateway.log')))


if __name__ == '__main__':
    unittest.main()
