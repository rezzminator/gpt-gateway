"""Reuse Codex's ChatGPT login in place, with guarded refresh and lossless token write-back.

Derived from openai/codex login/src/auth/manager.rs and storage.rs, changed
(Apache-2.0; see THIRD_PARTY_NOTICES.md).
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
import tempfile
import threading

from .. import clock
from . import auth

_REFRESH_LOCK = threading.Lock()


class CodexLogin:
    def __init__(self, knobs):
        self.path = knobs.codex_auth
        self.origin = knobs.auth_origin
        self.lock_path = os.path.join(os.path.dirname(knobs.auth_path), "codex-auth.lock")
        self.local = threading.local()
        self.pending = None  # (revision on disk, rotated login) while writing auth.json fails
        self.display_path = self.path
        if self.path and knobs.home:
            relative = os.path.relpath(self.path, knobs.home)
            if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
                self.display_path = os.path.join("~", relative)

    def _unreadable(self):
        return auth.AuthError("Cannot read the Codex login at %s" % self.display_path)

    def _read(self):
        if self.path is None:
            return None
        for attempt in range(4):
            try:
                with open(self.path, encoding="utf-8") as stream:
                    data = json.load(stream)
                break
            except FileNotFoundError:
                return None
            except (ValueError, UnicodeError):
                if attempt == 3:
                    raise self._unreadable() from None
                clock.sleep(0.05)
            except OSError:
                raise self._unreadable() from None
        if not isinstance(data, dict):
            raise self._unreadable()
        if data.get("auth_mode", "chatgpt") != "chatgpt" or data.get("tokens") is None:
            return None
        tokens = data["tokens"]
        if (not isinstance(tokens, dict)
                or not all(isinstance(tokens.get(k), str) and tokens[k] for k in ("access_token", "refresh_token"))
                or not auth.account_id(tokens)):
            raise self._unreadable()
        return data

    @staticmethod
    def _revision(data):
        if data is None:
            return None
        tokens = data["tokens"]
        return tokens["access_token"], tokens["refresh_token"], auth.account_id(tokens)

    def _remember(self, data):
        self.local.revision = self._revision(data)
        if data is None:
            return None
        return auth.Credentials(data["tokens"]["access_token"], auth.account_id(data["tokens"]))

    @staticmethod
    def _due(data):
        expiry = auth.token_expiry(data["tokens"]["access_token"])
        if expiry is not None:
            return expiry <= clock.now() + 300
        last = data.get("last_refresh")
        try:
            when = datetime.fromisoformat(last.replace("Z", "+00:00"))
            return when.utcoffset() is not None and when.timestamp() < clock.now() - 8 * 86400
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            return False

    @contextmanager
    def _locked(self):
        with _REFRESH_LOCK:
            fd = None
            try:
                os.makedirs(os.path.dirname(self.lock_path), mode=0o700, exist_ok=True)
                fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                if fd is not None:
                    os.close(fd)
                raise auth.AuthError("Cannot lock the Codex login refresh") from None
            try:
                yield
            finally:
                os.close(fd)

    def _persist(self, data, reply):
        if not isinstance(reply, dict):
            raise auth.AuthError("Invalid ChatGPT token response")
        access = reply.get("access_token")
        refresh = reply.get("refresh_token", data["tokens"]["refresh_token"])
        if (not all(isinstance(value, str) and value for value in (access, refresh))
                or ("id_token" in reply and not isinstance(reply["id_token"], str))):
            raise auth.AuthError("Incomplete ChatGPT token response")
        current, on_disk = self._load()
        # Codex may finish its own refresh or change accounts while the POST is in flight.
        if self._revision(current) != self._revision(data):
            return current
        current = json.loads(json.dumps(current))
        for name in ("access_token", "refresh_token", "id_token"):
            if name in reply:
                current["tokens"][name] = reply[name]
        current["last_refresh"] = datetime.fromtimestamp(clock.now(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            self._write(current)
            self.pending = None
        except auth.AuthError:
            # The server already retired the old refresh token: keep the rotated login, retry the write.
            self.pending = (on_disk, current)
        return current

    def _load(self):
        """The login and its on-disk revision; a rotated login whose write failed stands in for the retired one."""
        data = self._read()
        on_disk = self._revision(data)
        pending = self.pending
        if pending is None:
            return data, on_disk
        if on_disk != pending[0]:
            self.pending = None
            return data, on_disk
        try:
            self._write(pending[1])
            self.pending = None
        except auth.AuthError:
            pass
        return pending[1], on_disk

    def _write(self, current):
        serialized = json.dumps(current, indent=2, ensure_ascii=False)
        real_path = os.path.realpath(self.path)
        temp = None
        try:
            fd, temp = tempfile.mkstemp(prefix=".codex-auth-", dir=os.path.dirname(real_path))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, real_path)
        except OSError:
            raise auth.AuthError("Cannot write the Codex login at %s" % self.display_path) from None
        finally:
            if temp is not None and os.path.exists(temp):
                os.unlink(temp)

    def _refresh(self, data):
        tokens = data["tokens"]
        try:
            status, reply = auth.post_token(self.origin, "/oauth/token", {
                "client_id": auth.CLIENT_ID, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        except auth.AuthError:
            expiry = auth.token_expiry(tokens["access_token"])
            if expiry is not None and expiry > clock.now():
                return data
            raise
        if auth.refresh_refused(status, reply):
            current = self._load()[0]
            if current is None or current["tokens"]["refresh_token"] != tokens["refresh_token"]:
                return current
            raise auth.LoginRequired("Codex login expired; run `codex login`")
        if status != 200:
            expiry = auth.token_expiry(tokens["access_token"])
            if expiry is not None and expiry > clock.now():
                return data
            raise auth.AuthError("ChatGPT token refresh failed (HTTP %d)" % status)
        return self._persist(data, reply)

    def credentials(self, force_refresh=False):
        data = self._load()[0]
        if data is None:
            return self._remember(None)
        revision = self._revision(data)
        observed = getattr(self.local, "revision", None)
        if force_refresh and observed is not None and observed != revision:
            return self._remember(data)
        if not force_refresh and not self._due(data):
            return self._remember(data)
        with self._locked():
            current = self._load()[0]
            if self._revision(current) != revision:
                return self._remember(current)
            return self._remember(self._refresh(current))

    def status(self):
        data = self._load()[0]
        if data is None:
            return None
        held = "; rotated tokens held in memory, %s not writable" % self.display_path if self.pending else ""
        return "ChatGPT: Codex login (%s); account ...%s; access expires %s%s" % (
            self.display_path, auth.account_id(data["tokens"])[-4:], auth.expiry_text(data["tokens"]["access_token"]), held)
