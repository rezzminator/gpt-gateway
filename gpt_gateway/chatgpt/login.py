"""Browser PKCE callbacks and the headless device-code login flow.

Distilled from opencode, Copyright (c) 2025 opencode, MIT permission notice in
THIRD_PARTY_NOTICES.md; derived from openai/codex, changed (Apache-2.0, same notices).
"""
import base64
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from .. import clock
from .auth import AuthError, CLIENT_ID

CALLBACK_PORTS = (1455, 1457)
LOGIN_SECONDS = 15 * 60


def open_browser(url):
    """The replaceable browser-launch boundary."""
    return webbrowser.open(url)


# Codex codex-rs/login/src/server.rs:637-640.
def bind_callback(port, handler):
    """Bind only the local interface; no public callback listener."""
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


# opencode packages/opencode/src/plugin/openai/codex.ts:117-132.
def exchange_code(store, code, verifier, redirect, timeout=30):
    if not all(isinstance(v, str) and v for v in (code, verifier)):
        raise AuthError("Invalid ChatGPT authorization code response")
    status, tokens = store.post("/oauth/token", {"grant_type": "authorization_code", "code": code,
                                               "redirect_uri": redirect, "client_id": CLIENT_ID,
                                               "code_verifier": verifier}, form=True, timeout=timeout)
    if status != 200:
        raise AuthError("ChatGPT code exchange failed (HTTP %d)" % status)
    store.save(tokens)


# opencode packages/opencode/src/plugin/openai/codex.ts:24-34,88-101,164-233;
# Codex codex-rs/login/src/server.rs:637-683 (registered fallback port).
def browser_login(store, output=print):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    done = threading.Event()
    result = {}
    result_lock = threading.Lock()

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def do_GET(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            status = 404
            with result_lock:
                if parts.path == "/auth/callback" and not done.is_set():
                    state_values = query.get("state", [])
                    if len(state_values) != 1 or not secrets.compare_digest(state_values[0].encode(), state.encode()):
                        status = 400  # not our login's callback: a stray request never ends the wait
                    elif query.get("error") or len(query.get("code", [])) != 1:
                        result["error"] = "ChatGPT authorization refused or missing code"
                        status = 400
                        done.set()
                    else:
                        result["code"] = query["code"][0]
                        status = 200
                        done.set()
            body = b"Authorization received. Return to the terminal." if status == 200 else b"Authorization failed."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = None
    for port in CALLBACK_PORTS:
        try:
            server = bind_callback(port, Callback)
            break
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise AuthError("Cannot bind ChatGPT login callback") from None
    if server is None:
        raise AuthError("ChatGPT login callback ports are busy; use chatgpt-login --device")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        redirect = "http://localhost:%d/auth/callback" % server.server_port
        url = store.origin + "/oauth/authorize?" + urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": redirect,
            "scope": "openid profile email offline_access", "code_challenge": challenge,
            "code_challenge_method": "S256", "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true", "state": state, "originator": "codex_cli_rs"})
        output("Open this URL to log in:\n" + url)
        open_browser(url)
        if not done.wait(LOGIN_SECONDS):
            raise AuthError("ChatGPT browser login timed out after 15 minutes")
        if "error" in result:
            raise AuthError(result["error"])
        exchange_code(store, result["code"], verifier, redirect)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


# opencode packages/opencode/src/plugin/openai/codex.ts:473-547;
# Codex codex-rs/login/src/device_code_auth.rs:27-59,101-141,169-206.
def device_login(store, output=print):
    deadline = clock.monotonic() + LOGIN_SECONDS
    status, data = store.post("/api/accounts/deviceauth/usercode", {"client_id": CLIENT_ID})
    if status != 200:
        raise AuthError("ChatGPT device login failed (HTTP %d)" % status)
    device = data.get("device_auth_id")
    code = data.get("user_code", data.get("usercode"))
    if not all(isinstance(v, str) and v for v in (device, code)):
        raise AuthError("Invalid ChatGPT device authorization response")
    try:
        interval = max(1, int(data.get("interval", 5)))
    except (TypeError, ValueError, OverflowError):
        raise AuthError("Invalid ChatGPT device polling interval") from None
    output("Open %s/codex/device and enter %s (expires in 15 minutes)." % (store.origin, code))
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise AuthError("ChatGPT device login timed out after 15 minutes")
        status, reply = store.post("/api/accounts/deviceauth/token", {"device_auth_id": device, "user_code": code},
                                  timeout=min(30, remaining))
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise AuthError("ChatGPT device login timed out after 15 minutes")
        if status == 200:
            exchange_code(store, reply.get("authorization_code"), reply.get("code_verifier"),
                          store.origin + "/deviceauth/callback", timeout=min(30, remaining))
            return
        if status not in (403, 404):
            raise AuthError("ChatGPT device login failed (HTTP %d)" % status)
        clock.sleep(min(interval, remaining))
