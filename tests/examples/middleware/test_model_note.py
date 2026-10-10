"""Tests for examples/middleware/model_note.py, through the public harness."""
import os
import unittest

from model_gateway.harness import load, run
from model_gateway.settings import SettingsError
from tests import jail
from tests.fakes import SSE_BODY

NOTE = os.path.join(jail.ROOT, "examples", "middleware", "model_note.py")
BODY = b'{"model": "claude-x", "messages": []}'


class ModelNoteTest(unittest.TestCase):
    def test_notes_the_model_and_forwards_the_bytes_unchanged(self):
        out = run(NOTE, body=BODY, response=SSE_BODY)
        self.assertEqual(out.note, " kind=main model-note=claude-x")
        self.assertIs(out.forwarded, BODY)
        self.assertEqual((out.log, out.row["note"]), ((), "model-note=claude-x"))
        self.assertEqual((out.errors, out.health), ((), "model-note errors=0"))

    def test_enabled_from_the_file_and_from_the_env(self):
        self.assertEqual(run(NOTE, body=BODY, settings={"enabled": False}).note, " kind=main")
        for raw, note in (("0", " kind=main"), ("off", " kind=main"), ("yes", " kind=main model-note=claude-x")):
            self.assertEqual(run(NOTE, body=BODY, settings={"enabled": False},
                                 env={"MODEL_GATEWAY_MODEL_NOTE": raw}).note, note, raw)

    def test_a_bad_value_is_refused_naming_its_source(self):
        with self.assertRaises(SettingsError) as cm:
            load(NOTE, env={"MODEL_GATEWAY_MODEL_NOTE": "maybe"})
        self.assertIn("middleware model_note: enabled from env MODEL_GATEWAY_MODEL_NOTE: enabled must be true or false",
                      str(cm.exception))
        with self.assertRaises(SettingsError) as cm:
            load(NOTE, settings={"enabled": 3})
        self.assertIn("enabled from file harness", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
