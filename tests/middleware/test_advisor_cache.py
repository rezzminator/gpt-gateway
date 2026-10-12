"""Tests for middleware/advisor_cache.py: the advisor tool's caching and output cap, the model and agent scoping that
strips the advisor from a request, and the advisor rows of a response. The advisor section below has Claude Code's
shape and last line; every conversation around it is invented."""
import json
import os
import unittest
from unittest import mock

from gpt_gateway.harness import load, run
from gpt_gateway.settings import SettingsError
from tests import jail

MODULE = os.path.join(jail.ROOT, "middleware", "advisor_cache.py")
ADVISOR = {"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5"}
BASH = {"name": "Bash", "input_schema": {"type": "object"}}
SUB = {"x-claude-code-agent-id": "agent-invented"}
SECTION = ("# Advisor Tool\n\nYou have an `advisor` tool: call it before substantive work.\n\n"
           "Weigh its advice; a reconcile call is cheaper than committing to the wrong branch.")
RECORDED = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
CACHING_5M = {"type": "ephemeral", "ttl": "5m"}


def request(tools=(ADVISOR, BASH), model="m", **extra):
    body = {"model": model, "messages": [{"role": "user", "content": "Hello."}], "tools": [dict(t) for t in tools]}
    body.update(extra)
    return body


def raw_of(body):
    return json.dumps(body).encode()


def go(body, headers=None, settings=None, **kwargs):
    return run(MODULE, body=raw_of(body) if isinstance(body, dict) else body, headers=headers, settings=settings,
               response=kwargs.pop("response", RECORDED), **kwargs)


def sent(out):
    return json.loads(out.forwarded)


def advisor_dressed():
    """A request carrying the advisor everywhere Claude Code puts it: its tool, its instructions in a system-role
    message and in a system-reminder, and its deferred-tool announcement."""
    return request(
        tools=(ADVISOR, BASH),
        system=[{"type": "text", "text": "You are a coding agent."}],
        messages=[
            {"role": "system", "content": [{"type": "text", "text": "Environment.\n\n" + SECTION},
                                           {"type": "text", "text": "Project notes."}]},
            {"role": "user", "content": [
                {"type": "text", "text": "<system-reminder>\n" + SECTION + "\n</system-reminder>\n"},
                {"type": "text", "text": "Fix the bug."}]},
            {"role": "user", "content": [
                {"type": "tool_addition", "tool": {"name": "advisor", "definition": {"name": "advisor"}}},
                {"type": "text", "text": "Go on."}]}])


def advisor_stripped(model):
    body = advisor_dressed()
    body["model"] = model
    body["tools"] = [BASH]
    body["messages"][0]["content"][0]["text"] = "Environment."
    body["messages"][1]["content"] = [{"type": "text", "text": "Fix the bug."}]
    body["messages"][2]["content"] = [{"type": "text", "text": "Go on."}]
    return body


def system_message(text):
    return {"role": "system", "content": [{"type": "text", "text": text}]}


def refusal(settings=None, env=None):
    """The reason the gateway's start refuses these settings, or None when it takes them."""
    try:
        load(MODULE, settings, env)
    except SettingsError as e:
        return str(e)
    return None


def iterations(*rows):
    """A message_delta event carrying advisor iterations (model, input, cache read, cache write, output)."""
    its = [{"type": "message", "model": "claude-main", "input_tokens": 1, "output_tokens": 1}]
    its += [{"type": "advisor_message", "model": m, "input_tokens": i, "cache_read_input_tokens": r,
             "cache_creation_input_tokens": w, "output_tokens": o} for m, i, r, w, o in rows]
    return ("event: message_delta\ndata: %s\n\n" % json.dumps(
        {"type": "message_delta", "usage": {"iterations": its}})).encode()


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        _, values = load(MODULE)
        self.assertEqual(dict(values), {"ttl": "5m", "subagent_ttl": "5m", "max_tokens": None, "advisor_models": None,
                                        "advisor_agents": None, "agent_dirs": None, "debug": False})

    def test_the_file_sets_each_key_and_an_env_variable_wins(self):
        agents = jail.scratch(self)
        file_values = {"ttl": "1h", "subagent_ttl": "off", "max_tokens": 2048, "advisor_models": ["claude-opus"],
                       "advisor_agents": None, "agent_dirs": [agents], "debug": True}
        self.assertEqual(dict(load(MODULE, file_values)[1]), dict(file_values, advisor_models=("claude-opus",),
                                                                  agent_dirs=(agents,)))
        env = {"GPT_GATEWAY_ADVISOR_CACHE_TTL": "off", "GPT_GATEWAY_ADVISOR_CACHE_TTL_SUBAGENT": "1h",
               "GPT_GATEWAY_ADVISOR_MAX_TOKENS": "4096", "GPT_GATEWAY_ADVISOR_MODELS": "claude-a, claude-b",
               "GPT_GATEWAY_ADVISOR_AGENTS": "", "GPT_GATEWAY_ADVISOR_AGENT_DIRS": "/one:/two",
               "GPT_GATEWAY_ADVISOR_CACHE_DEBUG": "off"}
        self.assertEqual(dict(load(MODULE, file_values, env)[1]),
                         {"ttl": "off", "subagent_ttl": "1h", "max_tokens": 4096,
                          "advisor_models": ("claude-a", "claude-b"), "advisor_agents": None,
                          "agent_dirs": ("/one", "/two"), "debug": False})

    def test_null_clears_a_cap_and_a_scope(self):
        values = dict(load(MODULE, {"max_tokens": None, "advisor_models": None, "advisor_agents": None})[1])
        self.assertEqual((values["max_tokens"], values["advisor_models"], values["advisor_agents"]), (None,) * 3)
        self.assertEqual(dict(load(MODULE, {"advisor_models": []})[1])["advisor_models"], ())

    def test_a_value_outside_the_accepted_forms_is_refused_naming_key_and_source(self):
        for settings, env, fragment in (
                ({"ttl": "2h"}, {}, 'ttl from file harness: ttl must be one of 1h, 5m, off, got "2h"'),
                ({"ttl": 5}, {}, "ttl must be one of 1h, 5m, off, got 5"),
                ({"subagent_ttl": None}, {}, "subagent_ttl must be one of 1h, 5m, off, got null"),
                ({}, {"GPT_GATEWAY_ADVISOR_CACHE_TTL": "10m"}, "ttl from env GPT_GATEWAY_ADVISOR_CACHE_TTL"),
                ({"max_tokens": 1023}, {}, "max_tokens must be an integer >= 1024 or null, got 1023"),
                ({"max_tokens": 2048.5}, {}, "max_tokens must be an integer >= 1024 or null, got 2048.5"),
                ({"max_tokens": True}, {}, "max_tokens must be an integer >= 1024 or null, got true"),
                ({}, {"GPT_GATEWAY_ADVISOR_MAX_TOKENS": "20x8"}, "max_tokens from env GPT_GATEWAY_ADVISOR_MAX_TOKENS"),
                ({}, {"GPT_GATEWAY_ADVISOR_MAX_TOKENS": "-2048"}, "max_tokens must be an integer >= 1024"),
                ({"advisor_models": ["ok", ""]}, {}, "advisor_models must be a list of non-empty strings or null"),
                ({"advisor_models": "claude"}, {}, None),
                ({"advisor_agents": [3]}, {}, "advisor_agents must be a list of non-empty strings or null, got [3]"),
                ({}, {"GPT_GATEWAY_ADVISOR_AGENTS": "a,,b"}, "advisor_agents from env GPT_GATEWAY_ADVISOR_AGENTS"),
                ({"agent_dirs": ["relative/dir"]}, {}, "agent_dirs must be a list of absolute directories or null"),
                ({"debug": "maybe"}, {}, "debug must be true or false"),
                ({"debug": 1}, {}, "debug must be true or false"),
                ({"unknown_key": 1}, {}, "unknown setting(s) unknown_key")):
            with self.subTest(settings=settings, env=env):
                if fragment is None:  # a bare string is the env form: split at commas, accepted from the file too
                    self.assertEqual(load(MODULE, settings)[1]["advisor_models"], ("claude",))
                    continue
                with self.assertRaises(SettingsError) as caught:
                    load(MODULE, settings, env)
                self.assertIn(fragment, str(caught.exception))

    def test_the_env_forms_of_a_flag(self):
        for word, expected in (("1", True), ("true", True), ("YES", True), ("on", True),
                               ("0", False), ("false", False), ("No", False), ("off", False)):
            with self.subTest(word):
                env = {"GPT_GATEWAY_ADVISOR_CACHE_DEBUG": word}
                self.assertIsNone(refusal(env=env))
                self.assertIs(load(MODULE, env=env)[1]["debug"], expected)


class CheckTest(unittest.TestCase):
    """`advisor_agents` entries are looked up verbatim in the agent files, so a typo stops the start."""

    def agents(self, files):
        root = jail.scratch(self)
        for name, text in files.items():
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        return root

    def test_an_entry_found_in_any_agent_file_passes(self):
        root = self.agents({"a.md": "You are the Reviewer role.", "deep/b.md": "The Planner role.", "c.txt": "Ignored."})
        self.assertIsNone(refusal({"advisor_agents": ["Reviewer role", "Planner role"], "agent_dirs": [root]}))

    def test_an_entry_in_no_agent_file_is_refused_with_where_it_looked(self):
        root = self.agents({"a.md": "You are the Reviewer role.", "c.txt": "Missing role."})
        missing = os.path.join(root, "absent")
        with self.assertRaises(SettingsError) as caught:
            load(MODULE, {"advisor_agents": ["Reviewer role", "Missing role"], "agent_dirs": [root, missing]})
        message = str(caught.exception)
        self.assertIn("check failed: advisor_agents: no agent *.md under", message)
        self.assertIn("%s (1 files); %s (missing) contains \"Missing role\"" % (root, missing), message)
        self.assertNotIn('"Reviewer role"', message)

    def test_no_scope_and_an_empty_scope_read_no_directory(self):
        gone = os.path.join(jail.scratch(self), "absent")
        for scope in (None, []):
            with self.subTest(scope=scope):
                self.assertEqual(load(MODULE, {"advisor_agents": scope, "agent_dirs": [gone]})[1]["agent_dirs"], (gone,))

    def test_a_symlinked_file_and_directory_are_followed_and_a_cycle_ends(self):
        root = self.agents({"own/own.md": "Own role."})
        outside = self.agents({"y.md": "File role.", "sub/z.md": "Directory role."})
        os.symlink(os.path.join(outside, "y.md"), os.path.join(root, "own", "linked.md"))
        os.symlink(os.path.join(outside, "sub"), os.path.join(root, "own", "linked-dir"))
        os.symlink(root, os.path.join(outside, "sub", "back"))  # a cycle: root -> own -> linked-dir -> back -> root
        for entry in ("Own role", "File role", "Directory role"):
            with self.subTest(entry):
                self.assertIsNone(refusal({"advisor_agents": [entry], "agent_dirs": [root]}))
        self.assertIn('contains "Nowhere role"', refusal({"advisor_agents": ["Nowhere role"], "agent_dirs": [root]}))

    def test_the_default_directory_is_the_home_agents_directory(self):
        home = self.agents({".claude/agents/r.md": "Home role."})
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertIsNone(refusal({"advisor_agents": ["Home role"]}))
            reason = refusal({"advisor_agents": ["Other role"]})
        self.assertIn("%s (1 files)" % os.path.join(home, ".claude", "agents"), reason)

    def test_an_unreadable_agent_file_is_refused_naming_the_directory(self):
        root = self.agents({"a.md": "Role."})
        os.symlink(os.path.join(root, "gone.md"), os.path.join(root, "broken.md"))
        os.mkdir(os.path.join(root, "locked"))
        os.chmod(os.path.join(root, "locked"), 0)
        self.addCleanup(os.chmod, os.path.join(root, "locked"), 0o700)
        with self.assertRaises(SettingsError) as caught:
            load(MODULE, {"advisor_agents": ["Role"], "agent_dirs": [root]})
        self.assertIn("advisor_agents: cannot read the agents under %s" % root, str(caught.exception))


class CachingTest(unittest.TestCase):
    def test_the_advisor_tool_gets_caching_at_the_ttl_of_its_kind(self):
        for why, headers, settings, kind, ttl in (
                ("main, default", None, {}, "main", "5m"),
                ("main, ttl", None, {"ttl": "1h", "subagent_ttl": "off"}, "main", "1h"),
                ("sub, default", SUB, {}, "sub", "5m"),
                ("sub, subagent_ttl", SUB, {"ttl": "5m", "subagent_ttl": "1h"}, "sub", "1h"),
                ("sub, not the main ttl", SUB, {"ttl": "1h", "subagent_ttl": "5m"}, "sub", "5m")):
            with self.subTest(why):
                out = go(request(), headers, settings)
                tools = sent(out)["tools"]
                self.assertEqual(tools[0], dict(ADVISOR, caching={"type": "ephemeral", "ttl": ttl}))
                self.assertEqual(tools[1], BASH)
                self.assertEqual(sent(out)["messages"], request()["messages"])
                self.assertEqual(out.note, " kind=%s model=m advisor-caching=%s" % (kind, ttl))
                self.assertEqual((out.changed, out.errors), (True, ()))

    def test_off_leaves_that_kind_byte_for_byte(self):
        raw = raw_of(request())
        for headers, settings in ((None, {"ttl": "off"}), (SUB, {"subagent_ttl": "off"})):
            with self.subTest(headers=headers):
                out = go(raw, headers, settings)
                self.assertIs(out.forwarded, raw)
                self.assertFalse(out.changed)
                self.assertNotIn("advisor-caching", out.note)
        self.assertTrue(go(raw, SUB, {"ttl": "off"}).changed)

    def test_max_tokens_is_added_when_set_and_kept_when_the_request_has_one(self):
        out = go(request(), settings={"max_tokens": 2048})
        self.assertEqual(sent(out)["tools"][0], dict(ADVISOR, caching=CACHING_5M, max_tokens=2048))
        self.assertEqual(out.note, " kind=main model=m advisor-caching=5m advisor-max_tokens=2048")
        raw = raw_of(request(tools=(dict(ADVISOR, max_tokens=4096), BASH)))
        out = go(raw, settings={"max_tokens": 2048, "ttl": "off"})
        self.assertIs(out.forwarded, raw)
        self.assertNotIn("advisor-max_tokens", out.note)
        out = go(request(), settings={"ttl": "off", "max_tokens": 2048})
        self.assertEqual(sent(out)["tools"][0], dict(ADVISOR, max_tokens=2048))
        self.assertEqual(out.note, " kind=main model=m advisor-max_tokens=2048")

    def test_a_caching_the_request_carries_is_kept_and_logged_with_its_ttl(self):
        for caching, shown in (({"type": "ephemeral", "ttl": "1h"}, "1h"), ({"type": "ephemeral"}, "5m"),
                               ({"type": "ephemeral", "ttl": "5m"}, "5m"), (None, "5m")):
            with self.subTest(caching=caching):
                raw = raw_of(request(tools=(dict(ADVISOR, caching=caching), BASH)))
                out = go(raw, settings={"ttl": "1h" if shown == "5m" else "5m"})
                self.assertIs(out.forwarded, raw)
                self.assertEqual(out.note, " kind=main model=m advisor-caching-kept=%s" % shown)

    def test_a_request_with_tools_but_no_advisor_is_marked_and_untouched(self):
        raw = raw_of(request(tools=(BASH,)))
        out = go(raw, settings={"max_tokens": 2048})
        self.assertIs(out.forwarded, raw)
        self.assertEqual(out.note, " kind=main model=m advisor-tool=none")
        raw = raw_of({"model": "m", "messages": []})
        out = go(raw)
        self.assertIs(out.forwarded, raw)
        self.assertEqual(out.note, " kind=main model=m")

    def test_every_request_that_is_no_messages_post_with_an_object_body_passes_untouched(self):
        body = raw_of(request())
        for why, raw, method, path in (
                ("not a POST", body, "GET", "/v1/messages"),
                ("count tokens", body, "POST", "/v1/messages/count_tokens"),
                ("other endpoint", body, "POST", "/v1/models"),
                ("not JSON", b"not json", "POST", "/v1/messages"),
                ("a JSON list", b"[1, 2]", "POST", "/v1/messages"),
                ("empty", b"", "POST", "/v1/messages")):
            with self.subTest(why):
                out = go(raw, method=method, url_path=path)
                self.assertIs(out.forwarded, raw)
                self.assertEqual((out.note, out.errors, out.log), (" kind=main", (), ()))
        out = go(body, url_path="/v1/messages?beta=true")
        self.assertTrue(out.changed)
        self.assertEqual(sent(out)["tools"][0]["caching"], CACHING_5M)


class ScopeTest(unittest.TestCase):
    def test_a_model_outside_advisor_models_loses_every_trace_of_the_advisor(self):
        out = go(advisor_dressed() | {"model": "gpt-invented"}, settings={"advisor_models": ["claude-"]})
        self.assertEqual(sent(out), advisor_stripped("gpt-invented"))
        self.assertEqual(out.note, " kind=main model=gpt-invented advisor-filtered model=gpt-invented"
                                   " stripped=tool,tool_reference,section")
        self.assertEqual((out.changed, out.errors, out.log), (True, (), ()))

    def test_a_model_matches_by_its_id_or_by_a_prefix_of_it(self):
        for why, model, models, kept in (
                ("every model", "gpt-invented", None, True),
                ("the exact id", "claude-opus-5-5", ["claude-opus-5-5"], True),
                ("a dated snapshot", "claude-opus-5-5-20261001", ["claude-opus-5-5"], True),
                ("one of two", "claude-sonnet-5-5", ["claude-opus-5-5", "claude-sonnet-5"], True),
                ("a shorter id", "claude-opus-5", ["claude-opus-5-5"], False),
                ("a longer entry", "claude-opus-5-5", ["claude-opus-5-5-20261001"], False),
                ("no model", "claude-opus-5-5", [], False)):
            with self.subTest(why):
                out = go(advisor_dressed() | {"model": model}, settings={"advisor_models": models})
                self.assertEqual("advisor-caching=5m" in out.note, kept)
                self.assertEqual(sent(out) == advisor_stripped(model), not kept)

    def test_a_request_without_a_model_is_outside_a_model_scope(self):
        body = advisor_dressed()
        del body["model"]
        self.assertIn("advisor-filtered model=None", go(body, settings={"advisor_models": ["claude"]}).note)

    def agents(self, *texts):
        root = jail.scratch(self)
        for i, text in enumerate(texts):
            with open(os.path.join(root, "agent%d.md" % i), "w") as f:
                f.write(text)
        return {"advisor_agents": list(texts), "agent_dirs": [root]}

    def test_a_sub_agent_keeps_the_advisor_only_when_its_system_text_names_an_entry(self):
        scope = self.agents("You are the reviewer.", "You are the planner.")
        reviewer = dict(advisor_dressed(), system=[{"type": "text", "text": "Context.\nYou are the reviewer."}])
        planner = dict(advisor_dressed(), system="You are the planner. Be brief.")
        other = dict(advisor_dressed(), system=[{"type": "text", "text": "You are the explorer."}])
        for why, body, expected in (("reviewer, a block", reviewer, True), ("planner, a string", planner, True),
                                    ("another agent", other, False)):
            with self.subTest(why):
                out = go(body, SUB, scope)
                if expected:
                    self.assertEqual(sent(out)["tools"][0], dict(ADVISOR, caching=CACHING_5M))
                    self.assertEqual(out.note, " kind=sub model=m advisor-agent=kept advisor-caching=5m")
                else:
                    self.assertEqual(sent(out), advisor_stripped("m") | {"system": other["system"]})
                    self.assertEqual(out.note, " kind=sub model=m advisor-filtered agent"
                                               " stripped=tool,tool_reference,section")

    def test_the_agent_scope_leaves_the_main_session_alone(self):
        out = go(advisor_dressed(), settings=self.agents("You are the reviewer."))
        self.assertEqual(sent(out)["tools"][0], dict(ADVISOR, caching=CACHING_5M))
        self.assertEqual(out.note, " kind=main model=m advisor-caching=5m")

    def test_an_empty_agent_scope_strips_every_sub_agent(self):
        out = go(advisor_dressed(), SUB, {"advisor_agents": []})
        self.assertEqual(sent(out), advisor_stripped("m"))

    def test_the_model_scope_decides_before_the_agent_scope(self):
        scope = self.agents("You are the reviewer.")
        body = dict(advisor_dressed(), model="gpt-invented", system="You are the reviewer.")
        out = go(body, SUB, dict(scope, advisor_models=["claude-"]))
        self.assertIn(" advisor-filtered model=gpt-invented stripped=", out.note)
        self.assertNotIn("advisor-agent=kept", out.note)

    def test_the_stripped_request_is_a_pure_function_of_the_request(self):
        first = go(advisor_dressed(), SUB, {"advisor_models": ["nothing"]})
        again = go(advisor_dressed(), SUB, {"advisor_models": ["nothing"]})
        self.assertEqual(first.forwarded, again.forwarded)
        self.assertEqual(first.note, again.note)

    def test_a_conversation_that_used_the_advisor_is_left_whole(self):
        used = advisor_dressed()
        used["messages"].append({"role": "assistant", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "advisor", "input": {}}]})
        out = go(used, settings={"advisor_models": ["nothing"]})
        self.assertEqual(sent(out)["tools"][0], dict(ADVISOR, caching=CACHING_5M))
        self.assertEqual(sent(out)["messages"], used["messages"])
        self.assertEqual(out.note, " kind=main model=m advisor-filter-skipped: advisor in history advisor-caching=5m")
        used["messages"][-1]["content"] = [{"type": "advisor_tool_result", "tool_use_id": "srvtoolu_1"}]
        self.assertIn("advisor in history", go(used, settings={"advisor_models": ["nothing"]}).note)

    def test_a_tool_the_request_does_not_carry_leaves_nothing_to_strip(self):
        raw = raw_of(request(tools=(BASH,)))
        out = go(raw, settings={"advisor_models": ["nothing"]})
        self.assertIs(out.forwarded, raw)
        self.assertEqual(out.note, " kind=main model=m advisor-tool=none")


class StripTest(unittest.TestCase):
    """What the strip removes and what it warns about, section by section."""

    def strip(self, body):
        return go(body, settings={"advisor_models": ["nothing"]})

    def test_only_the_section_and_the_blank_line_that_joins_it_go(self):
        for why, text, left in (
                ("tail of a block", "Before.\n\n" + SECTION, "Before."),
                ("head of a block", SECTION + "\n\n# Next\n\nText.", "# Next\n\nText."),
                ("between headings", "Before.\n\n" + SECTION + "\n\n# Next\n\nText.", "Before.\n\n# Next\n\nText."),
                ("inside a reminder", "<system-reminder>\nNote.\n\n" + SECTION + "\n</system-reminder>\n",
                 "<system-reminder>\nNote.\n</system-reminder>\n")):
            with self.subTest(why):
                out = self.strip(request(messages=[system_message(text)]))
                self.assertEqual(sent(out)["messages"], [system_message(left)])
                self.assertEqual(out.log, ())

    def test_text_after_the_section_in_its_block_goes_with_it_and_warns(self):
        out = self.strip(request(messages=[system_message("Before.\n\n" + SECTION + "\n\nAfter the section.")]))
        self.assertEqual(sent(out)["messages"], [system_message("Before.")])
        self.assertTrue(out.note.endswith(" warnings=section-end-unexpected"), out.note)

    def test_a_system_message_with_string_content_is_stripped_and_an_empty_one_goes(self):
        body = request(messages=[{"role": "system", "content": "Before.\n\n" + SECTION},
                                 {"role": "system", "content": SECTION}, {"role": "user", "content": "Hi."}])
        self.assertEqual(sent(self.strip(body))["messages"],
                         [{"role": "system", "content": "Before."}, {"role": "user", "content": "Hi."}])

    def test_a_user_text_that_only_mentions_the_heading_stays(self):
        body = request(messages=[{"role": "user", "content": "Explain:\n\n" + SECTION},
                                 {"role": "user", "content": [{"type": "text", "text": "x\n" + SECTION}]}])
        out = self.strip(body)
        self.assertEqual(sent(out)["messages"], body["messages"])
        self.assertIn("warnings=section-not-found", out.note)

    def test_a_missing_section_and_a_reworded_one_each_warn_in_the_note_and_the_log(self):
        for why, system, warning in (
                ("no section", "Only this.", "section-not-found"),
                ("reworded end", "A.\n\n# Advisor Tool\n\nNew wording.", "section-end-unexpected")):
            with self.subTest(why):
                out = self.strip(request(messages=[system_message(system)]))
                self.assertIn(" advisor-filtered model=m stripped=%s" % ("tool" if "not-found" in warning
                                                                          else "tool,section"), out.note)
                self.assertTrue(out.note.endswith(" warnings=" + warning), out.note)
                self.assertEqual(len(out.log), 1)
                line = out.log[0]
                self.assertTrue(line.startswith("advisor-filter-warning: %s in m;" % warning), line)
                self.assertTrue(line.endswith("request dir=- (capture off)"), line)

    def test_a_dropped_block_hands_its_cache_breakpoint_to_the_block_before(self):
        addition = {"type": "tool_addition", "tool": {"name": "advisor"}, "cache_control": {"type": "ephemeral"}}
        keep = {"type": "text", "text": "Keep."}
        handed = self.strip(request(messages=[{"role": "user", "content": [dict(keep), dict(addition)]}]))
        self.assertEqual(sent(handed)["messages"][0]["content"], [dict(keep, cache_control={"type": "ephemeral"})])
        self.assertNotIn("cache_control-dropped", handed.note)
        lost = self.strip(request(messages=[{"role": "user", "content": [dict(addition), dict(keep)]}]))
        self.assertEqual(sent(lost)["messages"][0]["content"], [keep])
        self.assertIn("cache_control-dropped", lost.note)

    def test_a_message_that_only_held_advisor_blocks_goes_and_is_reported(self):
        body = request(messages=[{"role": "user", "content": [{"type": "tool_addition", "tool": {"name": "advisor"}}]},
                                 {"role": "user", "content": []}, {"role": "user", "content": "Hi."}])
        out = self.strip(body)
        self.assertEqual(sent(out)["messages"], [{"role": "user", "content": []}, {"role": "user", "content": "Hi."}])
        self.assertIn("message-emptied", out.note)

    def test_a_deferred_tool_that_is_not_the_advisor_stays(self):
        other = {"type": "tool_addition", "tool": {"name": "other", "definition": {"name": "other", "type": "custom"}}}
        out = self.strip(request(messages=[{"role": "user", "content": [other]}]))
        self.assertEqual(sent(out)["messages"], [{"role": "user", "content": [other]}])


class ResponseTest(unittest.TestCase):
    def test_each_advisor_iteration_of_a_response_is_one_log_line(self):
        out = go(request(), response=iterations(("claude-opus-5-5", 11, 0, 9000, 300), ("claude-opus-5-5", 12, 9000, 40, 250)))
        self.assertEqual(out.log, (
            "advisor claude-opus-5-5 kind=main ttl=5m in=11 cache_read=0 cache_write=9000 out=300",
            "advisor claude-opus-5-5 kind=main ttl=5m in=12 cache_read=9000 cache_write=40 out=250"))
        self.assertEqual(out.errors, ())

    def test_a_line_names_the_kind_and_the_ttl_the_request_ran_at(self):
        row = iterations(("claude-opus-5-5", 1, 2, 3, 4))
        for why, headers, settings, body, ttl in (
                ("sub at its own ttl", SUB, {"subagent_ttl": "1h"}, request(), "1h"),
                ("caching off", None, {"ttl": "off"}, request(), "-"),
                ("caching the caller set", None, {}, request(tools=(dict(ADVISOR, caching={"ttl": "1h"}), BASH)), "1h")):
            with self.subTest(why):
                out = go(body, headers, settings, response=row)
                self.assertEqual(len(out.log), 1)
                self.assertEqual(out.log[0], "advisor claude-opus-5-5 kind=%s ttl=%s in=1 cache_read=2 cache_write=3 out=4" % (
                    "sub" if headers else "main", ttl))

    def test_the_last_event_with_advisor_iterations_is_the_one_read(self):
        stream = iterations(("claude-opus-5-5", 1, 1, 1, 1)) + iterations(("claude-opus-5-5", 2, 2, 2, 2)) + RECORDED
        self.assertEqual(go(request(), response=stream).log,
                         ("advisor claude-opus-5-5 kind=main ttl=5m in=2 cache_read=2 cache_write=2 out=2",))

    def test_a_json_response_is_read_the_same(self):
        usage = {"usage": {"iterations": [{"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 5,
                                           "cache_read_input_tokens": 6, "cache_creation_input_tokens": 7,
                                           "output_tokens": 8}]}}
        self.assertEqual(go(request(), response=usage).log,
                         ("advisor claude-opus-5-5 kind=main ttl=5m in=5 cache_read=6 cache_write=7 out=8",))

    def test_a_response_without_advisor_iterations_or_to_another_endpoint_logs_nothing(self):
        plain = ('event: message_delta\ndata: {"type":"message_delta","usage":{"iterations":'
                 '[{"type":"message","input_tokens":3,"output_tokens":4}]}}\n\n').encode()
        self.assertEqual(go(request(), response=plain).log, ())
        self.assertEqual(go(request(), response=iterations(("claude-opus-5-5", 1, 1, 1, 1)),
                            url_path="/v1/messages/count_tokens").log, ())

    def test_debug_logs_the_thinking_of_a_request_that_carries_the_advisor(self):
        body = request(context_management={"edits": []}, thinking={"type": "adaptive"})
        on = go(body, settings={"debug": True})
        self.assertEqual(on.log, ('debug context_management={"edits": []} thinking={"type": "adaptive"}',))
        self.assertEqual(go(body).log, ())
        self.assertEqual(go(dict(body, tools=[BASH]), settings={"debug": True}).log, ())

    def test_the_health_entry_shows_the_scopes_and_the_cap(self):
        self.assertEqual(getattr(go(request()), "health"),
                         "advisor-cache ttl=5m subagent_ttl=5m max_tokens=- advisor_models=null advisor_agents=null errors=0")
        agents = jail.scratch(self)
        for name in ("a.md", "b.md"):
            with open(os.path.join(agents, name), "w") as f:
                f.write("Role %s." % name)
        out = go(request(), settings={"ttl": "1h", "subagent_ttl": "off", "max_tokens": 2048,
                                      "advisor_models": ["claude-a", "claude-b"], "advisor_agents": ["Role a", "Role b"],
                                      "agent_dirs": [agents]})
        self.assertEqual(getattr(out, "health"), 'advisor-cache ttl=1h subagent_ttl=off max_tokens=2048 '
                                     'advisor_models=["claude-a","claude-b"] advisor_agents=2 errors=0')


if __name__ == "__main__":
    unittest.main()
