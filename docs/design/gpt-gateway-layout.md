# gpt-gateway layout

The gateway has an Anthropic passthrough, a middleware contract and an optional ChatGPT route. `install.sh` generates a 0600 `route-secret` once; `run.sh` places it as a base-URL path segment and the proxy strips it before Anthropic forwarding, while only an authenticated ChatGPT candidate may reach the catalog. The tables below define its units, vocabulary and façades; co-change evidence marked "assumed" is a design boundary, not a measured history. `scripts/checks.sh` and `scripts/gate.sh` enforce the countable laws.

## Contents

- [1. Units of change](#1-units-of-change)
- [2. Tree](#2-tree)
- [3. Glossary](#3-glossary)
- [4. Façades](#4-façades)
- [5. Registries](#5-registries)
- [6. Gates](#6-gates)
- [7. Metrics](#7-metrics)
- [8. Migration](#8-migration)
- [9. Brief template and work tree](#9-brief-template-and-work-tree)
- [10. Open rulings](#10-open-rulings)

## 1. Units of change

| Unit | Concepts it owns | Directory or module | Co-change evidence |
| --- | --- | --- | --- |
| entry | the `gateway.py` script: import path setup, hand-off to the CLI | `gateway.py` | assumed: changes only when the package's entry changes |
| cli | core and `chatgpt-login`, `chatgpt-status`, `chatgpt-stats` commands; usage | `gpt_gateway/cli.py` | assumed: a new command adds a table row and its function together |
| env | environment reads, `GPT_GATEWAY_*`, `CoreEnv`, `ChatGPTEnv` | `gpt_gateway/env.py` | assumed: a new knob lands here alone |
| clock | wall time, local-time stamps, the time the host slept, sleeping | `gpt_gateway/clock.py` | assumed: never co-changes |
| log | the locked text log and bounded append-only diagnostic rows split by level | `gpt_gateway/log.py` | assumed: rotation and line format change together |
| db | the SQLite database: its schema and views, the in-place column migration, the one writer thread off a bounded queue, drops and failures counted and recorded as `db_lost`, retention, the readers | `gpt_gateway/db.py` | assumed: a column, its view and its reader change together |
| usage | a relayed response's token usage and error code, read beside the relay | `gpt_gateway/usage.py` | assumed: changes with the Messages API usage shape only |
| settings | `settings.json`: the core keys, the `middleware` entries, env over file over default for core keys and each module's `SETTINGS`, `check()`, the strict start read, the mtime/size/inode reload with last-good | `gpt_gateway/settings.py` | assumed: a new core key changes its default, validator and reload together |
| middleware | loading `{middleware_dir}/{module}.py`, the module attribute checks, the `SETTINGS` shape | `gpt_gateway/middleware.py` | assumed: the module half of the contract |
| hooks | `Context`, `Request`, `Result`, the hook chain in list order, failure isolation, error counters, the single re-serialization, a hook's answer, the last turn message | `gpt_gateway/hooks.py` | assumed: the per-request half of the contract |
| headers | the secret headers, the hop-by-hop set, the read-only `ctx.headers` mapping, the debug header pick | `gpt_gateway/headers.py` | assumed: the secret set changes once, for capture and hooks alike |
| events | splitting an SSE stream into events across chunk boundaries; the one-call JSON event | `gpt_gateway/events.py` | assumed: changes only with the SSE wire format |
| capture | the capture folder layout, secret redaction, sequence numbers | `gpt_gateway/capture.py` | assumed: the layout and its file names change together |
| upstream | the upstream connection leased from the pool, the one retry of a kept-alive connection that failed before its answer, TLS, the WebSocket upgrade, an out-of-handles failure named as one, a failure's class | `gpt_gateway/upstream.py` | assumed: never co-changes with the hooks |
| pool | the idle upstream connections shared across serving threads: give back when a body is complete, the idle cap, the reaper | `gpt_gateway/pool.py` | assumed: the lease rule and the bounds co-change |
| handles | the soft open-file limit raised at start, the open count, the out-of-handles diagnosis and its rate-limited event | `gpt_gateway/handles.py` | assumed: the limit, the count and the diagnosis co-change |
| reach | a failed upstream exchange's class, failures in a row as one start and one end event, host pauses | `gpt_gateway/reach.py` | assumed: a class and the events that count it change together |
| frames | RFC 6455 frames on an upgraded socket: masking, fragments, pings, closes, the idle check | `gpt_gateway/frames.py` | assumed: the wire format changes alone |
| proxy | one exchange: read the body (chunked included), run the hooks, forward or take a hook's answer, relay chunk by chunk, the request's row | `gpt_gateway/proxy.py` | assumed: the exchange's steps co-change, so they are one unit (see § Tree, flow order) |
| response | the in-memory response the relay reads where no upstream HTTP response exists: a hook's answer, a ChatGPT-route body | `gpt_gateway/response.py` | assumed: changes with the relay's reading surface only |
| health | the health line | `gpt_gateway/health.py` | assumed: changes with the health contract only |
| server | the listening socket with `SO_REUSEPORT` and its backlog, accept failures, in-flight tracking, SIGTERM drain | `gpt_gateway/server.py` | assumed: graceful restart and drain change together |
| runtime | composing env, log, database, settings, middleware, upstream, the host pause watch and drain into one object | `gpt_gateway/runtime.py` | assumed: changes when a new mechanism joins |
| harness | the public middleware test harness | `gpt_gateway/harness.py` | assumed: changes with the hooks contract |
| chatgpt signature | reasoning signature codec | `gpt_gateway/chatgpt/signature.py` | assumed: replay and stream share the codec |
| chatgpt auth | credential source selection, own token storage and refresh | `gpt_gateway/chatgpt/auth.py` | assumed: token lifecycle co-changes |
| chatgpt Codex login | in-place Codex login reuse and guarded rotation | `gpt_gateway/chatgpt/codex_login.py` | assumed: shared login lifecycle co-changes |
| chatgpt login | browser PKCE and device-code login | `gpt_gateway/chatgpt/login.py` | assumed: OAuth flow changes together |
| chatgpt keychain | macOS Keychain token storage | `gpt_gateway/chatgpt/keychain.py` | assumed: platform storage changes alone |
| chatgpt catalog | model parsing, fetch and last-good cache | `gpt_gateway/chatgpt/catalog.py` | assumed: refresh and model fields co-change |
| chatgpt request | Anthropic to Responses body translation | `gpt_gateway/chatgpt/request.py` | assumed: request mapping changes together |
| chatgpt tools | tool definitions, choice and name mapping | `gpt_gateway/chatgpt/tools.py` | assumed: tool request and response names agree |
| chatgpt schema | pure JSON schema sanitization | `gpt_gateway/chatgpt/schema.py` | assumed: schema rules change alone |
| chatgpt lane | per-session, per-agent cache key | `gpt_gateway/chatgpt/lane.py` | assumed: affinity identity changes alone |
| chatgpt turn | a request's turn position and the lane's `x-codex-turn-state` token | `gpt_gateway/chatgpt/turn.py` | assumed: Codex's turn contract changes alone |
| chatgpt stream | Responses SSE translation and accumulation | `gpt_gateway/chatgpt/stream.py` | assumed: event mapping and accumulation agree |
| chatgpt errors | error decisions and retry backoff | `gpt_gateway/chatgpt/errors.py` | assumed: status and retry policy co-change |
| chatgpt estimate | per-lane token estimate calibration | `gpt_gateway/chatgpt/estimate.py` | assumed: measurement and estimate co-change |
| chatgpt identity | Codex client version and identity headers | `gpt_gateway/chatgpt/identity.py` | assumed: OAuth and backend share identity |
| chatgpt client | Responses over the WebSocket or HTTP, and models requests | `gpt_gateway/chatgpt/client.py` | assumed: headers and endpoints co-change |
| chatgpt websocket | the Responses WebSocket: per-lane socket pool, response.create, the answer the route reads | `gpt_gateway/chatgpt/websocket.py` | assumed: socket lifecycle and Codex's WebSocket contract co-change |
| chatgpt keeper | the thread that answers and pings the idle pooled WebSockets, and takes a dead one out of the pool | `gpt_gateway/chatgpt/keeper.py` | assumed: pool lifecycle and the server's idle behaviour co-change |
| chatgpt continuation | Codex's incremental request: a socket's last completed response, what the next request sends, output-item identity | `gpt_gateway/chatgpt/continuation.py` | assumed: Codex's continuation rule and the translator's round trip co-change |
| chatgpt route | routing decision, authenticated candidates, attempts, stream commit, lanes | `gpt_gateway/chatgpt/route.py` | assumed: lifecycle and relay integration agree |
| chatgpt response | the reader thread behind a translated SSE body, read through the core's `Response` | `gpt_gateway/chatgpt/response.py` | assumed: commit, heartbeat and EOF agree with the relay |
| example | the minimal middleware, the contract's executable documentation | `examples/middleware/model_note.py` | assumed: changes with the contract |
| shipped middleware | the middleware an install copies into its `middleware/`, each file its own, and a fresh `settings.json` lists | `middleware/` | assumed: each module changes alone, against the contract |
| service | the portable supervisor (`run.sh`, fail-open), platform-dispatching install, control and removal, launchd agent and systemd user unit, generated route credential | `service/` | assumed: the stored service environment and lifecycle co-change |
| gates | `scripts/checks.sh`, `scripts/gate.sh`, `scripts/baselines/` | `scripts/` | assumed: a new check and its baseline land together |

## 2. Tree

The request path is one unit (`proxy`), not a prefixed pipeline: its steps share one handler's state and co-change, so splitting them into `s1_…` directories would make one change touch four. The support modules sit beside it, named by what they are.

```text
.gitignore
LICENSE                                   MIT, holder "gpt-gateway contributors"
THIRD_PARTY_NOTICES.md                     distilled sources' licences and notices
README.md                                 install, settings, middleware, health, fail-open, capture, ChatGPT route, operating it
gateway.py                                entry: puts its own directory on sys.path, runs gpt_gateway.cli.main
gpt_gateway/__init__.py                 package purpose
gpt_gateway/cli.py                      COMMANDS table → serve | check | mw | chatgpt-login | chatgpt-status | chatgpt-stats
gpt_gateway/env.py                      façade: environment reads; CoreEnv, ChatGPTEnv
gpt_gateway/clock.py                    façade: time
gpt_gateway/log.py                      façade: text and level logs
gpt_gateway/db.py                       façade: Db, query, chatgpt_responses, anthropic_timings, anthropic_transport, DbError
gpt_gateway/settings.py                 Settings, Snapshot, SettingsError, module_settings
gpt_gateway/middleware.py               Module, load_modules, MiddlewareError
gpt_gateway/hooks.py                    Context, Request, Answer, Result, Chain, last_turn_message
gpt_gateway/headers.py                  Headers, SECRET, HOP, debug_headers
gpt_gateway/events.py                   EventSplitter, json_event
gpt_gateway/capture.py                  façade: Capture, Sequence
gpt_gateway/upstream.py                 façade: Upstream, UpstreamError, Upgrade
gpt_gateway/pool.py                     Pool, PooledResponse: the idle upstream connections
gpt_gateway/handles.py                  raise_limit, open_count, diagnose, announce: file handles
gpt_gateway/reach.py                    error_class, Runs, Pause: reachability as data
gpt_gateway/frames.py                   WebSocket, mask, parse_frame: RFC 6455 frames on an upgraded socket
gpt_gateway/proxy.py                    Handler, the request row
gpt_gateway/usage.py                    Meter: usage and error code off a relayed response
gpt_gateway/response.py                 Response: the in-memory response the relay reads
gpt_gateway/health.py                   health_line (pid, fds, ...)
gpt_gateway/server.py                   Server, Drain, serve
gpt_gateway/runtime.py                  Runtime
gpt_gateway/harness.py                  run, run_cli, load
gpt_gateway/chatgpt/__init__.py         package purpose
gpt_gateway/chatgpt/signature.py        reasoning signature encode, decode
gpt_gateway/chatgpt/auth.py             credential source selection, own store and refresh
gpt_gateway/chatgpt/codex_login.py      Codex login reuse, guarded refresh and write-back
gpt_gateway/chatgpt/login.py            browser and device-code login
gpt_gateway/chatgpt/keychain.py         macOS Keychain storage
gpt_gateway/chatgpt/catalog.py          Model, parse, Catalog
gpt_gateway/chatgpt/request.py          deterministic Responses request translation
gpt_gateway/chatgpt/tools.py            tool mapping and name restoration
gpt_gateway/chatgpt/schema.py           pure schema sanitizer
gpt_gateway/chatgpt/lane.py             lane key
gpt_gateway/chatgpt/turn.py             position, Turn: the turn-state token
gpt_gateway/chatgpt/stream.py           Translator, accumulate
gpt_gateway/chatgpt/errors.py           Decision, classify, backoff
gpt_gateway/chatgpt/estimate.py         Estimator
gpt_gateway/chatgpt/identity.py         CODEX_CLIENT_VERSION, headers
gpt_gateway/chatgpt/client.py           separate ChatGPT Upstream, responses and models
gpt_gateway/chatgpt/websocket.py        Sockets, Answer, Refusal, message: the Responses WebSocket
gpt_gateway/chatgpt/keeper.py           Keeper: answers and pings the idle pooled sockets
gpt_gateway/chatgpt/continuation.py     Request, Baseline, produced: the continuation of a socket's last response
gpt_gateway/chatgpt/route.py            decision, retries, Lane: estimator and turn state
gpt_gateway/chatgpt/response.py         Reader: the reader thread behind a translated SSE body
examples/middleware/model_note.py         NAME "model-note", SETTINGS enabled, on_request
middleware/status_label.py                NAME "status-label", on_request: answers the activity-summary fork
middleware/advisor_cache.py               NAME "advisor-cache", SETTINGS ttl … debug, check, on_request, on_event, on_response_end, health: the advisor tool's caching, output cap and scoping; shipped, listed by no fresh install
service/install.sh                        copies SHIP to ~/.local/share/gpt-gateway/, preserves enabled state, writes the platform's service
service/uninstall.sh                      stops, unlinks, removes the service and copy, backs up user configuration
service/run.sh                            portable supervisor: links env.ANTHROPIC_BASE_URL and env.ENABLE_TOOL_SEARCH only while healthy
service/ctl.sh                            start | stop | restart | status; on | off aliases
service/platform.sh                       service-manager paths, lifecycle, Linux unit rendering and stored environment
service/settings.sh                       tri-state settings reads and gateway-owned cleanup retries
service/listener.py                       Linux listener descriptor inherited across supervisor and gateway execs
scripts/checks.sh                         every countable law, one CHECK line each
scripts/gate.sh                           the only definition of ready
scripts/baselines/*.txt                   committed baselines, one per ratchet
docs/design/gpt-gateway-layout.md       this document
docs/design/_index.md                   design navigation
docs/operations/_index.md               operations navigation
docs/operations/linux-service.md        quiet-window install, verification, switch-off and rollback
tests/__init__.py
tests/jail.py                             the test jail: scratch roots, free ports, a clean env
tests/fakes.py                            fake upstreams (SSE, split SSE, slow SSE, JSON, error), the recording middleware, a gateway on a free port
tests/test_gateway.py                     ↔ gateway.py
tests/test_jail.py                        ↔ tests/jail.py
tests/gpt_gateway/__init__.py
tests/gpt_gateway/test_{cli,env,clock,log,db,usage,settings,middleware,hooks,headers,events,capture,upstream,pool,handles,reach,frames,proxy,response,health,server,runtime,harness}.py
tests/gpt_gateway/chatgpt/__init__.py
tests/gpt_gateway/chatgpt/test_codex_login.py
tests/gpt_gateway/chatgpt/test_response.py
tests/gpt_gateway/chatgpt/test_{signature,auth,login,keychain,catalog,request,tools,schema,lane,stream,errors,estimate,identity,client,route,websocket,keeper,retention,continuation}.py
tests/gpt_gateway/chatgpt/fixtures/      invented or scrubbed wire shapes, with provenance
tests/examples/__init__.py
tests/examples/middleware/__init__.py
tests/examples/middleware/test_model_note.py
tests/middleware/__init__.py
tests/middleware/test_status_label.py
tests/middleware/test_advisor_cache.py
tests/service/__init__.py
tests/service/stub.py                     scratch HOME, one-test source suite, stub launchctl/systemctl and macOS tools first on PATH
tests/service/test_{install,uninstall,run,ctl,platform,settings,listener}.py
tests/scripts/__init__.py
tests/scripts/test_{checks,gate}.py
```

Top-level install items are flat: `install.sh` copies `gateway.py`, `gpt_gateway/`, `service/run.sh`, `service/ctl.sh`, `service/platform.sh`, `service/settings.sh`, `service/listener.py`, `README.md`, `LICENSE` and `THIRD_PARTY_NOTICES.md` to `~/.local/share/gpt-gateway/`, and creates `middleware/` there, copying each shipped `middleware/*.py` into it as its own file (a user's module beside it stays); a first install writes `settings.json` there, listing `status_label` (every shipped module is copied; only that one is listed). The package copy includes every `*.py` recursively, preserving subdirectories through the `.new` swap. Linux validation runs with `-B`; macOS keeps its interpreter bytecode behavior. `tests/jail.py` mirrors that layout with `os.walk`. `run.sh` starts `$DIR/gateway.py` with `/usr/bin/python3 -I`; `gateway.py` puts its own directory on `sys.path`, since `-I` leaves it off.

`service/` is the neutral service unit of change. Each user-facing script selects the platform through `platform.sh` and `uname -s`; shared packaging, health polling and settings ownership stay in the entry scripts. macOS uses `local.gpt-gateway` in `~/Library/LaunchAgents/` and logs in `~/Library/Logs/`. Linux requires systemd 252+ and a reachable user bus, checked before installation; it uses the user unit `gpt-gateway.service` under `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`, logs in `~/.local/state/gpt-gateway/`, and an installed `service.json` carrying its environment. `XDG_CONFIG_HOME` is a standard path input, outside the product-knob prefix census.

On Linux the unit uses `Restart=on-failure`, `KillMode=mixed` and `TimeoutStopSec=630`: TERM reaches the supervisor first, which tries to unlink Claude Code before signalling its children to drain; failed cleanup retries alongside that drain. Enable/disable carry on/off state; a first install enables and starts. An on update sends `systemctl --user kill -s HUP --kill-whom=main gpt-gateway.service` unless the supervisor or unit changed, in which case the service drains and reloads. HUP targets the main process because signalling every process would terminate Python before it can drain. An off update stays disabled. Unit strings escape systemd specifiers; ExecStart and Environment quote their words, while StandardError takes a literal path after `append:`. The ExecStart `:` prefix disables dollar expansion. The Linux supervisor holds one inherited listening socket, so both gateways consume the same accept queue. Acceptance registers a pending first request before its handler thread starts; drain keeps those connections once request data has arrived, until the first request completes or the drain limit expires. A silent accepted client closes as idle; a request line already read stays pending while its headers arrive. Independently bound Linux reuse-port listeners can lose their queued connections when closed. An absent settings file is already unlinked. On stop, settings cleanup retries read and rewrite failures alongside the full gateway drain, ending one second before `GPT_GATEWAY_STOP_SECONDS` (default 630). The listener bootstrap imports no gateway package, ignores HUP until its shell takes over, and returns startup failures through the supervisor cleanup. The supervisor closes its listener before retry sleeps or crash cleanup; ctl waits for the supervisor and never edits the shared settings. Without linger the user manager stops at logout.

Anatomy: every source module opens with a purpose docstring and has a test home at `tests/{dir}/test_{stem}.py`, preserving package subdirectories; `__init__.py` files are package markers and exempt.

## 3. Glossary

| Concept | Canonical term | Rejected variants | Where it appears |
| --- | --- | --- | --- |
| the product | `gpt-gateway` | any other spelling of the two words: joined, camel-cased, spaced, capitalized; the former product name in any spelling | README, LICENSE holder, `server_version`, error prefix `gpt-gateway:`, launchd label `local.gpt-gateway`, install dir, log file names; census `spelling-product` |
| its Python package | `gpt_gateway` | the same set; the hyphen is impossible in a Python identifier | package directory, `gpt_gateway_mw_{module}` loader names; census `spelling-product` |
| the env prefix | `GPT_GATEWAY_` | any other prefix on a product knob read by the gateway or its scripts | `gpt_gateway/env.py`, `service/*.sh`, plist and unit; standard HOME, CODEX_HOME and XDG_CONFIG_HOME inputs exempt; census `spelling-env-prefix` |
| the predecessor's names | none survive | the predecessor's product-name family, matched by `scripts/checks.sh`; the word its feature is about (the advisor tool) is free, since `middleware/advisor_cache.py` is about it | census `spelling-predecessor`, `spelling-old-name` |
| Claude Code's second-opinion server tool | advisor (`advisor_20260301`, `NAME` `advisor-cache` for its middleware) | — | `middleware/advisor_cache.py`, README § Shipped middleware |
| a middleware file | module (`module` key, file stem) | plugin, extension, filter | `settings.json` `middleware[].module`, `middleware.Module`, `load_modules` |
| a middleware's label | `NAME` | id, title | health line, hook failure lines, `gateway.py mw NAME` |
| a hook | `on_request`, `on_event`, `on_response_end`, `health`, `cli`, `check` | callbacks, handlers | `hooks.Chain`, the example, README § Middleware |
| main session vs sub-agent | kind (`main`, `sub`) | conv, conversation type | `ctx.kind`, the row's `kind`; ChatGPT capture uses `gpt`; census `spelling-kind` |
| the capture folder tree | capture | dump, trace, record | `capture_dir`, `GPT_GATEWAY_CAPTURE_DIR`, `capture.Capture` |
| the API the gateway forwards to | upstream | backend, origin server | `GPT_GATEWAY_UPSTREAM`, `upstream.Upstream` |
| SIGTERM wind-down | drain | graceful shutdown | `GPT_GATEWAY_DRAIN_SECONDS`, `server.Drain` |
| the routing target | route (`"anthropic"`, `"chatgpt"`) | codex bridge, translator module, provider | `ctx.route`, `chatgpt/route.py` |
| cache affinity per session and agent | lane key | conversation id, session-only key | `chatgpt/lane.py`, `prompt_cache_key`, `session-id`, `x-client-request-id` |
| sticky routing within one agent turn | turn-state token | sticky id, routing cookie | `chatgpt/turn.py`, `x-codex-turn-state`, gpt line `turn_state=` |
| models and their capabilities | catalog | allowlist, model table | `chatgpt/catalog.py`, disk cache, route decision |
| a response a hook gives in place of upstream's | answer (`req.answer`, `Answer`) | reply, short-circuit, local response | `gpt_gateway/hooks.py`, the note's `" answered="`, the row's `answered_by`, harness `out.answer` |
| Claude Code's request for a sub-agent's status label | activity-summary fork | label call, status call, summary request | `chatgpt/turn.py` `FORK`, `chatgpt/request.py` `activity_summary`, `middleware/status_label.py` |
| catalog-selected Responses wire variant | Responses-Lite | lite mode, Sol protocol | `use_responses_lite`, `additional_tools`, `x-openai-internal-codex-responses-lite` |
| replayable encrypted reasoning envelope | reasoning signature | thinking cache, reasoning blob | `chatgpt/signature.py`, `gpt-gateway:chatgpt:v1:` |

The census of the product spelling exempts this document, since its glossary must be able to name what it rejects, and one line in each of `service/install.sh` and `tests/service/test_install.py`: the `LEGACY_NAME` constant the migration from the former install derives every old path from; the predecessor's names are never written, even here.

## 4. Façades

| Concern | Module | The primitive it hides |
| --- | --- | --- |
| environment reads | `gpt_gateway/env.py` | `os.environ`, `os.getenv` |
| clock | `gpt_gateway/clock.py` | `time.time`, `time.sleep`, `time.strftime`, `time.localtime`, `time.monotonic`, `time.clock_gettime` |
| logging | `gpt_gateway/log.py` | text and level appends, rotation, the `logging` module |
| the database | `gpt_gateway/db.py` | the `sqlite3` module: connections, the schema, every read and write |
| settings | `gpt_gateway/settings.py` | reading `settings.json` and its file name |
| upstream HTTP | `gpt_gateway/upstream.py` | `http.client.HTTPConnection`, `HTTPSConnection`, `ssl.create_default_context`, `socket.create_connection`; its pool is `gpt_gateway/pool.py` |
| file handles | `gpt_gateway/handles.py` | `resource.setrlimit`, the `/dev/fd` listing |
| capture writes | `gpt_gateway/capture.py` | the capture file names and their writes |
| test scratch root | `tests/jail.py` | `tempfile.mkdtemp`, `mkstemp`, `TemporaryDirectory`, `NamedTemporaryFile` in tests |

Every other line the gateway observes is a `requests` row or an `events` row of the database, written only through `gpt_gateway/db.py`; the text log keeps the start, stop, fatal, settings-error, database-failure and proxy-failure lines.

`Log.record(level, event, msg, where, detail=None)` keeps that text line and appends a UTC diagnostic row to `errors.jsonl`, `warnings.jsonl` or `info.jsonl`. The logging façade's `record(writer, ...)` adapter retains single-message callbacks while routing bound `Log.write` callbacks through `Log.record`. `env.plugin_log_dir(environ)` resolves `$PLUGIN_LOG_DIR/gpt-gateway/`, otherwise `$HOME/.local/state/gpt-gateway/logs/` on every platform. Rows are at most 4096 bytes, with oversized detail and messages truncated; a level file past 20 MiB moves to `.1` and emits `log-capped`. New level directories use `0700` and new files `0600`. Write failures never interrupt work and are reported once per failing streak, re-armed after success. The installer includes `PLUGIN_LOG_DIR` in the platform service environment only when supplied.

The ChatGPT route introduces no new primitive façade: it uses `gpt_gateway/upstream.py` for its separate HTTP client and its WebSocket upgrades, `gpt_gateway/env.py` for its knobs and `gpt_gateway/clock.py` for time and backoff. Its rows and events, and capture, go through the existing façades. Its package modules own translation and lifecycle concepts, not parallel implementations of those mechanisms.

## 5. Registries

| Derived artifact | Source | Regeneration | Tracked or built |
| --- | --- | --- | --- |
| `gateway.py` usage text | `gpt_gateway/cli.py` `COMMANDS` | built at run time from the table | built |
| the installed file set | `service/install.sh` `SHIP=` line | read by `install.sh` and by `tests/service` staging | tracked, one line |
| the shipped middleware | `middleware/*.py` | copied by `install.sh`, one file each; a fresh `settings.json` lists `status_label` only | tracked |
| the middleware list | `settings.json` `middleware` | none: the path `{middleware_dir}/{module}.py` is the registry | the adopter's file |
| health and log middleware labels | each module's `NAME` | none | — |

There is no hand-kept list of modules, commands or knobs beside the one that defines them.

## 6. Gates

| Check id | Class | Asserts | Broken state reports | Baseline |
| --- | --- | --- | --- | --- |
| size-src | Size | no source over 400 lines (slack 5) | `FAIL path (new N)` | `ceiling-src.txt` |
| size-test | Size | no test file over 600 lines (slack 5) | `FAIL path (new N)` | `ceiling-test.txt` |
| negation-dirs | Structure | no `util*`, `helpers`, `common`, `misc`, `shared` directory | `FAIL new: dir` | `negation-dirs.txt` |
| module-purpose | Structure | every source `.py` opens with a docstring; every `.sh` has a comment on line 2 | `FAIL new: path` | `no-purpose.txt` |
| test-mirror | Structure | every source has its test home `tests/{dir}/test_{stem}.py` | `FAIL new: path` | `untested-sources.txt` |
| test-jail | Structure | no temp root created in tests outside `tests/jail.py` | `FAIL path (new N)` | `test-jail.txt` |
| stray-runners | Structure | no `*.sh` outside `scripts/` and `service/` | `FAIL new: path` | `stray-runners.txt` |
| facade-env | Façade | no `os.environ`/`os.getenv` outside `gpt_gateway/env.py` | `FAIL path (new N)` | `facade-env.txt` |
| facade-clock | Façade | no `time.*` clock call outside `gpt_gateway/clock.py` | `FAIL path (new N)` | `facade-clock.txt` |
| facade-log | Façade | no `logging` and no append-mode `open` outside `gpt_gateway/log.py` | `FAIL path (new N)` | `facade-log.txt` |
| facade-settings | Façade | the `settings.json` name only in `gpt_gateway/settings.py` among Python sources | `FAIL path (new N)` | `facade-settings.txt` |
| facade-upstream | Façade | no HTTP connection or TLS context outside `gpt_gateway/upstream.py` | `FAIL path (new N)` | `facade-upstream.txt` |
| facade-capture | Façade | capture file names only in `gpt_gateway/capture.py` | `FAIL path (new N)` | `facade-capture.txt` |
| facade-db | Façade | no `sqlite3` import or call outside `gpt_gateway/db.py` | `FAIL path (new N)` | `facade-db.txt` |
| spelling-predecessor | Spelling census | zero hits of the predecessor's product name, any case, with or without its cache word, joined by a hyphen, an underscore, a space or nothing, every file; its feature's subject word alone is no hit | `FAIL path (new N)` | `spelling-predecessor.txt` |
| spelling-old-name | Spelling census | zero hits of the predecessor's dashed, underscored and env-prefix spellings | `FAIL path (new N)` | `spelling-old-name.txt` |
| spelling-product | Spelling census | the two words appear only as `gpt-gateway`, `gpt_gateway`, `GPT_GATEWAY`; the former product name only as the `LEGACY_NAME` line of `service/install.sh` and its test | `FAIL path (new N)` | `spelling-product.txt` |
| spelling-env-prefix | Spelling census | every product knob a shell script defaults (`${NAME:-…}`) and every name `env.py` reads starts with `GPT_GATEWAY_` (standard `HOME`, `CODEX_HOME`, `XDG_CONFIG_HOME` inputs exempt) | `FAIL new: NAME` | `spelling-env-prefix.txt` |
| spelling-kind | Spelling census | no `conv=` in sources | `FAIL path (new N)` | `spelling-kind.txt` |
| dup-functions | Duplicates (free functions) | one top-level `def` per name across sources, hook names and `main` excluded | `FAIL new: name: files` | `dup-functions.txt` |
| registry-parity | Registry parity | every `case` arm a `service/*.sh` dispatches appears in its usage line | `FAIL new: script:name`; `ERROR` when no arm or no usage parsed | `undocumented.txt` |
| orientation-truth | Orientation truth | every repo path cited in backticks in `README.md` and any `CLAUDE.md` exists, and every `GPT_GATEWAY_*` they cite is read by a source | `FAIL new: token`; `ERROR` when README is missing | `dangling.txt` |

`scripts/checks.sh` implements these with the skeleton's `say`, `ratchet` and `ratchet_counts`, over the files git tracks or would add. Its `setup` line is `ERROR` when git lists nothing, no source is listed, a listed file is unreadable, or a path holds a space (the greps word-split the list). `spelling-env-prefix` reads every `*.sh` source and `gpt_gateway/env.py`. `orientation-truth` checks the first word of each backticked citation: a relative path with a `/` must exist; a bare `*.py`, `*.sh` or `*.md` name must exist at the root or in `service/`, since the installed copy is flat. Exit 0 all PASS, 1 any FAIL, 2 any ERROR; `--measure` lowers baselines and never adds. Its tests are `tests/scripts/test_checks.py`.

The gate target, `scripts/gate.sh`, run from the plugin root on the host:

1. `compile`: `py_compile` of every tracked or new `*.py`, into a temporary directory, so the tree gets no bytecode.
2. `shellcheck`: every `*.sh`; a missing `shellcheck` is `ERROR` naming `brew install shellcheck`.
3. `checks`: `scripts/checks.sh`, its lines passed through.
4. `tests`: `/usr/bin/python3 -I -B -m unittest discover -s tests -t .` under `env -i` with a temporary `HOME`, so a test reaching for the real home finds an empty one; `Ran 0 tests`, a missing `Ran` line or any skipped test is `ERROR`. Service fixtures stub both service managers. Gate fixtures initialize real Git repositories in scratch directories and isolate PATH only to exclude the host's shellcheck; a setUp guard checks that isolation. The project repository receives no Git writes.

Each link prints `GATE <link> PASS|FAIL|ERROR <detail>`; the last line is `GATE PASS` or `GATE FAIL <links>` (exit 1) or `GATE ERROR <links>` (exit 2). Its tests are `tests/scripts/test_gate.py`, against a scratch tree with stub links.

Classes that do not apply, or lack an instrument:

- Format: Python's standard library ships no formatter — § Open rulings.
- Lint: no standard-library linter; `py_compile` is the compiler link; `shellcheck` lints the shell — § Open rulings.
- Token-based clone detector: no instrument in the toolchain — § Open rulings.
- Coverage: `trace` exists in the standard library but has no per-package floor mechanism — § Open rulings.
- Suite wall time: the suite is measured (§ Metrics) but not budgeted — § Open rulings.
- Publication: the parent project's leak gate is run over every tracked file at landing; this repository has no pre-push hook yet — § Open rulings.

## 7. Metrics

| Metric | Baseline today | Target |
| --- | --- | --- |
| dirs-per-change | UNMEASURED — no history; the predecessor was one file | 1 dominant directory per change |
| first-edit hops (source-only · orientation) | UNMEASURED — no sessions have edited this tree | ≤ 3 · ≤ 2 (orientation file, then this document) |
| re-reads | UNMEASURED | ≤ 2 files |
| zero-result searches | UNMEASURED | ≈ 0 |
| new file without a prior search | UNMEASURED | 0 |
| per-check violations | 0 for every check (all baselines empty) | 0 |

Named gaps: no transcripts exist for this tree; every count above other than the checks' is a design goal.

## 8. Migration

Not applicable: greenfield. Reuse manifest (predecessor → here):

- `gateway.py` → split into `gpt_gateway/*.py` · rewritten (the passthrough, relay, retry, chunked read, capture, settings reload, health, drain kept; the caching and output-cap rewrite, the scoping filter and the usage rows re-homed in `middleware/advisor_cache.py`; the stats function dropped, since the rows are in the database)
- `run.sh`, `ctl.sh`, `install.sh`, `uninstall.sh` → `service/` · rewritten for the K3 names
- `tests/test_gateway.py` → `tests/gpt_gateway/test_*.py` · the passthrough, relay, chunked, retry, capture, settings, health, drain and client-reset tests ported; the rewrite cases re-homed in `tests/middleware/test_advisor_cache.py`; the rest dropped with their code
- `tests/test_run.py` → `tests/service/test_run.py` · ported
- `tests/fixtures/` · dropped
- `README.md` → `README.md` · rewritten; its cost reasoning and measurements moved to § Shipped middleware

## 9. Brief template and work tree

A task in this repository carries:

- anchors: `path:line` quotes for every fact the builder depends on (the hook signature in `gpt_gateway/hooks.py`, the settings key in `gpt_gateway/settings.py`)
- commands: `scripts/gate.sh` from the plugin root; one test module as `/usr/bin/python3 -I -B -m unittest discover -s tests -t . -p test_hooks.py` (`-I` keeps the root off `sys.path`, so a dotted module name does not import)
- consumers: a middleware repository built against the contract starts at `gpt_gateway/hooks.py` (the per-request objects) and `gpt_gateway/harness.py` (its tests)

The work root lives outside the tree: `{flight directory}/` with `requirements.md` (the brief), `run.md` (progress), `receipts/` (gate and test output), `red/` (watched-failing logs).

## 10. Open rulings

- Size ceilings. Recommendation: 400 lines per source, 600 per test file, slack 5. Alternative: 300 per source would put the largest modules over and force a split of `proxy.py` with no co-change reason. Counts at build time are recorded in `scripts/baselines/` (empty: none over).
- Formatter. Recommendation: none until the owner admits a non-stdlib tool; the stdlib-only rule (K1) excludes `black` and `ruff format`. Alternative: pin `ruff` in a versions file and add a `format` link; cost: a tool install on every host that runs the gate.
- Python linter. Recommendation: same as the formatter — `ruff check` with the classes of § Lint, changed-lines view, once a tool is admitted. Until then `py_compile` and the `dup-functions` census are the only static Python links.
- Clone detector over shell and config. Recommendation: no instrument selected; `dup-functions` guards repeated free-function names in `service/`.
- Coverage floor. Recommendation: none until a tool is admitted; `trace` lacks a floor mechanism.
- Suite wall time. Recommendation: a 60-second suite budget once the owner sets a tolerance; suite time varies with host load, most of it real processes (drain, restart, `run.sh`) and the `scripts/checks.sh` tests, each a copy of the tree.
- Clean-environment run. Recommendation: the gate's `tests` link already runs under `env -i` with a temporary `HOME`; a container or CI runner joins when the repository gets a remote. Alternative: run the gate in a macOS CI runner; cost: none until a remote exists.
- Publication link. Recommendation: run the parent project's leak gate over every tracked file before each publish, and add a pre-push hook once the repository has a remote.
