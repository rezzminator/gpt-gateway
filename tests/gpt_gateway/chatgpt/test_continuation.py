"""The continuation of a socket's last completed response: what a request sends, and how an output item matches."""
import json
import unittest

from gpt_gateway.chatgpt import continuation


class ContinuationTest(unittest.TestCase):
    """Codex's continuation (codex-rs/core/src/client.rs:341-462,1401-1462), with Claude Code's round trip in between."""
    USER = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Look up Harbor"}]}
    # The output items as the backend produced them, then as Claude Code returns them translated back.
    PRODUCED = [{"type": "reasoning", "id": "rs_invented", "summary": [{"type": "summary_text", "text": "Checking"}],
                 "encrypted_content": "opaque-invented"},
                {"type": "function_call", "id": "fc_invented", "call_id": "call_invented", "name": "lookup",
                 "arguments": '{"city": "Harbor"}', "status": "completed"},
                {"type": "message", "id": "msg_invented", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": "Harbor", "annotations": []},
                             {"type": "output_text", "text": " is sunny", "annotations": []}]}]
    RETURNED = [{"type": "reasoning", "id": "rs_invented", "summary": [], "encrypted_content": "opaque-invented"},
                {"type": "function_call", "call_id": "call_invented", "name": "lookup", "arguments": '{"city":"Harbor"}'},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Harbor is sunny"}]}]
    NEW = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Thanks"}]}

    def body(self, items, **fields):
        return dict({"model": "gpt-test", "input": items, "reasoning": {"effort": "low"}, "store": False}, **fields)

    def frame(self, body, baseline):
        data, delta = continuation.Request(json.dumps(body).encode(), "ts-invented").frame(baseline)
        return json.loads(data), delta

    def test_a_request_extending_the_last_response_sends_only_its_new_items_else_the_whole_request_and_why(self):
        baseline = continuation.Baseline(continuation.Request(json.dumps(self.body([self.USER])).encode()), "resp_invented",
                                      self.PRODUCED)
        sent, delta = self.frame(self.body([self.USER] + self.RETURNED + [self.NEW]), baseline)
        self.assertEqual(sent, dict(self.body([self.NEW]), type="response.create", previous_response_id="resp_invented",
                                    client_metadata={"x-codex-turn-state": "ts-invented"}))
        self.assertEqual(delta, "1/5")
        reasoning, call, said = self.RETURNED
        bay = dict(self.USER, content=[{"type": "input_text", "text": "Look up Bay"}])
        for why, body, base in [
                ("none", self.body([self.USER] + self.RETURNED + [self.NEW]), None),
                ("fields", self.body([self.USER] + self.RETURNED + [self.NEW], reasoning={"effort": "high"}), baseline),
                ("prefix", self.body([bay] + self.RETURNED + [self.NEW]), baseline),
                ("prefix", self.body([]), baseline),
                ("output", self.body([self.USER, reasoning, dict(call, arguments='{"city":"Bay"}'), said]), baseline),
                ("output", self.body([self.USER, dict(reasoning, encrypted_content="other"), call, said]), baseline),
                ("output", self.body([self.USER, reasoning, call, dict(said, content=[])]), baseline),
                ("output", self.body([self.USER, reasoning, call]), baseline)]:
            with self.subTest(why=why, body=body):
                self.assertEqual(self.frame(body, base),
                                 (dict(body, type="response.create", client_metadata={"x-codex-turn-state": "ts-invented"}),
                                  "full:" + why))

    def test_roundtrip_rejects_semantic_loss_and_empty_response_identity(self):
        reasoning, call, said = self.RETURNED
        cases = [
            (dict(self.PRODUCED[0], id="different"), reasoning),
            (self.PRODUCED[0], dict(reasoning, summary=[{"type": "summary_text", "text": "Changed"}])),
            (dict(self.PRODUCED[1], namespace="different"), call),
            (dict(self.PRODUCED[1], arguments='{"city":true}'), dict(call, arguments='{"city":1}')),
            (dict(self.PRODUCED[1], arguments='{"city":"Bay","city":"Harbor"}'), call),
            (dict(self.PRODUCED[2], content=[{"type":"refusal","text":"Harbor is sunny"}]), said),
            (dict(self.PRODUCED[2], content=[{"type":"output_text","text":"Harbor is sunny",
                                             "annotations":[{"type":"url_citation","url":"https://example.org"}]}]), said),
            (self.PRODUCED[2], dict(said, encrypted_content="changed")),
        ]
        for output, sent in cases:
            with self.subTest(output=output, sent=sent):
                self.assertFalse(continuation.produced(output, sent))
        baseline = continuation.Request(json.dumps(self.body([self.USER])).encode()).baseline("", [])
        self.assertNotIn("previous_response_id", self.frame(self.body([self.USER, self.NEW]), baseline)[0])

    def test_empty_reasoning_content_roundtrips_but_nonempty_or_unknown_content_does_not(self):
        output, sent = self.PRODUCED[0], self.RETURNED[0]
        self.assertTrue(continuation.produced(dict(output, content=[]), sent))
        self.assertTrue(continuation.produced(dict(output, content=None), sent))
        self.assertFalse(continuation.produced(dict(output, content=[{'type':'reasoning_text','text':'hidden'}]), sent))
        self.assertFalse(continuation.produced(dict(output, internal_chat_message_metadata_passthrough={'x':'y'}), sent))

    def test_item_key_order_and_per_request_authorization_do_not_break_prefix(self):
        old = dict(self.body([self.USER]), access_programs={"program":"one"})
        base = continuation.Request(json.dumps(old).encode()).baseline("resp_invented", [])
        reordered = dict(reversed(list(self.USER.items())))
        current = dict(self.body([reordered, self.NEW]), access_programs={"program":"two"})
        sent, delta = self.frame(current, base)
        self.assertEqual(delta, "1/2")
        self.assertEqual(sent["access_programs"], {"program":"two"})

    def test_supported_effort_change_is_a_control_delta_and_survives_next_request(self):
        base = continuation.Request(json.dumps(self.body([self.USER])).encode()).baseline("resp_first", [])
        body = self.body([self.USER, self.NEW], reasoning={"effort":"high"})
        current = continuation.Request(json.dumps(body).encode())
        current.supports_effort_updates = True
        frame, delta = current.frame(base)
        sent = json.loads(frame)
        self.assertEqual(delta, "2/2")
        self.assertEqual(sent['reasoning'], {'effort':'low'})
        self.assertEqual(sent['input'], [{'type':'configuration_update','reasoning':{'effort':'high'}}, self.NEW])
        next_base = current.baseline('resp_second', [])
        again = continuation.Request(json.dumps(self.body([self.USER, self.NEW, self.NEW],
                                                          reasoning={'effort':'high'})).encode())
        again.supports_effort_updates = True
        sent = json.loads(again.frame(next_base)[0])
        self.assertEqual(sent['reasoning'], {'effort':'low'})
        self.assertEqual(sent['input'], [self.NEW])
        disabled = continuation.Request(json.dumps(again.body).encode())
        self.assertEqual(disabled.frame(next_base)[1], 'full:fields')
        # A replacement socket/full request has no hidden controls: effective effort stays top-level.
        self.assertEqual(json.loads(again.frame(None)[0])['reasoning'], {'effort':'high'})
        unsupported = continuation.Request(json.dumps(body).encode())
        self.assertEqual(unsupported.frame(base)[1], 'full:fields')
        changed_summary = continuation.Request(json.dumps(dict(body, reasoning={'effort':'high','summary':'detailed'})).encode())
        changed_summary.supports_effort_updates = True
        self.assertEqual(changed_summary.frame(base)[1], 'full:fields')

    def test_a_changed_top_level_tool_list_sends_the_whole_request(self):
        added = [{'type': 'function', 'name': 'later'}]
        output = {'type': 'function_call_output', 'call_id': 'call_invented', 'output': 'Tool loaded: later'}
        baseline = continuation.Baseline(continuation.Request(json.dumps(self.body([self.USER], tools=[])).encode()),
                                        'resp_invented', self.PRODUCED)
        body = self.body([self.USER] + self.RETURNED + [output, self.NEW], tools=added)
        sent, delta = self.frame(body, baseline)
        self.assertEqual(sent['input'], body['input'])
        self.assertEqual(sent['tools'], added)
        self.assertNotIn('previous_response_id', sent)
        self.assertEqual(delta, 'full:fields')

    def test_an_output_item_of_another_kind_is_its_input_item_but_for_its_id_and_status(self):
        produced = {"type": "web_search_call", "id": "ws_invented", "status": "completed", "action": {"query": "Harbor"}}
        self.assertFalse(continuation.produced(produced, {"type": "web_search_call", "action": {"query": "Harbor"}}))
        self.assertTrue(continuation.produced(produced, dict(produced)))
        self.assertFalse(continuation.produced(produced, {"type": "web_search_call", "action": {"query": "Bay"}}))
        self.assertFalse(continuation.produced(dict(self.PRODUCED[0], encrypted_content=""), dict(self.RETURNED[0], encrypted_content="")))
        self.assertFalse(continuation.produced(self.PRODUCED[1], "not an item"))


if __name__ == "__main__":
    unittest.main()
