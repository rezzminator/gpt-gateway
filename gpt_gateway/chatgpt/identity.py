"""Codex identity shared by ChatGPT Responses, models and OAuth requests.

Derived from openai/codex, changed (Apache-2.0); see THIRD_PARTY_NOTICES.md.
"""
import platform

CODEX_CLIENT_VERSION = "0.162.1"


# codex-rs/login/src/auth/default_client.rs:152-179; terminal-detection/src/lib.rs:152-183,234-236.
# codex-rs/model-provider-info/src/lib.rs:540: version is CARGO_PKG_VERSION.
def headers():
    system = platform.system()
    os_type = "Mac OS" if system == "Darwin" else system
    version = (platform.mac_ver()[0] or platform.release()) if system == "Darwin" else platform.release()
    agent = "codex_cli_rs/%s (%s %s; %s) unknown" % (CODEX_CLIENT_VERSION, os_type, version, platform.machine())
    return {"originator": "codex_cli_rs", "User-Agent": agent, "version": CODEX_CLIENT_VERSION}
