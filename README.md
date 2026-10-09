# model-gateway

A local passthrough between Claude Code and the Anthropic API, with a middleware hook API.

Claude Code reaches it through `ANTHROPIC_BASE_URL=http://127.0.0.1:18787`. Every request goes to `https://api.anthropic.com` byte for byte, and every response streams back chunk by chunk as it arrives. Your login (`Authorization`, `x-api-key`) passes through and is never logged or captured. Middleware you install can watch requests and responses, add a note to the log line, and edit a request body; with no middleware the gateway changes nothing.

The supervisor points Claude Code at the gateway only while the gateway answers, and removes that setting whenever it is down, so a broken gateway never cuts Claude Code off (§ Fail-open).

macOS, Python 3.9+ standard library only (`/usr/bin/python3`), `jq` for the settings edits.

## Install, update, remove

```sh
launchd/install.sh            # install or update; --port N (default 18787)
~/.local/share/model-gateway/ctl.sh status
launchd/uninstall.sh          # --purge also deletes the logs
```

`install.sh` runs the unit tests first and refuses to install when one fails. It copies `gateway.py`, the `model_gateway/` package, `run.sh`, `ctl.sh`, this README and the LICENSE to `~/.local/share/model-gateway/`, creates a `middleware` directory and, on a first install, `settings.json` there, and writes the launchd agent `local.model-gateway`. A first install turns the gateway on. An update keeps the on/off state: when on, the new code takes over through a graceful restart and no stream is cut. An existing `settings.json` is validated and kept.

`uninstall.sh` stops the agent (in-flight streams finish first), points Claude Code back at the API, deletes the agent and the installed copy, and keeps `settings.json` and the `middleware` directory under `~/.local/state/model-gateway/backups/`.

## Settings

`settings.json` in the install directory. Every key is optional; an unknown key is refused.

```json
{"capture_dir": null, "middleware": [{"module": "model_note", "settings": {"enabled": true}}], "middleware_dir": null}
```

| Key | Value | Default | Env override |
| --- | --- | --- | --- |
| `capture_dir` | an absolute directory (`~` allowed), or null for off | null | `MODEL_GATEWAY_CAPTURE_DIR` (`off` turns it off) |
| `middleware` | the ordered list of middleware, each `{"module": NAME, "settings": {...}}`; `module` matches `^[a-z][a-z0-9_]*$` and appears once | `[]` | none |
| `middleware_dir` | an absolute directory, or null | a `middleware` directory beside `settings.json` | `MODEL_GATEWAY_MIDDLEWARE_DIR` |

An environment variable that is set and not empty wins over the file, which wins over the default. An edit applies from the next request: the file is re-read whenever its modification time, size or inode changes. A file that fails validation stops the gateway at start (and `run.sh` fails open); at a reload it is logged once and the last good settings stay, and the health line says `settings=error(...)`. Two edits need a restart (`ctl.sh restart`) instead: a change to the middleware list (added, removed or reordered) and a change of `middleware_dir`; until then the reload reports "middleware list changed; restart to apply" and the running set stays.

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

`req.body` is the parsed JSON object, or `None` when the body is not one; `req.raw` is the bytes as sent. A hook that edits `req.body` calls `req.mark_changed()`. After every `on_request` hook has run, the gateway serializes the body once, as `json.dumps(body, separators=(",", ":")).encode()`, only if some hook marked a change; otherwise the original bytes go upstream unchanged.

`ctx` is one object per request, read-only: `kind` (`main`, or `sub` for a request carrying `x-claude-code-agent-id`), `route` (always `"anthropic"`; reserved), `method`, `path`, `headers` (case-insensitive, without `authorization`, `x-api-key`, `proxy-authorization` and `cookie`), `model` (the original body's), `settings` (this module's validated snapshot, the same for the whole request), `state` (a dict this module keeps across its hooks for this request), `request_id`, `capture_dir` (this request's capture folder, or `None`), `log(line)` and `log_path`.

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

`settings` is `ok`, `missing` (no file; defaults apply) or `error(...)` (the last edit was refused; the last good settings serve). Each middleware shows its `NAME`, its `health()` text when it has one (`health-error` when that raises), and its error count.

## Fail-open

`run.sh` supervises the gateway under launchd and owns `env.ANTHROPIC_BASE_URL` in Claude Code's `settings.json`: it sets it only once the gateway answers its health check, and removes it when Python is missing, when the gateway cannot start (a bad `settings.json`, a middleware that fails to load, a taken port), when the gateway exits, and when the agent stops. A URL other than the gateway's is never touched. HUP starts a new gateway on the same port (`SO_REUSEPORT`) and, once it answers, lets the old one drain: no in-flight stream is cut.

## Capture

Off by default. With `capture_dir` (or `MODEL_GATEWAY_CAPTURE_DIR`) naming a folder, each exchange lands in `{capture_dir}/{date}/{HHMMSS.mmm}-{seq}-{kind}-{METHOD}-{path}/`:

- `1-request.json`: method, path and headers, secrets redacted
- `2-request.body`: the body as the client sent it
- `3-request.sent.body`: the body as sent upstream, only when a middleware changed it
- `4-response.json`: status and headers
- `5-response.body`: the literal bytes streamed back

Capture never breaks a request: a write that fails is logged and capture stops for that exchange.

## Operating it

```sh
~/.local/share/model-gateway/ctl.sh on|off|restart|status
tail -f ~/Library/Logs/model-gateway.log
```

`on` starts it and waits until Claude Code uses it; `off` points Claude Code at the API at once and lets in-flight streams finish; `restart` takes up new code and a changed middleware list without cutting a stream; `status` prints the on/off state, the health line and where Claude Code points. The log has one line per messages request (`POST /v1/messages -> 200 kind=main`, plus each middleware's note) and per error response (with the request's shape, rate-limit headers and the body's head); the supervisor logs to `~/Library/Logs/model-gateway.stderr.log`.

## Development

`scripts/gate.sh` from the repository root is the only definition of ready: it compiles every source, lints the shell, runs `scripts/checks.sh` and the unit tests. The layout and its gates are in `docs/design/model-gateway-layout.md`.
