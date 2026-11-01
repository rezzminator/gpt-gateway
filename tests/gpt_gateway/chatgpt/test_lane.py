"""Lane identities are stable and partition session and agent pairs."""
import json
import unittest
import uuid
from gpt_gateway.chatgpt import lane


class LaneTest(unittest.TestCase):
    def test_lane_formula_and_partition(self):
        for session, agent in [('session-a', 'agent-a'), ('session-a', ''), ('', ''), ('s:1', 'a:2')]:
            pair = json.dumps([session, agent], separators=(',', ':'))
            self.assertEqual(lane.key(session, agent), str(uuid.uuid5(uuid.NAMESPACE_OID, pair)))
        self.assertNotEqual(lane.key('s', 'a'), lane.key('s', 'b'))
        self.assertNotEqual(lane.key('s', 'a'), lane.key('t', 'a'))

    def test_colons_cannot_alias_main_and_sub_or_two_sub_agents(self):
        for first, second in [(('session:agent', ''), ('session', 'agent:')),
                              (('session:part', 'agent'), ('session', 'part:agent'))]:
            with self.subTest(first=first, second=second):
                self.assertNotEqual(lane.key(*first), lane.key(*second))
