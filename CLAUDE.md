# model-gateway — a local passthrough between Claude Code and the Anthropic API, with a middleware hook API and an opt-in ChatGPT route

On the Anthropic route the core forwards every request byte for byte and relays every response chunk as it arrives; the ChatGPT route, the one route built in and off unless `chatgpt` is true, translates GPT sub-agent requests to the ChatGPT Codex backend and back; every other behaviour lives in middleware the core loads, never in the core. The layout, glossary, façades and gates are fixed by `docs/design/model-gateway-layout.md`; read it before adding a file.

# Vocabulary

- gateway: the entry `gateway.py` and the package `model_gateway/`, one module per unit · `docs/design/model-gateway-layout.md` § Units of change
- middleware: one Python file `{middleware_dir}/{module}.py` listed in `settings.json` `middleware`, labelled by its `NAME` · loading `model_gateway/middleware.py`
- the contract: the per-request objects `Context`, `Request`, `Result` and the hook chain · `model_gateway/hooks.py`; each module's `SETTINGS` and `check` · `model_gateway/settings.py`; documented in `README.md` § Middleware
- harness: the public middleware test harness, `run`, `run_cli`, `load` · `model_gateway/harness.py`
- example: the smallest middleware, the contract's executable documentation · `examples/middleware/model_note.py`
- kind: `main` or `sub` (a request carrying `x-claude-code-agent-id`); the log key is `kind=`; a ChatGPT-route capture folder carries `gpt` in its place
- ChatGPT route: GPT models as Claude Code sub-agents over the user's ChatGPT login, `ctx.route == "chatgpt"`, the `gpt` log lines and `gateway.py chatgpt-stats` · `model_gateway/chatgpt/`, `README.md` § ChatGPT route; its login, Codex's `auth.json` first and the gateway's own store second · `model_gateway/chatgpt/codex_login.py`, `model_gateway/chatgpt/auth.py`; third-party licences · `THIRD_PARTY_NOTICES.md`
- capture: one folder per exchange, off by default · `model_gateway/capture.py`
- launchd: the supervisor, `ctl.sh`, `install.sh`, `uninstall.sh`; the installed copy is flat under `~/.local/share/model-gateway/`, a shipped package keeping its subdirectories, label `local.model-gateway` · `launchd/`
- gates: `scripts/checks.sh` (every countable law, one `CHECK` line each), `scripts/gate.sh` (the only definition of ready), committed baselines · `scripts/baselines/`
- test jail: every scratch directory, port and environment a test uses · `tests/jail.py`; fake upstreams and the recording middleware · `tests/fakes.py`; the stub launchctl · `tests/launchd/stub.py`

# Runtime

- Python 3.9+, standard library only, run as `/usr/bin/python3 -I`; `-I` keeps the script directory off `sys.path`, so `gateway.py` inserts its own.
- The gate: `scripts/gate.sh` from this root — compile, shellcheck, checks, the whole suite under `env -i` with an empty `HOME`; its last line is `GATE PASS`, `GATE FAIL …` or `GATE ERROR …`.
- One test module: `/usr/bin/python3 -I -B -m unittest discover -s tests -t . -p test_hooks.py`; a dotted module name does not import under `-I`.
- `shellcheck` is a gate link: a host without it gets `GATE ERROR` naming `brew install shellcheck`.

# Rules

## Public repository

- Nothing identifying ships: no user name, email, machine-absolute path or private project name in any tracked file; the predecessor's name is never written (`scripts/checks.sh` census `spelling-predecessor`).
- The product is spelled `model-gateway`, the package `model_gateway`, every knob `MODEL_GATEWAY_*`; the census rejects every other spelling.

## Live systems

- No test, script or session runs `launchd/*.sh` against the real `HOME` or reaches the real launchd, the real Claude Code settings or a running gateway: tests take a scratch `HOME` from `tests/jail.py`, free ports, fake upstreams and the stub launchctl.
- No test reads or writes the real `~/.codex/auth.json`, the real Keychain entry `model-gateway.chatgpt`, the real ChatGPT token or catalog file, or reaches OpenAI: ChatGPT-route tests take a jailed `HOME` without `CODEX_HOME`, a jailed Codex login file or `MODEL_GATEWAY_CHATGPT_CODEX_AUTH=off`, a jailed file store (`MODEL_GATEWAY_CHATGPT_AUTH_STORE=file`) and the fake ChatGPT upstream in `tests/fakes.py`.
- The gateway shares Codex's ChatGPT login in place, the way Codex shares it between its own processes: it re-reads `auth.json` before every refresh, refreshes only on Codex's schedule, writes rotated tokens back in Codex's format and never marks or blanks that file; its own store and `chatgpt-login` serve only when no Codex login exists.

## Code

- One façade per mechanism: environment reads only in `model_gateway/env.py`, time only in `model_gateway/clock.py`, the log only through `model_gateway/log.py`, the settings file name only in `model_gateway/settings.py`, HTTP connections only in `model_gateway/upstream.py`, capture file names only in `model_gateway/capture.py`.
- The contract moves as one: a change to a hook, a `Context` field or a `Result` field lands in `model_gateway/hooks.py`, `model_gateway/harness.py`, `README.md` § Middleware and the example in the same change.
- A hook failure never cuts a stream or forwards a half-edited body: the core catches it, logs it once, counts it, and goes on as if that hook were absent.

## Gates and tests

- A new source file gets its test home `tests/{dir}/test_{stem}.py`; a new check gets its baseline file in `scripts/baselines/`, empty unless an exception is named in its commit.
- Baselines only shrink: `scripts/checks.sh --measure` locks a fix and never adds an entry.
- A test counts once it was watched failing on an assertion against the code without its rule.

## Git

- Git goes through gitter, per the parent directory's `CLAUDE.md`; this repository is checked out inside that directory.
