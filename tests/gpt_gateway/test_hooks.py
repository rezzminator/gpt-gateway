"""Tests for gpt_gateway.hooks: order, the single re-serialization, failure isolation, the model guard."""
import json
import os
import unittest
from types import MappingProxyType

from gpt_gateway.headers import Headers
from gpt_gateway.hooks import Chain, Context, Request, Result, parse_object
from gpt_gateway.middleware import load_modules
from tests import jail

RAW = b'{"model": "claude-x",  "messages": [{"role": "user", "content": "hi"}], "z": 1}'


class ChainCase(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(jail.scratch(self), "middleware")
        self.lines = []  # ctx.log lines and the chain's events, in order
        self.events = []

    def record(self, kind, request_id=None, lane=None, **fields):
        """The chain's database event, kept whole and as the line a test reads it by."""
        self.events.append((kind, request_id, fields))
        where = " at " + fields["where"] if fields.get("where") else ""
        self.lines.append("middleware %s %s failed: %s%s" % (fields.get("module"), fields.get("hook"),
                                                              fields.get("error"), where)
                          if kind == "middleware_failed" else "%s %r" % (kind, fields))

    def chain(self, sources, settings=None):
        for module, source in sources.items():
            jail.write_middleware(self.dir, module, source)
        modules = load_modules(list(sources), self.dir)
        return Chain(modules, self.record), modules

    def ctx(self, settings=None):
        return Context(log=self.lines.append, log_path="/log", settings=MappingProxyType(settings or {}),
                       kind="main", method="POST", path="/v1/messages", headers=Headers([("a", "b")]),
                       model="claude-x", request_id=1)

    def request(self, raw=RAW):
        return Request(raw, parse_object(raw))


ORDER = '''
NAME = "{n}"
def on_request(ctx, req):
    ctx.log("{n} on_request")
    return " {n}=1"
def on_event(ctx, event):
    ctx.log("{n} on_event")
def on_response_end(ctx, result):
    ctx.log("{n} on_response_end")
'''


class OrderTest(ChainCase):
    def test_chatgpt_route_is_readonly_and_model_changes_roll_back(self):
        chain, modules = self.chain({"switch": 'NAME = "switch"\ndef on_request(ctx, req):\n    req.body["model"] = "other"\n    req.mark_changed()\n'})
        ctx = Context(log=self.lines.append, log_path=None, settings={}, route="chatgpt")
        with self.assertRaises(AttributeError):
            ctx.route = "anthropic"
        req = self.request()
        self.assertIs(chain.run_request(ctx, req)[0], RAW)
        self.assertEqual(ctx.route, "chatgpt")
        self.assertEqual(modules[0].errors, 1)

    def test_hooks_run_in_list_order(self):
        chain, _ = self.chain({"c": ORDER.format(n="c"), "a": ORDER.format(n="a"), "b": ORDER.format(n="b")})
        ctx = self.ctx()
        sent, notes = chain.run_request(ctx, self.request())
        self.assertEqual(notes, [" c=1", " a=1", " b=1"])
        sink = chain.event_sink(ctx, "text/event-stream")
        sink.feed(b"data: {}\n\n")
        sink.close(True)
        chain.run_response_end(ctx, Result(200, "OK", (), b"", True, False, 0.1, ""))
        self.assertEqual(self.lines, ["%s %s" % (n, hook) for hook in ("on_request", "on_event", "on_response_end")
                                      for n in ("c", "a", "b")])


class BodyTest(ChainCase):
    def test_an_untouched_body_goes_as_its_original_bytes(self):
        chain, _ = self.chain({"look": 'NAME = "look"\ndef on_request(ctx, req):\n    req.body["model"]\n'})
        req = self.request()
        sent, _ = chain.run_request(self.ctx(), req)
        self.assertIs(sent, RAW)

    def test_an_edit_without_mark_changed_is_not_sent(self):
        chain, _ = self.chain({"quiet": 'NAME = "quiet"\ndef on_request(ctx, req):\n    req.body["z"] = 2\n'})
        self.assertIs(chain.run_request(self.ctx(), self.request())[0], RAW)

    def test_mark_changed_serializes_once_compactly(self):
        chain, _ = self.chain({
            "one": 'NAME = "one"\ndef on_request(ctx, req):\n    req.body["z"] = 2\n    req.mark_changed()\n',
            "two": 'NAME = "two"\ndef on_request(ctx, req):\n    req.body["added"] = "é"\n    req.mark_changed()\n'})
        sent, _ = chain.run_request(self.ctx(), self.request())
        expected = json.loads(RAW)
        expected.update(z=2, added="é")
        self.assertEqual(sent, json.dumps(expected, separators=(",", ":")).encode())
        self.assertEqual(sent, b'{"model":"claude-x","messages":[{"role":"user","content":"hi"}],"z":2,'
                               b'"added":"\\u00e9"}')

    def test_mark_changed_needs_a_json_object(self):
        chain, modules = self.chain({"mark": 'NAME = "mark"\ndef on_request(ctx, req):\n    req.mark_changed()\n'})
        raw = b"[1, 2]"
        self.assertIs(chain.run_request(self.ctx(), self.request(raw))[0], raw)
        self.assertEqual(modules[0].errors, 1)
        self.assertIn("middleware mark on_request failed: ValueError", self.lines[0])


FAILS = '''
NAME = "fails"
def on_request(ctx, req):
    req.body["messages"] = []
    req.mark_changed()
    raise RuntimeError("boom")
'''
EDITS = 'NAME = "edits"\ndef on_request(ctx, req):\n    req.body["z"] = 9\n    req.mark_changed()\n    return " edits=1"\n'
LATER = 'NAME = "later"\nRAN = []\ndef on_request(ctx, req):\n    RAN.append(1)\n    return " later=1"\n'


class FailureTest(ChainCase):
    def test_an_on_request_failure_sends_the_original_bytes_and_skips_the_rest(self):
        import sys
        chain, modules = self.chain({"edits": EDITS, "fails": FAILS, "later": LATER})
        sent, notes = chain.run_request(self.ctx(), self.request())
        self.assertIs(sent, RAW)
        self.assertEqual(notes, [])
        self.assertEqual(sys.modules["gpt_gateway_mw_later"].RAN, [])
        self.assertEqual([m.errors for m in modules], [0, 1, 0])
        (line,) = self.lines
        self.assertRegex(line, r"^middleware fails on_request failed: RuntimeError\('boom',?\) at fails\.py:6 in on_request$")
        (kind, request_id, fields), = self.events
        self.assertEqual((kind, request_id, fields["module"], fields["hook"], fields["where"]),
                         ("middleware_failed", 1, "fails", "on_request", "fails.py:6 in on_request"))

    def test_a_changed_model_is_a_failure_of_that_hook(self):
        chain, modules = self.chain({
            "edits": EDITS,
            "router": 'NAME = "router"\ndef on_request(ctx, req):\n    req.body["model"] = "other"\n    req.mark_changed()\n',
            "later": LATER})
        sent, _ = chain.run_request(self.ctx(), self.request())
        self.assertIs(sent, RAW)
        self.assertEqual([m.errors for m in modules], [0, 1, 0])
        self.assertIn("middleware router on_request failed: ValueError(\"changed body['model']", self.lines[0])
        chain, _ = self.chain({"drop": 'NAME = "drop"\ndef on_request(ctx, req):\n    del req.body["model"]\n'})
        self.assertIs(chain.run_request(self.ctx(), self.request())[0], RAW)
        self.assertIn("middleware drop on_request failed", self.lines[-1])

    def test_a_rebound_body_a_bad_return_and_an_unserializable_edit_fail(self):
        for module, source, text in (
                ("rebind", "def on_request(ctx, req):\n    req.body = {}\n", "AttributeError"),
                ("number", "def on_request(ctx, req):\n    return 3\n", "returned int, not str or None"),
                ("setish", "def on_request(ctx, req):\n    req.body['s'] = {1}\n    req.mark_changed()\n",
                 "TypeError"),
                ("exits", "import sys\ndef on_request(ctx, req):\n    sys.exit(1)\n", "SystemExit")):
            chain, modules = self.chain({module: 'NAME = "%s"\n' % module + source})
            self.assertIs(chain.run_request(self.ctx(), self.request())[0], RAW, module)
            self.assertEqual(modules[0].errors, 1, module)
            self.assertIn("middleware %s on_request failed: " % module, self.lines[-1])
            self.assertIn(text, self.lines[-1])

    def test_on_event_and_on_response_end_failures_are_skipped(self):
        bad = 'NAME = "bad"\nN = []\ndef on_event(ctx, e):\n    N.append(e)\n    raise KeyError("x")\n' \
              'def on_response_end(ctx, r):\n    raise ValueError("end")\n'
        good = 'NAME = "good"\nEVENTS = []\nENDS = []\ndef on_event(ctx, e):\n    EVENTS.append(e["event"])\n' \
               'def on_response_end(ctx, r):\n    ENDS.append(r.status)\n'
        import sys
        chain, modules = self.chain({"bad": bad, "good": good})
        ctx = self.ctx()
        sink = chain.event_sink(ctx, "text/event-stream; charset=utf-8")
        sink.feed(b"event: a\ndata: {}\n\nevent: b\ndata: {}\n\n")
        sink.close(True)
        chain.run_response_end(ctx, Result(200, "OK", (), b"", True, False, 0.0, ""))
        self.assertEqual(sys.modules["gpt_gateway_mw_good"].EVENTS, ["a", "b"])
        self.assertEqual(sys.modules["gpt_gateway_mw_good"].ENDS, [200])
        self.assertEqual(len(sys.modules["gpt_gateway_mw_bad"].N), 1)  # skipped for the rest of the response
        self.assertEqual(modules[0].errors, 2)
        self.assertEqual([line.split(":")[0] for line in self.lines],
                         ["middleware bad on_event failed", "middleware bad on_response_end failed"])

    def test_a_non_stream_json_body_is_one_event_and_others_none(self):
        import sys
        chain, _ = self.chain({"rec": 'NAME = "rec"\nE = []\ndef on_event(ctx, e):\n    E.append(e)\n'})
        sink = chain.event_sink(self.ctx(), "application/json")
        sink.feed(b'{"a":')
        sink.feed(b'1}')
        sink.close(True)
        self.assertEqual(sys.modules["gpt_gateway_mw_rec"].E, [{"event": None, "data": {"a": 1}, "raw": b'{"a":1}'}])
        self.assertIsNone(chain.event_sink(self.ctx(), "text/html"))
        chain, _ = self.chain({"plain": 'NAME = "plain"\n'})
        self.assertIsNone(chain.event_sink(self.ctx(), "text/event-stream"))

    def test_a_body_past_the_sink_limit_is_one_event_of_its_request(self):
        from unittest import mock
        from gpt_gateway import hooks
        chain, _ = self.chain({"rec": 'NAME = "rec"\nE = []\ndef on_event(ctx, e):\n    E.append(e)\n'})
        with mock.patch.object(hooks, "MAX_JSON_EVENT", 4):
            sink = chain.event_sink(self.ctx(), "application/json")
            sink.feed(b'{"a":')
            sink.feed(b'1}')
            sink.close(True)
        self.assertEqual(self.events, [("event_sink", 1, {"reason": "json_too_large", "limit": 4})])


ANSWERS = 'NAME = "answers"\ndef on_request(ctx, req):\n    req.answer(200, "text/plain", b"here")\n    return " answers=1"\n'
TURN = 'NAME = "turn"\ndef on_request(ctx, req):\n    last = req.last_turn_message()\n    return " turn=%s" % (last or {}).get("role")\n'


class AnswerTest(ChainCase):
    def test_an_answer_ends_the_chain_sends_nothing_and_names_its_module(self):
        import sys
        chain, modules = self.chain({"edits": EDITS, "answers": ANSWERS, "later": LATER})
        req = self.request()
        sent, notes = chain.run_request(self.ctx(), req)
        self.assertEqual(notes, [" edits=1", " answers=1", " answered=answers"])
        self.assertIs(sent, RAW)  # an earlier edit is never serialized: nothing goes upstream
        self.assertEqual(sys.modules["gpt_gateway_mw_later"].RAN, [])
        self.assertEqual((req.answered.status, req.answered.content_type, req.answered.data),
                         (200, "text/plain", b"here"))
        self.assertEqual([m.errors for m in modules], [0, 0, 0])
        self.assertIsNone(self.request().answered)

    def test_a_hook_that_answers_and_then_fails_leaves_no_answer(self):
        half = 'NAME = "half"\ndef on_request(ctx, req):\n    req.answer(200, "text/plain", b"x")\n    raise RuntimeError("after")\n'
        chain, modules = self.chain({"half": half, "later": LATER})
        req = self.request()
        sent, notes = chain.run_request(self.ctx(), req)
        self.assertIn("middleware half on_request failed: RuntimeError('after')", self.lines[-1])
        self.assertEqual((sent, notes, modules[0].errors), (RAW, [], 1))
        self.assertIsNone(req.answered)

    def test_a_malformed_answer_is_a_failure_of_its_hook(self):
        for call, text in (('200.0, "text/plain", b""', "ValueError('answer: status must be an int from 200 to 599"),
                           ('True, "text/plain", b""', "ValueError('answer: status must be an int from 200 to 599"),
                           ('199, "text/plain", b""', "ValueError('answer: status must be an int from 200 to 599"),
                           ('600, "text/plain", b""', "ValueError('answer: status must be an int from 200 to 599"),
                           ('200, "", b""', 'ValueError("answer: content_type must be a non-empty string'),
                           ('200, "text/plain", "text"', "TypeError('answer: data must be bytes, got str")):
            source = 'NAME = "bad"\ndef on_request(ctx, req):\n    req.answer(%s)\n' % call
            chain, modules = self.chain({"bad": source})
            req = self.request()
            self.assertIs(chain.run_request(self.ctx(), req)[0], RAW, call)
            self.assertIn("middleware bad on_request failed: " + text, self.lines[-1], call)
            self.assertIsNone(req.answered, call)

    def test_last_turn_message_skips_the_trailing_system_notices(self):
        chain, _ = self.chain({"turn": TURN})
        user, assistant = {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}
        notice = {"role": "system", "content": "a notice"}
        for messages, role in (([user, assistant], "assistant"),
                               ([assistant, user, notice, dict(notice, content=[])], "user"),
                               ([notice], None), ([], None), ("not a list", None)):
            raw = json.dumps({"model": "claude-x", "messages": messages}).encode()
            self.assertEqual(chain.run_request(self.ctx(), self.request(raw))[1], [" turn=%s" % role], messages)
        self.assertEqual(chain.run_request(self.ctx(), self.request(b"[1]"))[1], [" turn=None"])


class ContextTest(ChainCase):
    def test_settings_and_state_belong_to_the_running_module(self):
        src = 'NAME = "{n}"\nGOT = []\ndef on_request(ctx, req):\n    ctx.state["k"] = "{n}"\n    GOT.append(dict(ctx.settings))\n' \
              'def on_response_end(ctx, r):\n    GOT.append(ctx.state["k"])\n'
        import sys
        chain, _ = self.chain({"p": src.format(n="p"), "q": src.format(n="q")})
        ctx = self.ctx({"p": MappingProxyType({"v": 1}), "q": MappingProxyType({"v": 2})})
        chain.run_request(ctx, self.request())
        chain.run_response_end(ctx, Result(200, "OK", (), b"", True, False, 0.0, ""))
        self.assertEqual(sys.modules["gpt_gateway_mw_p"].GOT, [{"v": 1}, "p"])
        self.assertEqual(sys.modules["gpt_gateway_mw_q"].GOT, [{"v": 2}, "q"])
        self.assertEqual(dict(ctx.settings), {})

    def test_fields_are_read_only(self):
        ctx = self.ctx()
        self.assertEqual((ctx.route, ctx.kind, ctx.model), ("anthropic", "main", "claude-x"))
        for name in ("kind", "route", "model", "headers"):
            with self.assertRaises(AttributeError):
                setattr(ctx, name, "x")
        with self.assertRaises(AttributeError):
            ctx.extra = 1
        req = self.request()
        with self.assertRaises(AttributeError):
            req.raw = b""


class HealthEntriesTest(ChainCase):
    def test_entries_and_a_failing_health(self):
        chain, modules = self.chain({
            "plain": 'NAME = "plain"\n',
            "said": 'NAME = "said"\ndef health(ctx):\n    return "v=%s" % ctx.settings.get("v")\n',
            "sick": 'NAME = "sick"\ndef health(ctx):\n    raise OSError("disk")\n'})
        ctx = Context(log=self.lines.append, log_path=None, settings=MappingProxyType({"said": {"v": 3}}))
        self.assertEqual(chain.health_entries(ctx), ["plain errors=0", "said v=3 errors=0", "sick health-error errors=1"])


if __name__ == "__main__":
    unittest.main()
