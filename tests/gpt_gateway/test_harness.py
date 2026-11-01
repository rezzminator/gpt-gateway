"""Tests for gpt_gateway.harness: a middleware file run in process against a request and a recorded response."""
import contextlib
import io
import json
import os
import unittest

from gpt_gateway import db
from gpt_gateway.harness import load, run, run_cli
from gpt_gateway.middleware import MiddlewareError
from gpt_gateway.settings import SettingsError
from tests import jail
from tests.fakes import JSON_BODY, SSE_BODY

WIDE = '''
NAME = "wide"
SETTINGS = {"tag": ("t", str, "GPT_GATEWAY_WIDE_TAG")}

def on_request(ctx, req):
    if req.body is not None:
        req.body["tag"] = ctx.settings["tag"]
        req.mark_changed()
    ctx.state["kind"] = ctx.kind
    return " wide=%s" % ctx.settings["tag"]

def on_event(ctx, event):
    if event["event"] == "content_block_delta":
        raise RuntimeError("delta")

def on_response_end(ctx, result):
    ctx.log("wide end %s %s %s" % (result.status, ctx.state["kind"], result.note.strip()))

def health(ctx):
    return "tag=" + ctx.settings["tag"]

def cli(argv, ctx):
    print("tag", ctx.settings["tag"], argv)
    ctx.log("cli " + ",".join(argv))
    return 4
'''


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.dir = jail.scratch(self)
        self.path = jail.write_middleware(self.dir, "wide", WIDE)

    def test_selected_route_reaches_every_hook(self):
        source = 'NAME = "route"\ndef on_request(ctx, req):\n    return " route=" + ctx.route\n'
        path = jail.write_middleware(self.dir, "route", source)
        self.assertEqual(run(path, body={"model": "gpt-test"}, route="chatgpt").note,
                         " kind=main route=chatgpt")

    def test_run_returns_forwarded_bytes_log_errors_events_and_result(self):
        out = run(self.path, body={"model": "m", "messages": []}, headers={"x-claude-code-agent-id": "a"},
                  response=SSE_BODY, chunk_size=5, settings={"tag": "x"})
        self.assertEqual(out.forwarded, b'{"model":"m","messages":[],"tag":"x"}')
        self.assertTrue(out.changed)
        self.assertEqual(out.note, " kind=sub wide=x")
        self.assertEqual([e["event"] for e in out.events], ["message_start", "content_block_delta", "message_stop"])
        self.assertEqual([(e["module"], e["hook"], e["error"]) for e in out.errors],
                         [("wide", "on_event", "RuntimeError('delta')")])
        self.assertEqual(out.log, ("wide end 200 sub kind=sub wide=x",))
        self.assertEqual({k: out.row[k] for k in ("method", "path", "kind", "route", "model", "status", "note", "stream",
                                                  "completed", "agent_id")},
                         {"method": "POST", "path": "/v1/messages", "kind": "sub", "route": "anthropic", "model": "m",
                          "status": 200, "note": "wide=x", "stream": False, "completed": True, "agent_id": "a"})
        self.assertEqual((out.result.status, out.result.completed), (200, True))
        self.assertEqual(out.health, "wide tag=x errors=1")
        self.assertEqual(dict(out.settings), {"tag": "x"})

    def test_untouched_bytes_and_a_json_response(self):
        plain = jail.write_middleware(self.dir, "plain", 'NAME = "plain"\n')
        raw = b'{"model": "m",   "x": 1}'
        out = run(plain, body=raw, response={"id": "r"}, status=429, reason="Too Many Requests")
        self.assertIs(out.forwarded, raw)
        self.assertFalse(out.changed)
        self.assertEqual(out.events, ({"event": None, "data": {"id": "r"}, "raw": b'{"id": "r"}'},))
        self.assertEqual(out.result.error_head, b'{"id": "r"}')
        self.assertEqual((out.row["status"], out.row["error_head"]), (429, '{"id": "r"}'))  # text, as the database holds it
        self.assertEqual(out.errors, ())
        out = run(plain, body=raw, response=JSON_BODY, content_type="application/json")
        self.assertEqual(out.events[0]["data"], json.loads(JSON_BODY))
        self.assertEqual((out.row["input_tokens"], out.row["cache_read_tokens"], out.row["hit"]), (3, 0, 0.0))

    def test_an_answer_is_relayed_and_nothing_is_forwarded(self):
        import sys
        source = ('NAME = "answering"\nEVENTS = []\ndef on_request(ctx, req):\n'
                  '    req.answer(200, "text/event-stream", b\'event: message_stop\\ndata: {"type":"message_stop"}\\n\\n\')\n'
                  '    return " answering=1"\n'
                  'def on_event(ctx, event):\n    EVENTS.append(event["event"])\n')
        path = jail.write_middleware(self.dir, "answering", source)
        out = run(path, body={"model": "m"}, headers={"x-claude-code-agent-id": "a"}, response=SSE_BODY, chunk_size=4)
        self.assertEqual(out.note, " kind=sub answering=1 answered=answering")
        self.assertEqual((out.log, out.row["note"], out.row["answered_by"]),
                         ((), "answering=1 answered=answering", "answering"))
        self.assertIsNone(out.forwarded)
        self.assertFalse(out.changed)
        self.assertEqual([e["event"] for e in out.events], ["message_stop"])
        self.assertEqual(sys.modules["gpt_gateway_mw_answering"].EVENTS, ["message_stop"])
        self.assertEqual((out.result.status, out.result.reason, out.result.headers, out.result.note),
                         (200, "OK", (("content-type", "text/event-stream"),), out.note))
        self.assertEqual((out.answer.status, out.answer.content_type, out.answer.data[:20]),
                         (200, "text/event-stream", b"event: message_stop\n"))
        self.assertIsNone(run(self.path, body={"model": "m"}).answer)

    def test_env_overrides_and_load_errors(self):
        self.assertEqual(run(self.path, body=b"", env={"GPT_GATEWAY_WIDE_TAG": "e"}).note, " kind=main wide=e")
        with self.assertRaises(SettingsError):
            load(self.path, settings={"nope": 1})
        with self.assertRaises(SettingsError):
            load(os.path.join(self.dir, "Bad-Name.py"))
        with self.assertRaises(MiddlewareError):
            load(os.path.join(self.dir, "absent.py"))

    def test_a_check_warning_reaches_stderr(self):
        path = jail.write_middleware(self.dir, "careful", 'NAME = "careful"\ndef check(s):\n    return "mind the cache"\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            load(path)
        self.assertEqual(err.getvalue(), "gpt-gateway harness: middleware careful: check warning: mind the cache\n")

    def test_run_cli(self):
        log, path = os.path.join(self.dir, "gw.log"), os.path.join(self.dir, "gateway.db")
        code, out, err = run_cli(self.path, ["a", "b"], settings={"tag": "c"}, log_path=log, db_path=path)
        self.assertEqual((code, out, err), (4, "tag c ['a', 'b']\n", ""))
        self.assertEqual(db.query(path, "SELECT kind, fields FROM events"),
                         [{"kind": "middleware_log", "fields": '{"line": "cli a,b"}'}])
        self.assertFalse(os.path.exists(log))  # a middleware's line goes to the database, never the text log


if __name__ == "__main__":
    unittest.main()
