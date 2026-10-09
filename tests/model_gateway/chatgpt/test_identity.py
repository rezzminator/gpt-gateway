"""Codex identity matches the pinned producer on macOS and other platforms."""
import unittest
from unittest.mock import patch

from model_gateway.chatgpt import identity


class IdentityTest(unittest.TestCase):
    def test_codex_identity_shape(self):
        for system, version, expected in [("Darwin", "15.0", "Mac OS"), ("Linux", "6.0", "Linux")]:
            with self.subTest(system=system), patch.object(identity.platform, "system", return_value=system), \
                    patch.object(identity.platform, "mac_ver", return_value=(version, (), "")), \
                    patch.object(identity.platform, "release", return_value=version), \
                    patch.object(identity.platform, "machine", return_value="arm64"):
                self.assertEqual(identity.headers(), {"originator": "codex_cli_rs", "version": "0.155.1",
                    "User-Agent": "codex_cli_rs/0.155.1 (%s %s; arm64) unknown" % (expected, version)})

    def test_darwin_without_marketing_version_uses_kernel_release(self):
        with patch.object(identity.platform, "system", return_value="Darwin"), \
                patch.object(identity.platform, "mac_ver", return_value=("", (), "")), \
                patch.object(identity.platform, "release", return_value="invented-release"):
            self.assertIn("Mac OS invented-release;", identity.headers()["User-Agent"])
