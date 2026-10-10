"""Codex turn-state stickiness: request positions, one lane's token, and the token's life through a real gateway."""
import unittest
from unittest.mock import patch

from model_gateway.chatgpt import turn
from tests import fakes
from tests.model_gateway.chatgpt import test_route as route_tests

ASK = "Describe your most recent action in 3-5 words using present tense (-ing). Do not use tools."
CALL = dict(role="assistant", content=[dict(type="tool_use", id="call_invented", name="lookup", input={})])
RESULT = dict(role="user", content=[dict(type="tool_result", tool_use_id="call_invented", content="Harbor is sunny")])
FORK = dict(role="user", content=RESULT["content"] + [dict(type="text", text=ASK)])
START = [dict(role="user", content="Look up Harbor")]
NEXT = START + [CALL, RESULT, dict(role="assistant", content="Sunny."), dict(role="user", content="Now Quay")]


class TurnTest(unittest.TestCase):
    def test_positions(self):
        for messages, where in ((START, turn.START), (START + [CALL, RESULT], turn.CONTINUE), (START + [CALL, FORK], turn.FORK),
                                (NEXT, turn.START), (START + [CALL, dict(role="user", content=[dict(type="text", text="go")])],
                                                     turn.START), ([], turn.START), ([CALL], turn.START)):
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
        steps = [  # messages, ingress headers, (status, issued token) per attempt, the gpt line's ending
            (START, None, [(200, "ts-1")], "stale=0 turn_state=issued"),
            (START + [CALL, RESULT], None, [(200, "ts-2")], "stale=0 turn_state=sent"),
            (START + [CALL, FORK], None, [(200, "ts-fork")], "stale=0"),
            (START + [CALL, RESULT], other, [(200, None)], "stale=0"),
            (START + [CALL, RESULT, CALL, RESULT], None, [(503, "ts-error"), (200, None)], "stale=0 turn_state=sent"),
            (NEXT, None, [(200, None)], "stale=0"),
            (NEXT + [CALL, RESULT], None, [(200, "ts-3")], "stale=0 turn_state=issued"),
            (NEXT + [CALL, RESULT, CALL, RESULT], None, [(200, None)], "stale=0 turn_state=sent")]
        sent = [None, "ts-1", None, None, "ts-1", "ts-1", None, None, "ts-3"]  # the header each Responses request carried
        attempts = [attempt for step in steps for attempt in step[2]]
        self.fake.script = [(200, fakes.responses_turn()) if status == 200 else unavailable for status, _ in attempts]
        self.fake.issued = [token for _, token in attempts]
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            for messages, headers, _, _ in steps:
                self.assertEqual(self.send(port, dict(self.body, messages=messages), headers)[0].status, 200)
        self.assertEqual([x["headers"].get("x-codex-turn-state") for x in self.responses()], sent)
        lines = [x for x in fakes.logged(rt).splitlines() if "    gpt lane=" in x]
        self.assertEqual(len(lines), len(steps))
        for line, step in zip(lines, steps):
            with self.subTest(step=steps.index(step)):
                self.assertTrue(line.endswith(step[3]), line)


if __name__ == "__main__":
    unittest.main()
