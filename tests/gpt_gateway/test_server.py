"""Tests for gpt_gateway.server: real gateway processes; SIGTERM lets in-flight streams finish while a
successor takes the port, and the drain gives up at its limit."""
import errno
import fcntl
import http.client
import json
import os
import select
import signal
import socket
import subprocess
import threading
import time
import unittest
from types import SimpleNamespace

from unittest import mock

from gpt_gateway import cli, clock, db, handles, proxy
from gpt_gateway.server import Drain, Server
from gpt_gateway.log import Log
from tests import fakes, jail


class DrainTest(unittest.TestCase):
    def test_accept_registers_the_connection_before_a_handler_can_run(self):
        with mock.patch('gpt_gateway.server.sys.platform', 'linux'):
            runtime = SimpleNamespace(env=SimpleNamespace(), drain=Drain())
        server = Server(('127.0.0.1', jail.free_port()), runtime)
        self.addCleanup(server.server_close)
        client = socket.create_connection(server.server_address, timeout=2)
        self.addCleanup(client.close)
        accepted, _ = server.get_request()
        self.addCleanup(accepted.close)
        self.assertIn(accepted, runtime.drain.pending)

    def test_a_server_adopts_an_inherited_listening_descriptor_and_refuses_any_other(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen(5)
        address = listener.getsockname()
        descriptor = os.dup(listener.fileno())
        runtime = SimpleNamespace(env=SimpleNamespace(listener_fd=descriptor), drain=Drain())
        server = Server(address, runtime)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address, address)
        client = socket.create_connection(address, timeout=2)
        self.addCleanup(client.close)
        accepted, _ = server.get_request()
        accepted.close()
        connected = os.dup(client.fileno())
        with self.assertRaisesRegex(OSError, 'not the configured listener'):
            Server(address, SimpleNamespace(env=SimpleNamespace(listener_fd=connected), drain=Drain()))

    def setUp(self):
        self.tmp = jail.scratch(self)
        self.app = jail.stage_install(os.path.join(self.tmp, "app"))
        self.port = jail.free_port()
        self.upstream = fakes.start_upstream(self, fakes.SlowUpstream)
        self.log = os.path.join(self.tmp, "gw.log")
        self.procs = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def gateway(self, drain="10"):
        env = jail.clean_env(self.tmp, GPT_GATEWAY_PORT=str(self.port), GPT_GATEWAY_UPSTREAM=self.upstream,
                             GPT_GATEWAY_LOG=self.log, GPT_GATEWAY_DRAIN_SECONDS=drain)
        p = subprocess.Popen(["/usr/bin/python3", "-I", "-B", os.path.join(self.app, "gateway.py")], env=env)
        self.procs.append(p)
        return p

    def health_pid(self):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            conn.request("GET", "/__gateway/health")
            data = conn.getresponse().read()
            conn.close()
            return int(data.split(b"pid=")[1].split()[0])
        except (OSError, IndexError, ValueError):
            return None

    def stream(self, out):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("POST", "/v1/messages", body=b'{"model":"m","messages":[]}')
        out.append(conn.getresponse().read())
        conn.close()

    def logged(self):
        with open(self.log) as f:
            return f.read()

    def recorded(self, sql):
        """Rows of the jail's database, which both gateway processes write."""
        return db.query(jail.clean_env(self.tmp)["GPT_GATEWAY_DB"], sql)

    def test_a_stream_survives_a_restart_and_the_successor_takes_new_requests(self):
        old = self.gateway()
        self.assertTrue(jail.wait_for(lambda: self.health_pid() == old.pid))
        got = []
        t = threading.Thread(target=self.stream, args=(got,))
        t.start()
        time.sleep(0.4)  # the stream is now mid-flight on the old gateway
        new = self.gateway()
        time.sleep(0.5)  # the successor binds beside it
        old.send_signal(signal.SIGTERM)
        self.assertTrue(jail.wait_for(lambda: self.health_pid() == new.pid), "the successor never answered")
        t.join(20)
        self.assertEqual(len(got), 1)
        self.assertIn(b"message_stop", got[0])
        self.assertEqual(old.wait(timeout=10), 0)
        after = []
        self.stream(after)
        self.assertIn(b"message_stop", after[0])
        self.assertIn("stop: drained, exiting", self.logged())
        self.assertEqual([(r["pid"], json.loads(r["fields"])["left"]) for r in self.recorded(
            "SELECT pid, fields FROM events WHERE kind = 'stop'")], [(old.pid, 0)])
        build = cli.build(os.path.join(self.app, "gpt_gateway"))
        self.assertEqual(sorted((r["pid"], json.loads(r["fields"]).get("build")) for r in self.recorded(
            "SELECT pid, fields FROM events WHERE kind = 'start'")), sorted([(old.pid, build), (new.pid, build)]))
        self.assertRegex(str(build), "^[0-9a-f]{12}$")  # each start names the code it runs
        self.assertIn({"pid": old.pid, "completed": 1}, self.recorded("SELECT pid, completed FROM requests"))

    def test_drain_gives_up_at_its_limit(self):
        old = self.gateway(drain="0.3")
        self.assertTrue(jail.wait_for(lambda: self.health_pid() == old.pid))
        t = threading.Thread(target=lambda: self.assertRaises(Exception, self.stream, []))
        t.start()
        time.sleep(0.3)
        start = time.time()
        old.send_signal(signal.SIGTERM)
        self.assertEqual(old.wait(timeout=10), 0)
        self.assertLess(time.time() - start, 1.2)
        t.join(20)
        self.assertIn("still in flight", self.logged())
        self.assertEqual([json.loads(r["fields"])["left"] for r in self.recorded(
            "SELECT fields FROM events WHERE kind = 'stop'")], [1])


class DrainStateTest(unittest.TestCase):
    def test_drain_states_emit_classified_rows_and_keep_their_result(self):
        cases = (("empty", 0, {"info": ["draining", "drained"]}),
                 ("busy", 1, {"info": ["draining"], "warning": ["in-flight-exit"]}),
                 ("idle-error", 0, {"info": ["draining", "drained"], "warning": ["idle-shutdown-failed"]}))
        for state, result, expected in cases:
            with self.subTest(state=state):
                home = jail.scratch(self)
                with mock.patch("gpt_gateway.env.snapshot", return_value={"HOME": home, "PLUGIN_LOG_DIR": home}):
                    log = Log(os.path.join(home, "gw.log"))
                drain = Drain()
                if state == "busy":
                    drain.inflight = 1
                elif state == "idle-error":
                    conn = mock.Mock()
                    conn.shutdown.side_effect = OSError(errno.EBADF, "bad descriptor")
                    drain.connected(conn)
                    drain.begin(conn)
                    drain.end(conn)
                self.assertEqual(drain.wait(0, log.write), result)
                for level, events in expected.items():
                    name = "info.jsonl" if level == "info" else "warnings.jsonl"
                    path = os.path.join(home, "gpt-gateway", name)
                    self.assertTrue(os.path.isfile(path), "drain diagnostic must also reach its level file")
                    with open(path) as source:
                        rows = [json.loads(line) for line in source]
                    self.assertEqual([(r["level"], r["event"], r["where"]) for r in rows],
                                     [(level, event, "gpt_gateway/server.py#Drain.wait") for event in events])
                    with open(log.path) as source:
                        text = source.read()
                    for row in rows:
                        self.assertIn(row["msg"], text)

    def test_unfinished_database_writer_emits_error_without_changing_exit_status(self):
        from gpt_gateway import server
        home = jail.scratch(self)
        with mock.patch("gpt_gateway.env.snapshot", return_value={"HOME": home, "PLUGIN_LOG_DIR": home}):
            log = Log(os.path.join(home, "gw.log"))
        runtime = SimpleNamespace(log=log, env=SimpleNamespace(drain_seconds=0), drain=mock.Mock(),
                                  db=mock.Mock(), chatgpt=None)
        runtime.drain.wait.return_value = 0
        runtime.db.close.return_value = False
        with mock.patch.object(server.signal, "signal"):
            self.assertEqual(server.serve(runtime, mock.Mock()), 0)
        path = os.path.join(home, "gpt-gateway", "errors.jsonl")
        self.assertTrue(os.path.isfile(path), "unfinished writer must also reach errors.jsonl")
        with open(path) as source:
            row, = [json.loads(line) for line in source]
        self.assertEqual((row["level"], row["event"], row["where"]),
                         ("error", "writer-unfinished-rows-lost", "gpt_gateway/server.py#serve"))
        with open(log.path) as source:
            self.assertIn(row["msg"], source.read())

    def test_a_pending_request_above_the_select_descriptor_limit_is_preserved(self):
        jail.raise_open_files(self, 1100)
        client, original = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(original.close)
        accepted = socket.socket(fileno=fcntl.fcntl(original.fileno(), fcntl.F_DUPFD, 1025))
        self.addCleanup(accepted.close)
        client.sendall(b'G')
        with mock.patch('gpt_gateway.server.sys.platform', 'linux'):
            drain = Drain()
        drain.connected(accepted)
        self.assertEqual(drain.wait(0, lambda line: None), 1, 'readiness lost a valid high descriptor')

    def test_a_first_request_byte_already_read_is_not_treated_as_a_silent_client(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = socket.SocketIO.readinto
        rt, port = fakes.start_gateway(self)
        rt.drain.preserve_pending = True  # the Linux drain these two tests exercise; macOS closes pending clients
        def paused(reader, buffer):
            count = original(reader, buffer)
            if count and reader._sock.getsockname()[1] == port and not entered.is_set():
                entered.set()
                release.wait(5)
            return count
        with mock.patch.object(socket.SocketIO, 'readinto', new=paused):
            client = socket.create_connection(('127.0.0.1', port), timeout=3)
            self.addCleanup(client.close)
            client.sendall(b'G')
            try:
                self.assertTrue(entered.wait(3), 'the first byte was not read')
                rt.drain.stopping.set()
                self.assertEqual(rt.drain.wait(0, lambda line: None), 1,
                                 'request data consumed before a complete line was forgotten')
                client.sendall(b'ET /__gateway/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
                release.set()
                reply = http.client.HTTPResponse(client)
                reply.begin()
                self.assertEqual(reply.status, 200)
                self.assertTrue(reply.read().startswith(b'ok pid='))
            finally:
                release.set()

    def test_a_request_line_already_read_keeps_its_connection_while_headers_arrive(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = http.client.parse_headers
        def paused(stream, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                release.wait(5)
            return original(stream, *args, **kwargs)
        with mock.patch.object(http.client, 'parse_headers', side_effect=paused):
            rt, port = fakes.start_gateway(self)
            rt.drain.preserve_pending = True  # the Linux drain this test exercises; macOS closes pending clients
            client = socket.create_connection(('127.0.0.1', port), timeout=3)
            self.addCleanup(client.close)
            client.sendall(b'GET /__gateway/health HTTP/1.1\r\n')
            try:
                self.assertTrue(entered.wait(3), 'the request line was not read')
                rt.drain.stopping.set()
                self.assertEqual(rt.drain.wait(0, lambda line: None), 1,
                                 'a partially received request was mistaken for a silent client')
                client.sendall(b'Host: localhost\r\nConnection: close\r\n\r\n')
                release.set()
                reply = http.client.HTTPResponse(client)
                reply.begin()
                self.assertEqual(reply.status, 200)
                self.assertTrue(reply.read().startswith(b'ok pid='))
            finally:
                release.set()

    def test_a_silent_accepted_client_does_not_hold_the_linux_drain(self):
        client, accepted = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(accepted.close)
        with mock.patch('gpt_gateway.server.sys.platform', 'linux'):
            drain = Drain()
        drain.connected(accepted)
        self.assertEqual(drain.wait(0, lambda line: None), 0, 'a silent client was counted as a request')
        client.settimeout(1)
        self.assertEqual(client.recv(1), b'', 'the idle client was not closed')

    def test_an_accepted_request_already_on_the_wire_survives_the_linux_drain(self):
        client, accepted = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(accepted.close)
        client.sendall(b'GET /')
        with mock.patch('gpt_gateway.server.sys.platform', 'linux'):
            drain = Drain()
        drain.connected(accepted)
        self.assertEqual(drain.wait(0, lambda line: None), 1)
        self.assertEqual(accepted.recv(5), b'GET /')

    def test_an_idle_gateway_drains_at_once(self):
        lines = []
        d = Drain()
        d.connected("conn")
        d.begin("conn")
        self.assertEqual(d.idle, set())
        d.end("conn")
        self.assertEqual((d.inflight, d.idle), (0, {"conn"}))
        d.disconnected("conn")
        self.assertEqual(d.wait(5, lines.append), 0)
        self.assertEqual(lines, ["stop: draining 0 in-flight request(s), up to 5s", "stop: drained, exiting"])

    def test_a_shutdown_error_other_than_not_connected_is_logged(self):
        class Conn:
            def __init__(self, code):
                self.code = code

            def shutdown(self, how):
                raise OSError(self.code, os.strerror(self.code))
        lines = []
        d = Drain()
        for code in (errno.ENOTCONN, errno.EBADF):
            connection = Conn(code)
            d.connected(connection)
            d.begin(connection)
            d.end(connection)
        self.assertEqual(d.wait(0, lines.append), 0)
        failed = [line for line in lines if "shutdown" in line]
        self.assertEqual(len(failed), 1, lines)
        self.assertIn("OSError(%d, " % errno.EBADF, failed[0])


HEALTH = b"GET /__gateway/health HTTP/1.1\r\nhost: x\r\n\r\n"


def answer(sock):
    """The status line and headers of one health reply on `sock`, read to the end of its body."""
    data = b""
    while b"\r\n\r\n" not in data:
        got = sock.recv(4096)
        if not got:
            raise AssertionError("closed before an answer: %r" % data)
        data += got
    head, _, rest = data.partition(b"\r\n\r\n")
    length = int(head.lower().split(b"content-length: ")[1].split()[0])
    while len(rest) < length:
        got = sock.recv(4096)
        if not got:
            raise AssertionError("closed inside the body: %d of %d bytes" % (len(rest), length))
        rest += got
    return head


class IdleClientTest(unittest.TestCase):
    def test_a_client_idle_past_the_limit_is_closed_and_each_request_restarts_the_clock(self):
        with mock.patch.object(proxy, "CLIENT_IDLE_SECONDS", 1.0, create=True):
            rt, port = fakes.start_gateway(self, fakes.start_upstream(self, fakes.SlowUpstream))
            sock = socket.create_connection(("127.0.0.1", port), timeout=30)
            self.addCleanup(sock.close)
            for _ in range(3):  # 0.9 s in all, never 1 s between two requests (the 1.5 s stream below outlasts the limit)
                sock.sendall(HEALTH)
                self.assertNotIn(b"keep-alive", answer(sock).lower())  # no Keep-Alive hint: the reply's headers are the upstream's
                time.sleep(0.3)
            sock.settimeout(15)
            try:
                closed = sock.recv(1) == b""
            except socket.timeout:
                closed = False
            self.assertTrue(closed, "the gateway kept an idle client connection open")
            # A reply streaming for longer than the limit is in flight, not idle.
            resp, data = fakes.request(port, "POST", "/v1/messages", b'{"model":"m"}')
            self.assertIn(b"message_stop", data)


class BurstTest(unittest.TestCase):
    def test_a_burst_of_100_simultaneous_connects_is_served_without_a_reset(self):
        jail.raise_open_files(self, 1024)  # 100 clients and their 100 accepted ends, from a shell that starts at 256
        rt, _ = fakes.start_gateway(self)
        server = Server(("127.0.0.1", 0), rt)  # listening, not yet serving: every connect waits in the backlog
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        socks = []
        self.addCleanup(lambda: [s.close() for s in socks])
        for _ in range(100):
            s = socket.socket()
            s.setblocking(False)
            s.connect_ex(("127.0.0.1", port))
            socks.append(s)
        waiting = list(socks)
        deadline = time.time() + 3
        while waiting and time.time() < deadline:
            _, ready, _ = select.select([], waiting, [], 0.2)
            waiting = [s for s in waiting if s not in ready]
        self.assertEqual(len(waiting), 0, "%d of 100 connects were not accepted into the backlog" % len(waiting))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        failed = []
        for s in socks:
            s.setblocking(True)
            s.settimeout(10)
            try:
                s.sendall(HEALTH)
                if answer(s).split(b"\r\n")[0] != b"HTTP/1.1 200 OK":
                    failed.append("not a 200")
            except (OSError, AssertionError) as error:
                failed.append(repr(error))
        self.assertEqual((len(failed), failed[:2]), (0, []), "of 100 simultaneous connects")


class AcceptFailureTest(unittest.TestCase):
    def setUp(self):
        handles.THROTTLE.clear()

    def serve_after(self, failures):
        """A health answer from a gateway whose first accepts raise `failures`, and its events."""
        pending = list(failures)
        real = socket.socket._accept

        def flaky(sock):
            if pending:
                raise pending.pop(0)
            return real(sock)
        rt, port = fakes.start_gateway(self)
        with mock.patch.object(socket.socket, "_accept", flaky):
            resp, data = fakes.request(port, "GET", "/__gateway/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(pending, [])
        return rt

    def test_running_out_of_descriptors_at_accept_is_one_event_per_window(self):
        rt = self.serve_after([OSError(errno.EMFILE, "Too many open files")] * 3)
        found = fakes.events(rt, "fd_exhausted")
        self.assertEqual([e["fields"]["where"] for e in found], ["accept"])
        event = found[0]
        self.assertIn("Too many open files", event["fields"]["error"])
        self.assertEqual(fakes.events(rt, "accept_failed"), [])

    def test_any_other_accept_failure_is_an_event_too(self):
        rt = self.serve_after([OSError(errno.ECONNABORTED, "Software caused connection abort")])
        found = fakes.events(rt, "accept_failed")
        self.assertEqual(len(found), 1)
        self.assertIn("Software caused connection abort", found[0]["fields"]["error"])
        self.assertEqual(fakes.events(rt, "fd_exhausted"), [])


class AcceptBackoffTest(unittest.TestCase):
    """Repeated accept failures of any kind wait longer each time, up to a bound, and a success starts over."""

    def setUp(self):
        handles.THROTTLE.clear()

    def test_a_shared_queue_consumed_by_the_successor_is_not_an_accept_failure(self):
        waits, seen, events = self.accepts([BlockingIOError(errno.EAGAIN, 'queue consumed')])
        self.assertEqual((waits, events), ([], []))
        self.assertEqual(seen, [None])

    def accepts(self, outcomes, failures=0):
        """The waits and the events of a listener whose accepts end as `outcomes`, after `failures` in a row: an error is
        raised, a string returns."""
        server = Server.__new__(Server)
        server.runtime = mock.Mock()
        server.accept_failures = failures
        waits, seen = [], []
        for outcome in outcomes:
            with mock.patch.object(clock, "sleep", waits.append), \
                    mock.patch("http.server.HTTPServer.get_request",
                               side_effect=[outcome] if isinstance(outcome, OSError) else None,
                               return_value=outcome):
                try:
                    seen.append(server.get_request())
                except OSError:
                    seen.append(None)
        return waits, seen, server.runtime.db.event.call_args_list

    def test_waits_double_up_to_a_bound_for_any_failure_and_start_over_after_a_success(self):
        other = OSError(errno.ECONNABORTED, "Software caused connection abort")
        short = OSError(errno.EMFILE, "Too many open files")
        waits, seen, events = self.accepts([other, short, other, other, other, other, other, "client", other, short])
        self.assertEqual(waits, [0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0, 0.1, 0.2])
        self.assertEqual(seen.count("client"), 1)
        self.assertEqual([c[0][0] for c in events], ["accept_failed", "fd_exhausted"])
        self.assertEqual([c[1]["suppressed"] for c in events], [0, 0])
        # a listener failing for good, past the 1024 doublings a float holds, still waits the bound and goes on
        self.assertEqual(self.accepts([other], failures=5000)[:2], ([2.0], [None]))


if __name__ == "__main__":
    unittest.main()
