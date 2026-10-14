"""Capture: one folder per exchange under `capture_dir`, secrets redacted; and the request sequence.

{capture_dir}/{date}/{HHMMSS.mmm}-{seq}-{kind}-{METHOD}-{path}/ holds, in exchange order:
1-request.json (method, path, headers; secrets redacted), 2-request.body (as the client sent it),
3-request.sent.body (only when a hook changed it), 4-response.json (status, headers),
5-response.body (the literal bytes streamed back, SSE included). Capture never breaks a request:
a write failure is recorded as a `capture_error` event, and capture stops for that exchange; a failure
that is the process running out of handles is an `fd_exhausted` event instead (`handles.announce`: one per
window, the ones held back counted).
"""
import json
import os
import re
import stat
import threading

from . import clock, handles
from .headers import SECRET

REQUEST = "1-request.json"
REQUEST_BODY = "2-request.body"
SENT_BODY = "3-request.sent.body"
RESPONSE = "4-response.json"
RESPONSE_BODY = "5-response.body"


def _own_directory(path):
    st = os.lstat(path)
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


class Sequence:
    """Request numbers, 1, 2, ...: the capture folder's `seq` and the hooks' `ctx.request_id`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0

    def next(self):
        with self.lock:
            self.last += 1
            return self.last


class Capture:
    """The capture of one exchange; every method is a no-op when capture is off or has failed."""

    def __init__(self, root, seq, method, path, headers, kind, event):
        self.dir = None
        self.body = None
        self.seq, self.event = seq, event
        if not root:
            return
        try:
            now = clock.now()
            slug = re.sub(r"[^A-Za-z0-9]+", "_", path.split("?", 1)[0]).strip("_")[:60] or "root"
            name = "%s.%03d-%06d-%s-%s-%s" % (clock.stamp("%H%M%S", now), int(now * 1000) % 1000,
                                              seq, kind, method, slug)
            day = os.path.join(root, clock.stamp("%Y-%m-%d", now))
            self.dir = os.path.join(day, name)
            for directory in (root, day, self.dir):
                os.makedirs(directory, mode=0o700, exist_ok=True)
                # The user's root is tightened only when it is a directory this user owns, never through a symlink.
                if directory != root or _own_directory(root):
                    os.chmod(directory, 0o700)
            hdrs = [[k, "<redacted>" if k.lower() in SECRET else v] for k, v in headers.items()]
            self.write(REQUEST, json.dumps({"time": clock.stamp("%Y-%m-%d %H:%M:%S", now), "seq": seq,
                                            "kind": kind, "method": method, "path": path, "headers": hdrs},
                                           indent=2).encode())
        except Exception as e:
            self.fail(e)

    def fail(self, e):
        shortage = handles.diagnose(e)
        if shortage is None:
            self.event("capture_error", request_id=self.seq, dir=self.dir, error=repr(e))
        else:
            handles.announce(self.event, "fd_exhausted", where="capture", error=repr(e), open=shortage.open,
                             limit=shortage.limit)
        self.dir = None

    def _open_private(self, name):
        """Open `name` for writing as a regular file only the owner can read; a symlink there is refused."""
        f = os.fdopen(os.open(os.path.join(self.dir, name), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb")
        try:
            os.fchmod(f.fileno(), 0o600)
            f.truncate(0)
        except Exception:
            f.close()
            raise
        return f

    def write(self, name, data):
        if self.dir is None:
            return
        try:
            with self._open_private(name) as f:
                f.write(data)
        except Exception as e:
            self.fail(e)

    def request_body(self, data):
        self.write(REQUEST_BODY, data)

    def sent_body(self, data):
        self.write(SENT_BODY, data)

    def gateway_error(self, status, error):
        self.write(RESPONSE, json.dumps({"status": status, "gateway_error": error}).encode())

    def response(self, status, reason, headers):
        self.write(RESPONSE, json.dumps({"status": status, "reason": reason,
                                         "headers": [[k, v] for k, v in headers]}, indent=2).encode())
        if self.dir is not None:
            try:
                self.body = self._open_private(RESPONSE_BODY)
            except Exception as e:
                self.fail(e)

    def chunk(self, data):
        if self.body is not None:
            try:
                self.body.write(data)
            except Exception as e:
                self.close()
                self.fail(e)

    def close(self):
        if self.body is not None:
            try:
                self.body.close()
            except Exception as e:
                self.fail(e)
            self.body = None
