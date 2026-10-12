"""The gateway database: `requests`, one row per client request, and `events`, every other fact a running gateway records.

One SQLite file in WAL mode, written by one thread off a bounded queue, so a request never waits on the disk:
`request()` and `event()` put a row on the queue and return. A full queue drops the row and counts it; a write that
fails counts its rows as errors. Either is written once to the text log when it starts (the database cannot record its
own failure) and shows on the health line, and the gateway goes on. Rows older than the retention are pruned when the
writer starts and hourly. No request or response body enters it: a row holds ids, counts, times, statuses and at most
600 bytes of an error reply. This module is the only one that touches sqlite3; the readers live here too.
"""
import json
import os
import queue
import sqlite3
import threading
from urllib.parse import quote

from . import clock

QUEUE_SIZE = 10000
BATCH = 500  # rows per transaction
PRUNE_SECONDS = 3600
TEXT_CAP = 600  # characters of any text a row holds
ROW_ERRORS = (sqlite3.IntegrityError, sqlite3.InterfaceError, OverflowError, ValueError, TypeError)  # one row's values

COLUMNS = ("started", "ended", "pid", "request_id", "route", "kind", "method", "path", "model", "slug",
           "session_id", "agent_id", "lane", "status", "answered_by", "stream",
           "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "hit",
           "delta", "attempts", "ttfb_ms", "upstream_ms", "total_ms", "reused", "stale", "transport", "turn_state",
           "translation_notes", "empty", "completed", "client_disconnected", "error_code", "error_head", "note")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    started REAL NOT NULL,         -- epoch seconds the gateway received the request
    ended REAL,                    -- epoch seconds the relay ended
    pid INTEGER NOT NULL,          -- the gateway process: request_id counts per process
    request_id INTEGER,            -- ctx.request_id; NULL for a body the gateway could not read
    route TEXT NOT NULL,           -- anthropic | chatgpt
    kind TEXT,                     -- main | sub
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    model TEXT,                    -- the requested model
    slug TEXT,                     -- the ChatGPT catalog slug
    session_id TEXT,               -- the session_id in the body's metadata.user_id
    agent_id TEXT,                 -- x-claude-code-agent-id
    lane TEXT,                     -- the ChatGPT lane key
    status INTEGER,                -- the status the client got
    answered_by TEXT,              -- the NAME of the middleware that answered
    stream INTEGER,                -- the body's stream flag
    input_tokens INTEGER,          -- every prompt token: uncached, cache read and cache write
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    hit REAL,                      -- cache_read_tokens / input_tokens
    delta TEXT,                    -- ChatGPT WebSocket: K/N or full:{why}
    attempts INTEGER,
    ttfb_ms INTEGER,
    upstream_ms INTEGER,
    total_ms INTEGER,
    reused INTEGER,
    stale INTEGER,
    transport TEXT,                -- ws | http
    turn_state TEXT,               -- sent | issued
    translation_notes TEXT,        -- a JSON list of short strings, whole: past 600 characters, then "N more"
    empty INTEGER,
    completed INTEGER,
    client_disconnected INTEGER,
    error_code TEXT,               -- the error reply's error.type, stream_error, or gateway_error
    error_head TEXT,               -- at most 600 bytes of the error reply
    note TEXT                      -- the middleware notes
);
CREATE INDEX IF NOT EXISTS requests_started ON requests (started);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,              -- epoch seconds
    pid INTEGER NOT NULL,
    kind TEXT NOT NULL,
    request_id INTEGER,
    lane TEXT,
    fields TEXT NOT NULL           -- a JSON object of short fields
);
CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE VIEW IF NOT EXISTS cache_misses AS
    SELECT * FROM requests WHERE input_tokens >= 4096 AND hit < 0.5;
CREATE VIEW IF NOT EXISTS daily AS
    SELECT date(started, 'unixepoch', 'localtime') AS day, route, model, count(*) AS requests,
           sum(input_tokens) AS input_tokens, sum(cache_read_tokens) AS cache_read_tokens,
           sum(cache_write_tokens) AS cache_write_tokens, sum(output_tokens) AS output_tokens,
           sum(reasoning_tokens) AS reasoning_tokens,
           round(1.0 * sum(cache_read_tokens) / nullif(sum(input_tokens), 0), 3) AS hit
    FROM requests GROUP BY day, route, model;
"""
INSERT_REQUEST = "INSERT INTO requests (%s) VALUES (%s)" % (", ".join(COLUMNS), ", ".join("?" * len(COLUMNS)))
INSERT_EVENT = "INSERT INTO events (at, pid, kind, request_id, lane, fields) VALUES (?, ?, ?, ?, ?, ?)"


class DbError(Exception):
    """The database cannot be read; the message names the file."""


def cell(value):
    """A value as a column holds it: bools as 0/1, text cut to TEXT_CAP characters, anything else as its text."""
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return (value if isinstance(value, str) else str(value))[:TEXT_CAP]


def field(value):
    """An event field as its JSON holds it: a short object or list as plain JSON values (anything else in it as its
    text), a longer one as its cut text, one JSON cannot hold at all as why. The writer's encoding then never fails,
    so no field costs its batch's rows."""
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError, RecursionError) as error:  # a key JSON cannot hold, a cycle, a deep nesting
            return cell("unrecordable %s: %r" % (type(value).__name__, error))
        return json.loads(text) if len(text) <= TEXT_CAP else text[:TEXT_CAP]
    return cell(value)


class Db:
    """The writer of one database file; `start()` runs its thread, and every other method is safe from any thread."""

    def __init__(self, path, log, retention=lambda: 30, queue_size=QUEUE_SIZE):
        self.path, self.log, self.retention = path, log, retention
        self.queue = queue.Queue(queue_size)
        self.lock = threading.Lock()
        self.rows = self.drops = self.errors = 0
        self.dropping = self.failing = False
        self.pid = os.getpid()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="gpt-gateway-db", daemon=True)
            self.thread.start()

    def request(self, row):
        """Queue one `requests` row, a mapping of COLUMNS names; a name it lacks is NULL."""
        self._put(("requests", tuple(self.pid if name == "pid" else cell(row.get(name)) for name in COLUMNS)))

    def event(self, kind, request_id=None, lane=None, **fields):
        """Queue one `events` row: its kind, the request id or lane it belongs to, and short fields, each taken as it
        is now, so a caller that changes a field's object afterwards changes nothing queued."""
        self._put(("events", (clock.now(), kind, request_id, lane, {k: field(v) for k, v in fields.items()})))

    def health(self):
        with self.lock:
            return "db=%s db_rows=%d db_drops=%d db_errors=%d" % (self.path, self.rows, self.drops, self.errors)

    def flush(self, timeout=5.0):
        """Whether every row queued before this call was written or counted within `timeout` seconds."""
        return self._marker("flush", timeout)

    def close(self, timeout=5.0):
        """Write what is queued, then stop the writer; whether it stopped within `timeout` seconds."""
        if self.thread is None:
            return True
        stopped = self._marker("stop", timeout)
        self.thread.join(timeout)
        return stopped and not self.thread.is_alive()

    def _marker(self, kind, timeout):
        done = threading.Event()
        try:
            self.queue.put((kind, done), timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout)

    def _put(self, item):
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            with self.lock:
                self.drops += 1
                first, self.dropping = not self.dropping, True
            if first:
                self.log("db queue full at %d rows: dropping rows, counted as db_drops on the health line (%s)" % (
                    self.queue.maxsize, self.path))

    def _failed(self, rows, error):
        with self.lock:
            self.errors += rows
            first, self.failing = not self.failing, True
        if first:
            self.log("db error, rows are lost and counted as db_errors until a write succeeds (%s): %r" % (
                self.path, error))

    def _connect(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
        except BaseException:
            conn.close()
            raise
        return conn

    def _transaction(self, conn, statements):
        """(connection, None) after running `statements` [(sql, args)] in one transaction; (None, error) once it
        failed, the connection closed."""
        try:
            if conn is None:
                conn = self._connect()
            conn.execute("BEGIN")
            for sql, args in statements:
                conn.execute(sql, args)
            conn.execute("COMMIT")
            return conn, None
        except ROW_ERRORS + (sqlite3.Error, OSError) as error:
            self._discard(conn)
            return None, error

    def _discard(self, conn):
        """Close a connection an error left in doubt; the next write opens a fresh one."""
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as error:
                self.log("db close after an error failed (%s): %r" % (self.path, error))

    def _write(self, conn, items):
        statements = []
        for table, value in items:
            if table == "requests":
                statements.append((INSERT_REQUEST, value))
            else:
                at, kind, request_id, lane, fields = value
                statements.append((INSERT_EVENT, (at, self.pid, cell(kind), request_id, cell(lane),
                                                  json.dumps(fields, ensure_ascii=False))))
        conn, error = self._transaction(conn, statements)
        written = len(statements) if error is None else 0
        if isinstance(error, ROW_ERRORS) and len(statements) > 1:  # one row's value: the others go one by one
            for statement in statements:
                conn, error_alone = self._transaction(conn, [statement])
                written += error_alone is None
                error = error_alone or error
        if written:
            with self.lock:
                self.rows += written
                self.failing = False
        if written < len(statements):
            self._failed(len(statements) - written, error)
        return conn

    def _prune(self, conn):
        try:
            cutoff = clock.now() - float(self.retention()) * 86400
        except Exception as error:  # a retention source that fails keeps every row until it answers
            self.log("db retention unreadable, nothing pruned (%s): %r" % (self.path, error))
            return conn
        conn, error = self._transaction(conn, [("DELETE FROM requests WHERE started < ?", (cutoff,)),
                                               ("DELETE FROM events WHERE at < ?", (cutoff,))])
        if error is not None:
            self._failed(0, error)
        return conn

    def _run(self):
        conn, due = None, clock.monotonic()  # the first prune runs at start
        while True:
            try:
                batch = [self.queue.get(timeout=max(0.0, due - clock.monotonic()))]
            except queue.Empty:
                batch = []
            while batch and len(batch) < BATCH:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            rows = [item for item in batch if item[0] in ("requests", "events")]
            markers = [item for item in batch if item[0] in ("flush", "stop")]
            try:
                if rows:
                    conn = self._write(conn, rows)
                if clock.monotonic() >= due:
                    conn, due = self._prune(conn), clock.monotonic() + PRUNE_SECONDS
            except Exception as error:  # the one writer never dies: its rows are counted lost and it goes on
                self._discard(conn)
                conn = None
                self._failed(len(rows), error)
            with self.lock:
                if self.dropping and self.queue.empty():
                    self.dropping = False
            stop = any(kind == "stop" for kind, _ in markers)
            if stop and conn is not None:
                try:
                    conn.close()
                except sqlite3.Error as error:
                    self.log("db close failed (%s): %r" % (self.path, error))
            for _, done in markers:
                done.set()
            if stop:
                return


def query(path, sql, args=()):
    """The rows of one read of the database at `path`, opened read-only; DbError when it is missing or unreadable."""
    if not os.path.exists(path):
        raise DbError("%s: no database" % path)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % quote(path), uri=True, timeout=5.0)
        try:
            cursor = conn.execute(sql, args)
            names = [column[0] for column in cursor.description or ()]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as error:
        raise DbError("%s: %s" % (path, error))


def chatgpt_responses(path, since=0.0):
    """Each ChatGPT response that reported usage since `since` (epoch seconds), oldest first: lane, slug,
    input_tokens, cache_read_tokens, ttfb_ms and total_ms."""
    return query(path, "SELECT lane, slug, input_tokens, cache_read_tokens, ttfb_ms, total_ms FROM requests"
                       " WHERE route = 'chatgpt' AND input_tokens IS NOT NULL AND started >= ? ORDER BY id", (since,))


def anthropic_timings(path, since=0.0):
    """Each Anthropic-route request that carries timings since `since` (epoch seconds), oldest first: model,
    ttfb_ms, upstream_ms and total_ms."""
    return query(path, "SELECT model, ttfb_ms, upstream_ms, total_ms FROM requests WHERE route = 'anthropic'"
                       " AND ttfb_ms IS NOT NULL AND upstream_ms IS NOT NULL AND total_ms IS NOT NULL"
                       " AND started >= ? ORDER BY id", (since,))
