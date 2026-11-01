"""Picker ownership and configuration through jailed install/uninstall entry points."""
import glob
import json
import os
import unittest

from tests.service.stub import Host

DESCRIPTION = "GPT via local gpt-gateway (ChatGPT plan)"
DEFAULT_ROWS = [
    {"model": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "description": DESCRIPTION},
    {"model": "gpt-6-astra", "label": "GPT-6 Astra", "description": DESCRIPTION},
]
NOVA_UNCHECKED = ("env GPT_GATEWAY_MODEL_PICKER_MODELS: model gpt-7-nova could not be checked against the catalog; "
                  "kept with its default label")
FOREIGN = {"model": "claude-custom", "label": "My model", "description": "User-owned"}


class PickerTest(unittest.TestCase):
    def host(self):
        host = Host(self)
        host.env["GPT_GATEWAY_CHATGPT"] = "on"
        return host

    def installed(self, host):
        code, out = host.install()
        self.assertEqual(code, 0, out)

    def configure(self, host, values):
        os.makedirs(host.dest, exist_ok=True)
        with open(os.path.join(host.dest, "settings.json"), "w") as target:
            json.dump(values, target)

    def test_defaults_idempotence_foreign_order_backup_and_private_mode(self):
        h = self.host()
        other = {"model": "gpt-user", "label": "Keep", "description": "Not ours"}
        original = {"env": {"KEEP": "1"}, "modelPicker": {
            "options": [FOREIGN, other], "replaceBuiltInOptions": False}}
        h.write_claude(original)
        os.chmod(h.claude, 0o600)
        self.installed(h)
        expected = {"env": {"KEEP": "1"}, "modelPicker": {
            "options": [FOREIGN, other] + DEFAULT_ROWS, "replaceBuiltInOptions": False}}
        self.assertEqual(h.read_claude(), expected)
        self.assertEqual(os.stat(h.claude).st_mode & 0o777, 0o600)
        backups = glob.glob(os.path.join(h.home, ".local", "state", "gpt-gateway", "backups", "settings.json.pre-install-*"))
        self.assertEqual(len(backups), 1)
        with open(backups[0]) as source:
            self.assertEqual(json.load(source), original)
        before = os.stat(h.claude).st_mtime_ns
        self.installed(h)
        self.assertEqual(h.read_claude(), expected)
        self.assertEqual(os.stat(h.claude).st_mtime_ns, before, "an idempotent install rewrote settings")

    def test_identical_handwritten_rows_are_adopted_without_duplicates(self):
        h = self.host()
        h.write_claude({"modelPicker": {"options": DEFAULT_ROWS}})
        self.installed(h)
        self.assertEqual(h.read_claude(), {"modelPicker": {"options": DEFAULT_ROWS}})
        code, out = h.run("uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertNotIn("modelPicker", h.read_claude())

    def test_override_replaces_owned_rows_and_empty_list_removes_them(self):
        h = self.host()
        self.installed(h)
        h.env["GPT_GATEWAY_MODEL_PICKER_MODELS"] = '["gpt-6-astra"]'
        self.installed(h)
        self.assertEqual(h.read_claude()["modelPicker"]["options"], DEFAULT_ROWS[1:])
        h.env["GPT_GATEWAY_MODEL_PICKER_MODELS"] = '[]'
        self.installed(h)
        self.assertNotIn("modelPicker", h.read_claude())
        self.assertEqual(h.read_claude()["env"], {"KEEP": "1"})

    def test_file_list_is_persistent_env_wins_and_route_off_removes_owned_only(self):
        h = Host(self)
        self.configure(h, {"chatgpt": True, "model_picker_models": ["gpt-6-astra"]})
        self.installed(h)
        self.assertEqual(h.read_claude()["modelPicker"]["options"], DEFAULT_ROWS[1:])
        self.installed(h)
        self.assertEqual(h.read_claude()["modelPicker"]["options"], DEFAULT_ROWS[1:])
        h.env["GPT_GATEWAY_MODEL_PICKER_MODELS"] = '["gpt-6.1-sol"]'
        self.installed(h)
        self.assertEqual(h.read_claude()["modelPicker"]["options"], DEFAULT_ROWS[:1])
        current = h.read_claude()
        current["modelPicker"]["options"].insert(0, FOREIGN)
        h.write_claude(current)
        h.env["GPT_GATEWAY_CHATGPT"] = "off"
        self.installed(h)
        self.assertEqual(h.read_claude()["modelPicker"], {"options": [FOREIGN]})
        h = Host(self)
        self.installed(h)
        self.assertNotIn("modelPicker", h.read_claude(), "route off added picker rows")

    def test_invalid_override_is_named_before_any_claude_settings_change(self):
        for raw in ('', 'garbage', '{}', 'null', '[""]', '["gpt-unknown"]',
                    '["gpt-6-astra", "gpt-6-astra"]', '[42]', '["claude-custom"]'):
            with self.subTest(raw=raw):
                h = self.host()
                cache = os.path.join(h.home, "catalog.json")  # read whole: a model it lacks is refused
                with open(cache, "w") as target:
                    json.dump({"models": [{"slug": "gpt-7-comet", "supported_reasoning_levels": []}]}, target)
                h.env["GPT_GATEWAY_CHATGPT_MODELS_PATH"] = cache
                original = {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid", "KEEP": "1"}}
                h.write_claude(original)
                h.env["GPT_GATEWAY_MODEL_PICKER_MODELS"] = raw
                code, out = h.install()
                self.assertNotEqual(code, 0, out)
                self.assertIn("GPT_GATEWAY_MODEL_PICKER_MODELS", out)
                self.assertEqual(h.read_claude(), original)

    def test_catalog_labels_additional_models_and_deterministic_fallback(self):
        h = self.host()
        cache = os.path.join(h.home, "catalog.json")
        with open(cache, "w") as target:
            json.dump({"models": [
                {"slug": "gpt-6.1-sol", "display_name": "Catalog Sol", "supported_reasoning_levels": []},
                {"slug": "gpt-7-comet", "display_name": "Catalog Comet", "supported_reasoning_levels": []},
                {"slug": "gpt-7-nova", "supported_reasoning_levels": []},
            ]}, target)
        h.env["GPT_GATEWAY_CHATGPT_MODELS_PATH"] = cache
        h.env["GPT_GATEWAY_MODEL_PICKER_MODELS"] = '["gpt-7-comet", "gpt-7-nova", "gpt-6.1-sol"]'
        self.installed(h)
        self.assertEqual(h.read_claude()["modelPicker"]["options"], [
            {"model": "gpt-7-comet", "label": "Catalog Comet", "description": DESCRIPTION},
            {"model": "gpt-7-nova", "label": "GPT-7 Nova", "description": DESCRIPTION},
            {"model": "gpt-6.1-sol", "label": "Catalog Sol", "description": DESCRIPTION},
        ])

    def test_malformed_existing_picker_and_cache_are_errors_not_absence(self):
        for picker in ([], {"options": {}}, {"options": None}):
            with self.subTest(picker=picker):
                h = self.host()
                h.write_claude({"modelPicker": picker})
                code, out = h.install()
                self.assertNotEqual(code, 0, out)
                self.assertIn("modelPicker", out)
                self.assertEqual(h.read_claude(), {"modelPicker": picker})


    def test_a_catalog_cache_that_does_not_read_costs_only_its_labels_with_a_warning(self):
        bad_label = {"slug": "gpt-7-comet", "display_name": " ", "supported_reasoning_levels": []}
        for name, contents, picked, rows, warnings in (
                ("broken", "broken", '["gpt-6.1-sol", "gpt-7-nova"]',
                 DEFAULT_ROWS[:1] + [{"model": "gpt-7-nova", "label": "GPT-7 Nova", "description": DESCRIPTION}],
                 ["catalog {cache}: cannot read: Expecting value: line 1 column 1 (char 0); default labels used",
                  NOVA_UNCHECKED]),
                ("no models array", '{"models": {}}', '["gpt-6.1-sol"]', DEFAULT_ROWS[:1],
                 ["catalog {cache}: catalog.models must be an array in an object; default labels used"]),
                ("blank label and a bad entry", json.dumps({"models": [bad_label, {"slug": "gpt-7-nova"}]}),
                 '["gpt-7-comet", "gpt-7-nova", "gpt-6.1-sol"]',
                 [{"model": "gpt-7-comet", "label": "GPT-7 Comet", "description": DESCRIPTION},
                  {"model": "gpt-7-nova", "label": "GPT-7 Nova", "description": DESCRIPTION}, DEFAULT_ROWS[0]],
                 ["catalog {cache}: catalog.models[1]: supported_reasoning_levels must be an array of objects",
                  'catalog {cache}: gpt-7-comet: display_name " " is no label; the default label is used',
                  NOVA_UNCHECKED])):
            with self.subTest(name=name):
                h = self.host()
                cache = os.path.join(h.home, "catalog.json")
                with open(cache, "w") as target:
                    target.write(contents)
                h.env["GPT_GATEWAY_CHATGPT_MODELS_PATH"] = cache
                h.env["GPT_GATEWAY_MODEL_PICKER_MODELS"] = picked
                code, out = h.install()
                self.assertEqual(code, 0, out)
                self.assertEqual(h.read_claude()["modelPicker"]["options"], rows)
                said = [line for line in out.splitlines() if line.startswith("gpt-gateway: model picker: warning: ")]
                self.assertEqual(said, ["gpt-gateway: model picker: warning: " + w.format(cache=cache)
                                        for w in warnings] * 2, out)  # once by the check, once by the install

    def test_uninstall_removes_only_owned_rows_and_preserves_other_picker_fields(self):
        for remaining in ({"options": [FOREIGN], "replaceBuiltInOptions": True},
                          {"replaceBuiltInOptions": False}, None):
            with self.subTest(remaining=remaining):
                h = Host(self)
                self.installed(h)
                options = [DEFAULT_ROWS[0], FOREIGN, DEFAULT_ROWS[1]] if remaining and "options" in remaining else DEFAULT_ROWS
                picker = {"options": options}
                if remaining:
                    picker.update({key: val for key, val in remaining.items() if key != "options"})
                original = {"env": {"KEEP": "1"}, "modelPicker": picker}
                h.write_claude(original)
                code, out = h.run("uninstall.sh")
                self.assertEqual(code, 0, out)
                expected = {"env": {"KEEP": "1"}}
                if remaining is not None:
                    expected["modelPicker"] = remaining
                self.assertEqual(h.read_claude(), expected)
                backups = glob.glob(os.path.join(h.home, ".local", "state", "gpt-gateway", "backups", "settings.json.pre-uninstall-*"))
                self.assertEqual(len(backups), 1)
                with open(backups[0]) as source:
                    self.assertEqual(json.load(source), original)

    def test_install_and_uninstall_follow_claude_settings_symlink(self):
        h = self.host()
        store = os.path.join(h.home, "shared-settings.json")
        os.rename(h.claude, store)
        os.symlink(store, h.claude)
        self.installed(h)
        self.assertTrue(os.path.islink(h.claude))
        with open(store) as source:
            self.assertEqual(json.load(source)["modelPicker"]["options"], DEFAULT_ROWS)
        code, out = h.run("uninstall.sh")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.islink(h.claude))
        self.assertNotIn("modelPicker", h.read_claude())
