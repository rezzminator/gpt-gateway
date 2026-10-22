"""The Responses WebSocket: the sockets kept per lane, Codex's response.create message, and the answer the route reads.

Derived from openai/codex, changed (Apache-2.0; see THIRD_PARTY_NOTICES.md). Codex keeps one socket per session and
sends every request of it there (codex-rs/core/src/client.rs:605-621); replaying the same bodies, the backend serves
a kept socket's request from the prompt cache its previous request wrote far more often than a fresh HTTP request's.
One request is in flight per socket, and a lane keeps one socket: a second lands on another backend server, whose cache
holds only what that socket sent. Claude Code's activity-summary fork runs while its agent's request still does, and
the agent's next request can arrive while the fork runs, so a request whose lane's socket is busy waits for it, a
fork the longest, and opens another only once that wait runs out; every socket coming back goes to the pool as its lane's one idle socket,
and a spare the lane already holds there closes. A socket answering anything but a completed response is closed.
A socket keeps its last completed response for the next request to continue (continuation.py), and while it idles in
the pool the keeper (keeper.py) answers and pings it.
"""
import collections
import hashlib
from http import HTTPStatus
import json
import threading

from .. import clock
from ..frames import WebSocket
from ..upstream import Upgrade, header
from . import keeper, turn

PATH = "/backend-api/codex/responses"
BETA = "responses_websockets=2026-02-06"  # codex-rs/core/src/client.rs:175
# The backend ends a socket at 60 minutes with websocket_connection_limit_reached
# (codex-api/src/endpoint/responses_websocket.rs:164-165); a socket this old opens no new exchange.
MAX_AGE = 55 * 60
MAX_KEPT = 64  # idle sockets across lanes; past it the oldest check-in closes
# codex-api/src/endpoint/responses_websocket.rs:603-662: both codes ask for the request again on a new socket.
RECONNECT = ("websocket_connection_limit_reached", "previous_response_not_found")
ENDED = ("response.completed", "response.incomplete", "response.failed", "error")
KEPT_AFTER = ("response.completed", "response.incomplete")
# Transport failures in a row (no upgrade answer, a reset or a protocol error mid-response) that pause the
# WebSocket so HTTP serves for PAUSE_SECONDS; Codex falls back to HTTP for its session once its retries run out
# (codex-rs/core/src/client.rs:1522-1527).
PAUSE_AFTER, PAUSE_SECONDS = 5, 600
# An upgrade answered with a status meaning the backend serves no WebSocket here (Codex: 426) pauses it this long.
UNSERVED_PAUSE_SECONDS = 3600
# How long a request waits for its lane's busy socket before it opens another: the agent's request waits out a fork,
# and the fork, which only labels the agent's last action, waits out the longest generation of the agent's request.
LANE_WAIT_SECONDS, FORK_WAIT_SECONDS = 30, 600


def unserved(status):
    """Whether an upgrade's status means this endpoint serves no WebSocket: an ignored upgrade, a redirect, 400, 404, 405, 426."""
    return 200 <= status < 400 or status in (400, 404, 405, 426)


def message(body, turn_state=None):
    """Codex's response.create text frame (codex-api/src/common.rs:306-376): the Responses body's bytes after a first
    `type` key, and the turn's token in client_metadata (codex-rs/core/src/client.rs:1976-1978)."""
    if len(body) < 2 or body[:1] != b"{" or body[-1:] != b"}":
        raise ValueError("a Responses body is a JSON object")
    out = b'{"type":"response.create"' + (b"," + body[1:] if body[1:-1].strip() else b"}")
    if turn_state:
        metadata = json.dumps({turn.HEADER: turn_state}, ensure_ascii=False, separators=(",", ":")).encode()
        out = out[:-1] + b',"client_metadata":' + metadata + b"}"
    return out


def socket_key(lane_key, credentials):
    """The pool key: one lane, one login. A socket authenticated as another login serves no lane."""
    login = hashlib.sha256((credentials.access_token + "\0" + credentials.account_id).encode()).hexdigest()
    return (lane_key, login)


def json_headers(value):
    """(name, value) pairs of a JSON headers object; a list value gives its first item."""
    if not isinstance(value, dict):
        return []
    pairs = []
    for name, item in value.items():
        item = item[0] if isinstance(item, list) and item else item
        if isinstance(item, (str, int, float)):
            pairs.append((str(name), str(item)))
    return pairs


class Reconnect(Exception):
    """The backend asked for the request again on a new socket."""


class Kept:
    """One upgraded socket and its facts; the pool holds it between exchanges, one Answer during one."""

    def __init__(self, ws, pool_key, headers):
        self.ws, self.key, self.headers = ws, pool_key, headers
        self.opened = clock.monotonic()
        self.touched = self.opened  # the last time the pool put it back, or the keeper pinged it
        self.guard = threading.Lock()  # held by whoever is reading the idle socket: the keeper or a checkout
        self.exchanges = 0
        self.out = False  # checked out: one exchange owns it
        self.baseline = None  # the last exchange's completed response, which the next request may continue


class Sockets:
    """The idle sockets of every lane and whether the WebSocket serves; every method is thread-safe."""

    def __init__(self, upstream, event):
        self.upstream, self.event = upstream, event
        self.lock = threading.Lock()
        self.returned = threading.Condition(self.lock)  # a socket went back to the pool or closed
        self.idle = collections.OrderedDict()  # Kept -> None, oldest check-in first
        self.busy = collections.Counter()  # pool key -> sockets checked out
        self.failures = 0
        self.paused_until = float("-inf")
        self.keeper = keeper.Keeper(self)

    @property
    def max_age(self):
        return MAX_AGE

    def stop(self):
        """End the keeper and close every idle socket; the sockets checked out go back to a pool nobody tends."""
        self.keeper.stop()
        with self.lock:
            idle, self.idle = list(self.idle), collections.OrderedDict()
        for kept in idle:
            self.close(kept, "stopped", abort=True)

    def usable(self):
        with self.lock:
            return clock.monotonic() >= self.paused_until

    def pause(self, seconds, reason):
        with self.lock:
            self.failures = 0
            self.paused_until = clock.monotonic() + seconds
            idle, self.idle = list(self.idle), collections.OrderedDict()
        for kept in idle:
            self.close(kept, "paused")
        self.event("websocket_paused", seconds=seconds, reason=reason)

    def failed(self, reason):
        with self.lock:
            self.failures += 1
            tripped = self.failures >= PAUSE_AFTER
        if tripped:
            self.pause(PAUSE_SECONDS, "%d transport failures in a row, the last %s" % (PAUSE_AFTER, reason))

    def succeeded(self):
        with self.lock:
            self.failures = 0

    def checkout(self, pool_key, wait=0):
        """The lane's most recent idle socket still fit for a request, or None; the unfit ones close. With `wait`, a
        lane whose every socket is busy waits that many seconds for one to come back before it gets None. None
        reserves the socket the caller opens next as busy, so a request arriving during its handshake waits for it;
        `open` takes the reservation over, and a failed open releases it."""
        deadline = clock.monotonic() + wait
        while True:
            with self.lock:
                while True:
                    kept = next((k for k in reversed(self.idle) if k.key == pool_key), None)
                    remaining = deadline - clock.monotonic()
                    if kept is not None or not self.busy[pool_key] or remaining <= 0:
                        break
                    self.returned.wait(remaining)
                if kept is None:
                    timed_out = wait > 0 and self.busy[pool_key] > 0
                    self.busy[pool_key] += 1
                else:
                    del self.idle[kept]
                    kept.out = True
                    self.busy[pool_key] += 1
            if kept is None:
                if timed_out:
                    self.event("lane_wait", lane=pool_key[0], wait_s=wait)
                return None
            with kept.guard:  # the keeper may be reading it
                old = clock.monotonic() - kept.opened >= MAX_AGE
                if not old and kept.ws.alive():
                    return kept
            self.close(kept, "age" if old else "closed-while-idle")

    def checkin(self, kept):
        """Back to the pool as its lane's one idle socket; a spare the lane already holds there, left by a wait that
        ran out, closes."""
        with self.lock:
            self._returned(kept)
            spares = [k for k in self.idle if k.key == kept.key]
            for spare in spares:
                del self.idle[spare]
            self.idle[kept] = None
            kept.touched = clock.monotonic()
            self.keeper.ensure()
            over = [self.idle.popitem(last=False)[0] for _ in range(len(self.idle) - MAX_KEPT)]
        for spare in spares:
            self.close(spare, "lane-spare")
        for oldest in over:
            self.close(oldest, "pool-full")

    def open(self, pool_key, headers):
        """A Kept on 101, taking over the reservation checkout made, else the Upgrade with the answer's status;
        UpstreamError when nothing answered. Either failure releases the reservation."""
        started = clock.monotonic()
        try:
            answer = self.upstream.websocket(PATH, headers)
        except BaseException:
            self.release(pool_key)
            raise
        if answer.socket is None:
            self.release(pool_key)
            return answer
        self.event("websocket_open", lane=pool_key[0], handshake_ms=clock.milliseconds(clock.monotonic() - started))
        kept = Kept(WebSocket(answer.socket, answer.pending, self.event), pool_key, answer.headers)
        kept.out = True
        return kept

    def release(self, pool_key):
        """Drop the reservation of a socket that never opened; a waiting request may open its own."""
        with self.lock:
            self.busy[pool_key] -= 1
            if not self.busy[pool_key]:
                del self.busy[pool_key]
            self.returned.notify_all()

    def _returned(self, kept):
        """Under the lock: a checked-out socket is no longer busy, and a waiting request may take its lane's next one."""
        if kept.out:
            kept.out = False
            self.busy[kept.key] -= 1
            if not self.busy[kept.key]:
                del self.busy[kept.key]
            self.returned.notify_all()

    def close(self, kept, reason, abort=False):
        with self.lock:
            self._returned(kept)
        with kept.guard:  # the keeper may be reading it; never a caller already holding the guard
            (kept.ws.abort if abort else kept.ws.close)()
        self.event("websocket_close", lane=kept.key[0], reason=reason, age_s=int(clock.monotonic() - kept.opened),
                   exchanges=kept.exchanges)


class Refusal:
    """An upgrade the backend answered with a status other than 101, as the response the route reads."""

    will_close = True

    def __init__(self, upgrade: Upgrade):
        self.status, self.headers, self.body = upgrade.status, upgrade.headers, upgrade.body
        self.reason = HTTPStatus(self.status).phrase if self.status in HTTPStatus._value2member_map_ else "Error"

    def getheaders(self):
        return list(self.headers)

    def getheader(self, name, default=None):
        found = header(self.headers, name.lower())
        return default if found is None else found

    def read(self, n=-1):
        data, self.body = self.body, b""
        return data if n is None or n < 0 else data[:n]

    def read1(self, n=65536):
        return self.read(n)

    def close(self):
        pass


class Answer(Refusal):
    """One response.create on a kept socket, read as an http.client response: a refusal the backend wrapped in an
    error event (codex-api/src/endpoint/responses_websocket.rs:603-662) as its status, headers and body; otherwise
    status 200, the upgrade's headers on the socket's first exchange, and each message as one SSE event. A
    `response.metadata` event's turn-state token goes to `observe` (codex-api/src/sse/responses.rs:210-218). A
    completed response with an id becomes the socket's baseline for its `request`; any other end leaves it none."""

    will_close = False

    def __init__(self, sockets, kept, observe=None, request=None):
        super().__init__(Upgrade(200, list(kept.headers) if kept.exchanges == 0 else []))
        self.sockets, self.kept, self.observe, self.request = sockets, kept, observe, request
        kept.exchanges += 1
        kept.baseline = None  # this exchange replaces the response the backend keeps for the socket
        self.output, self.response_id = [], None
        self.lock = threading.Lock()
        self.state = "open"  # "kept": back in the pool; "closed"
        self.ended = None  # the type of the message that ended the response
        self.first = None

    def _event(self, text):
        try:
            event = json.loads(text)
        except ValueError:
            event = None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self.sockets.event("websocket_skipped", bytes=len(text))
            return None
        return event

    def start(self, data):
        """Send the request and read its first message; Reconnect when the backend asks for a new socket."""
        self.kept.ws.send(data)
        while self.first is None:
            text = self.kept.ws.receive()
            event = self._event(text)
            if event is None:
                continue
            if event["type"] == "error":
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                if error.get("code") in RECONNECT:
                    raise Reconnect(error["code"])
                status = event.get("status")
                if isinstance(status, int) and not 200 <= status < 300:
                    self.status, self.reason, self.body, self.ended = status, "Error", text, "error"
                    self.headers = json_headers(event.get("headers")) + json_headers(error.get("headers"))
                    return
            self.first = (event, text)

    def read(self, n=-1):
        if self.status != 200:
            return super().read(n)
        chunks = []
        while True:
            chunk = self.read1()
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def read1(self, n=65536):
        """The next message as one SSE event; empty once the response ended. One event may exceed `n`."""
        if self.ended:
            return b""
        try:
            while True:
                if self.first is not None:
                    (event, text), self.first = self.first, None
                else:
                    text = self.kept.ws.receive()
                    event = self._event(text)
                    if event is None:
                        continue
                kind = event["type"]
                if kind == "response.metadata" and self.observe is not None:
                    token = header(json_headers(event.get("headers")), turn.HEADER)
                    if token:
                        self.observe(token)
                if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                    self.output.append(event["item"])
                elif kind == "response.completed" and isinstance(event.get("response"), dict):
                    self.response_id = event["response"].get("id")
                if kind in ENDED:
                    self.ended = kind
                if b"\n" in text:  # SSE frames one data line; a message's JSON may span lines
                    text = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
                return b"event: " + kind.encode() + b"\ndata: " + text + b"\n\n"
        except Exception as error:
            if self.state == "open":  # an abort's own cut is no transport failure
                self.sockets.failed(repr(error))
            raise

    def close(self):
        """The reader's end of the response: the socket goes back to the pool after a completed response, else closes."""
        with self.lock:
            if self.state != "open":
                return
            keep = self.ended in KEPT_AFTER
            self.state = "kept" if keep else "closed"
        if keep:
            if self.ended == "response.completed" and self.request is not None and self.response_id \
                    and isinstance(self.response_id, str):
                self.kept.baseline = self.request.baseline(self.response_id, self.output)
            self.sockets.succeeded()
            self.sockets.checkin(self.kept)
        else:
            self.sockets.close(self.kept, "ended-" + (self.ended or "early"), abort=True)

    def abort(self):
        """Close the socket under a response still open, so a reader blocked on it returns."""
        with self.lock:
            if self.state != "open":
                return
            self.state = "closed"
        self.sockets.close(self.kept, "abort", abort=True)
