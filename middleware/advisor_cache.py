"""advisor-cache: the advisor tool's prompt cache, output cap and scoping, as a middleware.

Claude Code sends the advisor server tool without `caching`, so the advisor re-reads the whole transcript at
full price on every call. For each POST /v1/messages this middleware gives the advisor tool
`caching: {"type": "ephemeral", "ttl": TTL}` (and, when set, `max_tokens`), and removes every trace of the
advisor from a request that should not carry it: a model outside `advisor_models`, or a sub-agent outside
`advisor_agents`. Everything else goes upstream byte for byte. A field the request already carries is kept.

Shipped with gpt-gateway, off until settings.json lists it:

    {"middleware": [{"module": "advisor_cache", "settings": {"ttl": "5m", "advisor_agents": ["..."]}}]}

Settings, every key optional, each with a GPT_GATEWAY_ADVISOR_* env variable that wins when set and not empty:
ttl, subagent_ttl, max_tokens, advisor_models, advisor_agents, agent_dirs, debug (README.md § Shipped middleware).
"""
import json
import os
import re

NAME = "advisor-cache"
TTLS = ("1h", "5m", "off")
MIN_MAX_TOKENS = 1024
MESSAGES_PATH = "/v1/messages"
ADVISOR_AGENT_KEPT = " advisor-agent=kept"


def ttl_value(key):
    def check_ttl(value):
        if isinstance(value, str) and value in TTLS:
            return value
        raise ValueError("%s must be one of 1h, 5m, off, got %s" % (key, json.dumps(value)))
    return check_ttl


def max_tokens_value(value):
    if isinstance(value, str):  # the env hands every value over as a string
        if not (value.isascii() and value.isdigit()):
            raise ValueError("max_tokens must be an integer >= %d, got %s" % (MIN_MAX_TOKENS, json.dumps(value)))
        value = int(value)
    if value is None:
        return None
    if type(value) is int and value >= MIN_MAX_TOKENS:
        return value
    raise ValueError("max_tokens must be an integer >= %d or null, got %s" % (MIN_MAX_TOKENS, json.dumps(value)))


def names_value(key):
    def check_names(value):
        if isinstance(value, str):  # the env hands over one comma-separated string
            value = [part.strip() for part in value.split(",")]
        if value is None:
            return None
        if isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value):
            return tuple(value)
        raise ValueError("%s must be a list of non-empty strings or null, got %s" % (key, json.dumps(value)))
    return check_names


def dirs_value(value):
    if isinstance(value, str):
        value = value.split(os.pathsep)
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(v, str) and os.path.isabs(os.path.expanduser(v.strip()))
                                       for v in value):
        return tuple(os.path.expanduser(v.strip()) for v in value)
    raise ValueError("agent_dirs must be a list of absolute directories or null, got %s" % json.dumps(value))


def debug_value(value):
    """A bool from the file (true/false) or from the env (1/0, true/false, yes/no, on/off)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
    raise ValueError("debug must be true or false (env: 1/0, true/false, yes/no, on/off), got %s" % json.dumps(value))


SETTINGS = {
    "ttl": ("5m", ttl_value("ttl"), "GPT_GATEWAY_ADVISOR_CACHE_TTL"),
    "subagent_ttl": ("5m", ttl_value("subagent_ttl"), "GPT_GATEWAY_ADVISOR_CACHE_TTL_SUBAGENT"),
    "max_tokens": (None, max_tokens_value, "GPT_GATEWAY_ADVISOR_MAX_TOKENS"),
    "advisor_models": (None, names_value("advisor_models"), "GPT_GATEWAY_ADVISOR_MODELS"),
    "advisor_agents": (None, names_value("advisor_agents"), "GPT_GATEWAY_ADVISOR_AGENTS"),
    "agent_dirs": (None, dirs_value, "GPT_GATEWAY_ADVISOR_AGENT_DIRS"),
    "debug": (False, debug_value, "GPT_GATEWAY_ADVISOR_CACHE_DEBUG"),
}


def raise_walk_error(error):
    raise error


def agent_texts(directory):
    """The text of every *.md under `directory`, symlinked files and directories followed; OSError if one cannot be read."""
    texts, seen = [], set()
    for root, subdirs, files in os.walk(directory, followlinks=True, onerror=raise_walk_error):
        real = os.path.realpath(root)
        if real in seen:  # a symlink cycle
            subdirs[:] = []
            continue
        seen.add(real)
        for name in sorted(files):
            path = os.path.realpath(os.path.join(root, name))
            if name.endswith(".md") and os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    texts.append(f.read())
    return texts


def check(settings):
    """A warning naming each `advisor_agents` entry no agent file holds verbatim (kept); ValueError if one is unreadable."""
    entries = settings["advisor_agents"]
    if not entries:  # null: unscoped; []: no entry to find
        return
    texts, searched = [], []
    for directory in settings["agent_dirs"] or (os.path.join(os.path.expanduser("~"), ".claude", "agents"),):
        if not os.path.isdir(directory):
            searched.append("%s (missing)" % directory)
            continue
        try:
            found = agent_texts(directory)
        except OSError as e:
            raise ValueError("advisor_agents: cannot read the agents under %s: %r" % (directory, e))
        texts.extend(found)
        searched.append("%s (%d files)" % (directory, len(found)))
    missing = [e for e in entries if not any(e in text for text in texts)]
    if missing:
        return "advisor_agents: no agent *.md under %s contains %s; it matches no sub-agent" % (
            "; ".join(searched), ", ".join(json.dumps(e) for e in missing))


def advisor_tools(j):
    return [t for t in j.get("tools") or [] if isinstance(t, dict) and str(t.get("type", "")).startswith("advisor_")]


def advisor_allowed(model, allowed):
    """Whether a request for `model` keeps the advisor: allowed None means every model, [] none;
    otherwise `model` must equal an entry or start with it (an entry covers its dated snapshots)."""
    if allowed is None:
        return True
    model = model if isinstance(model, str) else ""
    return any(model == e or model.startswith(e) for e in allowed)


# Claude Code's advisor instructions: one section, either the tail of a system-role message's text block
# ("...\n\n# Advisor Tool\n\n...") or a user text block of its own ("<system-reminder>\n# Advisor Tool...
# \n</system-reminder>\n"). It runs from its heading to the next "# " heading, the reminder's close or the
# block's end; ADVISOR_LAST_LINE is how it ends today, so a section ending otherwise is a wording drift.
ADVISOR_HEADING = "# Advisor Tool\n"
ADVISOR_LAST_LINE = "a reconcile call is cheaper than committing to the wrong branch."
REMINDER_OPEN, REMINDER_CLOSE = "<system-reminder>\n", "\n</system-reminder>"


def strip_section(text):
    """(text without the advisor section, whether found, whether it ended as expected); None for the text
    when the section is the whole system-reminder, so the block itself goes. Only the section and the
    blank line that joins it to its neighbour are removed; every other byte stays."""
    if text.startswith(ADVISOR_HEADING):
        start = 0
    elif text.startswith(REMINDER_OPEN + ADVISOR_HEADING):
        start = len(REMINDER_OPEN)
    else:
        start = text.find("\n" + ADVISOR_HEADING)
        if start < 0:
            return text, False, False
        start += 1
    ends = [i for i in (text.find("\n\n# ", start), text.find(REMINDER_CLOSE, start)) if i >= 0]
    end = min(ends) if ends else len(text)
    expected = text[start:end].rstrip().endswith(ADVISOR_LAST_LINE)
    before, after = text[:start], text[end:]
    if before == REMINDER_OPEN and after.rstrip("\n") == REMINDER_CLOSE:
        return None, True, expected
    if before.endswith("\n\n"):
        return before[:-2] + after, True, expected       # "...prev\n\n<section>" -> "...prev"
    if after.startswith("\n\n"):
        return before + after[2:], True, expected        # "<section>\n\nnext..." -> "next..."
    return before + after, True, expected


def is_advisor_block(b):
    """A message block that is the advisor's own: its deferred-tool announcement, its calls and results."""
    t = b.get("type")
    if t == "tool_addition":
        tool = b.get("tool") if isinstance(b.get("tool"), dict) else {}
        d = tool.get("definition") if isinstance(tool.get("definition"), dict) else {}
        return tool.get("name") == "advisor" or d.get("name") == "advisor" \
            or str(d.get("type", "")).startswith("advisor_")
    return False


def advisor_in_history(j):
    for m in j.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and ((b.get("type") == "server_tool_use" and b.get("name") == "advisor")
                                        or b.get("type") == "advisor_tool_result"):
                return True
    return False


def drop_block(content, i):
    """Remove content[i], handing its cache_control to the block before it so no breakpoint is lost."""
    b = content.pop(i)
    if "cache_control" in b and i > 0 and "cache_control" not in content[i - 1]:
        content[i - 1]["cache_control"] = b["cache_control"]
        return True
    return "cache_control" not in b


def strip_advisor(j):
    """Remove every trace of the advisor from a parsed request, in place. Returns (stripped, warnings, skip):
    stripped names what went (tool, tool_reference, section); warnings say what looked unexpected; skip is
    a reason the request was left whole. Pure in its input, so a conversation's requests strip alike and
    the main model's prompt cache keeps matching."""
    if advisor_in_history(j):
        return [], [], "advisor in history"
    stripped, warnings = [], []
    tools = j.get("tools")
    if isinstance(tools, list):
        kept = [t for t in tools if not (isinstance(t, dict) and str(t.get("type", "")).startswith("advisor_"))]
        if len(kept) != len(tools):
            j["tools"] = kept
            stripped.append("tool")
    section = False
    messages = j.get("messages") if isinstance(j.get("messages"), list) else []
    for mi in range(len(messages) - 1, -1, -1):
        m = messages[mi]
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            if ADVISOR_HEADING not in content or not (
                    role == "system" or (role == "user" and content.startswith(REMINDER_OPEN))):
                continue
            text, found, expected = strip_section(content)
            if found:
                section = True
                if not expected:
                    warnings.append("section-end-unexpected")
                if text:
                    m["content"] = text
                else:
                    messages.pop(mi)
            continue
        if not isinstance(content, list) or not content:
            continue  # an empty message Claude Code sent stays as it is
        for bi in range(len(content) - 1, -1, -1):
            b = content[bi]
            if not isinstance(b, dict):
                continue
            if is_advisor_block(b):
                if not drop_block(content, bi):
                    warnings.append("cache_control-dropped")
                stripped.append("tool_reference")
                continue
            text = b.get("text")
            if b.get("type") != "text" or not isinstance(text, str) or ADVISOR_HEADING not in text:
                continue
            if role != "system" and not (role == "user" and text.startswith(REMINDER_OPEN)):
                continue
            text, found, expected = strip_section(text)
            if not found:
                continue
            section = True
            if not expected:
                warnings.append("section-end-unexpected")
            if text:
                b["text"] = text
            elif not drop_block(content, bi):
                warnings.append("cache_control-dropped")
        if not content:
            messages.pop(mi)
            warnings.append("message-emptied")
    if section:
        stripped.append("section")
    elif "tool" in stripped:
        warnings.append("section-not-found")
    return sorted(set(stripped), key=stripped.index), sorted(set(warnings)), None


def system_text(j):
    """All the request's `system` text, a plain string or its text blocks joined by a newline."""
    system = j.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b["text"] for b in system
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def agent_keeps_advisor(j, entries):
    """Whether a sub-agent request names one of `entries` in its system text."""
    text = system_text(j)
    return any(entry in text for entry in entries)


def is_messages(ctx):
    return ctx.method == "POST" and (ctx.path or "").split("?", 1)[0] == MESSAGES_PATH


def drop_advisor(ctx, j, label):
    """Strip the advisor from `j`; the (note, skip note) for the log line, `label` naming why it went."""
    stripped, warnings, skip = strip_advisor(j)
    if skip:
        return "", " advisor-filter-skipped: " + skip
    if not stripped:
        return "", ""
    filtered = " advisor-filtered%s stripped=%s" % (label, ",".join(stripped))
    if warnings:
        filtered += " warnings=" + ",".join(warnings)
        ctx.log("advisor-filter-warning: %s in %s; strip_advisor's markers may no longer match Claude Code's "
                "wording, or the instruction section is absent (a compacted session drops it). request dir=%s" % (
                    ",".join(warnings), j.get("model"), ctx.capture_dir or "- (capture off)"))
    return filtered, ""


def on_request(ctx, req):
    """Scope the advisor to the models and agents that keep it, then give what is left its caching and cap.

    A field the caller already set is kept. A body that is not a JSON object, or carries no advisor
    tool, goes upstream untouched. The note is the request's log fields.
    """
    j = req.body
    if j is None or not is_messages(ctx):
        return None
    s = ctx.settings
    sub = ctx.kind == "sub"
    ttl, max_tokens = (s["subagent_ttl"] if sub else s["ttl"]), s["max_tokens"]
    scoped = sub and s["advisor_agents"] is not None
    agent_kept = False
    note = " model=%s" % j.get("model")
    filtered = ""
    if not advisor_allowed(j.get("model"), s["advisor_models"]):
        filtered, skip = drop_advisor(ctx, j, " model=%s" % j.get("model"))
        note += skip
    elif scoped:
        agent_kept = agent_keeps_advisor(j, s["advisor_agents"])
        if not agent_kept:
            filtered, skip = drop_advisor(ctx, j, " agent")
            note += skip
    tools = advisor_tools(j)
    if s["debug"] and tools:
        ctx.log("debug context_management=%s thinking=%s" % (
            json.dumps(j.get("context_management")), json.dumps(j.get("thinking"))))
    changed = []
    kept = []
    for t in tools:
        if "caching" in t:
            c = t["caching"]
            # The caller's own caching: logged with its TTL (the API's default is 5m), so the advisor row can price it.
            kept.append("advisor-caching-kept=%s" % ((c.get("ttl") if isinstance(c, dict) else None) or "5m"))
        elif ttl != "off":
            t["caching"] = {"type": "ephemeral", "ttl": ttl}
            changed.append("advisor-caching=" + ttl)
        if max_tokens is not None and "max_tokens" not in t:
            t["max_tokens"] = max_tokens
            changed.append("advisor-max_tokens=%d" % max_tokens)
    if kept:
        note += " " + " ".join(kept)
    elif not tools and j.get("tools") and not filtered:
        # Marks a request whose tools carry no advisor, so an advisor row logged for it is explained.
        note += " advisor-tool=none"
    note += filtered
    if agent_kept and tools:
        note += ADVISOR_AGENT_KEPT
    if changed or filtered:
        req.mark_changed()
    return note + "".join(" " + c for c in changed)


def on_event(ctx, event):
    """Keep the last event of a /v1/messages response that carries advisor iterations."""
    if is_messages(ctx) and isinstance(event["data"], dict) and b"advisor_message" in event["raw"]:
        ctx.state["usage"] = event["data"]


def on_response_end(ctx, result):
    """One `advisor` row per advisor iteration of the response, after the request's own line."""
    usage = (ctx.state.pop("usage", None) or {}).get("usage") or {}
    for it in [i for i in usage.get("iterations") or [] if isinstance(i, dict) and i.get("type") == "advisor_message"]:
        ttl = re.search(r"advisor-caching(?:-kept)?=(\w+)", result.note)
        ctx.log("advisor %s kind=%s ttl=%s in=%s cache_read=%s cache_write=%s out=%s" % (
            it.get("model"), ctx.kind or "-", ttl.group(1) if ttl else "-",
            it.get("input_tokens"), it.get("cache_read_input_tokens"),
            it.get("cache_creation_input_tokens"), it.get("output_tokens")))


def health(ctx):
    s = ctx.settings
    return "ttl=%s subagent_ttl=%s max_tokens=%s advisor_models=%s advisor_agents=%s" % (
        s["ttl"], s["subagent_ttl"], "-" if s["max_tokens"] is None else s["max_tokens"],
        json.dumps(s["advisor_models"], separators=(",", ":")),
        "null" if s["advisor_agents"] is None else "%d" % len(s["advisor_agents"]))
