"""Tests for gpt_gateway/db.py: the rows, WAL, the one writer, drops, failures, retention, the views and the readers,
then both routes end to end into the file and a load of concurrent requests."""
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
import sqlite3
import threading
import time
import unittest
from unittest import mock

from gpt_gateway import db
from gpt_gateway.log import Log
from tests import fakes, jail
from tests.gpt_gateway.chatgpt import test_route as route_tests

DAY = 86400


def opened(test, path, **kw):
    """A started Db at `path` whose text-log lines go to the returned list; closed at cleanup."""
    lines = []
    store = db.Db(path, lines.append, **kw)
    store.start()
    test.addCleanup(store.close)
    return store, lines


class RowsTest(unittest.TestCase):
    def test_database_diagnostic_sites_emit_their_declared_levels_and_events(self):
        for event, level, function in (("queue-full-dropping", "error", "_put"),
                                       ("db-rows-lost", "error", "_failed"),
                                       ("close-after-error-failed", "error", "_discard"),
                                       ("retention-unreadable", "warning", "_prune"),
                                       ("close-failed", "error", "_run")):
            with self.subTest(event=event):
                home = jail.scratch(self)
                with mock.patch("gpt_gateway.env.snapshot", return_value={"HOME": home, "PLUGIN_LOG_DIR": home}):
                    log = Log(os.path.join(home, "gw.log"))
                store = db.Db(os.path.join(home, "gateway.db"), log.write, queue_size=1)
                conn = mock.Mock()
                conn.close.side_effect = sqlite3.OperationalError("close failed")
                if event == "queue-full-dropping":
                    store.event("first")
                    store.event("lost")
                elif event == "db-rows-lost":
                    store._failed(2, RuntimeError("write failed"))
                elif event == "close-after-error-failed":
                    store._discard(conn)
                elif event == "retention-unreadable":
                    store.retention = mock.Mock(side_effect=RuntimeError("retention failed"))
                    self.assertIsNone(store._prune(None))
                else:
                    store.queue.put_nowait(("stop", threading.Event()))
                    with mock.patch.object(store, "_prune", return_value=conn):
                        store._run()
                filename = "warnings.jsonl" if level == "warning" else "errors.jsonl"
                path = os.path.join(home, "gpt-gateway", filename)
                self.assertTrue(os.path.isfile(path), "database diagnostic must also reach its level file")
                with open(path) as source:
                    row, = [json.loads(line) for line in source]
                self.assertEqual((row["level"], row["event"], row["where"]),
                                 (level, event, "gpt_gateway/db.py#" + function))
                with open(log.path) as source:
                    self.assertIn(row["msg"], source.read())

    def test_writer_failure_records_level_row_and_existing_text(self):
        home = jail.scratch(self)
        text = os.path.join(home, "gw.log")
        with mock.patch("gpt_gateway.env.snapshot", return_value={"HOME": home, "PLUGIN_LOG_DIR": home}):
            log = Log(text)
        store = db.Db(os.path.join(home, "gateway.db"), log.write)
        store.start()
        self.addCleanup(store.close)
        with mock.patch.object(store, "_write", side_effect=RuntimeError("boom")):
            store.event("lost")
            self.assertTrue(store.flush())
        with open(text) as source:
            self.assertIn("db error, rows are lost", source.read())
        rows_path = os.path.join(home, "gpt-gateway", "errors.jsonl")
        self.assertTrue(os.path.isfile(rows_path), "DB row loss must also reach errors.jsonl")
        with open(rows_path) as source:
            row, = [json.loads(line) for line in source]
        self.assertEqual((row["level"], row["event"], row["where"]),
                         ("error", "db-rows-lost", "gpt_gateway/db.py#_failed"))
        self.assertIn("RuntimeError('boom')", row["msg"])
        self.assertEqual(store.errors, 1)

    def setUp(self):
        self.path = os.path.join(jail.scratch(self), "state", "gateway.db")

    def test_request_and_event_rows_round_trip(self):
        store, lines = opened(self, self.path)
        now = time.time()
        store.request({"started": now, "request_id": 7, "route": "anthropic", "kind": "sub", "method": "POST",
                       "path": "/v1/messages", "status": 200, "stream": True, "input_tokens": 5000,
                       "cache_read_tokens": 4000, "hit": 0.8, "error_head": b"x" * 700, "unknown": "ignored"})
        store.event("websocket_open", lane="lane-1", handshake_ms=12, reason=b"bytes")
        self.assertTrue(store.flush())
        (row,) = db.query(self.path, "SELECT * FROM requests")
        self.assertEqual((row["request_id"], row["route"], row["kind"], row["status"], row["stream"], row["pid"]),
                         (7, "anthropic", "sub", 200, 1, os.getpid()))
        self.assertEqual((row["input_tokens"], row["cache_read_tokens"], row["hit"], row["model"]),
                         (5000, 4000, 0.8, None))
        self.assertEqual(row["error_head"], "x" * 600)
        (event,) = db.query(self.path, "SELECT kind, lane, request_id, fields, pid FROM events")
        self.assertEqual(event, {"kind": "websocket_open", "lane": "lane-1", "request_id": None,
                                 "fields": '{"handshake_ms": 12, "reason": "bytes"}', "pid": os.getpid()})
        self.assertEqual(store.health(), "db=%s db_rows=2 db_drops=0 db_errors=0" % self.path)
        self.assertEqual(lines, [])

    def test_a_field_json_cannot_hold_costs_no_row(self):
        store, lines = opened(self, self.path)
        store.request({"started": time.time(), "route": "anthropic", "method": "POST", "path": "/v1/messages"})
        store.event("middleware_log", line={"raw": b"bytes", "seen": {2}})  # a middleware's ctx.log of a structure
        store.event("middleware_log", line={b"key": 1})  # a key JSON cannot hold
        self.assertTrue(store.flush())
        self.assertEqual((store.errors, lines, len(db.query(self.path, "SELECT id FROM requests"))), (0, [], 1))
        held = [json.loads(r["fields"])["line"] for r in db.query(self.path, "SELECT fields FROM events ORDER BY id")]
        self.assertEqual(held[0], {"raw": "b'bytes'", "seen": "{2}"})
        self.assertTrue(held[1].startswith("unrecordable dict: TypeError("), held[1])

    def test_a_row_sqlite_cannot_bind_costs_only_itself(self):
        lines = []
        store = db.Db(self.path, lines.append)  # rows queued before the writer starts share its first batch
        for path, tokens in (("/kept", 5), ("/lost", 2 ** 64)):
            store.request({"started": time.time(), "route": "anthropic", "method": "POST", "path": path,
                           "input_tokens": tokens})
        store.event("kept")
        store.start()
        self.addCleanup(store.close)
        self.assertTrue(store.flush())
        self.assertEqual((store.errors, store.rows, len(lines)), (1, 2, 1))
        self.assertEqual([r["path"] for r in db.query(self.path, "SELECT path FROM requests")], ["/kept"])

    def test_an_event_holds_its_fields_as_they_were_when_queued(self):
        store = db.Db(self.path, [].append)
        metadata = {"user_id": "u"}
        store.event("debug", metadata=metadata)
        metadata["user_id"] = "edited by a hook after the event"
        store.start()
        self.addCleanup(store.close)
        self.assertTrue(store.flush())
        self.assertEqual([json.loads(r["fields"]) for r in db.query(self.path, "SELECT fields FROM events")],
                         [{"metadata": {"user_id": "u"}}])

    def test_wal_mode_and_private_file(self):
        store, _ = opened(self, self.path)
        store.event("start")
        self.assertTrue(store.flush())
        self.assertEqual(db.query(self.path, "PRAGMA journal_mode"), [{"journal_mode": "wal"}])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_one_writer_thread(self):
        store, _ = opened(self, self.path)
        first = store.thread
        store.start()
        self.assertIs(store.thread, first)
        self.assertEqual([t for t in threading.enumerate() if t.name == "gpt-gateway-db" and t is not first
                          and t.is_alive() and getattr(t, "_target", None) == store._run], [])

    def test_views(self):
        store, _ = opened(self, self.path)
        now = time.time()
        for model, tokens, read in (("m", 5000, 1000), ("m", 5000, 4000), ("m", 1000, 0)):
            store.request({"started": now, "route": "anthropic", "method": "POST", "path": "/v1/messages",
                           "model": model, "input_tokens": tokens, "cache_read_tokens": read, "hit": read / tokens})
        self.assertTrue(store.flush())
        misses = db.query(self.path, "SELECT input_tokens, cache_read_tokens FROM cache_misses")
        self.assertEqual(misses, [{"input_tokens": 5000, "cache_read_tokens": 1000}])
        (daily,) = db.query(self.path, "SELECT route, model, requests, input_tokens, cache_read_tokens, hit FROM daily")
        self.assertEqual(daily, {"route": "anthropic", "model": "m", "requests": 3, "input_tokens": 11000,
                                 "cache_read_tokens": 5000, "hit": 0.455})


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(jail.scratch(self), "gateway.db")

    def test_full_queue_drops_counts_and_logs_once(self):
        lines = []
        store = db.Db(self.path, lines.append, queue_size=2)  # no writer: the queue only fills
        began = time.monotonic()
        for n in range(5):
            store.event("lane_wait", lane="l%d" % n)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(store.drops, 3)
        self.assertEqual(len(lines), 1)
        self.assertIn("db queue full at 2 rows", lines[0])
        self.assertIn("db_drops=3", store.health())

    def test_a_stalled_writer_never_blocks_a_caller(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        store, lines = opened(self, self.path, queue_size=10)
        with mock.patch.object(store, "_write", side_effect=lambda conn, items: gate.wait() and None):
            store.event("start")
            time.sleep(0.2)  # the writer now waits inside its write
            began = time.monotonic()
            for _ in range(100):
                store.event("lane_wait")
            self.assertLess(time.monotonic() - began, 0.5)
            self.assertEqual(store.drops, 90)
            gate.set()

    def test_dropping_ends_once_the_queue_drains(self):
        lines = []
        store = db.Db(self.path, lines.append, queue_size=1)
        store.event("a")
        store.event("b")
        store.start()
        self.addCleanup(store.close)
        self.assertTrue(store.flush())
        self.assertEqual((len(lines), store.drops, store.dropping), (1, 1, False))
        self.assertEqual([(r["kind"], json.loads(r["fields"])) for r in db.query(self.path, "SELECT kind, fields FROM events")],
                         [("a", {}), ("db_lost", {"drops": 1, "errors": 0})])  # the file itself says what it lacks


class FailureTest(unittest.TestCase):
    def test_a_database_that_cannot_open_is_logged_once_counted_and_never_raises(self):
        d = jail.scratch(self)
        with open(os.path.join(d, "file"), "w") as f:
            f.write("not a directory")
        path = os.path.join(d, "file", "gateway.db")
        store, lines = opened(self, path)
        for n in range(3):
            store.request({"started": time.time(), "route": "anthropic", "method": "GET", "path": "/%d" % n})
            self.assertTrue(store.flush())
        self.assertEqual(store.errors, 3)
        self.assertEqual(len(lines), 1)
        self.assertIn("db error", lines[0])
        self.assertIn(path, lines[0])
        self.assertIn("db_errors=3", store.health())

    def test_a_corrupt_file_counts_errors(self):
        path = os.path.join(jail.scratch(self), "gateway.db")
        with open(path, "wb") as f:
            f.write(b"this is no sqlite database, not even close" * 100)
        store, lines = opened(self, path)
        store.event("start")
        self.assertTrue(store.flush())
        self.assertEqual((store.errors, store.rows, len(lines)), (1, 0, 1))


class WriterTest(unittest.TestCase):
    def test_an_unexpected_writer_error_is_counted_and_the_writer_goes_on(self):
        path = os.path.join(jail.scratch(self), "gateway.db")
        store, lines = opened(self, path)
        with mock.patch.object(store, "_write", side_effect=RuntimeError("boom")):
            store.event("lost")
            self.assertTrue(store.flush())
        store.event("kept")
        self.assertTrue(store.flush())
        self.assertEqual([(r["kind"], json.loads(r["fields"])) for r in db.query(path, "SELECT kind, fields FROM events")],
                         [("kept", {}), ("db_lost", {"drops": 0, "errors": 1})])
        store.event("later")
        self.assertTrue(store.flush())
        self.assertEqual(len(db.query(path, "SELECT 1 FROM events WHERE kind = 'db_lost'")), 1)  # one per loss
        self.assertEqual((store.errors, len(lines)), (1, 1))
        self.assertIn("RuntimeError('boom')", lines[0])

    def test_a_db_lost_event_that_cannot_be_written_is_carried_by_the_next(self):
        path = os.path.join(jail.scratch(self), "gateway.db")
        store, _ = opened(self, path)
        write = store._write

        def refusing(conn, rows):
            if any(table == "events" and value[1] == "db_lost" for table, value in rows):
                raise RuntimeError("db_lost refused")
            return write(conn, rows)
        with mock.patch.object(store, "_write", side_effect=RuntimeError("boom")):
            store.event("lost")
            self.assertTrue(store.flush())
        with mock.patch.object(store, "_write", side_effect=refusing):
            store.event("kept")
            self.assertTrue(store.flush())
        store.event("later")
        self.assertTrue(store.flush())
        self.assertEqual([(r["kind"], json.loads(r["fields"])) for r in db.query(path, "SELECT kind, fields FROM events")],
                         [("kept", {}), ("later", {}), ("db_lost", {"drops": 0, "errors": 2})])


ADDED = ("error_class", "request_bytes")  # the requests columns a database made before them lacks


class MigrationTest(unittest.TestCase):
    def test_a_database_made_before_new_columns_gains_them_in_place_and_keeps_every_row(self):
        path = os.path.join(jail.scratch(self), "gateway.db")
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        for name in ADDED:
            if name in [c[1] for c in conn.execute("PRAGMA table_info(requests)")]:
                conn.execute("ALTER TABLE requests DROP COLUMN %s" % name)
        conn.executemany("INSERT INTO requests (started, pid, route, method, path, status) VALUES (?, 1, 'anthropic',"
                         " 'POST', '/v1/messages', 200)", [(time.time() - n,) for n in range(5000)])  # inside the retention
        conn.commit()
        conn.close()
        store, lines = opened(self, path)
        store.request({"started": time.time(), "route": "anthropic", "method": "POST", "path": "/v1/messages",
                       "status": 502, "error_class": "tls", "request_bytes": 12})
        self.assertTrue(store.flush())
        names = [c["name"] for c in db.query(path, "PRAGMA table_info(requests)")]
        self.assertEqual([name for name in ADDED if name in names], list(ADDED))
        self.assertEqual(names[-len(ADDED):], list(ADDED))  # appended, as a fresh database has them
        self.assertEqual(db.query(path, "SELECT count(*) AS n, count(error_class) AS typed FROM requests"),
                         [{"n": 5001, "typed": 1}])
        self.assertEqual(db.query(path, "SELECT error_class, request_bytes FROM requests WHERE status = 502"),
                         [{"error_class": "tls", "request_bytes": 12}])
        self.assertEqual((store.errors, lines), (0, []))
        store.close()
        again, _ = opened(self, path)  # a second start finds nothing to add
        again.event("start")
        self.assertTrue(again.flush())
        self.assertEqual(again.errors, 0)

    def test_a_column_another_gateway_added_first_is_no_failure(self):
        path = os.path.join(jail.scratch(self), "gateway.db")
        store, _ = opened(self, path)
        store.event("start")
        self.assertTrue(store.flush())
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)

        class Late:
            """The listing this gateway read before the other one's ALTER TABLE: the new columns still missing."""

            def execute(self, sql, *args):
                rows = conn.execute(sql, *args)
                return [r for r in rows if r[1] not in ADDED] if sql.startswith("PRAGMA table_info") else rows
        try:
            store._migrate(Late())
        except sqlite3.OperationalError as error:
            self.fail("a column the other gateway added first failed this one's start: %r" % error)
        self.assertEqual([c[1] for c in conn.execute("PRAGMA table_info(requests)")][-len(ADDED):], list(ADDED))


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(jail.scratch(self), "gateway.db")

    def seed(self, ages):
        """Write one request and one event per age in days, with a writer that keeps everything."""
        store = db.Db(self.path, [].append, retention=lambda: 10000)
        store.start()
        for age in ages:
            when = time.time() - age * DAY
            store.request({"started": when, "route": "anthropic", "method": "GET", "path": "/%g" % age})
            with mock.patch.object(db.clock, "now", return_value=when):
                store.event("age", days=age)
        self.assertTrue(store.close())

    def kept(self):
        return (sorted(r["path"] for r in db.query(self.path, "SELECT path FROM requests")),
                sorted(r["fields"] for r in db.query(self.path, "SELECT fields FROM events")))

    def test_rows_past_the_retention_are_pruned_at_start(self):
        self.seed([31, 29])
        store, _ = opened(self, self.path, retention=lambda: 30)
        self.assertTrue(store.flush())
        self.assertEqual(self.kept(), (["/29"], ['{"days": 29}']))

    def test_rows_past_the_retention_are_pruned_hourly(self):
        days = [10000]
        self.seed([5])
        with mock.patch.object(db, "PRUNE_SECONDS", 0.3):
            store, _ = opened(self, self.path, retention=lambda: days[0])
            self.assertTrue(store.flush())
            self.assertEqual(self.kept(), (["/5"], ['{"days": 5}']))
            days[0] = 4
            jail.wait_for(lambda: self.kept() == ([], []), timeout=5)
        self.assertEqual(self.kept(), ([], []))


    def test_a_retention_that_cannot_be_read_prunes_nothing_and_loses_no_row(self):
        self.seed([31])

        def unreadable():
            raise RuntimeError("invented")
        store, lines = opened(self, self.path, retention=unreadable)
        store.event("after")
        self.assertTrue(store.flush())
        self.assertEqual((store.errors, self.kept()[0], len(lines)), (0, ["/31"], 1))
        self.assertIn("db retention unreadable, nothing pruned", lines[0])


class ReaderTest(unittest.TestCase):
    def test_a_missing_database_is_an_error_never_an_empty_result(self):
        path = os.path.join(jail.scratch(self), "absent.db")
        with self.assertRaises(db.DbError) as caught:
            db.chatgpt_responses(path)
        self.assertIn("absent.db: no database", str(caught.exception))
        self.assertFalse(os.path.exists(path))

    def test_chatgpt_responses_since(self):
        path = os.path.join(jail.scratch(self), "gateway.db")
        store, _ = opened(self, path)
        now = time.time()
        for started, route, tokens in ((now - 7200, "chatgpt", 100), (now, "chatgpt", 200), (now, "chatgpt", None),
                                       (now, "anthropic", 300)):
            store.request({"started": started, "route": route, "method": "POST", "path": "/v1/messages",
                           "lane": "L", "slug": "gpt-x", "input_tokens": tokens, "cache_read_tokens": 50,
                           "ttfb_ms": 10, "total_ms": 20})
        self.assertTrue(store.flush())
        self.assertEqual([r["input_tokens"] for r in db.chatgpt_responses(path)], [100, 200])
        self.assertEqual(db.chatgpt_responses(path, now - 60), [
            {"lane": "L", "slug": "gpt-x", "input_tokens": 200, "cache_read_tokens": 50, "ttfb_ms": 10,
             "total_ms": 20}])


SENTINEL = "sentinel-q7v2-body-text"  # an invented string placed in every request and response body


class SentinelUpstream(BaseHTTPRequestHandler):
    """The Anthropic Messages API, invented: a stream or a JSON reply, each carrying SENTINEL and a usage."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        streamed = json.loads(self.rfile.read(int(self.headers["content-length"]))).get("stream")
        usage = {"input_tokens": 10, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 100}
        if streamed:
            events = [("message_start", {"type": "message_start", "message": {"id": "msg_1", "usage": dict(
                          usage, output_tokens=1)}}),
                      ("content_block_delta", {"type": "content_block_delta", "delta": {"text": SENTINEL}}),
                      ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 7}}),
                      ("message_stop", {"type": "message_stop"})]
            data, ctype = b"".join(b"event: %s\ndata: %s\n\n" % (n.encode(), json.dumps(e).encode())
                                   for n, e in events), "text/event-stream"
        else:
            data, ctype = json.dumps({"type": "message", "content": [{"type": "text", "text": SENTINEL}],
                                      "usage": dict(usage, output_tokens=2)}).encode(), "application/json"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class EndToEndTest(unittest.TestCase):
    """Both routes, streaming and JSON, through a served gateway into its database file."""
    setUp = route_tests.RouteTest.setUp
    jwt = route_tests.RouteTest.jwt
    responses = route_tests.RouteTest.responses

    def test_rows_and_events_on_both_routes_and_no_body_byte_in_the_file(self):
        turn = fakes.responses_turn().replace(b"Hello", SENTINEL.encode())
        self.fake.script = [(503, {"error": {"message": "unavailable"}}), (200, turn), (200, turn)]
        rt, port = fakes.start_gateway(self, fakes.start_upstream(self, SentinelUpstream), env=self.env)
        user = json.dumps({"device_id": "device-invented", "account_uuid": "account-invented", "session_id": "s-1"})
        claude = {"model": "claude-x", "metadata": {"user_id": user}, "messages": [{"role": "user", "content": SENTINEL}]}
        gpt = dict(self.body, messages=[{"role": "user", "content": SENTINEL}])
        with mock.patch("gpt_gateway.chatgpt.errors.backoff", return_value=0):
            for body in (dict(claude, stream=True), dict(claude, stream=False), dict(gpt, stream=True),
                         dict(gpt, stream=False)):
                resp, data = fakes.request(port, "POST", "/" + self.route_secret + "/v1/messages", json.dumps(body).encode(), self.headers)
                self.assertEqual((resp.status, SENTINEL.encode() in data), (200, True))
        names = ("route", "stream", "status", "model", "slug", "session_id", "input_tokens", "cache_read_tokens",
                 "cache_write_tokens", "output_tokens", "hit", "attempts", "transport", "completed")
        rows = [tuple(r[n] for n in names) for r in fakes.recorded(rt)]
        self.assertEqual(rows, [
            ("anthropic", 1, 200, "claude-x", None, "s-1", 5110, 5000, 100, 7, 5000 / 5110, 1, None, 1),
            ("anthropic", 0, 200, "claude-x", None, "s-1", 5110, 5000, 100, 2, 5000 / 5110, 1, None, 1),
            ("chatgpt", 1, 200, "gpt-test", "gpt-test", None, 100, 60, 0, 8, 0.6, 2, "http", 1),
            ("chatgpt", 0, 200, "gpt-test", "gpt-test", None, 100, 60, 0, 8, 0.6, 1, "http", 1)])
        retries = fakes.events(rt, "retry")
        self.assertEqual([(e["fields"]["status"], e["fields"]["attempt"], bool(e["lane"])) for e in retries],
                         [(503, 1, True)])
        self.assertEqual(len(self.responses()), 3)
        self.assertEqual(fakes.db_bytes(rt).count(SENTINEL.encode()), 0, "a body byte reached the database file")


class LoadTest(unittest.TestCase):
    def test_concurrent_requests_never_block_on_the_database_and_drop_nothing(self):
        jail.raise_open_files(self, 1024)  # 50 clients, their 50 accepted ends and 50 upstream connections
        with mock.patch.object(fakes.QuietServer, "request_queue_size", 128):  # 50 gateway threads connect at once
            origin = fakes.start_upstream(self, SentinelUpstream)
        rt, port = fakes.start_gateway(self, origin)
        record, waits = rt.db.request, []

        def timed(row):
            began = time.monotonic()
            record(row)
            waits.append(time.monotonic() - began)
        rt.db.request = timed
        statuses = []

        def client():  # one kept-alive connection per thread, as Claude Code holds them
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            for _ in range(40):
                conn.request("POST", "/v1/messages", b'{"model":"m","stream":true}')
                resp = conn.getresponse()
                resp.read()
                statuses.append(resp.status)
            conn.close()
        threads = [threading.Thread(target=client) for _ in range(50)]
        for t in threads:
            t.start()
            time.sleep(0.01)  # the listen backlog takes a burst of connects a few at a time
        for t in threads:
            t.join(120)
        self.assertEqual((len(statuses), set(statuses)), (2000, {200}))
        # A handler records its row after the last byte went out, so the client can finish first.
        self.assertTrue(jail.wait_for(lambda: len(waits) == 2000, 30), "%d of 2000 rows recorded" % len(waits))
        self.assertTrue(rt.db.flush(30))
        self.assertEqual((len(waits), rt.db.drops, rt.db.errors), (2000, 0, 0))
        self.assertLess(max(waits), 1.0, "a request waited on the database")  # a blocked write is seconds; a loaded host adds 0.2
        self.assertEqual(db.query(rt.db.path, "SELECT count(*) AS n, sum(input_tokens) AS t FROM requests"),
                         [{"n": 2000, "t": 2000 * 5110}])


if __name__ == "__main__":
    unittest.main()
