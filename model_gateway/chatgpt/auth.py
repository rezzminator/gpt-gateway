"""Own ChatGPT credentials, private persistence and serialized refresh across threads and processes.

Distilled from opencode, Copyright (c) 2025 opencode, MIT permission notice in
THIRD_PARTY_NOTICES.md; derived from openai/codex, changed (Apache-2.0, same notices).
"""
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import http.client
import json
import math
import os
import tempfile
import threading
from urllib.parse import urlencode

from .. import clock
from ..upstream import Upstream
from .keychain import Keychain, KeychainError
from . import identity

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class AuthError(Exception):
    """An auth operation failed; safe to display, with no tokens or upstream body."""


class LoginRequired(AuthError):
    """A login is absent or permanently refused; the caller must ask the user to log in."""


@dataclass(frozen=True, repr=False)
class Credentials:
    access_token: str
    account_id: str


# opencode packages/opencode/src/plugin/openai/codex.ts:49-57; Codex login/src/token_data.rs:137-151.
def jwt_claims(token):
    """Decode expiry/account metadata only; this is not a JWT authentication verifier."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims if isinstance(claims, dict) else {}
    except (AttributeError, ValueError, UnicodeError):
        return {}


# Codex codex-rs/login/src/token_data.rs:141-151.
def token_expiry(token):
    exp = jwt_claims(token).get("exp")
    return exp if type(exp) in (int, float) and math.isfinite(exp) else None


# Codex login/src/token_data.rs:88-117; opencode packages/opencode/src/plugin/openai/codex.ts:59-65.
def account_id(tokens):
    claims = jwt_claims(tokens.get("id_token"))
    nested = claims.get("https://api.openai.com/auth")
    nested = nested if isinstance(nested, dict) else {}
    for candidate in (tokens.get("account_id"), claims.get("chatgpt_account_id"), nested.get("chatgpt_account_id")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


# Codex codex-rs/login/src/auth/manager.rs:1659-1667,1680-1691.
def refresh_refused(status, reply):
    """True when the token endpoint refused the refresh token for good; a new login is needed."""
    error = reply.get("error")
    code = error.get("code", error.get("type")) if isinstance(error, dict) else error
    code = code.lower() if isinstance(code, str) else None
    return (status == 401 or (status == 400 and code == "invalid_grant")
            or code in ("refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"))


def expiry_text(token):
    """The access token's expiry as local time, or "unknown"."""
    expiry = token_expiry(token)
    try:
        return clock.stamp("%Y-%m-%d %H:%M:%S %z", expiry) if expiry is not None else "unknown"
    except (OverflowError, ValueError, OSError):
        return "unknown"


# Codex login/src/oauth/client.rs:76-126, rewritten atop the core's connection façade.
def post_token(origin, path, body, form=False, timeout=30):
    """One token request. Never replay a rotating refresh token after a transport failure."""
    encoded = (urlencode(body) if form else json.dumps(body, separators=(",", ":"))).encode()
    kind = "application/x-www-form-urlencoded" if form else "application/json"
    upstream = Upstream(origin)
    try:
        conn = upstream.connection()
        conn.timeout = timeout
        conn.request("POST", path, encoded, dict(identity.headers(), **{"Content-Type": kind}))
        response = conn.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise AuthError("ChatGPT auth response too large")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            if response.status >= 400:
                data = {}
            else:
                raise AuthError("Invalid ChatGPT auth response") from None
        if not isinstance(data, dict):
            raise AuthError("Invalid ChatGPT auth response")
        return response.status, data
    except (OSError, http.client.HTTPException):
        raise AuthError("ChatGPT auth connection failed") from None
    finally:
        upstream.drop()


class Store:
    """Construct with env.ChatGPTEnv.parse(mapping); all network calls use its auth_origin.

    A thread remembers the token generation it received so a 401 arriving after another refresh
    reuses the rotated credentials. Cross-process flock also excludes independent Store objects.
    """

    def __init__(self, knobs):
        self.path = knobs.auth_path
        self.origin = knobs.auth_origin
        self.local = threading.local()
        self.backend = None
        if knobs.auth_store == "keychain":
            if not Keychain.available():
                raise AuthError("ChatGPT Keychain unavailable")
            self.backend = Keychain()

    @contextmanager
    def locked(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            if self.backend is not None:
                data = self.backend.read()
            else:
                try:
                    fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    return None
                with os.fdopen(fd) as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    data = json.load(stream)
            if data is not None and not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, OSError, KeychainError):
            raise AuthError("Cannot read ChatGPT login storage") from None

    def _write(self, data):
        if self.backend is not None:
            try:
                self.backend.write(data)
                return
            except KeychainError:
                raise AuthError("Cannot write ChatGPT Keychain") from None
        fd, path = tempfile.mkstemp(prefix=".chatgpt-auth-", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _normalized(self, tokens, previous=None):
        if not isinstance(tokens, dict):
            raise AuthError("Invalid ChatGPT token response")
        previous = previous or {}
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token", previous.get("refresh_token"))
        identity = tokens.get("id_token", previous.get("id_token"))
        merged = dict(previous, **tokens)
        merged["id_token"] = identity
        account = account_id(merged)
        if not all(isinstance(v, str) and v for v in (access, refresh, identity, account)):
            raise AuthError("Incomplete ChatGPT token response")
        return dict(access_token=access, refresh_token=refresh, id_token=identity,
                    account_id=account, last_refresh=clock.now())

    def save(self, tokens):
        """Persist a completed login, replacing any permanent refusal marker."""
        data = self._normalized(tokens)
        with self.locked():
            self._write(data)

    def post(self, path, body, form=False, timeout=30):
        return post_token(self.origin, path, body, form, timeout)

    @staticmethod
    def _revision(data):
        if not data:
            return None
        return (data.get("access_token"), data.get("refresh_token"), data.get("last_refresh"))

    # opencode packages/opencode/src/plugin/openai/codex.ts:341-398; Codex login/src/oauth/client.rs:76-90.
    def credentials(self, force_refresh=False):
        observed = getattr(self.local, "revision", None)
        if force_refresh and observed is None:
            before = self._read()
            observed = self._revision(before)
        with self.locked():
            data = self._read()
            if not data or data.get("login_required"):
                raise LoginRequired("ChatGPT login required; log in again with chatgpt-login")
            access = data.get("access_token")
            expiry = token_expiry(access)
            changed = force_refresh and self._revision(data) != observed
            refresh = (expiry is None or expiry <= clock.now() + 300 or force_refresh) and not changed
            if refresh:
                if not data.get("refresh_token"):
                    raise LoginRequired("ChatGPT refresh token missing; log in again with chatgpt-login")
                status, reply = self.post("/oauth/token", {"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                                          "refresh_token": data["refresh_token"]})
                if refresh_refused(status, reply):
                    self._write({"login_required": True})
                    raise LoginRequired("ChatGPT refresh refused; log in again with chatgpt-login")
                if status != 200:
                    raise AuthError("ChatGPT token refresh failed (HTTP %d)" % status)
                data = self._normalized(reply, data)
                self._write(data)
            account = account_id(data)
            if not isinstance(data.get("access_token"), str) or not account:
                raise AuthError("Incomplete ChatGPT login storage")
            self.local.revision = self._revision(data)
            return Credentials(data["access_token"], account)

    def status(self):
        """Login presence, masked account suffix and expiry; no network or token output."""
        with self.locked():
            data = self._read()
        if not data or data.get("login_required"):
            return "ChatGPT: not logged in"
        account = account_id(data)
        return "ChatGPT: logged in; account ...%s; access expires %s" % (
            (account or "unknown")[-4:], expiry_text(data.get("access_token")))


class Source:
    """Choose Codex on every call; construct the gateway's own store only when needed."""

    def __init__(self, knobs):
        from .codex_login import CodexLogin
        self.knobs = knobs
        self.codex = CodexLogin(knobs)
        self.own = None
        self.lock = threading.Lock()

    def _store(self):
        with self.lock:
            if self.own is None:
                self.own = Store(self.knobs)
            return self.own

    def credentials(self, force_refresh=False):
        credentials = self.codex.credentials(force_refresh)
        if credentials is not None:
            return credentials
        try:
            return self._store().credentials(force_refresh)
        except LoginRequired:
            raise LoginRequired("ChatGPT login required: run codex login, or gateway.py chatgpt-login") from None

    def status(self):
        status = self.codex.status()
        if status is not None:
            return status
        status = self._store().status()
        if status == "ChatGPT: not logged in":
            status += "; run codex login, or gateway.py chatgpt-login"
        return status


def source(knobs):
    return Source(knobs)
