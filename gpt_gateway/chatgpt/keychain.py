"""The macOS Keychain storage and the single process-execution boundary it needs."""
import json
import subprocess
import sys

SERVICE = "gpt-gateway.chatgpt"
ACCOUNT = "tokens"
# security -i reads each command through a 4096-byte buffer; a longer line runs as fragments, the
# first storing a truncated item over the old one.
LINE_LIMIT = 4095


class KeychainError(Exception):
    """Keychain access failed; messages never include the command or its secret output."""


def security(arguments, input=None):
    """Run Apple's security tool without a shell; callers inspect only its status and payload."""
    try:
        return subprocess.run(["/usr/bin/security"] + arguments, capture_output=True, text=True, timeout=10, input=input)
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
        encoded = json.dumps(data, separators=(",", ":")).encode().hex()
        command = "add-generic-password -U -s %s -a %s -X %s\n" % (SERVICE, ACCOUNT, encoded)
        if len(command) > LINE_LIMIT:
            raise KeychainError("ChatGPT Keychain write failed")
        try:
            result = security(["-i"], input=command)
            if result.returncode or self.read() != data:
                raise KeychainError("ChatGPT Keychain write failed")
        except KeychainError:
            raise KeychainError("ChatGPT Keychain write failed") from None
