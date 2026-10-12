"""Tests for gpt_gateway.chatgpt.signature."""
import unittest

from gpt_gateway.chatgpt import signature


class SignatureTest(unittest.TestCase):
    def test_round_trips_slug_id_and_content(self):
        s = signature.encode("gpt-6.1-sol", "rs_0a1b", "gAAAA:with=colons+/")
        self.assertTrue(s.startswith(signature.PREFIX))
        self.assertEqual(signature.decode(s), ("gpt-6.1-sol", "rs_0a1b", "gAAAA:with=colons+/"))

    def test_the_same_item_encodes_to_the_same_bytes(self):
        self.assertEqual(signature.encode("gpt-6-luna", "rs_1", "x"), signature.encode("gpt-6-luna", "rs_1", "x"))

    def test_a_foreign_signature_is_none(self):
        self.assertIsNone(signature.decode("EqQBCkgIBRABGAIiQL"))
        self.assertIsNone(signature.decode(None))

    def test_a_damaged_signature_of_ours_raises(self):
        for bad in (signature.PREFIX, signature.PREFIX + "abc", signature.PREFIX + "a::c", signature.PREFIX + "%%:aa:c"):
            with self.assertRaises(ValueError, msg=bad):
                signature.decode(bad)

    def test_encode_refuses_an_empty_part(self):
        with self.assertRaises(ValueError):
            signature.encode("gpt-6-sol", "", "x")


if __name__ == "__main__":
    unittest.main()
