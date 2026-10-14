# gpt-gateway — a local passthrough between Claude Code and the Anthropic API, with a middleware hook API and an opt-in ChatGPT route

On the Anthropic route the core forwards every request byte for byte and relays every response chunk as it arrives; the ChatGPT route, the one route built in and off unless `chatgpt` is true, translates GPT sub-agent requests to the ChatGPT Codex backend and back; every other behaviour lives in middleware the core loads, never in the core. The layout, glossary, façades and gates are fixed by `docs/design/gpt-gateway-layout.md`; read it before adding a file.

# Vocabulary

- gateway: the entry `gateway.py` and the package `gpt_gateway/`, one module per unit · `docs/design/gpt-gateway-layout.md` § Units of change
- middleware: one Python file `{middleware_dir}/{module}.py` listed in `settings.json` `middleware`, labelled by its `NAME` · loading `gpt_gateway/middleware.py`; the shipped modules, installed beside the gateway · `middleware/`
- the contract: the per-request objects `Context`, `Request`, `Result` and the hook chain · `gpt_gateway/hooks.py`; each module's `SETTINGS` and `check` · `gpt_gateway/settings.py`; documented in `README.md` § Middleware
- harness: the public middleware test harness, `run`, `run_cli`, `load` · `gpt_gateway/harness.py`
- example: the smallest middleware, the contract's executable documentation · `examples/middleware/model_note.py`
- kind: `main` or `sub` (a request carrying `x-claude-code-agent-id`); the request row's `kind` column; a ChatGPT-route capture folder carries `gpt` in its place
- ChatGPT route: GPT models as Claude Code sub-agents over the user's ChatGPT login, `ctx.route == "chatgpt"`, its `requests` rows and `retry`/`final_error` events, and `gateway.py chatgpt-stats` · `gpt_gateway/chatgpt/`, `README.md` § ChatGPT route; its login, Codex's `auth.json` first and the gateway's own store second · `gpt_gateway/chatgpt/codex_login.py`, `gpt_gateway/chatgpt/auth.py`; third-party licences · `THIRD_PARTY_NOTICES.md`
- capture: one folder per exchange, off by default · `gpt_gateway/capture.py`
- database: `~/.local/state/gpt-gateway/gateway.db` (`GPT_GATEWAY_DB`), tables `requests` and `events`, views `cache_misses` and `daily`, retention `db_retention_days`; never a request or response body · `gpt_gateway/db.py`, `README.md` § Database
- service: the portable supervisor and platform-dispatching `ctl.sh`, `install.sh`, `uninstall.sh`; flat install under `~/.local/share/gpt-gateway/`, package subdirectories preserved; macOS label `local.gpt-gateway`, Linux user unit `gpt-gateway.service` · `service/`
- gates: `scripts/checks.sh` (every countable law, one `CHECK` line each), `scripts/gate.sh` (the only definition of ready), committed baselines · `scripts/baselines/`
- test jail: scratch directories, ports, environments and scratch Git fixtures · `tests/jail.py`; fake upstreams and recording middleware · `tests/fakes.py`; stub launchctl, systemctl and macOS tools · `tests/service/stub.py`

# Runtime

- Python 3.9+, standard library only, run as `/usr/bin/python3 -I`; `-I` keeps the script directory off `sys.path`, so `gateway.py` inserts its own.
- The gate: `scripts/gate.sh` from this root — compile, shellcheck, checks, the whole suite under `env -i` with an empty `HOME`; its last line is `GATE PASS`, `GATE FAIL …` or `GATE ERROR …`.
- One test module: `/usr/bin/python3 -I -B -m unittest discover -s tests -t . -p test_hooks.py`; a dotted module name does not import under `-I`.
- `shellcheck` is a gate link pinned to one version (`SHELLCHECK_VERSION` in `scripts/gate.sh`) on macOS, Linux and the fence: a host without it, or with another version, gets `GATE ERROR` naming the fix.

# Rules

## Public repository

- Nothing identifying ships: no user name, email, machine-absolute path or private project name in any tracked file; the predecessor's name is never written (`scripts/checks.sh` census `spelling-predecessor`).
- The product is spelled `gpt-gateway`, the package `gpt_gateway`, every knob `GPT_GATEWAY_*`; the census rejects every other spelling.

## Live systems

- No test, script or session runs `service/*.sh` against the real `HOME` or reaches the real launchd or systemd user manager, Claude Code settings, database or a running gateway: tests take a scratch `HOME` and `GPT_GATEWAY_DB` from `tests/jail.py`, free ports, fake upstreams and stub launchctl/systemctl.
- No test reads or writes the real `~/.codex/auth.json`, the real Keychain entry `gpt-gateway.chatgpt`, the real ChatGPT token or catalog file, or reaches OpenAI: ChatGPT-route tests take a jailed `HOME` without `CODEX_HOME`, a jailed Codex login file or `GPT_GATEWAY_CHATGPT_CODEX_AUTH=off`, a jailed file store (`GPT_GATEWAY_CHATGPT_AUTH_STORE=file`) and the fake ChatGPT upstream in `tests/fakes.py`.
- The gateway shares Codex's ChatGPT login in place, the way Codex shares it between its own processes: it re-reads `auth.json` before every refresh, refreshes only on Codex's schedule, writes rotated tokens back in Codex's format and never marks or blanks that file; its own store and `chatgpt-login` serve only when no Codex login exists.

## Code

- One façade per mechanism: environment reads only in `gpt_gateway/env.py`, time only in `gpt_gateway/clock.py`, the log only through `gpt_gateway/log.py`, the settings file name only in `gpt_gateway/settings.py`, HTTP connections only in `gpt_gateway/upstream.py` and its pool `gpt_gateway/pool.py`, capture file names only in `gpt_gateway/capture.py`, the database only through `gpt_gateway/db.py`, the open-file limit and the open-handle count only in `gpt_gateway/handles.py`.
- The contract moves as one: a change to a hook, a `Context` field or a `Result` field lands in `gpt_gateway/hooks.py`, `gpt_gateway/harness.py`, `README.md` § Middleware and the example in the same change.
- A hook failure never cuts a stream or forwards a half-edited body: the core catches it, records it once as a `middleware_failed` event, counts it, and goes on as if that hook were absent.

## Gates and tests

- A new source file gets its test home `tests/{dir}/test_{stem}.py`; a new check gets its baseline file in `scripts/baselines/`, empty unless an exception is named in its commit.
- Baselines only shrink: `scripts/checks.sh --measure` locks a fix and never adds an entry.
- A test counts once it was watched failing on an assertion against the code without its rule.

## Git

- Git goes through gitter, per the parent directory's `CLAUDE.md`; this repository is checked out inside that directory.
