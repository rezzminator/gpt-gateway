"""status-label: the gateway answers Claude Code's activity-summary fork itself, so it never goes upstream.

While a sub-agent runs, Claude Code asks the sub-agent's own model, over the sub-agent's whole conversation, for a
3-5 word status label of its latest action: the activity-summary fork. This module recognises that request exactly
and answers it with a label built from the sub-agent's last tool call, as an Anthropic message (a stream when the
request asks for one), on either route. Any other request passes untouched: a Claude Code release that rewords the
prompt only turns the answer off. The answer's row says `answered_by` `status-label`.

Shipped with gpt-gateway; a fresh install lists it in settings.json as `{"module": "status_label"}`. Its setting
`answer` is `always` (every fork is answered, the default) or `marked` (only a fork whose request carries the header
`x-cache-live-control-summaries: block`, which the cache-live-control plugin's `/cache summaries off` sets).
"""
import json
import posixpath
import re
import shlex
from urllib.parse import urlsplit

NAME = "status-label"

# Claude Code's prompt, byte for byte: the ask, an optional `Previous` paragraph, then the examples.
ASK = ("Describe your most recent action in 3-5 words using present tense (-ing). Name the file or function, "
       "not the branch. Do not use tools.\n\n")
EXAMPLES = ('Good: "Reading runAgent.ts"\nGood: "Fixing null check in validate.ts"\nGood: "Running auth module tests"\n'
            'Good: "Adding retry logic to fetchUser"\n\nBad (past tense): "Analyzed the branch diff"\n'
            'Bad (too vague): "Investigating the issue"\n'
            'Bad (too long): "Reviewing full branch diff and AgentTool.tsx integration"\n'
            'Bad (branch name): "Analyzed adam/background-summary branch diff"\n')
# Claude Code 2.1.296 ends the prompt at the last example's closing quote, earlier releases with a newline after it.
EXAMPLES_BARE = EXAMPLES.rstrip("\n")
MARK_HEADER = "x-cache-live-control-summaries"
MARK_BLOCK = "block"
ANSWERS = ("always", "marked")
PREVIOUS = re.compile('Previous: "[^\n]*" — say something NEW\\.\n\n')

MAX_WORDS = 5
MAX_TOKEN = 40
PATH_KEYS = ("file_path", "notebook_path")
FILE_VERBS = {"Read": "Reading", "Edit": "Editing", "MultiEdit": "Editing", "NotebookEdit": "Editing",
              "Write": "Writing"}
INTERPRETERS = {"sh", "bash", "zsh", "python", "python3", "node", "ruby", "perl"}
SUBCOMMAND_TOOLS = {"go", "git", "make", "npm", "pnpm", "yarn", "cargo", "docker", "pfm", "gh", "kubectl", "uv", "pip"}
# The options before a subcommand that take the next word as their value: `git -c k=v log` runs `git log`.
VALUE_OPTIONS = {"git": {"-C", "-c"}, "make": {"-C", "-f", "--file", "-I"}, "go": {"-C"},
                 "npm": {"--prefix", "-w", "--workspace"}, "pnpm": {"-C", "--dir", "-F", "--filter"},
                 "yarn": {"--cwd"}, "cargo": {"-C", "-Z", "--config"},
                 "docker": {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level"},
                 "kubectl": {"-n", "--namespace", "--context", "--kubeconfig", "-s", "--server"},
                 "gh": {"-R", "--repo"}, "uv": {"--directory", "--project"}}
PREFIX_WORDS = {"sudo", "env", "time"}
SEPARATORS = {"&&", ";"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
PUNCTUATION = re.compile(r"[();<>|&]+\Z")
FALLBACK_WORDS = re.compile(r"&&|\|\||[;|&<>()]|[^\s;|&<>()]+")


def is_label_ask(text):
    """True when `text` is Claude Code's prompt, with or without its `Previous: "…" — say something NEW.` paragraph."""
    if not isinstance(text, str) or not text.startswith(ASK):
        return False
    for examples in (EXAMPLES, EXAMPLES_BARE):
        if len(text) >= len(ASK) + len(examples) and text.endswith(examples):
            middle = text[len(ASK):len(text) - len(examples)]
            if middle == "" or PREVIOUS.fullmatch(middle) is not None:
                return True
    return False


def fork_message(ctx, req):
    """The activity-summary fork's user message, past Claude Code's trailing notices, or None for any other request."""
    if ctx.kind != "sub" or ctx.method != "POST" or (ctx.path or "").split("?", 1)[0] != "/v1/messages":
        return None
    last = req.last_turn_message()
    if not isinstance(last, dict) or last.get("role") != "user" or not isinstance(last.get("content"), list):
        return None
    texts = (block.get("text") for block in last["content"] if isinstance(block, dict) and block.get("type") == "text")
    return last if any(is_label_ask(text) for text in texts) else None


def token(value):
    """A value as one label token: its whitespace collapsed, cut to MAX_TOKEN characters."""
    return " ".join(str(value).split())[:MAX_TOKEN]


def base(path):
    trimmed = path.rstrip("/")
    return posixpath.basename(trimmed) or trimmed


def words(command):
    """The command's leading words, quotes resolved where they balance; separators and redirections stand alone."""
    command = command[:2000]
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return FALLBACK_WORDS.findall(command)


def bash_what(command):
    """What a Bash command runs: past leading assignments, `cd … &&`/`cd …;`, sudo, env, time and a subshell's `(`,
    an interpreter's script by its basename, a subcommand tool with its subcommand, else the first word's basename;
    None when nothing is left."""
    rest = words(command)
    while rest:
        if ASSIGNMENT.match(rest[0]) or rest[0] in PREFIX_WORDS or PUNCTUATION.match(rest[0]):  # `;`, `&&`, `(`
            rest = rest[1:]
        elif rest[0] == "cd" and any(word in SEPARATORS for word in rest):
            rest = rest[next(i for i, word in enumerate(rest) if word in SEPARATORS) + 1:]
        else:
            break
    if not rest:
        return None
    tool = base(rest[0])
    following = rest[1:]
    if tool in INTERPRETERS:
        if following and not following[0].startswith("-") and not PUNCTUATION.match(following[0]):
            return token(base(following[0]))
        return token(tool)
    if tool in SUBCOMMAND_TOOLS:
        skip = False
        for word in following:
            if skip:
                skip = False
            elif word in VALUE_OPTIONS.get(tool, ()):
                skip = True  # git -C dir, npm --prefix dir: the option's value is no subcommand
            elif word.startswith("-"):
                continue
            elif PUNCTUATION.match(word):
                break
            else:
                return "%s %s" % (token(tool), token(word))
    return token(tool)


def tool_label(name, args):
    """The label for one tool_use: its name and its input object."""
    def text(key):
        value = args.get(key)
        return value if isinstance(value, str) and value.strip() else None

    if name in FILE_VERBS:
        path = next((text(key) for key in PATH_KEYS if text(key)), None)
        return "%s %s" % (FILE_VERBS[name], token(base(path))) if path else None
    if name == "Grep":
        return "Searching for " + token(text("pattern")) if text("pattern") else None
    if name == "Glob":
        return "Listing " + token(text("pattern")) if text("pattern") else None
    if name == "Bash":
        what = bash_what(text("command")) if text("command") else None
        return "Running " + what if what else None
    if name == "WebFetch":
        try:
            host = urlsplit(text("url")).hostname if text("url") else None
        except ValueError:
            host = None
        return "Fetching " + token(host) if host else None
    if name == "WebSearch":
        return "Searching the web"
    if name in ("Agent", "Task"):
        return "Delegating to " + (token(text("subagent_type")) if text("subagent_type") else "an agent")
    if name == "TodoWrite":
        return "Updating the task list"
    if name == "ToolSearch":
        return "Loading tool definitions"
    if name == "Skill":
        return "Loading %s skill" % token(text("skill")) if text("skill") else None
    if name == "SendMessage":
        return "Messaging " + token(text("to")) if text("to") else None
    parts = name.split("__", 2)
    if len(parts) == 3 and parts[0] == "mcp" and parts[2]:
        return "Calling " + token(parts[2])
    return None


def label(messages, fork):
    """The status label for the fork message `fork` of `messages`: from the last tool call of the message before it."""
    at = next(i for i, message in enumerate(messages) if message is fork)
    before = messages[at - 1] if at > 0 else None
    if not isinstance(before, dict) or before.get("role") != "assistant":
        seen = any(isinstance(m, dict) and m.get("role") == "assistant" for m in messages[:at])
        return "Planning the next step" if seen else "Starting the task"
    content = before.get("content")
    calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] if isinstance(content, list) else []
    if not calls:
        return "Planning the next step"
    name = calls[-1].get("name")
    name = name if isinstance(name, str) and name else "a tool"
    args = calls[-1].get("input")
    text = tool_label(name, args if isinstance(args, dict) else {}) or "Using " + token(name)
    return " ".join(text.split()[:MAX_WORDS])


def answer_body(label_text, model, request_id, stream):
    """(content type, bytes): an Anthropic message holding `label_text`, as an SSE stream or as one JSON object."""
    message_id = "msg_local_%s" % request_id
    # No model produced the label, so every usage figure is 0; the gateway counts no tokens.
    zero = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    if not stream:
        body = {"id": message_id, "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": label_text}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": zero}
        return "application/json", json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": zero}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": label_text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 0}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    data = "".join("event: %s\ndata: %s\n\n" % (name, json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                   for name, event in events)
    return "text/event-stream", data.encode("utf-8")


def answer_value(value):
    if value in ANSWERS:
        return value
    raise ValueError("answer must be one of %s, got %s" % (", ".join(ANSWERS), json.dumps(value)))


SETTINGS = {"answer": ("always", answer_value, "GPT_GATEWAY_STATUS_LABEL_ANSWER")}


def marked(ctx):
    """True when the request carries the plugin's header with the word `block`."""
    return str(ctx.headers.get(MARK_HEADER) or "").strip().lower() == MARK_BLOCK


def on_request(ctx, req):
    fork = fork_message(ctx, req)
    if fork is None or (ctx.settings["answer"] == "marked" and not marked(ctx)):
        return None
    content_type, data = answer_body(label(req.body["messages"], fork), ctx.model, ctx.request_id,
                                     req.body.get("stream") is True)
    req.answer(200, content_type, data)
    return None
