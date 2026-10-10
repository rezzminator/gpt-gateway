"""The continuation of a socket's last completed response: what a request sends, and how an output item matches."""
import json
import unittest

from model_gateway.chatgpt import continuation


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

    def test_a_tool_delivered_after_a_result_is_in_the_new_tail_and_the_prefix_stays(self):
        added = {"type": "additional_tools", "role": "developer", "tools": [{"type": "function", "name": "later"}]}
        output = {"type": "function_call_output", "call_id": "call_invented", "output": "Tool loaded: later"}
        baseline = continuation.Baseline(continuation.Request(json.dumps(self.body([self.USER])).encode()), "resp_invented",
                                      self.PRODUCED)
        sent, delta = self.frame(self.body([self.USER] + self.RETURNED + [output, added, self.NEW]), baseline)
        self.assertEqual(sent["input"], [output, added, self.NEW])
        self.assertEqual(sent["previous_response_id"], "resp_invented")

    def test_an_output_item_of_another_kind_is_its_input_item_but_for_its_id_and_status(self):
        produced = {"type": "web_search_call", "id": "ws_invented", "status": "completed", "action": {"query": "Harbor"}}
        self.assertTrue(continuation.produced(produced, {"type": "web_search_call", "action": {"query": "Harbor"}}))
        self.assertFalse(continuation.produced(produced, {"type": "web_search_call", "action": {"query": "Bay"}}))
        self.assertFalse(continuation.produced(dict(self.PRODUCED[0], encrypted_content=""), dict(self.RETURNED[0], encrypted_content="")))
        self.assertFalse(continuation.produced(self.PRODUCED[1], "not an item"))


if __name__ == "__main__":
    unittest.main()
