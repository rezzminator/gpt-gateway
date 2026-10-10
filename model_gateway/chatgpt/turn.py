"""Codex's `x-codex-turn-state` sticky-routing token, kept per lane for one agent turn.

Derived from openai/codex, changed (Apache-2.0); see THIRD_PARTY_NOTICES.md. codex-rs/core/src/client.rs:269-292
and codex-api/src/sse/responses.rs:64-70: the backend issues the token on a turn's first response; the client
replays it unchanged on every later request of that turn, retries included, and never sends it into another turn.
A Claude Code agent's turn starts at a user message without a tool result and continues while each request answers
the model's tool calls; the trailing system messages Claude Code appends (task notifications, hook feedback, the
environment) never start one, and the activity-summary fork is no part of the turn.
"""
from . import request

HEADER = "x-codex-turn-state"
START, CONTINUE, FORK = "start", "continue", "fork"


def position(body):
    """FORK for the activity-summary fork, CONTINUE when the last turn message is the user's and carries a tool result, else START."""
    if request.activity_summary(body):
        return FORK
    last = request.last_turn_message(body)
    content = last.get("content") if isinstance(last, dict) and last.get("role") == "user" else None
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return CONTINUE
    return START


class Turn:
    """One lane's token; the route calls every method under its lock."""

    def __init__(self):
        self.token = None

    def begin(self, where):
        """A new turn drops the last turn's token before its first attempt."""
        if where == START:
            self.token = None

    def outgoing(self, where):
        """The token an attempt sends: none from a fork, otherwise whatever this turn holds."""
        return None if where == FORK else self.token

    def observe(self, where, issued):
        """Keep the first token the turn's responses issue; True when this response's token was kept."""
        if where == FORK or not issued or self.token is not None:
            return False
        self.token = issued
        return True
