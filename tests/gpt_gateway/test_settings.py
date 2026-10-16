"""Tests for gpt_gateway.settings: env > file > default, hot reload with last-good, the middleware list
fixed until a restart, and each module's settings validated from each source."""
import os
import json
from unittest.mock import patch
import unittest

from gpt_gateway.middleware import MiddlewareError
from gpt_gateway.log import Log
from gpt_gateway.settings import Settings, SettingsError
from tests import jail

POSITIVE = '''
NAME = "pos"

def positive(value):
    value = int(value)
    if value <= 0:
        raise ValueError("must be > 0, got %r" % (value,))
    return value

def check(settings):
    if settings["low"] > settings["high"]:
        raise ValueError("low above high")

SETTINGS = {"low": (1, positive, "GPT_GATEWAY_POS_LOW"), "high": (5, positive, None)}
'''


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = jail.scratch(self)
        self.path = os.path.join(self.tmp, "settings.json")
        self.log = os.path.join(self.tmp, "gw.log")
        self.mdir = os.path.join(self.tmp, "middleware")
        self.lines = []  # the text log
        self.events = []  # the database

    def write(self, data, bump=0):
        jail.write_json(self.path, data, bump)

    def settings(self, **env):
        return Settings(self.path, env, self.lines.append, lambda kind, **fields: self.events.append((kind, fields)))

    def loaded(self, **env):
        s = self.settings(**env)
        s.load()
        return s


class CoreSettingsTest(SettingsCase):
    def test_last_good_reload_emits_one_warning_and_keeps_the_snapshot(self):
        self.write({"capture_dir": "/a"})
        with patch("gpt_gateway.env.snapshot", return_value={"HOME": self.tmp, "PLUGIN_LOG_DIR": self.tmp}):
            log = Log(self.log)
        settings = Settings(self.path, {}, log.write)
        settings.load()
        self.write({"capture_dir": "relative"}, bump=1)
        self.assertEqual(settings.current().capture_dir, "/a")
        settings.current()
        path = os.path.join(self.tmp, "gpt-gateway", "warnings.jsonl")
        self.assertTrue(os.path.isfile(path), "last-good reload must also reach warnings.jsonl")
        with open(path) as source:
            row, = [json.loads(line) for line in source]
        self.assertEqual((row["level"], row["event"], row["where"]),
                         ("warning", "settings-kept-last-good", "gpt_gateway/settings.py#reload"))
        with open(self.log) as source:
            self.assertIn(row["msg"], source.read())

    def test_chatgpt_precedence_validation_and_restart(self):
        for raw in ("1", "true", "on", "TRUE"):
            with self.subTest(raw=raw):
                snap = self.loaded(GPT_GATEWAY_CHATGPT=raw).current()
                self.assertIs(getattr(snap, "chatgpt", False), True)
        self.write({"chatgpt": True})
        self.assertTrue(self.loaded().current().chatgpt)
        for raw in ("0", "false", "off"):
            self.assertFalse(self.loaded(GPT_GATEWAY_CHATGPT=raw).current().chatgpt)
        s = self.loaded()
        self.write({"chatgpt": False}, bump=1)
        self.assertTrue(s.current().chatgpt)
        self.assertIn("restart to apply", s.current().state)
        for value in (1, "true", [], {}):
            self.write({"chatgpt": value})
            with self.assertRaisesRegex(SettingsError, "chatgpt"):
                self.loaded()
        self.write({"chatgpt": None})
        self.assertFalse(self.loaded().current().chatgpt)
        with self.assertRaisesRegex(SettingsError, "GPT_GATEWAY_CHATGPT"):
            self.loaded(GPT_GATEWAY_CHATGPT="yes")

    def test_missing_file_means_defaults_not_an_error(self):
        snap = self.loaded().current()
        self.assertEqual((snap.capture_dir, snap.middleware_dir, snap.modules, snap.state),
                         (None, self.mdir, (), "missing"))
        self.assertEqual(dict(snap.sources), {"capture_dir": "default", "middleware_dir": "default",
                                              "middleware": "default", "chatgpt": "default",
                                              "db_retention_days": "default", "chatgpt_cache_retention": "default"})
        self.assertEqual((snap.db_retention_days, snap.chatgpt_cache_retention), (30, "off"))

    def test_reads_the_file(self):
        self.write({"capture_dir": "/cap", "middleware_dir": "/mw", "middleware": []})
        snap = self.loaded().current()
        self.assertEqual((snap.capture_dir, snap.middleware_dir, snap.state), ("/cap", "/mw", "ok"))
        self.assertEqual(dict(snap.sources), {"capture_dir": "file", "middleware_dir": "file", "middleware": "file",
                                              "chatgpt": "default", "db_retention_days": "default",
                                              "chatgpt_cache_retention": "default"})

    def test_env_wins_over_the_file_which_wins_over_the_default(self):
        self.write({"capture_dir": "/cap", "middleware_dir": "/mw"})
        snap = self.loaded(GPT_GATEWAY_CAPTURE_DIR="/envcap", GPT_GATEWAY_MIDDLEWARE_DIR="/envmw").current()
        self.assertEqual((snap.capture_dir, snap.middleware_dir), ("/envcap", "/envmw"))
        self.assertEqual((snap.sources["capture_dir"], snap.sources["middleware_dir"]), ("env", "env"))
        self.assertIsNone(self.loaded(GPT_GATEWAY_CAPTURE_DIR="off").current().capture_dir)
        self.assertEqual(self.loaded(GPT_GATEWAY_CAPTURE_DIR=" ").current().capture_dir, "/cap")
        self.assertEqual(self.loaded(GPT_GATEWAY_CAPTURE_DIR="~/c").current().capture_dir, os.path.expanduser("~/c"))

    def test_every_bad_input_is_refused_by_name(self):
        cases = [("{not json", "not valid JSON"), ("[]", "must be a JSON object"),
                 ({"capture_dir": None, "ttl": "5m"}, "unknown key(s) ttl"),
                 ({"capture_dir": 3}, "capture_dir must be an absolute path or null"),
                 ({"capture_dir": "relative/dir"}, "capture_dir must be an absolute path"),
                 ({"middleware_dir": ""}, "middleware_dir must be an absolute path"),
                 ({"middleware": {}}, "middleware must be a list"),
                 ({"middleware": ["x"]}, "middleware[0] must be an object"),
                 ({"middleware": [{"module": "a", "extra": 1}]}, "middleware[0]: unknown key(s) extra"),
                 ({"middleware": [{"module": "Bad-Name"}]}, "module must match ^[a-z][a-z0-9_]*"),
                 ({"middleware": [{"module": "a"}, {"module": "a"}]}, "middleware[1]: module a appears twice"),
                 ({"middleware": [{"module": "a", "settings": []}]}, "settings must be an object")]
        for data, message in cases:
            self.write(data)
            with self.assertRaises(SettingsError) as cm:
                self.loaded()
            self.assertIn(message, str(cm.exception), data)

    def test_db_retention_days_is_a_whole_number_of_days_at_least_one(self):
        self.write({"db_retention_days": 7})
        s = self.loaded()
        self.assertEqual((s.current().db_retention_days, s.current().sources["db_retention_days"]), (7, "file"))
        self.write({"db_retention_days": 90}, bump=1)
        self.assertEqual(s.current().db_retention_days, 90)  # applies without a restart
        for bad in (0, -1, True, 1.5, "30", None):
            self.write({"db_retention_days": bad})
            with self.assertRaisesRegex(SettingsError, "db_retention_days must be a whole number of days >= 1, got"):
                self.loaded()

    def test_chatgpt_cache_retention_is_off_or_24h_and_applies_without_a_restart(self):
        s = self.loaded()
        self.assertEqual((s.current().chatgpt_cache_retention, s.current().sources["chatgpt_cache_retention"]),
                         ("off", "default"))
        self.write({"chatgpt_cache_retention": "24h"}, bump=1)
        self.assertEqual((s.current().chatgpt_cache_retention, s.current().sources["chatgpt_cache_retention"]),
                         ("24h", "file"))
        self.assertEqual(self.events[-1][1]["chatgpt_cache_retention"], "24h")
        self.assertEqual(self.events[-1][0], "settings_reload")
        self.write({"chatgpt_cache_retention": "off"}, bump=2)
        self.assertEqual(s.current().chatgpt_cache_retention, "off")
        for bad in ("1h", "24H", "on", 24, True, None):
            self.write({"chatgpt_cache_retention": bad})
            with self.assertRaisesRegex(SettingsError, "chatgpt_cache_retention must be off or 24h, got"):
                self.loaded()

    def test_a_bad_env_value_is_refused_not_defaulted(self):
        for env in ({"GPT_GATEWAY_CAPTURE_DIR": "relative"}, {"GPT_GATEWAY_MIDDLEWARE_DIR": "rel/mw"}):
            with self.assertRaises(SettingsError) as cm:
                self.loaded(**env)
            self.assertIn("env " + list(env)[0], str(cm.exception))

    def test_an_edit_applies_at_the_next_read(self):
        self.write({"capture_dir": "/a"})
        s = self.loaded()
        self.write({"capture_dir": "/b"}, bump=1)
        self.assertEqual((s.current().capture_dir, s.current().state), ("/b", "ok"))
        self.assertEqual(self.lines, [])  # a reload goes to the database alone
        self.assertEqual(self.events[-1], ("settings_reload", {
            "path": self.path, "state": "ok", "db_retention_days": 30, "chatgpt_cache_retention": "off",
            "settings": "capture_dir=/b(file) middleware_dir=%s(default) middleware=[](default)" % self.mdir}))
        os.remove(self.path)
        snap = s.current()
        self.assertEqual((snap.capture_dir, snap.sources["capture_dir"], snap.state), (None, "default", "missing"))

    def test_a_bad_file_at_reload_keeps_the_last_good_settings(self):
        self.write({"capture_dir": "/a"})
        s = self.loaded()
        self.write({"capture_dir": "rel"}, bump=1)
        snap = s.current()
        self.assertEqual(snap.capture_dir, "/a")
        self.assertTrue(snap.state.startswith("error(") and "capture_dir must be an absolute path" in snap.state)
        s.current()
        self.assertEqual(sum("settings error, keeping the last good settings" in line for line in self.lines), 1)
        refused = [fields for kind, fields in self.events if kind == "settings_error"]
        self.assertEqual([f["path"] for f in refused], [self.path])
        self.assertIn("capture_dir must be an absolute path", refused[0]["error"])
        self.write({"capture_dir": "/c"}, bump=2)
        self.assertEqual((s.current().capture_dir, s.current().state), ("/c", "ok"))


class MiddlewareSettingsTest(SettingsCase):
    def setUp(self):
        super().setUp()
        jail.write_middleware(self.mdir, "pos", POSITIVE)
        jail.write_middleware(self.mdir, "other", 'NAME = "other"\n')

    def entry(self, **values):
        return {"middleware": [{"module": "pos", "settings": values}]}

    def test_each_source_env_file_default(self):
        self.write(self.entry(high=9))
        snap = self.loaded(GPT_GATEWAY_POS_LOW="2").current()
        self.assertEqual(dict(snap.module_settings["pos"]), {"low": 2, "high": 9})
        self.assertEqual(dict(self.loaded().current().module_settings["pos"]), {"low": 1, "high": 9})
        self.write(self.entry())
        self.assertEqual(dict(self.loaded().current().module_settings["pos"]), {"low": 1, "high": 5})
        self.assertEqual(dict(self.loaded(GPT_GATEWAY_POS_LOW="").current().module_settings["pos"])["low"], 1)

    def test_env_values_reach_the_validator_as_strings(self):
        jail.write_middleware(self.mdir, "raw", 'NAME = "raw"\nSEEN = []\n'
                                                'SETTINGS = {"v": (None, lambda v: SEEN.append(v) or v, "GPT_GATEWAY_RAW_V")}\n')
        self.write({"middleware": [{"module": "raw"}]})
        self.assertEqual(self.loaded(GPT_GATEWAY_RAW_V=" 12 ").current().module_settings["raw"]["v"], "12")

    def test_a_validator_error_names_the_module_the_key_and_the_source(self):
        self.write(self.entry(low=0))
        for env, where in (({"GPT_GATEWAY_POS_LOW": "-3"}, "low from env GPT_GATEWAY_POS_LOW: must be > 0"),
                           ({}, "low from file %s: must be > 0" % self.path)):
            with self.assertRaises(SettingsError) as cm:
                self.loaded(**env)
            self.assertIn("middleware pos: " + where, str(cm.exception))
        jail.write_middleware(self.mdir, "pos", POSITIVE.replace('(5, positive, None)', '(-5, positive, None)'))
        self.write(self.entry())
        with self.assertRaises(SettingsError) as cm:
            self.loaded()
        self.assertIn("middleware pos: high from default: must be > 0", str(cm.exception))

    def test_an_undeclared_setting_is_refused(self):
        self.write(self.entry(lowest=1))
        with self.assertRaises(SettingsError) as cm:
            self.loaded()
        self.assertIn("middleware pos: unknown setting(s) lowest; known: high, low", str(cm.exception))

    def test_check_runs_over_the_validated_settings(self):
        self.write(self.entry(low=7, high=3))
        with self.assertRaises(SettingsError) as cm:
            self.loaded()
        self.assertIn("middleware pos: check failed: low above high", str(cm.exception))

    def test_a_module_that_cannot_load_stops_the_load(self):
        self.write({"middleware": [{"module": "absent"}]})
        with self.assertRaises(MiddlewareError):
            self.loaded()

    def test_reload_revalidates_reruns_check_and_keeps_the_last_good(self):
        self.write(self.entry(high=9))
        s = self.loaded()
        self.write(self.entry(high=4), bump=1)
        self.assertEqual(s.current().module_settings["pos"]["high"], 4)
        self.write(self.entry(low=6, high=4), bump=2)
        snap = s.current()
        self.assertEqual(dict(snap.module_settings["pos"]), {"low": 1, "high": 4})
        self.assertIn("check failed: low above high", snap.state)
        self.write(self.entry(high=0), bump=3)
        self.assertIn("high from file", s.current().state)
        self.assertEqual(s.current().module_settings["pos"]["high"], 4)

    def test_a_changed_module_list_is_a_reload_error(self):
        self.write({"middleware": [{"module": "pos"}, {"module": "other"}]})
        s = self.loaded()
        for i, listed in enumerate(([{"module": "other"}, {"module": "pos"}], [{"module": "pos"}],
                                    [{"module": "pos"}, {"module": "other"}, {"module": "third"}]), 1):
            self.write({"middleware": listed}, bump=i)
            snap = s.current()
            self.assertIn("middleware list changed; restart to apply", snap.state)
            self.assertEqual(snap.modules, ("pos", "other"))
        self.assertEqual([m.module for m in s.modules], ["pos", "other"])
        self.assertIn("middleware list changed; restart to apply", self.lines[-1])

    def test_a_changed_middleware_dir_is_a_reload_error(self):
        self.write(self.entry())
        s = self.loaded()
        self.write(dict(self.entry(), middleware_dir="/elsewhere"), bump=1)
        self.assertIn("middleware_dir changed; restart to apply", s.current().state)
        self.assertEqual(s.current().middleware_dir, self.mdir)


if __name__ == "__main__":
    unittest.main()
