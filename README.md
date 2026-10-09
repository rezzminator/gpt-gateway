# model-gateway

A local passthrough between Claude Code and the Anthropic API, with a middleware hook API.

Claude Code reaches it through `ANTHROPIC_BASE_URL=http://127.0.0.1:18787`. By default, every request goes to `https://api.anthropic.com` byte for byte, and every response streams back chunk by chunk as it arrives. Your login (`Authorization`, `x-api-key`) passes through and is never logged or captured. Middleware you install can watch requests and responses, add a note to the log line, and edit a request body; with no middleware the default route changes nothing. The optional [ChatGPT route](#chatgpt-route) runs GPT sub-agents over your ChatGPT subscription.

The supervisor points Claude Code at the gateway only while the gateway answers, and removes that setting whenever it is down, so a broken gateway never cuts Claude Code off (§ Fail-open).

macOS, Python 3.9+ standard library only (`/usr/bin/python3`), `jq` for the settings edits.

## Install, update, remove

```sh
launchd/install.sh            # install or update; --port N (default 18787)
~/.local/share/model-gateway/ctl.sh status
launchd/uninstall.sh          # --purge also deletes the logs
```

`install.sh` runs the unit tests first and refuses to install when one fails. It copies `gateway.py`, the `model_gateway/` package (all Python sources, preserving subdirectories), `run.sh`, `ctl.sh`, this README, the LICENSE and `THIRD_PARTY_NOTICES.md` to `~/.local/share/model-gateway/`, creates a `middleware` directory and, on a first install, `settings.json` there, and writes the launchd agent `local.model-gateway`. A first install turns the gateway on. An update keeps the on/off state: when on, the new code takes over through a graceful restart and no stream is cut. An existing `settings.json` is validated and kept.

`uninstall.sh` stops the agent (in-flight streams finish first), points Claude Code back at the API, deletes the agent and the installed copy, and keeps `settings.json` and the `middleware` directory under `~/.local/state/model-gateway/backups/`.

## Settings

`settings.json` in the install directory. Every key is optional; an unknown key is refused.

```json
{"capture_dir": null, "middleware": [{"module": "model_note", "settings": {"enabled": true}}], "middleware_dir": null}
```

| Key | Value | Default | Env override |
| --- | --- | --- | --- |
| `capture_dir` | an absolute directory (`~` allowed), or null for off | null | `MODEL_GATEWAY_CAPTURE_DIR` (`off` turns it off) |
| `chatgpt` | a JSON boolean, or null for the default | false | `MODEL_GATEWAY_CHATGPT` (`1`, `true`, `on` or `0`, `false`, `off`) |
| `middleware` | the ordered list of middleware, each `{"module": NAME, "settings": {...}}`; `module` matches `^[a-z][a-z0-9_]*$` and appears once | `[]` | none |
| `middleware_dir` | an absolute directory, or null | a `middleware` directory beside `settings.json` | `MODEL_GATEWAY_MIDDLEWARE_DIR` |

An environment variable that is set and not empty wins over the file, which wins over the default. An edit applies from the next request: the file is re-read whenever its modification time, size or inode changes. A file that fails validation stops the gateway at start (and `run.sh` fails open); at a reload it is logged once and the last good settings stay, and the health line says `settings=error(...)`. Changes to the middleware list (added, removed or reordered), `middleware_dir` or `chatgpt` need a restart (`ctl.sh restart`); until then the reload reports "restart to apply" and the running settings stay.

`gateway.py check [FILE]` validates a settings file and loads its middleware the way a start would; it exits 1 with the reason.

The environment alone sets the rest (the launchd agent sets them in its plist):

| Variable | Meaning | Default |
| --- | --- | --- |
| `MODEL_GATEWAY_PORT` | listen port on 127.0.0.1 | 18787 |
| `MODEL_GATEWAY_UPSTREAM` | upstream origin | `https://api.anthropic.com` |
| `MODEL_GATEWAY_LOG` | the log file, rotated to `.1` past 5 MiB | `~/Library/Logs/model-gateway.log` |
| `MODEL_GATEWAY_DRAIN_SECONDS` | on SIGTERM, how long in-flight requests may finish | 600 |
| `MODEL_GATEWAY_DEBUG` | set to log each messages request's non-secret headers and metadata | off |

## Middleware

A middleware is one Python file, `{middleware_dir}/{module}.py`, listed in `settings.json`. It never imports the gateway. Its code loads once, at start; `ctl.sh restart` reloads it. The smallest one, `examples/middleware/model_note.py`, adds ` model-note=<model>` to each request's log line:

```python
NAME = "model-note"

def enabled_value(value): ...   # bool from the file, 1/0, true/false, yes/no, on/off from the env

SETTINGS = {"enabled": (True, enabled_value, "MODEL_GATEWAY_MODEL_NOTE")}

def on_request(ctx, req):
    if ctx.settings["enabled"]:
        return " model-note=%s" % ctx.model
    return None
```

Install it with `cp examples/middleware/model_note.py ~/.local/share/model-gateway/middleware/`, list it as `{"module": "model_note"}`, then `ctl.sh restart`.

### Module attributes

Only `NAME` is required.

- `NAME`: the label in the health line, the log and the CLI; it matches `^[a-z][a-z0-9_-]*$` and is unique.
- `SETTINGS`: `{key: (default, validator, env_var_or_None)}`. `validator(value)` returns the value or raises `ValueError`; the gateway reports it naming the module, the key and the source (env, file or default). An env variable that is set and not empty wins over the entry's `settings`, which win over the default; env values reach the validator as strings. A `settings` key the module does not declare is refused.
- `check(settings)`: a whole-settings check (cross-key rules, files on disk); raises `ValueError`.
- `on_request(ctx, req)`: runs before the request goes upstream, for every proxied request except the health endpoint. A string it returns is appended verbatim to the request's log line.
- `on_event(ctx, event)`: once per complete SSE event of a streaming response, as `{"event": name, "data": parsed JSON or None, "raw": bytes}`; once for a non-streaming JSON response, with `"event": None`. Error responses included. The gateway splits events across chunk boundaries and never holds a chunk back from the client.
- `on_response_end(ctx, result)`: after the response; `result` has `status`, `reason`, `headers`, `error_head` (the first 600 bytes when status >= 400), `completed`, `client_disconnected`, `duration_s` and `note` (the assembled log note).
- `health(ctx)`: a string for the health line.
- `cli(argv, ctx)`: run by `gateway.py mw NAME ARGS...`, which exits with its return value.

Load errors (a missing file, an import error, a missing or duplicate `NAME`, a refused setting, a failing `check`) stop the start, logged with the module and the cause, and `gateway.py check` reports the same and exits 1.

### The request

`req.body` is the parsed JSON object, or `None` when the body is not one; `req.raw` is the bytes as sent. A hook that edits `req.body` calls `req.mark_changed()`. After every `on_request` hook has run, the gateway serializes the body once, as `json.dumps(body, separators=(",", ":")).encode()`, only if some hook marked a change. The Anthropic route otherwise forwards the original bytes unchanged; the ChatGPT route translates the resulting Anthropic body.

`ctx` is one object per request, read-only: `kind` (`main`, or `sub` for a request carrying `x-claude-code-agent-id`), `route` (`"anthropic"` or `"chatgpt"`), `method`, `path`, `headers` (case-insensitive, without `authorization`, `x-api-key`, `proxy-authorization` and `cookie`), `model` (the original body's), `settings` (this module's validated snapshot, the same for the whole request), `state` (a dict this module keeps across its hooks for this request), `request_id`, `capture_dir` (this request's capture folder, or `None`), `log(line)` and `log_path`.

**Determinism.** A body edit must be a pure function of the request and the settings snapshot: no clock, no randomness, no per-request state. Otherwise the request's prefix changes from one request to the next and the main model's prompt cache misses on every request.

### Order and failures

Hooks run in list order: `on_request` first to last, then `on_event` and `on_response_end` in the same order. A hook never breaks a request or cuts a stream:

- A hook that raises is logged as `middleware {NAME} {hook} failed: {exc!r} at {file}:{line} in {function}`, and its module's error count rises.
- When `on_request` raises, the request goes upstream as its original bytes, every module's edits and notes are discarded, and the later modules' `on_request` is skipped.
- A hook that changes `body["model"]` has failed the same way: routing is the gateway's.
- A failing `on_event` is skipped for the rest of that response; a failing `on_response_end` is skipped.

### Testing a middleware

`model_gateway/harness.py` runs one middleware file in process, with no network, through the gateway's own settings resolution, hook chain and SSE splitting:

```python
import sys
sys.path.insert(0, "path/to/model-gateway")
from model_gateway.harness import run, run_cli, load

out = run("my_mw.py", body={"model": "m", "messages": []}, headers={"x-claude-code-agent-id": "a1"},
          response=b'event: message_stop\ndata: {"type":"message_stop"}\n\n', chunk_size=7,
          settings={"enabled": True}, env={})
out.forwarded, out.changed, out.note, out.log, out.errors, out.events, out.result, out.health
code, stdout, stderr = run_cli("my_mw.py", ["stats"], log_path="gw.log")
```

`load()` raises `SettingsError` or `MiddlewareError` where a start would fail. `tests/examples/middleware/test_model_note.py` tests the example this way.

## Health

`GET http://127.0.0.1:18787/__gateway/health` answers one line:

```
ok pid=4242 capture=off settings=ok middleware=[model-note errors=0]
```

`settings` is `ok`, `missing` (no file; defaults apply) or `error(...)` (the last edit was refused; the last good settings serve). Each middleware shows its `NAME`, its `health()` text when it has one (`health-error` when that raises), and its error count. With `chatgpt` enabled, `chatgpt=on catalog=N models` (or `catalog=none`) appears before `middleware=`; with it disabled the line stays as shown.

## Fail-open

`run.sh` supervises the gateway under launchd and owns `env.ANTHROPIC_BASE_URL` in Claude Code's `settings.json`: it sets it only once the gateway answers its health check, and removes it when Python is missing, when the gateway cannot start (a bad `settings.json`, a middleware that fails to load, a taken port), when the gateway exits, and when the agent stops. A URL other than the gateway's is never touched. HUP starts a new gateway on the same port (`SO_REUSEPORT`) and, once it answers, lets the old one drain: no in-flight stream is cut.

## Capture

Off by default. With `capture_dir` (or `MODEL_GATEWAY_CAPTURE_DIR`) naming a folder, each exchange lands in `{capture_dir}/{date}/{HHMMSS.mmm}-{seq}-{kind}-{METHOD}-{path}/`:

- `1-request.json`: method, path and headers, secrets redacted
- `2-request.body`: the body as the client sent it
- `3-request.sent.body`: the body as sent upstream, when middleware changed it or the ChatGPT route translated it
- `4-response.json`: status and headers
- `5-response.body`: the literal bytes streamed back

Capture never breaks a request: a write that fails is logged and capture stops for that exchange.

## ChatGPT route

The optional ChatGPT route runs GPT models as Claude Code sub-agents over your ChatGPT subscription. It translates Claude Code's Anthropic requests into Codex backend Responses requests and translates the results back, including tools, reasoning signatures and usage. Claude Code keeps running the tools and owning compaction. The main session remains on Claude.

Enable it with `"chatgpt": true` in `settings.json`, or `MODEL_GATEWAY_CHATGPT=true` in the gateway's environment, then restart. The setting defaults to false; env overrides file, then default. Invalid env values are rejected with the variable's name. With the setting false, every model follows the existing Anthropic path and the ChatGPT modules are not loaded; ChatGPT-specific knobs are validated at gateway start only when it is enabled.

Codex's login is used in place first, from `$CODEX_HOME/auth.json` when set, otherwise `~/.codex/auth.json`. Use `gateway.py chatgpt-login` only without a Codex ChatGPT login, through the installed entry point:

```sh
/usr/bin/python3 -I ~/.local/share/model-gateway/gateway.py chatgpt-login
/usr/bin/python3 -I ~/.local/share/model-gateway/gateway.py chatgpt-login --device
/usr/bin/python3 -I ~/.local/share/model-gateway/gateway.py chatgpt-status
```

The gateway re-reads Codex's login before every refresh, refreshes on Codex's schedule (within five minutes of access expiry, or after eight days when only `last_refresh` is available), and reuses tokens that Codex has already rotated. It writes rotated tokens back in Codex's format, preserves other fields and symlinks, and never marks, blanks or deletes Codex's login on failure.

`gateway.py chatgpt-login` uses browser login; `--device` uses device-code login. The gateway's own tokens live in `~/.local/state/model-gateway/chatgpt-auth.json`, mode 0600; the macOS Keychain service `model-gateway.chatgpt` is used only when explicitly selected. `gateway.py chatgpt-status` reports the active login source. A later `codex login` or `codex logout` takes effect without restarting the gateway.

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

The catalog loads from disk at start, without network access. A candidate request refreshes a copy older than 300 seconds; refreshes share a lock and have a 10-second timeout. A failed refresh keeps the last good copy and waits 60 seconds before another attempt. Claude requests never wait on the catalog. Each session/sub-agent pair has its own lane key for cache affinity and token estimates. The count-tokens endpoint estimates locally after translation, without a Responses call or credentials for that estimate; routing still needs a catalog copy.

Before output begins, retryable failures get at most four retries after the initial attempt, with exponential backoff. A 401 triggers one token refresh per request. Rejected encrypted reasoning triggers one replay without the route's signed thinking blocks, logged as `gpt drop-reasoning`. After output begins, failures are translated into stream errors. A missing login or final 401 becomes 403 `permission_error`: `chatgpt upstream: ChatGPT login required: run codex login, or gateway.py chatgpt-login`, with `x-should-retry: false`; it never becomes a client-facing 401. A transient auth failure becomes 503 `api_error` with `chatgpt upstream: {message}`. Final upstream decisions retain their mapped status, JSON error and retry headers: quota exhaustion is 429, a rejected prompt or policy refusal 400 (never retried, as in Codex), context overflow 413, overload 529, and unavailable subscription usage 403.

Non-streaming requests drain the translated stream and return one Anthropic JSON response; an accumulated stream error returns 502. Middleware sees the Anthropic body in `on_request`, translated Anthropic events in `on_event`, and the client-facing result in `on_response_end`; `ctx.route` identifies the route and hooks cannot change the model. The middleware harness accepts `run(..., route="chatgpt")` to test this context.

### Identity and knobs

The gateway identifies itself to ChatGPT and OAuth as the Codex CLI: `originator: codex_cli_rs`, `version: 0.155.1`, and `User-Agent: codex_cli_rs/0.155.1 ({os_type} {os_version}; {arch}) unknown`. OS type is `Mac OS` on macOS, otherwise the platform system name; version and architecture come from the platform. The terminal token `unknown` is Codex's fallback when no terminal is detected. Responses requests also send `x-openai-subagent: collab_spawn`. It builds the ChatGPT headers from scratch using its own access token and account id; Claude Code's `authorization` and `x-api-key` never leave the process on this route. Transport is HTTP SSE with full input on every request; Responses-Lite is selected from the catalog.

| Variable | Meaning | Default |
| --- | --- | --- |
| `MODEL_GATEWAY_CHATGPT_ORIGIN` | ChatGPT backend origin | `https://chatgpt.com` |
| `MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN` | OAuth origin | `https://auth.openai.com` |
| `MODEL_GATEWAY_CHATGPT_AUTH_STORE` | `auto` uses the 0600 file store on every platform; `file` selects it explicitly; `keychain` fails if unavailable | `auto` |
| `MODEL_GATEWAY_CHATGPT_CODEX_AUTH` | Codex login file, absolute with `~` allowed, or `off` | `$CODEX_HOME/auth.json`, else `~/.codex/auth.json` |
| `MODEL_GATEWAY_CHATGPT_AUTH_PATH` | token file path, absolute with `~` allowed | `~/.local/state/model-gateway/chatgpt-auth.json` |
| `MODEL_GATEWAY_CHATGPT_MODELS_PATH` | catalog cache path, absolute with `~` allowed; mode 0600 | `~/.local/state/model-gateway/chatgpt-models.json` |

### Logs, stats and capture

The normal request line still carries `kind=sub`. Each completed ChatGPT response adds a line with total input and cached input tokens, output tokens and the response's timing; each upstream error or retry decision records its action and at most the first 300 bytes of its error body, as a repr. These lines contain no token, account id or request body:

```text
    gpt lane={lane} slug={slug} status={status} input={input} cached={cached} ratio={ratio:.3f} attempts={n} out={output} reasoning={reasoning|-} ttfb_ms={ms} upstream_ms={ms} total_ms={ms} reused={0|1} stale={0|1}
    gpt error lane={lane} slug={slug} status={status|transport} attempt={n} action={retry|refresh|drop_reasoning|final} body={body_head!r}
    gpt drop-reasoning lane={lane} slug={slug}
```

The fields after `attempts` come in this order and only ever grow at the end. `out` is the output tokens; `reasoning` is `usage.output_tokens_details.reasoning_tokens` of the Responses `response.completed`, `-` when upstream reports none. `ttfb_ms` runs from the gateway's receipt of the client request to the first upstream body byte of the attempt that answered, `upstream_ms` from that attempt's upstream request sent to the upstream terminal event, and `total_ms` from receipt to the last byte written to the client (for a non-stream request, to the hand-off of the response to the relay). `reused=1` means the attempt that answered rode a kept-alive connection. `stale=1` means a kept-alive connection failed before any body byte (`IncompleteRead` with 0 bytes, `RemoteDisconnected`, `ConnectionResetError` or `BrokenPipeError`) and the request went again at once on a fresh connection: that retry has no backoff, no `gpt error` line and no share of the retry budget, and a failure on a fresh connection, or a second failure, is an ordinary attempt.

`gateway.py chatgpt-stats [LOG...]` reads these completion lines, using the core log and, once it exists, its `.1` rotation by default. It prints lanes sorted by key as `{lane} responses={n} input={sum} cached={sum} hit={cached/input:.3f}`, then the same totals prefixed by `total`. When lines carry the timing fields, one more line per slug follows, `slug={slug} timed={n} ttfb_ms p50={ms} p95={ms} total_ms p50={ms} p95={ms}` (nearest-rank percentiles; lines from before the fields are counted above but not timed). An unreadable log names the file and exits 1; no matching lines prints `no gpt responses in {files}` and exits 0.

When capture is enabled, the folder kind is `gpt`, while `ctx.kind` stays `sub`. `3-request.sent.body` contains the translated Responses bytes of the attempt that answered; the response capture contains the client-facing Anthropic translation. Capture includes conversation bodies, unlike the aggregate `gpt lane=` log lines. Third-party licence texts and attribution ship in `THIRD_PARTY_NOTICES.md`.

## Operating it

```sh
~/.local/share/model-gateway/ctl.sh on|off|restart|status
tail -f ~/Library/Logs/model-gateway.log
```

`on` starts it and waits until Claude Code uses it; `off` points Claude Code at the API at once and lets in-flight streams finish; `restart` takes up new code and a changed middleware list without cutting a stream; `status` prints the on/off state, the health line and where Claude Code points. The log has one line per messages request (`POST /v1/messages -> 200 kind=main`, plus each middleware's note) and per error response (with the request's shape, rate-limit headers and the body's head); the supervisor logs to `~/Library/Logs/model-gateway.stderr.log`.

## Development

`scripts/gate.sh` from the repository root is the only definition of ready: it compiles every source, lints the shell, runs `scripts/checks.sh` and the unit tests. The layout and its gates are in `docs/design/model-gateway-layout.md`.
