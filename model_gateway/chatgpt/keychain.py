"""The macOS Keychain storage and the single process-execution boundary it needs."""
import json
import subprocess
import sys

SERVICE = "model-gateway.chatgpt"
ACCOUNT = "tokens"


class KeychainError(Exception):
    """Keychain access failed; messages never include the command or its secret output."""


def security(arguments):
    """Run Apple's security tool without a shell; callers inspect only its status and payload."""
    try:
        return subprocess.run(["/usr/bin/security"] + arguments, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise KeychainError("ChatGPT Keychain unavailable") from None


class Keychain:
    @staticmethod
    def available():
        if sys.platform != "darwin":
            return False
        try:
            return security(["default-keychain", "-d", "user"]).returncode == 0
        except KeychainError:
            return False

    def read(self):
        result = security(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"])
        if result.returncode == 44:
            return None
        if result.returncode:
            raise KeychainError("ChatGPT Keychain unavailable")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise KeychainError("ChatGPT Keychain contains invalid data") from None

    def write(self, data):
        result = security(["add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT,
                           "-w", json.dumps(data, separators=(",", ":"))])
        if result.returncode:
            raise KeychainError("ChatGPT Keychain write failed")
