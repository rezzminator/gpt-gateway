"""Tests for middleware/status_label.py: Claude Code's activity-summary fork answered in the gateway, every other
request untouched. The prompt below is Claude Code's, byte for byte; every conversation around it is invented."""
import json
import os
import unittest

from gpt_gateway.harness import run
from tests import jail

MODULE = os.path.join(jail.ROOT, "middleware", "status_label.py")
ASK = ("Describe your most recent action in 3-5 words using present tense (-ing). Name the file or function, "
       "not the branch. Do not use tools.\n\n")
EXAMPLES = ('Good: "Reading runAgent.ts"\nGood: "Fixing null check in validate.ts"\nGood: "Running auth module tests"\n'
            'Good: "Adding retry logic to fetchUser"\n\nBad (past tense): "Analyzed the branch diff"\n'
            'Bad (too vague): "Investigating the issue"\n'
            'Bad (too long): "Reviewing full branch diff and AgentTool.tsx integration"\n'
            'Bad (branch name): "Analyzed adam/background-summary branch diff"\n')
SUB = {"x-claude-code-agent-id": "agent-invented"}
RECORDED = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def ask(previous=None):
    return ASK + ('Previous: "%s" — say something NEW.\n\n' % previous if previous else "") + EXAMPLES


def call(name, args, thinking=False):
    content = [{"type": "thinking", "thinking": "", "signature": "sig-invented"}] if thinking else []
    return {"role": "assistant", "content": content + [{"type": "tool_use", "id": "toolu_1", "name": name, "input": args}]}


def fork(before, text=None, stream=True, trailing=(), opening=({"role": "user", "content": "Start the work."},)):
    """A fork request: the opening messages, `before`, the fork's user message, then `trailing` notices."""
    blocks = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "done"},
              {"type": "text", "text": ask() if text is None else text}, {"type": "text", "text": "Keep the house style."}]
    messages = list(opening) + ([before] if before is not None else []) + [{"role": "user", "content": blocks}]
    return {"model": "claude-invented", "stream": stream, "system": [{"type": "text", "text": "You are a sub-agent."}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}], "messages": messages + list(trailing)}


def answered_label(out):
    return "".join(e["data"]["delta"]["text"] for e in out.events if e["event"] == "content_block_delta")


LABELS = (
    ("Read", {"file_path": "/work/src/app/server.py"}, "Reading server.py"),
    ("Edit", {"file_path": "/work/src/notes.md", "old_string": "a", "new_string": "b"}, "Editing notes.md"),
    ("MultiEdit", {"file_path": "lib/store.go", "edits": []}, "Editing store.go"),
    ("NotebookEdit", {"notebook_path": "/work/analysis.ipynb"}, "Editing analysis.ipynb"),
    ("Write", {"file_path": "/work/out/report.txt"}, "Writing report.txt"),
    ("Grep", {"pattern": "def handle_any"}, "Searching for def handle_any"),
    ("Glob", {"pattern": "**/*.py"}, "Listing **/*.py"),
    ("Bash", {"command": "cd /work/repo && git -C sub status --short"}, "Running git status"),
    ("Bash", {"command": "FOO=1 BAR='a b' sudo env time python3 tools/run_checks.py --all"}, "Running run_checks.py"),
    ("Bash", {"command": "cd /work; sh scripts/gate.sh"}, "Running gate.sh"),
    ("Bash", {"command": "R=/tmp/x; python3 - <<'EOF'\nprint(1)\nEOF"}, "Running python3"),
    ("Bash", {"command": "/usr/bin/python3 -I -B -m unittest"}, "Running python3"),
    ("Bash", {"command": "make -C engine prompts"}, "Running make prompts"),
    ("Bash", {"command": "go test ./..."}, "Running go test"),
    ("Bash", {"command": "git -c core.pager=cat log --oneline"}, "Running git log"),
    ("Bash", {"command": "npm --prefix web run build"}, "Running npm run"),
    ("Bash", {"command": "kubectl -n jobs get pods"}, "Running kubectl get"),
    ("Bash", {"command": "make -n test"}, "Running make test"),
    ("Bash", {"command": "grep -n 'x' file.txt | head"}, "Running grep"),
    ("Bash", {"command": "/opt/tools/bin/rg -n pattern"}, "Running rg"),
    ("Bash", {"command": "echo 'unbalanced"}, "Running echo"),
    ("Bash", {"command": "git"}, "Running git"),
    ("Bash", {"command": "( cd sub && make build )"}, "Running make build"),
    ("Bash", {"command": "   "}, "Using Bash"),
    ("WebFetch", {"url": "https://docs.example.org/guide?x=1"}, "Fetching docs.example.org"),
    ("WebFetch", {"url": "http://[bad"}, "Using WebFetch"),
    ("WebSearch", {"query": "anything"}, "Searching the web"),
    ("Agent", {"subagent_type": "tracer", "prompt": "p"}, "Delegating to tracer"),
    ("Task", {"prompt": "p"}, "Delegating to an agent"),
    ("TodoWrite", {"todos": []}, "Updating the task list"),
    ("ToolSearch", {"query": "select:x"}, "Loading tool definitions"),
    ("Skill", {"skill": "quality:prompt"}, "Loading quality:prompt skill"),
    ("SendMessage", {"to": "main", "message": "m"}, "Messaging main"),
    ("mcp__docs-server__fetch_page", {"id": 1}, "Calling fetch_page"),
    ("LSP", {}, "Using LSP"),
    ("Read", {}, "Using Read"),
    ("Grep", {"pattern": "one two  three\nfour five six"}, "Searching for one two three"),
    ("Read", {"file_path": "/w/" + "a" * 60 + ".py"}, "Reading " + "a" * 40),
)


class LabelTest(unittest.TestCase):
    def test_the_label_comes_from_the_last_tool_call_before_the_fork(self):
        for name, args, expected in LABELS:
            with self.subTest(tool=name, args=args):
                out = run(MODULE, body=fork(call(name, args)), headers=SUB, response=RECORDED)
                self.assertEqual(answered_label(out), expected)

    def test_the_message_before_the_fork_decides_when_it_holds_no_tool_call(self):
        two_calls = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "first.py"}},
            {"type": "tool_use", "id": "b", "name": "Write", "input": {"file_path": "second.py"}}]}
        cases = (
            ("the last of two calls", fork(two_calls), "Writing second.py"),
            ("a call after thinking", fork(call("Read", {"file_path": "x.py"}, thinking=True)), "Reading x.py"),
            ("text only", fork({"role": "assistant", "content": [{"type": "text", "text": "Thinking aloud."}]}),
             "Planning the next step"),
            ("string content", fork({"role": "assistant", "content": "Plain words."}), "Planning the next step"),
            ("no assistant message at all", fork(None), "Starting the task"),
            ("a user message before, an assistant earlier",
             fork({"role": "user", "content": "More."},
                  opening=({"role": "user", "content": "Go."}, call("Read", {"file_path": "x.py"}))),
             "Planning the next step"),
        )
        for why, body, expected in cases:
            with self.subTest(why):
                self.assertEqual(answered_label(run(MODULE, body=body, headers=SUB, response=RECORDED)), expected)


class DetectionTest(unittest.TestCase):
    def test_the_fork_is_answered_with_or_without_previous_and_past_trailing_notices(self):
        notices = ({"role": "system", "content": "A background task finished."},
                   {"role": "system", "content": [], "output_config": {"effort": "high"}})
        for why, body, path in (
                ("first label", fork(call("Read", {"file_path": "a.py"})), "/v1/messages"),
                ("previous label", fork(call("Read", {"file_path": "a.py"}), ask('Reading "a.py" now')), "/v1/messages"),
                ("trailing notices", fork(call("Read", {"file_path": "a.py"}), trailing=notices), "/v1/messages"),
                ("beta query", fork(call("Read", {"file_path": "a.py"})), "/v1/messages?beta=true")):
            with self.subTest(why):
                out = run(MODULE, body=body, headers=SUB, response=RECORDED, url_path=path)
                self.assertEqual(out.note, " kind=sub answered=status-label")
                self.assertEqual(answered_label(out), "Reading a.py")
                self.assertIsNone(out.forwarded)

    def test_every_other_request_passes_untouched(self):
        read = call("Read", {"file_path": "a.py"})
        in_result = fork(read, text="Keep going.")
        in_result["messages"][-1]["content"][0]["content"] = ask()
        answered = fork(read)
        answered["messages"].append({"role": "assistant", "content": "Reading a.py"})
        for why, body, headers, method, path in (
                ("main session", fork(read), {}, "POST", "/v1/messages"),
                ("not a POST", fork(read), SUB, "GET", "/v1/messages"),
                ("count tokens", fork(read), SUB, "POST", "/v1/messages/count_tokens"),
                ("reworded ask", fork(read, ask().replace("3-5 words", "3-6 words")), SUB, "POST", "/v1/messages"),
                ("ask with text after it", fork(read, ask() + "\nAlso say why."), SUB, "POST", "/v1/messages"),
                ("previous without its dash", fork(read, ASK + 'Previous: "x" - say something NEW.\n\n' + EXAMPLES),
                 SUB, "POST", "/v1/messages"),
                ("ask inside a tool result", in_result, SUB, "POST", "/v1/messages"),
                ("ask in an earlier turn", answered, SUB, "POST", "/v1/messages"),
                ("no JSON body", b"not json", SUB, "POST", "/v1/messages")):
            with self.subTest(why):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                out = run(MODULE, body=raw, headers=headers, response=RECORDED, method=method, url_path=path)
                self.assertIs(out.forwarded, raw)
                self.assertIsNone(out.answer)
                self.assertEqual(out.events, run(MODULE, body=b"", response=RECORDED).events)
                self.assertEqual(out.errors, ())


class AnswerTest(unittest.TestCase):
    def test_a_stream_is_an_anthropic_message_stream(self):
        out = run(MODULE, body=fork(call("Bash", {"command": "go test ./..."})), headers=SUB, response=RECORDED,
                  request_id=7)
        self.assertEqual(out.note, " kind=sub answered=status-label")
        self.assertEqual((out.answer.status, out.answer.content_type), (200, "text/event-stream"))
        self.assertEqual([e["event"] for e in out.events],
                         ["message_start", "content_block_start", "content_block_delta", "content_block_stop",
                          "message_delta", "message_stop"])
        self.assertEqual([e["data"]["type"] for e in out.events], [e["event"] for e in out.events])
        start = out.events[0]["data"]["message"]
        self.assertEqual((start["id"], start["model"], start["role"], start["content"]),
                         ("msg_local_7", "claude-invented", "assistant", []))
        self.assertEqual(set(start["usage"].values()), {0})
        self.assertEqual(out.events[1]["data"]["content_block"], {"type": "text", "text": ""})
        self.assertEqual(out.events[2]["data"]["delta"], {"type": "text_delta", "text": "Running go test"})
        self.assertEqual(out.events[4]["data"]["delta"]["stop_reason"], "end_turn")
        self.assertEqual(out.events[4]["data"]["usage"], {"output_tokens": 3})
        self.assertEqual((out.log, out.row["answered_by"]), ((), "status-label"))
        self.assertEqual((out.result.status, out.result.completed), (200, True))

    def test_without_stream_it_is_one_json_message(self):
        out = run(MODULE, body=fork(call("Read", {"file_path": "/w/a.py"}), stream=False), headers=SUB, request_id=9)
        self.assertIsNone(out.forwarded)
        self.assertEqual(out.answer.content_type, "application/json")
        message = json.loads(out.answer.data)
        self.assertEqual({k: message[k] for k in ("id", "type", "role", "model", "content", "stop_reason")},
                         {"id": "msg_local_9", "type": "message", "role": "assistant", "model": "claude-invented",
                          "content": [{"type": "text", "text": "Reading a.py"}], "stop_reason": "end_turn"})
        self.assertEqual(message["usage"]["output_tokens"], 2)
        self.assertEqual(out.events, ({"event": None, "data": message, "raw": out.answer.data},))


if __name__ == "__main__":
    unittest.main()
