"""Lane identities are stable and partition session and agent pairs."""
import unittest
import uuid
from model_gateway.chatgpt import lane


class LaneTest(unittest.TestCase):
    def test_lane_formula_and_partition(self):
        for session, agent in [('session-a', 'agent-a'), ('session-a', ''), ('', ''), ('s:1', 'a:2')]:
            self.assertEqual(lane.key(session, agent), str(uuid.uuid5(uuid.NAMESPACE_OID, session + ':' + agent)))
        self.assertNotEqual(lane.key('s', 'a'), lane.key('s', 'b'))
        self.assertNotEqual(lane.key('s', 'a'), lane.key('t', 'a'))
