"""Codex turn-state stickiness: request positions, one lane's token, and the token's life through a real gateway;
the activity-summary fork's and the compaction request's effort through a real gateway."""
import json
import unittest
from unittest.mock import patch

from model_gateway.chatgpt import turn
from tests import fakes, jail
from tests.model_gateway.chatgpt import test_route as route_tests

ASK = "Describe your most recent action in 3-5 words using present tense (-ing). Do not use tools."
CALL = dict(role="assistant", content=[dict(type="tool_use", id="call_invented", name="lookup", input={})])
RESULT = dict(role="user", content=[dict(type="tool_result", tool_use_id="call_invented", content="Harbor is sunny")])
FORK = dict(role="user", content=RESULT["content"] + [dict(type="text", text=ASK)])
# Claude Code's compaction request: its summary prompt opens with this preamble, after the last tool result or alone.
COMPACT_ASK = "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool."
COMPACT = dict(role="user", content=RESULT["content"] + [dict(type="text", text=COMPACT_ASK)])
START = [dict(role="user", content="Look up Harbor")]
NEXT = START + [CALL, RESULT, dict(role="assistant", content="Sunny."), dict(role="user", content="Now Quay")]
# Claude Code appends harness notices (a background task finished, hook feedback, the environment) as trailing
# system messages; they never start a turn.
NOTICE = dict(role="system", content="[SYSTEM NOTIFICATION - NOT USER INPUT] A background task finished.")


class TurnTest(unittest.TestCase):
    def test_positions(self):
        for messages, where in ((START, turn.START), (START + [CALL, RESULT], turn.CONTINUE), (START + [CALL, FORK], turn.FORK),
                                (NEXT, turn.START), (START + [CALL, dict(role="user", content=[dict(type="text", text="go")])],
                                                     turn.START), ([], turn.START), ([CALL], turn.START),
                                (START + [NOTICE], turn.START), (START + [CALL, RESULT, NOTICE, NOTICE], turn.CONTINUE),
                                (START + [CALL, FORK, NOTICE], turn.FORK), (NEXT + [NOTICE], turn.START), ([NOTICE], turn.START)):
            with self.subTest(where=where, last=messages[-1:]):
                self.assertEqual(turn.position(dict(messages=messages)), where)

    def test_one_token_per_turn_never_from_or_to_a_fork(self):
        lane = turn.Turn()
        lane.begin(turn.START)
        self.assertEqual((lane.outgoing(turn.START), lane.observe(turn.START, "ts-1")), (None, True))
        self.assertEqual((lane.outgoing(turn.FORK), lane.observe(turn.FORK, "ts-fork")), (None, False))
        self.assertEqual((lane.outgoing(turn.CONTINUE), lane.observe(turn.CONTINUE, "ts-2")), ("ts-1", False))
        lane.begin(turn.CONTINUE)
        self.assertEqual(lane.outgoing(turn.CONTINUE), "ts-1")
        lane.begin(turn.START)
        self.assertEqual((lane.outgoing(turn.CONTINUE), lane.observe(turn.CONTINUE, None)), (None, False))


class TurnRouteTest(unittest.TestCase):
    setUp = route_tests.RouteTest.setUp
    jwt = route_tests.RouteTest.jwt
    gateway = route_tests.RouteTest.gateway
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses

    def test_token_replays_within_a_turn_and_never_crosses_turns_lanes_forks_or_failures(self):
        other = dict(self.headers, **{"x-claude-code-agent-id": "agent-other"})
        unavailable = (503, {"error": {"message": "unavailable"}})
        steps = [  # messages, ingress headers, (status, issued token) per attempt, the request row's turn_state
            (START, None, [(200, "ts-1")], "issued"),
            (START + [CALL, RESULT], None, [(200, "ts-2")], "sent"),
            (START + [CALL, RESULT, CALL, RESULT, NOTICE], None, [(200, "ts-notice")], "sent"),
            (START + [CALL, RESULT, CALL, FORK, NOTICE], None, [(200, "ts-fork-notice")], None),
            (START + [CALL, FORK], None, [(200, "ts-fork")], None),
            (START + [CALL, RESULT], other, [(200, None)], None),
            (START + [CALL, RESULT, CALL, RESULT], None, [(503, "ts-error"), (200, None)], "sent"),
            (NEXT, None, [(200, None)], None),
            (NEXT + [CALL, RESULT], None, [(200, "ts-3")], "issued"),
            (NEXT + [CALL, RESULT, CALL, RESULT], None, [(200, None)], "sent")]
        sent = [None, "ts-1", "ts-1", None, None, None, "ts-1", "ts-1", None, None, "ts-3"]  # the header each Responses request carried
        attempts = [attempt for step in steps for attempt in step[2]]
        self.fake.script = [(200, fakes.responses_turn()) if status == 200 else unavailable for status, _ in attempts]
        self.fake.issued = [token for _, token in attempts]
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            for messages, headers, _, _ in steps:
                self.assertEqual(self.send(port, dict(self.body, messages=messages), headers)[0].status, 200)
        self.assertEqual([x["headers"].get("x-codex-turn-state") for x in self.responses()], sent)
        self.assertEqual([(r["turn_state"], r["stale"], r["transport"]) for r in fakes.gpt_rows(rt)],
                         [(step[3], 0, "http") for step in steps])

    def test_a_fork_sends_its_lanes_last_effort_and_an_estimate_never_moves_it(self):
        # Claude Code sends the activity-summary fork at the agent's declared effort while an effort tag can run the
        # agent's turns at another; the backend's prompt cache keys on effort, so the fork rides its lane's cache only
        # at the effort the lane's last request sent.
        levels = [dict(effort=effort, description=effort) for effort in ("low", "medium", "high")]
        self.fake.models_body = dict(models=[dict(fakes.CHATGPT_MODELS["models"][0], supported_reasoning_levels=levels)])
        jail.write_json(self.models_path, self.fake.models_body)
        other = dict(self.headers, **{"x-claude-code-agent-id": "agent-other"})
        steps = [  # messages, effort asked, ingress headers, path, status of each Responses attempt
            (START, "medium", None, "/v1/messages", [200]),
            (START + [CALL, FORK], "high", None, "/v1/messages", [200]),
            (START + [CALL, RESULT], "low", None, "/v1/messages/count_tokens", []),
            (START + [CALL, RESULT, CALL, FORK, NOTICE], "high", None, "/v1/messages", [400, 200]),
            (START + [CALL, FORK], "high", other, "/v1/messages", [200]),
            (START + [CALL, RESULT], "low", None, "/v1/messages", [200]),
            (START + [CALL, RESULT, CALL, FORK], "high", None, "/v1/messages", [200])]
        sent = ["medium", "medium", "medium", "medium", "high", "low", "low"]  # each Responses attempt's effort
        dropped = (400, {"error": {"code": "invalid_encrypted_content"}})  # the retry drops reasoning, keeps the pin
        self.fake.script = [dropped if status == 400 else (200, fakes.responses_turn()) for step in steps for status in step[4]]
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            for messages, effort, headers, path, _ in steps:
                body = dict(self.body, messages=messages, output_config=dict(effort=effort))
                self.assertEqual(self.send(port, body, headers, path)[0].status, 200)
        self.assertEqual([json.loads(x["body"])["reasoning"].get("effort") for x in self.responses()], sent)
        self.assertEqual(len(fakes.events(rt, "drop_reasoning")), 1)

    def test_a_compaction_sends_its_lanes_last_effort_and_never_moves_it(self):
        # Claude Code sends every compaction request at effort high whatever effort the agent runs at; the backend's
        # prompt cache keys on effort, so at another effort the summary re-reads the agent's whole context uncached.
        levels = [dict(effort=effort, description=effort) for effort in ("low", "medium", "high")]
        self.fake.models_body = dict(models=[dict(fakes.CHATGPT_MODELS["models"][0], supported_reasoning_levels=levels)])
        jail.write_json(self.models_path, self.fake.models_body)
        steps = [(START, "low"), (START + [CALL, COMPACT], "high"), (START + [CALL, FORK, NOTICE], "high"),
                 ([dict(role="user", content=COMPACT_ASK)], "high"), ([dict(role="user", content="Summary: Harbor")], "medium"),
                 (START + [CALL, RESULT, dict(role="user", content=[dict(type="text", text="Please " + COMPACT_ASK)])], "high")]
        self.fake.script = [(200, fakes.responses_turn()) for _ in steps]
        _, port = self.gateway()
        for messages, effort in steps:
            body = dict(self.body, messages=messages, output_config=dict(effort=effort))
            self.assertEqual(self.send(port, body)[0].status, 200)
        sent = [json.loads(x["body"])["reasoning"].get("effort") for x in self.responses()]
        self.assertEqual(sent, ["low", "low", "low", "low", "medium", "high"])


if __name__ == "__main__":
    unittest.main()
