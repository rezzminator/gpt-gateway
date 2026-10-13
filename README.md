# gpt-gateway

A local passthrough between Claude Code and the Anthropic API, with a middleware hook API.

Claude Code reaches it through `ANTHROPIC_BASE_URL=http://127.0.0.1:18787`. By default, every request goes to `https://api.anthropic.com` byte for byte, and every response streams back chunk by chunk as it arrives. Your login (`Authorization`, `x-api-key`) passes through and is never logged or captured. Middleware you install can watch requests and responses, add a note to the request's database row, and edit a request body; with no middleware the default route changes nothing. The optional [ChatGPT route](#chatgpt-route) runs GPT sub-agents over your ChatGPT subscription.

The supervisor points Claude Code at the gateway only while the gateway answers, and removes that setting whenever it is down, so a broken gateway never cuts Claude Code off (§ Fail-open).

macOS and Linux, Python 3.9+ standard library only (`/usr/bin/python3`), `jq` for the settings edits.

## Install, update, remove

```sh
service/install.sh            # install or update; --port N (default 18787)
~/.local/share/gpt-gateway/ctl.sh status
service/uninstall.sh          # --purge also deletes the logs and the database
```

The commands select macOS or Linux with `uname -s`. Both require Python 3.9+, jq and curl, and an existing Claude Code settings file. `GPT_GATEWAY_CLAUDE_SETTINGS` selects another settings file; its real path is recorded, so a symlink stays intact.

`install.sh` runs the unit tests first and refuses to install when one fails. It keeps the whole run in `gpt-gateway-install-tests.log` under the platform's log directory, prints that path and the failing test names, and stops a run still going after `GPT_GATEWAY_TEST_SECONDS` (default 1800) with every thread's stack in the log. It copies `gateway.py`, the `gpt_gateway/` package (all Python sources, preserving subdirectories), `run.sh`, `ctl.sh`, `platform.sh`, `settings.sh`, `listener.py`, this README, LICENSE and `THIRD_PARTY_NOTICES.md` to `~/.local/share/gpt-gateway/`, and copies the shipped middleware into `middleware/`, replacing only each module's own file. A first install writes `settings.json` listing `status_label` (§ Shipped middleware) and turns the gateway on. An update validates and keeps that file and the on/off state: when on, new gateway code takes over through HUP without cutting a stream; a changed supervisor or service definition drains and reloads the service. Newly shipped middleware is copied but unlisted: add it to `middleware`, then `ctl.sh restart`.

### macOS

Run `service/install.sh` from the checkout. It writes the launchd agent `local.gpt-gateway` under `~/Library/LaunchAgents/`. Logs, including install-test output and supervisor stderr, live under `~/Library/Logs/`. The system Python needs the command line developer tools.

On macOS an install under the former product name is migrated once: its settings and middleware seed a first install, its agent drains and its plist goes, its state directory moves to `~/.local/state/gpt-gateway/` unless that already exists, and its installed copy is deleted. A ChatGPT login under the former Keychain name is not moved: run `gateway.py chatgpt-login` again, or use Codex's login.

### Linux

Run `service/install.sh` from the checkout in a session with a systemd 252+ user manager. Install checks this minimum and bus reachability before changing files. It writes `gpt-gateway.service` under `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`, reloads the user manager, and enables and starts a first install. `systemctl --user enable`/`disable` carry the on/off state across logins; updating a disabled unit keeps it off. Logs and install-test output live under `~/.local/state/gpt-gateway/`. The installed `service.json` records the environment used by `ctl.sh` and `uninstall.sh`.

Without linger, the user manager and gateway stop at logout. Check with `loginctl show-user "$USER" -p Linger`; an owner who needs it to run between logins enables linger with `loginctl enable-linger "$USER"`. HUP is sent with `systemctl --user kill -s HUP --kill-whom=main gpt-gateway.service`: it reaches the supervisor, which replaces and drains the gateways. `Restart=on-failure` retries a failed supervisor, and `KillMode=mixed` gives `run.sh` TERM first so it tries to unlink settings before signalling its children to drain. Failed cleanup retries alongside the full drain, with its deadline one second before the 630-second stop timeout. The [Linux switch-on runbook](docs/operations/linux-service.md) covers backup, verification, switch-off and rollback.

`uninstall.sh` stops the agent (in-flight streams finish first), points Claude Code back at the API, deletes the agent and the installed copy, and keeps `settings.json` and the `middleware` directory under `~/.local/state/gpt-gateway/backups/`.

## Settings

`settings.json` in the install directory. Every key is optional; an unknown key is refused.

```json
{"capture_dir": null, "middleware": [{"module": "model_note", "settings": {"enabled": true}}], "middleware_dir": null}
```

| Key | Value | Default | Env override |
| --- | --- | --- | --- |
| `capture_dir` | an absolute directory (`~` allowed), or null for off | null | `GPT_GATEWAY_CAPTURE_DIR` (`off` turns it off) |
| `chatgpt` | a JSON boolean, or null for the default | false | `GPT_GATEWAY_CHATGPT` (`1`, `true`, `on` or `0`, `false`, `off`) |
| `chatgpt_cache_retention` | `off` or `24h`: with `24h`, every ChatGPT Responses request also asks for 24-hour prompt-cache retention (`prompt_cache_retention`; see [Cache retention](#cache-retention)) | `off` | none |
| `db_retention_days` | how many days of rows the [database](#database) keeps, a whole number of at least 1 | 30 | none |
| `middleware` | the ordered list of middleware, each `{"module": NAME, "settings": {...}}`; `module` matches `^[a-z][a-z0-9_]*$` and appears once | `[]` | none |
| `middleware_dir` | an absolute directory, or null | a `middleware` directory beside `settings.json` | `GPT_GATEWAY_MIDDLEWARE_DIR` |

An environment variable that is set and not empty wins over the file, which wins over the default. An edit applies from the next request: the file is re-read whenever its modification time, size or inode changes. A file that fails validation stops the gateway at start (and `run.sh` fails open); at a reload it is logged once (the text log and a `settings_error` event) and the last good settings stay, and the health line says `settings=error(...)`. Changes to the middleware list (added, removed or reordered), `middleware_dir` or `chatgpt` need a restart (`ctl.sh restart`); until then the reload reports "restart to apply" and the running settings stay.

`gateway.py check [FILE]` validates a settings file and loads its middleware the way a start would; it exits 1 with the reason.

The environment alone sets the rest (the service manager sets them in its plist or user unit):

| Variable | Meaning | Default |
| --- | --- | --- |
| `GPT_GATEWAY_PORT` | listen port on 127.0.0.1 | 18787 |
| `GPT_GATEWAY_UPSTREAM` | upstream origin | `https://api.anthropic.com` |
| `GPT_GATEWAY_LOG` | the text log, rotated to `.1` past 5 MiB: start, stop, fatal, settings-error and database-failure lines only; an absolute path or `~/` under `HOME` | macOS: `~/Library/Logs/gpt-gateway.log`; Linux: `~/.local/state/gpt-gateway/gpt-gateway.log` |
| `GPT_GATEWAY_DB` | the [database](#database), an absolute path or `~/` under `HOME` | `~/.local/state/gpt-gateway/gateway.db` |
| `GPT_GATEWAY_DRAIN_SECONDS` | on SIGTERM, how long in-flight requests may finish | 600 |
| `GPT_GATEWAY_DEBUG` | set to record each messages request's non-secret headers and metadata as a `debug` event | off |

## Middleware

A middleware is one Python file, `{middleware_dir}/{module}.py`, listed in `settings.json`. It never imports the gateway. Its code loads once, at start; `ctl.sh restart` reloads it. The gateway ships two, `status_label` and `advisor_cache` (§ Shipped middleware). The smallest one, `examples/middleware/model_note.py`, adds ` model-note=<model>` to each request's note:

```python
NAME = "model-note"

def enabled_value(value): ...   # bool from the file, 1/0, true/false, yes/no, on/off from the env

SETTINGS = {"enabled": (True, enabled_value, "GPT_GATEWAY_MODEL_NOTE")}

def on_request(ctx, req):
    if ctx.settings["enabled"]:
        return " model-note=%s" % ctx.model
    return None
```

Install it with `cp examples/middleware/model_note.py ~/.local/share/gpt-gateway/middleware/`, list it as `{"module": "model_note"}`, then `ctl.sh restart`.

### Module attributes

Only `NAME` is required.

- `NAME`: the label in the health line, the database and the CLI; it matches `^[a-z][a-z0-9_-]*$` and is unique.
- `SETTINGS`: `{key: (default, validator, env_var_or_None)}`. `validator(value)` returns the value or raises `ValueError`; the gateway reports it naming the module, the key and the source (env, file or default). An env variable that is set and not empty wins over the entry's `settings`, which win over the default; env values reach the validator as strings. A `settings` key the module does not declare is refused.
- `check(settings)`: a whole-settings check (cross-key rules, files on disk); raises `ValueError`.
- `on_request(ctx, req)`: runs before the request goes upstream, for every proxied request except the health endpoint. A string it returns is appended verbatim to the request's note. It may answer the request itself (`req.answer`, § The request).
- `on_event(ctx, event)`: once per complete SSE event of a streaming response, as `{"event": name, "data": parsed JSON or None, "raw": bytes}`; once for a non-streaming JSON response, with `"event": None`. Error responses included. The gateway splits events across chunk boundaries and never holds a chunk back from the client.
- `on_response_end(ctx, result)`: after the response; `result` has `status`, `reason`, `headers`, `error_head` (the first 600 bytes when status >= 400), `completed`, `client_disconnected`, `duration_s` and `note` (` kind={kind}` and then each module's note).
- `health(ctx)`: a string for the health line.
- `cli(argv, ctx)`: run by `gateway.py mw NAME ARGS...`, which exits with its return value.

Load errors (a missing file, an import error, a missing or duplicate `NAME`, a refused setting, a failing `check`) stop the start, written to the text log with the module and the cause, and `gateway.py check` reports the same and exits 1.

### The request

`req.body` is the parsed JSON object, or `None` when the body is not one; `req.raw` is the bytes as sent. A hook that edits `req.body` calls `req.mark_changed()`. After every `on_request` hook has run, the gateway serializes the body once, as `json.dumps(body, separators=(",", ":")).encode()`, only if some hook marked a change. The Anthropic route otherwise forwards the original bytes unchanged; the ChatGPT route translates the resulting Anthropic body.

`req.answer(status, content_type, data)` answers the request in the gateway: `status` an int from 200 to 599, `content_type` a non-empty string, `data` the body's bytes. Nothing then goes upstream on either route (on the ChatGPT route no Responses request, and the row has no lane), the later modules' `on_request` is skipped, and the gateway relays the answer as it relays any response: capture, `on_event`, `on_response_end` and the request's row, whose note ends ` answered={NAME}` and whose `answered_by` is `NAME`. `req.answered` is that answer, with `status`, `content_type` and `data`, or `None`. `req.last_turn_message()` is the body's last user or assistant message, past the role-system notices Claude Code appends after it (task notifications, hook feedback, the environment), or `None`.

`ctx` is one object per request, read-only: `kind` (`main`, or `sub` for a request carrying `x-claude-code-agent-id`), `route` (`"anthropic"` or `"chatgpt"`), `method`, `path`, `headers` (case-insensitive, without `authorization`, `x-api-key`, `proxy-authorization` and `cookie`), `model` (the original body's), `settings` (this module's validated snapshot, the same for the whole request), `state` (a dict this module keeps across its hooks for this request), `request_id`, `capture_dir` (this request's capture folder, or `None`), `log(line)`, which records the line as a `middleware_log` event of the request, and `log_path`, the text log's path.

**Determinism.** A body edit must be a pure function of the request and the settings snapshot: no clock, no randomness, no per-request state. Otherwise the request's prefix changes from one request to the next and the main model's prompt cache misses on every request.

### Order and failures

Hooks run in list order: `on_request` first to last, then `on_event` and `on_response_end` in the same order. A hook never breaks a request or cuts a stream:

- A hook that raises is one `middleware_failed` event of its request, with `module`, `hook`, `error` (`{exc!r}`) and `where` (`{file}:{line} in {function}`), and its module's error count rises.
- When `on_request` raises, the request goes upstream as its original bytes, every module's edits and notes are discarded, and the later modules' `on_request` is skipped; an answer it gave before raising is discarded too.
- A hook that answers ends the `on_request` run: the later modules' `on_request` is skipped and no edit is serialized.
- A hook that changes `body["model"]` has failed the same way: routing is the gateway's.
- A failing `on_event` is skipped for the rest of that response; a failing `on_response_end` is skipped.

### Testing a middleware

`gpt_gateway/harness.py` runs one middleware file in process, with no network, through the gateway's own settings resolution, hook chain, SSE splitting and request row:

```python
import sys
sys.path.insert(0, "path/to/gpt-gateway")
from gpt_gateway.harness import run, run_cli, load

out = run("my_mw.py", body={"model": "m", "messages": []}, headers={"x-claude-code-agent-id": "a1"},
          response=b'event: message_stop\ndata: {"type":"message_stop"}\n\n', chunk_size=7,
          settings={"enabled": True}, env={})
out.forwarded, out.changed, out.note, out.log, out.errors, out.events, out.result, out.health, out.answer, out.row
code, stdout, stderr = run_cli("my_mw.py", ["stats"], log_path="gw.log", db_path="gateway.db")
```

`out.log` holds the lines the module wrote through `ctx.log`, `out.errors` each hook failure as its `middleware_failed` event's fields, and `out.row` the request's row as the database records it, its times and process left out. `run_cli` records the cli's `ctx.log` lines and a failure in the database at `db_path`, as `gateway.py mw` does; without it they are dropped. When a hook answers, `out.answer` holds its answer, `out.forwarded` is `None`, and `out.events` and `out.result` are the answer's, relayed in place of `response`, as the gateway relays it. `load()` raises `SettingsError` or `MiddlewareError` where a start would fail. `tests/examples/middleware/test_model_note.py` tests the example this way, and `tests/middleware/test_status_label.py` and `tests/middleware/test_advisor_cache.py` the shipped `status_label` and `advisor_cache`.

### Shipped middleware

An install copies every `middleware/*.py` into its `middleware/` directory, each file replacing only its own copy. A fresh install lists `status_label` in `settings.json` and nothing else; `advisor_cache` ships with it and stays off until listed.

#### status_label

`middleware/status_label.py` (`NAME` `status-label`) answers Claude Code's activity-summary fork in the gateway. While a sub-agent runs, Claude Code now and then sends the sub-agent's whole conversation to the sub-agent's own model again, to get a 3-5 word status label for its UI: a full context of input tokens per label, and on the ChatGPT route a turn on the lane's socket. The module recognises that request exactly, a `sub` POST to `/v1/messages` whose last turn message (`req.last_turn_message()`) is the user's and holds Claude Code's prompt as a text block, with or without its `Previous: "…" — say something NEW.` paragraph (and with or without the newline older Claude Code releases end it with), and answers it from the last tool call of the message before: `Reading {file}`, `Editing {file}`, `Writing {file}`, `Searching for {pattern}`, `Listing {pattern}`, `Running {command}`, `Fetching {host}`, `Searching the web`, `Delegating to {agent}`, `Updating the task list`, `Loading tool definitions`, `Loading {skill} skill`, `Messaging {recipient}`, `Calling {MCP tool}`, else `Using {tool}`; `Planning the next step` after a turn without a tool call and `Starting the task` before any. A label has at most five words, a path shows as its file name, and a value is cut to 40 characters. The answer is an Anthropic message, a stream when the request asks for one, with zero input usage. Any other request passes untouched, so a Claude Code release that rewords the prompt only turns the answer off. A first install lists it; to turn it off, remove `{"module": "status_label"}` from `middleware` and `ctl.sh restart`. Its one setting, `answer` (env `GPT_GATEWAY_STATUS_LABEL_ANSWER`), is `always` by default, every fork answered; `marked` answers only a fork whose request carries the header `x-cache-live-control-summaries: block`, which the cache-live-control plugin's `/cache summaries off` adds to Claude Code's requests, so a chat without the header keeps Claude Code's own summary calls: `{"module": "status_label", "settings": {"answer": "marked"}}`.

#### advisor_cache

`middleware/advisor_cache.py` (`NAME` `advisor-cache`) gives Claude Code's advisor its prompt cache, can cap the advisor's output, and can take the advisor away from the models and sub-agents that should not carry it. It is off until `settings.json` lists it; every setting has a default, so the smallest entry is `{"module": "advisor_cache"}`:

```json
{"capture_dir": null, "middleware": [{"module": "status_label"},
  {"module": "advisor_cache", "settings": {"ttl": "5m", "subagent_ttl": "5m", "max_tokens": 2048}}]}
```

Then `ctl.sh restart` (a changed `middleware` list applies at a restart; an edit to its `settings` applies from the next request). To turn it off, remove the entry and restart.

The advisor is still Claude Code's own: Anthropic runs it on its servers. It reads what the main model's context holds, which includes the main model's thinking from the current turn but not from earlier turns. The module changes the advisor tool in the request and nothing else.

**How it works.** The Anthropic API's advisor tool (`advisor_20260301`) accepts `caching: {"type": "ephemeral", "ttl": "5m" | "1h"}`, which is off by default. Claude Code 2.1.293 builds the advisor tool as `{type, name, model}` and never adds `caching`; no setting changes that, and a plugin cannot touch server tools. So on every `POST /v1/messages` whose body is a JSON object, the module works in this order, and every other request goes upstream byte for byte:

1. **Scope.** A request whose `model` is outside `advisor_models`, or a `sub` request whose system text names no entry of `advisor_agents`, loses every trace of the advisor: the tool, its instruction section (in a system-role message, or a user system-reminder of its own, from its `# Advisor Tool` heading to the next heading, the reminder's close or the block's end) and its deferred-tool announcement. The model scope decides first. The strip is a pure function of the request, so a conversation's requests strip alike and the main model's prompt cache keeps matching. A conversation whose history already holds an advisor call is left whole (noted `advisor-filter-skipped: advisor in history`).
2. **Caching.** An advisor tool without `caching` gets `{"type": "ephemeral", "ttl": TTL}`, where TTL is `ttl` for a main session (no `x-claude-code-agent-id` header) and `subagent_ttl` for a sub-agent; `off` leaves that kind's tool as sent. A conversation keeps one TTL, because a 1h request does not read an entry written at 5m (measured).
3. **Output cap.** With `max_tokens` set, an advisor tool without its own `max_tokens` gets it.

A field the request already carries is kept: if Claude Code sets `caching` or `max_tokens` itself, the module steps aside for that field (the feature request is anthropics/claude-code#91110). On the ChatGPT route the same body edit runs before the route translates the request.

**Settings**, every key optional:

| Key | Accepted values | Default | Env variable | What it does |
| --- | --- | --- | --- | --- |
| `ttl` | `"1h"`, `"5m"`, `"off"` | `"5m"` | `GPT_GATEWAY_ADVISOR_CACHE_TTL` | the cache lifetime a main session's advisor gets; a read refreshes it |
| `subagent_ttl` | `"1h"`, `"5m"`, `"off"` | `"5m"` | `GPT_GATEWAY_ADVISOR_CACHE_TTL_SUBAGENT` | the same for sub-agents |
| `max_tokens` | an integer of at least 1024, or `null` | `null` (no cap) | `GPT_GATEWAY_ADVISOR_MAX_TOKENS` (digits) | caps the advisor's output per call, thinking plus text |
| `advisor_models` | a list of non-empty model IDs or ID prefixes, or `null` | `null` (every model keeps it) | `GPT_GATEWAY_ADVISOR_MODELS` (comma-separated) | the models that keep the advisor: the request's model equals an entry or starts with it, so an ID covers its dated snapshots; `[]` keeps it for none |
| `advisor_agents` | a list of non-empty system-text fragments, or `null` | `null` (every sub-agent keeps it) | `GPT_GATEWAY_ADVISOR_AGENTS` (comma-separated) | the sub-agents that keep the advisor: their system text contains an entry verbatim; `[]` keeps it for none; main sessions are never scoped by it |
| `agent_dirs` | a list of absolute directories (`~` expands), or `null` | `null` (`~/.claude/agents`) | `GPT_GATEWAY_ADVISOR_AGENT_DIRS` (colon-separated) | where `check` looks for the `advisor_agents` entries |
| `debug` | `true` or `false` (env: `1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`) | `false` | `GPT_GATEWAY_ADVISOR_CACHE_DEBUG` | logs each advisor request's `context_management` and `thinking` as a `middleware_log` event |

Anthropic's advisor documentation reports that a `max_tokens` of `2048` cut advisor output about 7x with no detectable loss of quality; it is off by default because it changes the advice itself and not only its price. The API accepts `max_tokens` on the advisor through the subscription login (HTTP 200).

`check` runs at start and at every settings reload: each `advisor_agents` entry must appear verbatim in at least one `*.md` file under the agent directories (subdirectories and symlinks followed, symlink cycles ended). A typo is then a refused start, or a refused edit that keeps the last good settings, naming the entry and every directory searched (a missing one as `(missing)`), and never a sub-agent silently losing its advisor.

**What it records.** The request's note (the `note` column of `requests`) carries `model=…` and then, as they apply: `advisor-caching=5m` (added) or `advisor-caching-kept=1h` (the request's own), `advisor-max_tokens=2048`, `advisor-tool=none` (the tools hold no advisor), `advisor-agent=kept`, `advisor-filtered model=… stripped=tool,tool_reference,section` or `advisor-filtered agent stripped=…`, and `warnings=…` when the strip met something unexpected: `section-not-found` (a compacted session drops the instruction section), `section-end-unexpected` (Claude Code reworded it; the strip still ends where the next heading or the block does), `cache_control-dropped` and `message-emptied`. A warning also writes an `advisor-filter-warning` `middleware_log` event naming the request's capture folder. Each advisor call of a response is one `middleware_log` event of its request:

```text
advisor claude-opus-5-5 kind=main ttl=5m in=11 cache_read=0 cache_write=9000 out=300
```

`ttl` is the TTL the request ran at (its own caching's, else the one added; `-` when none). Read the rows with `sqlite3`:

```sh
sqlite3 ~/.local/state/gpt-gateway/gateway.db "select datetime(at, 'unixepoch', 'localtime'), request_id, json_extract(fields, '$.line') from events where kind = 'middleware_log' and json_extract(fields, '$.line') like 'advisor %' order by at desc limit 20"
```

The health line shows `advisor-cache ttl=5m subagent_ttl=5m max_tokens=- advisor_models=null advisor_agents=null`, the agent scope as its entry count.

**Cost.** Caching changes only the price of the advisor's input; its output costs the same either way. Relative to reading a token uncached:

- **Cache write:** 1.25x at 5m and 2x at 1h, paid on the part of the input not yet in the cache: everything on a first call or after a miss, only the new tokens on a hit.
- **Cache read:** 0.05x on Opus 5.5 and Sonnet 5.5, 0.025x on Fable 5.1 and Mythos 5.1, and 0.1x on other models. The Claude Code advisor is Opus 5.5, the 0.05x case. Prices from platform.claude.com, 2026-10-08.
- **TTL:** a read refreshes it.

Each call stores a snapshot of the advisor's input from the first token up to that call. A later call reads a snapshot only if it is an exact prefix of the later call's input; partial matches don't count. At a turn boundary that follows thinking, the earlier snapshot stops matching, because Claude Code sends that thinking back emptied (see the measurements below). So count advisor calls per turn. Take a turn with `k` advisor calls, a transcript of `T` tokens at the first call, and `δ` new tokens between calls, with Opus 5.5 as the advisor:

| Calls in the turn | Uncached | 5m | 1h |
| --- | --- | --- | --- |
| 1 | T | 1.25T | 2T |
| 2 | 2T + δ | 1.30T + 1.25δ | 2.05T + 2δ |
| 3 | 3T + 3δ | 1.35T + 2.55δ | 2.1T + 4.05δ |

Example: `T` = 150k and `δ` = 100k. Opus 5.5 prices per million tokens: input $4, 5m write $5, 1h write $8, cache read $0.20.

| | Call 1 | Call 2 | Two calls |
| --- | --- | --- | --- |
| uncached | $0.60 | $1.00 | $1.60 |
| 5m | $0.75 | $0.53 | $1.28 |
| 1h | $1.20 | $0.83 | $2.03 |
| 1h, with a thinking turn between the calls | $1.20 | $2.00 (miss: all 250k written) | $3.20 |

- **5m** costs 25% more on a turn with one call, and wins on a turn with two or more calls less than 5 minutes apart, unless `δ` is above `2.8T`. When a gap passes 5 minutes, that call costs 1.25x instead of 1x, so the loss is bounded at 25%.
- **1h** costs double on a turn with one call, still loses with two, and wins only from three calls in one turn, while `δ` stays below about `0.86T`. It beats 5m only when calls in one turn sit more than 5 minutes apart, or when the turns between calls have no thinking.

Hence both kinds default to 5m. Price your real calls from the rows above (`in` uncached, `cache_write` at its TTL's rate, `cache_read` at the model's rate), split by `kind`; if a kind shows a loss after a few days, switch it with `ttl` or `subagent_ttl` (`off` stops caching for that kind).

**Measured on 2026-10-08**, Claude Code 2.1.293, Haiku 5.5 as the main model, Opus 5.5 as the advisor:

| Run | advisor call 1 | advisor call 2 |
| --- | --- | --- |
| without the module | 99,097 tokens read at full price | 99,413 tokens read at full price |
| with the module, 1h | 96,642 written to the cache | 96,642 read from the cache, 200 written |

- **Within a turn, the cache always holds.** Later advisor calls in the same turn read everything the earlier ones wrote, in main sessions and in sub-agents (73,107 tokens read on a sub-agent's second call at 5m).
- **Across turns, it holds only when the earlier turn had no thinking.** The turn-1 advisor call reads the main model's thinking live. In turn 2, Claude Code sends that thinking back as an empty block with only its signature, and the API leaves earlier-turn thinking out of the context. So the advisor's cached prefix no longer matches, and its first call in the new turn re-writes everything: with no thinking in turn 1, turn 2 read all 98,518 tokens; with thinking, it read 0. This happens on the API side, and the main model's own cache misses at the same point; no gateway can change it.
- **The advisor reads current-turn thinking only.** Adding 2,470 tokens of thinking before an advisor call added exactly 2,470 tokens to the advisor's input. Over 1,000 tokens of thinking in an earlier turn added nothing; the main model's own billed input didn't grow from it either, although Claude Code sends `clear_thinking` with `keep: "all"`.
- **A conversation can't change TTL and keep its cache.** A 1h call after a 5m call read 0 tokens and re-wrote 99,844.

**Known behaviour.**

- **Parallel advisor calls don't share the cache.** When the main model puts two advisor calls in one message, both read the same prefix at the same moment, so both write it and neither reads (measured: two writes of 96,285 tokens). Only a call made after an earlier one has answered reads from the cache.
- **Two advisor calls cached at 1h without the module adding it** (one long main session): the request that ran the advisor showed no rewrite, and the API reported the write as `ephemeral_1h_input_tokens`. Unexplained; the `advisor-caching` markers in the note tell the next one apart.
- **With the gateway on, Claude Code doesn't see a first-party API.** `isFirstPartyAnthropicBaseUrl()` is true only for the host `api.anthropic.com`, so it is false at `http://127.0.0.1:18787`. Which features that flag gates in 2.1.294 was not traced. The advisor and its cache work through the gateway (see the measurements above).

## Health

`GET http://127.0.0.1:18787/__gateway/health` answers one line:

```text
ok pid=4242 fds=212/10240 capture=off settings=ok db=~/.local/state/gpt-gateway/gateway.db db_rows=1830 db_drops=0 db_errors=0 middleware=[model-note errors=0]
```

`fds` is the process's open file handles over its soft limit (see [File handles](#file-handles)). `settings` is `ok`, `missing` (no file; defaults apply) or `error(...)` (the last edit was refused; the last good settings serve). `db` is the database's path (expanded on the real line), `db_rows` the rows this process wrote, `db_drops` the rows it dropped on a full queue and `db_errors` the rows lost to a database failure. Each middleware shows its `NAME`, its `health()` text when it has one (`health-error` when that raises), and its error count. With `chatgpt` enabled, `chatgpt=on catalog=N models` (or `catalog=none`) and `cache_retention=off|24h|24h(rejected)` appear before `middleware=`; with it disabled the line stays as shown.

## File handles

Every client connection and every upstream connection is a file handle, and a process short of them fails in ways that read as other faults: macOS answers a name lookup with `EAI_NONAME` when the resolver cannot open its socket, so the gateway once answered a storm with 502s blaming DNS while the open count sat at its limit of 256.

- **The limit.** `serve` raises the soft `RLIMIT_NOFILE` to 10240, or to the hard limit when that is lower, before it opens a socket; a soft limit already at or above that is left alone. A refusal is an `fd_limit` event and a start-up line in the text log, and the gateway starts anyway. The health line's `fds=` shows the open count over the soft limit.
- **Idle clients.** A client connection that sends nothing for `CLIENT_IDLE_SECONDS` (600, twice the five-minute prompt-cache TTL; `gpt_gateway/proxy.py`) is closed by the gateway. A request being read, forwarded or streamed is never idle, however long it runs. The gateway sends no `Keep-Alive: timeout=` header: a client that learns the limit would close first, but the header is one the upstream did not send, and the Anthropic route relays the response's headers as received. A request that crosses the close fails as a dropped connection, which Claude Code's SDK retries on its own.
- **Upstream connections.** They are pooled across clients, not held one per client: a request takes an idle connection or opens one, and gives it back the moment its response body is complete; a response cut short, by an error or by the client leaving before the body was read to its end, closes its connection, while a body read completely goes back to the pool even when the client has already gone. At most 16 stay idle (`MAX_IDLE`), and a reaper closes those idle for more than 90 s (`IDLE_SECONDS`), so open handles follow the requests in flight plus that reserve, never the number of connected chats. The ChatGPT WebSocket pool keeps its own caps.
- **Backlog.** The listen backlog is 256 (`REQUEST_QUEUE_SIZE`), so a burst of simultaneous connects is queued, not reset.
- **Running out.** A failure that is the process out of handles (`EMFILE`, `ENFILE`, or a name lookup that fails within 16 handles of the limit) is answered with `out of file handles: N open of L allowed`, not as an unreachable upstream, and recorded as an `fd_exhausted` event with `where` (`upstream`, `websocket` or `accept`), `error`, `open`, `limit` and `suppressed`, at most one per 30 s per kind, `suppressed` counting those held back. An accept that fails for any other reason is an `accept_failed` event, on the same limit. Accept failures in a row wait 0.1 s, doubling to 2 s (`ACCEPT_BACKOFF`, `ACCEPT_BACKOFF_MAX`), and the first success resets the wait, so a listener that cannot accept never spins. Capture running out of handles is an `fd_exhausted` event with `where` `capture` on the same limit, never an error past capture's boundary.

## Fail-open

`run.sh` supervises the gateway under launchd or a systemd user service and owns `env.ANTHROPIC_BASE_URL` in Claude Code's `settings.json`, and `env.ENABLE_TOOL_SEARCH=true` beside it (Claude Code turns tool search off behind any base URL that is not Anthropic's; the gateway forwards `defer_loading` and `tool_reference` intact). Every Claude Code session reading that settings file is routed through the gateway; it is not scoped to one chat. Set `GPT_GATEWAY_CLAUDE_SETTINGS` when installing to point at another settings file.

The supervisor sets these values only once the gateway answers its health check, and removes them when Python is missing, when the gateway cannot start (bad gateway settings, rejected middleware, a taken port), when the gateway exits, and when the service stops. An absent settings file is already unlinked. A failed read or rewrite is logged and retried; on stop the retries run alongside the gateway drain and end one second before `GPT_GATEWAY_STOP_SECONDS` (default 630), so they never postpone the drain. ctl and uninstall report `could not read settings` when an existing file cannot be read; ctl only waits for the supervisor to unlink. A URL other than the gateway's is never touched, and a user-selected `ENABLE_TOOL_SEARCH` other than `"true"` is kept; `"true"` reads as the supervisor's value and leaves with the URL. On Linux, the supervisor holds a `SO_REUSEPORT` listening socket and passes that same descriptor to each gateway. HUP starts a successor on the shared accept queue before stopping the predecessor, so closing the old gateway leaves queued connections available to the successor. Accepted connections with request data and in-flight streams drain on their original gateway, up to the drain limit. Accepted clients that have sent no request data close as idle clients at drain start. On a failed start or the last gateway exit, the supervisor closes its listener before cleanup or a retry sleep, so new connections are refused. macOS retains its separate-listener handover.

## Capture

Off by default. With `capture_dir` (or `GPT_GATEWAY_CAPTURE_DIR`) naming a folder, each exchange lands in `{capture_dir}/{date}/{HHMMSS.mmm}-{seq}-{kind}-{METHOD}-{path}/`:

- `1-request.json`: method, path and headers, secrets redacted
- `2-request.body`: the body as the client sent it
- `3-request.sent.body`: the body as sent upstream, when middleware changed it or the ChatGPT route translated it
- `4-response.json`: status and headers
- `5-response.body`: the literal bytes streamed back

Capture never breaks a request: a write that fails is a `capture_error` event and capture stops for that exchange.

## ChatGPT route

The optional ChatGPT route runs GPT models as Claude Code sub-agents over your ChatGPT subscription. It translates Claude Code's Anthropic requests into Codex backend Responses requests and translates the results back, including tools, reasoning signatures and usage. Text, base64 images and base64 PDFs reach the model, in a message or a tool result; a `tool_reference` becomes the line `Tool loaded: {name}`; any other content block becomes an `[unsupported {type}]` placeholder. With Claude Code's tool search on, a model served as Responses lite (sol, terra, luna and astra, unless the catalog says otherwise) gets a `defer_loading` tool only once a tool result names it, as one `additional_tools` item right after that result, so the list ahead of it, and the prompt cache, never change; any other model gets every `defer_loading` tool in its `tools` list. A tool Claude Code connects mid-conversation, a role-system `tool_addition` block, reaches a Responses-lite model the same way, as one `additional_tools` item where the block sits, and any other model in its `tools` list. Claude Code keeps running the tools and owning compaction. The main session remains on Claude.

Enable it with `"chatgpt": true` in `settings.json`, or `GPT_GATEWAY_CHATGPT=true` in the gateway's environment, then restart. The setting defaults to false; env overrides file, then default. Invalid env values are rejected with the variable's name. With the setting false, every model follows the existing Anthropic path and the ChatGPT modules are not loaded; ChatGPT-specific knobs are validated at gateway start only when it is enabled.

Codex's login is used in place first, from `$CODEX_HOME/auth.json` when set, otherwise `~/.codex/auth.json`. Use `gateway.py chatgpt-login` only without a Codex ChatGPT login, through the installed entry point:

```sh
/usr/bin/python3 -I ~/.local/share/gpt-gateway/gateway.py chatgpt-login
/usr/bin/python3 -I ~/.local/share/gpt-gateway/gateway.py chatgpt-login --device
/usr/bin/python3 -I ~/.local/share/gpt-gateway/gateway.py chatgpt-status
```

The gateway re-reads Codex's login before every refresh, refreshes on Codex's schedule (within five minutes of access expiry, or after eight days when only `last_refresh` is available), and reuses tokens that Codex has already rotated. It writes rotated tokens back in Codex's format, preserves other fields and symlinks, and never marks, blanks or deletes Codex's login on failure.

`gateway.py chatgpt-login` uses browser login; `--device` uses device-code login. The gateway's own tokens live in `~/.local/state/gpt-gateway/chatgpt-auth.json`, mode 0600; the macOS Keychain service `gpt-gateway.chatgpt` is used only when explicitly selected. `gateway.py chatgpt-status` reports the active login source. A later `codex login` or `codex logout` takes effect without restarting the gateway.

For a Claude Code session connected to the enabled gateway, an agent definition at `.claude/agents/gpt-worker.md` can contain:

```markdown
---
name: gpt-worker
description: Complete a scoped task — use for a delegated implementation. Returns changes and test results.
model: gpt-6.1-sol
---
Implement the assigned change and run its affected tests. Report changes, test results and remaining limitations.
```

Spawn it by name, for example `Agent(subagent_type: "gpt-worker", description: "Implement the change", prompt: "...")`, with no `model` argument. A spawn-time model override replaces the definition's GPT model. Use this definition only in sessions connected to the enabled gateway: without that route its GPT request goes to Anthropic.

### Routing and failures

Only POST requests to `/v1/messages` or `/v1/messages/count_tokens` (ignoring the query string) whose `model` is a string not starting with `claude-` are candidates. All other requests retain the Anthropic path. A candidate's trailing `[1m]`, then trailing `-fast`, are stripped for catalog lookup; `-fast` selects the priority service tier.

| Candidate | Result |
| --- | --- |
| Catalog slug with `x-claude-code-agent-id` | ChatGPT route; the client-facing shape stays Anthropic |
| Catalog slug without that header | 400 `invalid_request_error`: `chatgpt upstream: GPT models run as sub-agents only` |
| Unknown model | 400 `invalid_request_error`: `chatgpt upstream: unknown model "{id}"; the ChatGPT catalog has: {slugs, comma-separated}` |
| No catalog copy | 503 `api_error`: `chatgpt upstream: no model catalog ({cause})`; 403 login required (below) when the cause is a missing login |

The catalog loads from disk at start, without network access. A candidate request refreshes a copy older than 300 seconds; refreshes share a lock and have a 10-second timeout. A failed refresh keeps the last good copy and waits 60 seconds before another attempt. Claude requests never wait on the catalog. Each session/sub-agent pair has its own lane key for cache affinity and token estimates. Claude Code's activity-summary fork, a request whose last user message's own text opens with `Describe your most recent action in 3-5 words` (the system messages Claude Code appends after it, such as task notifications and hook feedback, skipped), never reaches the route while `status_label` answers it (§ Shipped middleware: listed, with `answer` at `always` or the request marked); otherwise the fork sends the effort its lane's last other request sent, or its own when the lane has sent none, because the backend's prompt cache keys on reasoning effort and Claude Code sends the fork at the agent's declared effort even when its turns run at another; a count-tokens estimate never moves that effort. Claude Code's compaction request, whose last user message's own text opens with `CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.`, sends that same lane effort and never moves it, because Claude Code sends every compaction at `high` whatever effort the agent runs at. Within one agent turn, from a user message without a tool result through the requests that answer its tool calls, trailing system messages never starting one, the lane replays the backend's `x-codex-turn-state` token as Codex does: it keeps the first token the turn's responses issue, sends it on every later request of that turn, retries included, and never into the next turn, another lane or the activity-summary fork. A request names a reasoning summary only when the catalog's `default_reasoning_summary` for the model is not `none`, as Codex does; reasoning replay never depends on it. The count-tokens endpoint estimates locally after translation, each image or PDF at a flat 2000 tokens, without a Responses call or credentials for that estimate; routing still needs a catalog copy.

Before output begins, retryable failures get at most four retries after the initial attempt, with exponential backoff unless the server advised a delay, as Codex waits on it: a `retry-after-ms` or `Retry-After` header (seconds or an HTTP date), or, for a stream's `rate_limit_exceeded` or `slow_down` failure, the "try again in Xs" its message names. That stream failure is a 429 `rate_limit_error` the route retries, as Codex always does; advice longer than 60 seconds is never slept in the gateway, so the request ends there, and every final answer that had advice carries it to Claude Code as `retry-after` (whole seconds, rounded up) and `retry-after-ms`; a plain upstream 429 goes to Claude Code at once, with the advice when there is one. A 401 triggers one token refresh per request. Rejected encrypted reasoning triggers one replay without the route's signed thinking blocks, recorded as a `drop_reasoning` event. A 400 or 422 (or a stream error) naming `prompt_cache_retention` triggers one replay without it (see [Cache retention](#cache-retention)). After output begins, failures are translated into stream errors. A missing login or final 401 becomes 403 `permission_error`: `chatgpt upstream: ChatGPT login required: run codex login, or gateway.py chatgpt-login`, with `x-should-retry: false`; it never becomes a client-facing 401. A transient auth failure becomes 503 `api_error` with `chatgpt upstream: {message}`. Final upstream decisions retain their mapped status, JSON error and retry headers: quota exhaustion is 429, a rejected prompt or policy refusal 400 (never retried, as in Codex), context overflow 413 `request_too_large` with a message naming the context window, which Claude Code answers by compacting, overload 529, and unavailable subscription usage 403.

Non-streaming requests drain the translated stream and return one Anthropic JSON response; an accumulated stream error returns 502. Middleware sees the Anthropic body in `on_request`, translated Anthropic events in `on_event`, and the client-facing result in `on_response_end`; `ctx.route` identifies the route and hooks cannot change the model. The middleware harness accepts `run(..., route="chatgpt")` to test this context.

### Identity and knobs

The gateway identifies itself to ChatGPT and OAuth as the Codex CLI: `originator: codex_cli_rs`, `version: 0.162.1`, and `User-Agent: codex_cli_rs/0.162.1 ({os_type} {os_version}; {arch}) unknown`. OS type is `Mac OS` on macOS, otherwise the platform system name; version and architecture come from the platform. The terminal token `unknown` is Codex's fallback when no terminal is detected. Responses requests also send `x-openai-subagent: collab_spawn`. It builds the ChatGPT headers from scratch using its own access token and account id; Claude Code's `authorization` and `x-api-key` never leave the process on this route. Every request carries its full input, as Codex's do after a failure; Responses-Lite is selected from the catalog; the transport is the Responses WebSocket (below).

| Variable | Meaning | Default |
| --- | --- | --- |
| `GPT_GATEWAY_CHATGPT_ORIGIN` | ChatGPT backend origin | `https://chatgpt.com` |
| `GPT_GATEWAY_CHATGPT_AUTH_ORIGIN` | OAuth origin | `https://auth.openai.com` |
| `GPT_GATEWAY_CHATGPT_AUTH_STORE` | `auto` uses the 0600 file store on every platform; `file` selects it explicitly; `keychain` fails if unavailable | `auto` |
| `GPT_GATEWAY_CHATGPT_CODEX_AUTH` | Codex login file, absolute with `~` allowed, or `off` | `$CODEX_HOME/auth.json`, else `~/.codex/auth.json` |
| `GPT_GATEWAY_CHATGPT_AUTH_PATH` | token file path, absolute with `~` allowed | `~/.local/state/gpt-gateway/chatgpt-auth.json` |
| `GPT_GATEWAY_CHATGPT_MODELS_PATH` | catalog cache path, absolute with `~` allowed; mode 0600 | `~/.local/state/gpt-gateway/chatgpt-models.json` |
| `GPT_GATEWAY_CHATGPT_TRANSPORT` | `websocket` keeps a Responses WebSocket per lane, as Codex does; `http` sends every request as an HTTP POST with an SSE answer | `websocket` |
| `GPT_GATEWAY_CHATGPT_AGENT_CONTRACT` | `on` appends Claude Code's sub-agent delivery contract to the developer message carrying its system text, as a second part: the final message is the return, and SendMessage is for what the instructions require or only the recipient can settle, never progress; GPT models do not infer it from Claude Code's prompt and message the agent that spawned them instead. `off` sends the system text alone | `on` |

### Transport

Responses requests go over the Responses WebSocket, as Codex sends them. A lane opens a socket at `/backend-api/codex/responses` on the backend origin with the HTTP request's headers plus `OpenAI-Beta: responses_websockets=2026-02-06` and `thread-id`, sends each request as one `response.create` text message (the translated Responses body after a first `"type":"response.create"` key, the turn's `x-codex-turn-state` token in `client_metadata`), reads the events the SSE stream would carry, and keeps the socket for its next request: the backend serves a kept socket's request from the prompt cache the previous one wrote far more reliably than a fresh HTTP request. One request is in flight per socket, and a lane keeps one socket: a second lands on another backend server, whose cache holds only what that socket sent. Claude Code's activity-summary fork runs while its agent's request still does, and the agent's next request can arrive while the fork runs, so a request whose lane's socket is busy waits for it, up to 30 seconds, or 600 for the fork, which only labels its agent's last action and can sit behind a long generation, and opens another only once that wait runs out; a socket coming back goes to the pool as its lane's one idle socket, and a spare the lane already holds there closes (`reason=lane-spare`). A socket goes back to its lane after a completed or incomplete response and closes on any other end; at 55 minutes of age, under the backend's 60-minute limit, it takes no new request; at most 64 idle sockets stay open, the oldest closing first; a socket authenticated as another login, or opened with the other Responses-Lite header, never serves the lane. An idle socket the backend closed, a kept socket that fails before its first message, and the backend's `websocket_connection_limit_reached` or `previous_response_not_found` answer send the request again at once on a fresh socket, as `stale` below. A refusal the backend wraps in an `error` event with a status is that status's answer, as the HTTP error would be, and a token a `response.metadata` event issues is kept as an issued header is. An upgrade answered with 426, or with another status meaning the endpoint serves no WebSocket (2xx, 3xx, 400, 404, 405), pauses the WebSocket for an hour; five transport failures in a row (no upgrade answer, a reset or a protocol error mid-response; a completed response resets the count) pause it for ten minutes. HTTP serves while it is paused. The backend keeps a socket's last completed response in memory, the one response a later request on that socket can continue (`store` is false), as Codex continues it: a request whose fields other than its input equal that response's request, and whose input is that request's input and then the response's output items, sends only the items after them, with the response's id as `previous_response_id`. Claude Code returns each output item translated back, so an output item matches by what survives the round trip: a reasoning item by its encrypted content, a call by its id, name and parsed arguments, a message by its role and text, any other item but for its id and status. Any other request goes whole, and a socket whose exchange ended without a completed response holds nothing to continue; the activity-summary fork's response is the socket's last, so the agent's request after it goes whole. Socket events, each with its fields:

```text
websocket_open     lane, handshake_ms
websocket_close    lane, reason={age|closed-while-idle|keeper-closed|keeper-age|stopped|lane-spare|pool-full|paused|abort|ended-{type}|ended-early}, age_s, exchanges
websocket_paused   seconds, reason: HTTP serves until then
lane_wait          lane, wait_s: the wait ran out and another socket opens
websocket_skipped  bytes: a message that is no JSON event
```

`abort` means the route or the client ended the exchange before its terminal event; `keeper-closed` and `keeper-age` are the [keeper](#keeper)'s findings on an idle socket (dead, or past 55 minutes); `stopped` is the gateway ending; `ended-{type}` names the terminal event that closed the socket; `ended-early`, a socket that stopped before any.

### Keeper

Nothing reads a pooled socket between two exchanges, and measured on the live route the backend closed idle sockets (every one at 101 s or more idle) whose conversation the next request then re-sent whole on a new socket, losing the prompt cache the old socket held. A keeper thread (`gpt_gateway/chatgpt/keeper.py`) runs while any socket idles: every `TICK_SECONDS` (5) it answers what the backend sent (a ping with a pong, a close), sends a ping of its own to a socket that has gone `PING_SECONDS` (20) without a frame from the gateway, and takes a socket it finds dead out of the pool as a `websocket_close` of reason `keeper-closed` (`keeper-age` at the 55-minute limit), so the next request opens a fresh socket at once instead of finding the dead one at checkout. It works on a socket only while it is in the pool and holds the socket's guard, which a checkout waits on, so it never touches a socket that is checked out and never reads one with a request. A failure of its own is a `keeper_failed` event (rate-limited), never a failed request. The 20-second ping is a fifth of the shortest observed death, and well under the 30 s idle gap (p99) the backend let pass unserviced; whether the backend ends sockets for missed pongs or on a timer of its own is unknown, so the keeper does both: it answers, and it pings.

### Cache retention

`chatgpt_cache_retention` (settings key, `off` by default) set to `24h` adds `prompt_cache_retention: "24h"` to each Responses request after `prompt_cache_key`; it changes neither the key nor the delta logic, but a request's fields other than its input are part of the continuation check, so switching the setting makes one full re-send per socket. If the backend refuses the parameter (a 400 or 422 naming it), the gateway retries that request once without it, records one `retention_rejected` event (`request_id`, `lane`, `slug`, `retention`) and sends it no more, whatever the number of requests in flight, until the settings file changes (any edit, a request in between or none) or the gateway restarts; the health line shows `cache_retention=24h(rejected)`. The refusal's `retry` event carries `action` `drop_retention`. Compare `requests.cache_read_tokens` and `hit` for ChatGPT rows with the setting on and off, over equal windows, and the `events` rows of kind `websocket_close` and `retention_rejected`.

### Rows, events, stats and capture

A ChatGPT request's row in the [database](#database) carries `route` `chatgpt`, `kind` `sub`, its `lane` and `slug`, and, from the attempt that answered, the backend's usage and the response's timing: `input_tokens` (every input token, cached included), `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens` (`usage.output_tokens_details.reasoning_tokens` of the Responses `response.completed`, NULL when upstream reports none), `hit`, `attempts`, `ttfb_ms`, `upstream_ms`, `total_ms`, `reused`, `stale`, `turn_state`, `transport`, `delta`, `translation_notes` and `empty`. A streamed request whose client leaves mid-stream (`client_disconnected` 1) keeps the usage the upstream had already reported; the gateway never reads on to get more, so a reply cut off earlier has NULL tokens, never zeros. Each upstream error or retry decision is a `retry` event of its request and lane, the last one `final_error`, with `slug`, `status` (a number, or `transport`), `attempt`, `action` (`retry`, `refresh`, `drop_reasoning`, `drop_retention` or `final`), `retry_after` (the server's advised delay in seconds, or null) and `error_head`, at most the first 300 bytes of its error body: for a failure inside a stream, the failing event's type, status and error, never the stream's opening bytes or the response it carries. None of these carries a token, an account id or a request body.

`ttfb_ms` runs from the gateway's receipt of the client request to the first upstream body byte of the attempt that answered, `upstream_ms` from that attempt's upstream request sent to the upstream terminal event, and `total_ms` from receipt to the last byte written to the client (for a non-stream request, to the hand-off of the response to the relay). `transport` names the transport of the attempt that answered: `ws` over the lane's WebSocket, `http` as a POST. `delta`, over the WebSocket only, is K/N when the request continued its socket's last completed response, sending K of its N input items, and otherwise `full:` and why it went whole: `none`, the socket holds no completed response; `fields`, a field outside the input differs from that response's request; `prefix`, the input does not open with that request's input; `output`, the items after it are not the response's output. `reused` 1 means the attempt that answered rode a kept-alive connection, over the WebSocket a socket kept from an earlier exchange; a kept-alive connection idle more than 90 s is closed, so the attempt after an idle spell opens a fresh one (`reused` 0). Every upstream socket, on either route, runs TCP keepalive: after 15 s without traffic the kernel probes the peer every 5 s and fails the socket after 4 unanswered probes, so a path that died without a FIN or RST (a Wi-Fi roam, a dropped NAT mapping) ends its read in about 35 s, a transport failure the route retries before output like any other; a peer that is only slow, a model reasoning before its first token, still answers the probes. `turn_state` `sent` means the answering attempt carried its turn's `x-codex-turn-state` token, and `issued` that its response issued the token the turn now keeps; it is NULL when neither happened. `translation_notes` is set only when the translation changed or dropped something, a JSON list of short strings such as `"dropped server tool: web_search_20250305"` or `"activity summary at low"`, kept whole: past 600 characters its last entry counts the notes left out (`"12 more"`). `empty` is 1 only when the response completed with no text, tool call or signed reasoning: the model chose to say nothing, as one woken by a notice it has nothing to add to does. The gateway answers it as the Messages API answers such a turn, `end_turn` with no content, never retried, because a retry regenerates the same nothing over the whole context; Claude Code then asks the agent once for visible output and otherwise ends the turn, and an agent's result falls back to its last message with text. `stale` 1 means a kept-alive connection failed before any body byte (`IncompleteRead` with 0 bytes, `RemoteDisconnected`, `ConnectionResetError` or `BrokenPipeError`) and the request went again at once on a fresh connection (over the WebSocket, any of the socket cases under Transport): that retry has no backoff, no `retry` event and no share of the retry budget, and a failure on a fresh connection, or a second failure, is an ordinary attempt.

`gateway.py chatgpt-stats [--since 30m|24h|7d]` reads the database's ChatGPT rows that carry the backend's usage, all of them or those started within the window (`{N}m`, `{N}h` or `{N}d`). It prints lanes sorted by key as `{lane} responses={n} input={sum} cached={sum} hit={cached/input:.3f}`, then the same totals prefixed by `total`. One more line per slug follows for the rows with timings, `slug={slug} timed={n} ttfb_ms p50={ms} p95={ms} total_ms p50={ms} p95={ms}` (nearest-rank percentiles). Then one line per model for the Claude route's timed rows in the same window, `anthropic model={model} timed={n} ttfb_ms p50={ms} p95={ms} upstream_ms p50={ms} p95={ms} total_ms p50={ms} p95={ms}`, also when there are no ChatGPT rows. A missing database names its path and exits 1; no ChatGPT rows prints `no gpt responses in {path}`, plus ` in the last {window}` with a window, and exits 0.

When capture is enabled, the folder kind is `gpt`, while `ctx.kind` stays `sub`. `3-request.sent.body` contains the translated Responses bytes of the attempt that answered (over the WebSocket, the message sent is those bytes after a first `"type":"response.create"` key, with any turn-state token in `client_metadata`); the response capture contains the client-facing Anthropic translation. Capture includes conversation bodies; the database never does. Third-party licence texts and attribution ship in `THIRD_PARTY_NOTICES.md`.

## Database

The gateway records what it observes in one SQLite file, `GPT_GATEWAY_DB` (default `~/.local/state/gpt-gateway/gateway.db`, mode 0600, WAL), written by one thread off a queue of 10,000 rows, so no request waits on it. A full queue drops the row; a database failure loses the rows of that write; both are counted on the health line and noted once in the text log, and the gateway goes on. Rows older than `db_retention_days` (default 30) are deleted at start and hourly. No request or response body enters it.

- `requests`: one row per client request on both routes: `started` and `ended` (epoch seconds), `pid`, `request_id` (counted per process), `route`, `kind`, `method`, `path`, `model`, `slug`, `session_id` (from the body's `metadata.user_id`), `agent_id`, `lane`, `status`, `answered_by`, `stream`, `input_tokens` (every input token: uncached, cache read and cache write), `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `hit` (cache read over input), `delta`, `attempts`, `ttfb_ms`, `upstream_ms`, `total_ms`, `reused`, `stale`, `transport`, `turn_state`, `translation_notes`, `empty`, `completed`, `client_disconnected`, `error_code`, `error_head` (at most 600 bytes of an error reply) and `note` (the middleware notes). `error_code` is the error reply's `error.type`, `stream_error` when the relay broke (its `error_head` the exception), or `gateway_error` when the exchange broke before its row (the same). On the Anthropic route `ttfb_ms` runs from the gateway's receipt of the request to the first upstream body byte and `upstream_ms` from the request sent upstream to the upstream's last byte (NULL when the client left first, and on a request that reached no upstream), both read from the clock beside the relay without delaying a chunk. The usage of a `POST /v1/messages` comes from the response's `message_start` and `message_delta` usage, or a JSON reply's `usage`, read beside the relay without delaying a chunk.
- `events`: everything else, one row each with `at`, `pid`, `kind`, `request_id`, `lane` and its JSON `fields`: `start`, `stop`, `settings_reload`, `settings_error`, `middleware_failed`, `middleware_log`, `event_sink`, `capture_error`, `client_error`, `debug`, `error_detail` (an error reply's request shape and rate-limit headers), `usage_unread` (a messages reply whose usage could not be read), `upstream_socket`, `catalog_refresh_failed`, `retry`, `final_error`, `drop_reasoning`, `websocket_open`, `websocket_close`, `websocket_paused`, `lane_wait`, `websocket_skipped`, `keeper_failed`, `retention_rejected`, `fd_limit` (the open-file limit could not be raised), `fd_exhausted` (a failure that was the process out of file handles) and `accept_failed` (any other failed accept).
- `cache_misses`: the requests with at least 4096 input tokens and a hit below 0.5.
- `daily`: per local day, route and model, the request count, the token sums and the hit.

## Operating it

```sh
~/.local/share/gpt-gateway/ctl.sh start|stop|restart|status
sqlite3 -readonly ~/.local/state/gpt-gateway/gateway.db 'SELECT * FROM daily ORDER BY day DESC LIMIT 10'
```

`start` (`on`) enables it and waits until Claude Code uses it; `stop` (`off`) disables it, points Claude Code at the API at once and lets in-flight streams finish; `restart` takes up new code and a changed middleware list without cutting a stream; `status` prints the on/off state, health and where Claude Code points. The database holds a row per request and an event for everything else (§ Database); the text log keeps the start, stop, fatal, settings-error and database-failure lines. Supervisor stderr is `gpt-gateway.stderr.log` in the platform's log directory.

## Development

`scripts/gate.sh` from the repository root is the only definition of ready: it compiles every source, lints the shell, runs `scripts/checks.sh` and the unit tests. The layout and its gates are in `docs/design/gpt-gateway-layout.md`.
