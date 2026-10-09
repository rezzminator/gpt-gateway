# model-gateway layout

Greenfield design per `/quality:llm-codebase`. The units of change come from the single-file predecessor gateway (one 926-line module plus four shell scripts); this repository has no history yet, so every co-change below is marked "assumed". Written before the code; the tree under § Tree is what `scripts/checks.sh` and `scripts/gate.sh` hold the build to.

## 1. Units of change

| Unit | Concepts it owns | Directory or module | Co-change evidence |
| --- | --- | --- | --- |
| entry | the `gateway.py` script: import path setup, hand-off to the CLI | `gateway.py` | assumed: changes only when the package's entry changes |
| cli | the commands `serve`, `check [FILE]`, `mw NAME ARGS…`, their usage text | `model_gateway/cli.py` | assumed: a new command adds a table row and its function together |
| env | every environment read; the core knob names (`MODEL_GATEWAY_*`); parsing the knobs into `CoreEnv` | `model_gateway/env.py` | assumed: a new knob lands here alone |
| clock | wall time, local-time stamps, sleeping | `model_gateway/clock.py` | assumed: never co-changes |
| log | the locked, rotating gateway log | `model_gateway/log.py` | assumed: rotation and line format change together |
| settings | `settings.json`: the core keys, the `middleware` entries, env over file over default for core keys and each module's `SETTINGS`, `check()`, the strict start read, the mtime/size/inode reload with last-good | `model_gateway/settings.py` | assumed: a new core key changes its default, validator and reload together |
| middleware | loading `{middleware_dir}/{module}.py`, the module attribute checks, the `SETTINGS` shape | `model_gateway/middleware.py` | assumed: the module half of the contract |
| hooks | `Context`, `Request`, `Result`, the hook chain in list order, failure isolation, error counters, the single re-serialization | `model_gateway/hooks.py` | assumed: the per-request half of the contract |
| headers | the secret headers, the hop-by-hop set, the read-only `ctx.headers` mapping, the debug header pick | `model_gateway/headers.py` | assumed: the secret set changes once, for capture and hooks alike |
| events | splitting an SSE stream into events across chunk boundaries; the one-call JSON event | `model_gateway/events.py` | assumed: changes only with the SSE wire format |
| capture | the capture folder layout, secret redaction, sequence numbers | `model_gateway/capture.py` | assumed: the layout and its file names change together |
| upstream | the per-thread upstream connection, the one fresh retry, TLS | `model_gateway/upstream.py` | assumed: never co-changes with the hooks |
| proxy | one exchange: read the body (chunked included), run the hooks, forward, relay chunk by chunk, the request log line | `model_gateway/proxy.py` | assumed: the exchange's steps co-change, so they are one unit (see § Tree, flow order) |
| health | the health line | `model_gateway/health.py` | assumed: changes with the health contract only |
| server | the listening socket with `SO_REUSEPORT`, in-flight tracking, SIGTERM drain | `model_gateway/server.py` | assumed: graceful restart and drain change together |
| runtime | composing env, log, settings, middleware, upstream and drain into one object | `model_gateway/runtime.py` | assumed: changes when a new mechanism joins |
| harness | the public middleware test harness | `model_gateway/harness.py` | assumed: changes with the hooks contract |
| example | the minimal middleware, the contract's executable documentation | `examples/middleware/model_note.py` | assumed: changes with the contract |
| launchd | the supervisor (`run.sh`, fail-open), `ctl.sh`, `install.sh`, `uninstall.sh`, the launchd agent | `launchd/` | assumed: the plist's variables appear in all four, so they co-change |
| gates | `scripts/checks.sh`, `scripts/gate.sh`, `scripts/baselines/` | `scripts/` | assumed: a new check and its baseline land together |

## 2. Tree

The request path is one unit (`proxy`), not a prefixed pipeline: its steps share one handler's state and co-change, so splitting them into `s1_…` directories would make one change touch four. The support modules sit beside it, named by what they are.

```
.gitignore
LICENSE                                   MIT, holder "model-gateway contributors"
README.md                                 what it is, install, settings, the middleware contract, health, fail-open, capture, operating it
gateway.py                                entry: puts its own directory on sys.path, runs model_gateway.cli.main
model_gateway/__init__.py                 package purpose
model_gateway/cli.py                      COMMANDS table → serve | check | mw; usage generated from the table
model_gateway/env.py                      façade: environment reads; CoreEnv
model_gateway/clock.py                    façade: time
model_gateway/log.py                      façade: the gateway log
model_gateway/settings.py                 Settings, Snapshot, SettingsError, module_settings
model_gateway/middleware.py               Module, load_modules, MiddlewareError
model_gateway/hooks.py                    Context, Request, Result, Chain
model_gateway/headers.py                  Headers, SECRET, HOP, debug_headers
model_gateway/events.py                   EventSplitter, json_event
model_gateway/capture.py                  façade: Capture, Sequence
model_gateway/upstream.py                 façade: Upstream, UpstreamError
model_gateway/proxy.py                    Handler
model_gateway/health.py                   health_line
model_gateway/server.py                   Server, Drain, serve
model_gateway/runtime.py                  Runtime
model_gateway/harness.py                  run, run_cli, load
examples/middleware/model_note.py         NAME "model-note", SETTINGS enabled, on_request
launchd/install.sh                        copies the SHIP list to ~/.local/share/model-gateway/, writes the plist
launchd/uninstall.sh                      stops the agent, unlinks, removes the plist and the install dir
launchd/run.sh                            the agent's program: supervises gateway.py, sets env.ANTHROPIC_BASE_URL only while healthy
launchd/ctl.sh                            on | off | restart | status
scripts/checks.sh                         every countable law, one CHECK line each
scripts/gate.sh                           the only definition of ready
scripts/baselines/*.txt                   committed baselines, one per ratchet
docs/design/model-gateway-layout.md       this document
tests/__init__.py
tests/jail.py                             the test jail: scratch roots, free ports, a clean env
tests/fakes.py                            fake upstreams (SSE, split SSE, slow SSE, JSON, error), the recording middleware, a gateway on a free port
tests/test_gateway.py                     ↔ gateway.py
tests/model_gateway/__init__.py
tests/model_gateway/test_{cli,env,clock,log,settings,middleware,hooks,headers,events,capture,upstream,proxy,health,server,runtime,harness}.py
tests/examples/__init__.py
tests/examples/middleware/__init__.py
tests/examples/middleware/test_model_note.py
tests/launchd/__init__.py
tests/launchd/stub.py                     a scratch HOME, a source tree with a one-test suite, a stub launchctl first on PATH
tests/launchd/test_{install,uninstall,run,ctl}.py
tests/scripts/__init__.py
tests/scripts/test_{checks,gate}.py
```

The installed copy is flat: `install.sh` copies `gateway.py`, `model_gateway/` (its `*.py`), `launchd/run.sh`, `launchd/ctl.sh`, `README.md` and `LICENSE` to `~/.local/share/model-gateway/`, and creates `middleware/` and `settings.json` there. `run.sh` starts `$DIR/gateway.py` with `/usr/bin/python3 -I`; `gateway.py` puts its own directory on `sys.path`, since `-I` leaves it off.

Anatomy: every module of `model_gateway/` opens with a docstring stating its purpose and has exactly one test home, `tests/model_gateway/test_{stem}.py`; `__init__.py` files are package markers and exempt.

## 3. Glossary

| Concept | Canonical term | Rejected variants | Where it appears |
| --- | --- | --- | --- |
| the product | `model-gateway` | any other spelling of the two words: joined, camel-cased, spaced, capitalized | README, LICENSE holder, `server_version`, error prefix `model-gateway:`, launchd label `local.model-gateway`, install dir, log file names; census `spelling-product` |
| its Python package | `model_gateway` | the same set; the hyphen is impossible in a Python identifier | package directory, `model_gateway_mw_{module}` loader names; census `spelling-product` |
| the env prefix | `MODEL_GATEWAY_` | any other prefix on a knob read by the gateway or its scripts | `model_gateway/env.py`, `launchd/*.sh`, the plist; census `spelling-env-prefix` |
| the predecessor's names | none survive | the predecessor's name family, matched by `scripts/checks.sh` | census `spelling-predecessor`, `spelling-old-name` |
| a middleware file | module (`module` key, file stem) | plugin, extension, filter | `settings.json` `middleware[].module`, `middleware.Module`, `load_modules` |
| a middleware's label | `NAME` | id, title | health line, hook failure lines, `gateway.py mw NAME` |
| a hook | `on_request`, `on_event`, `on_response_end`, `health`, `cli`, `check` | callbacks, handlers | `hooks.Chain`, the example, README § Middleware |
| main session vs sub-agent | kind (`main`, `sub`) | conv, conversation type | `ctx.kind`, log note `kind=`, capture folder names; census `spelling-kind` |
| the capture folder tree | capture | dump, trace, record | `capture_dir`, `MODEL_GATEWAY_CAPTURE_DIR`, `capture.Capture` |
| the API the gateway forwards to | upstream | backend, origin server | `MODEL_GATEWAY_UPSTREAM`, `upstream.Upstream` |
| SIGTERM wind-down | drain | graceful shutdown | `MODEL_GATEWAY_DRAIN_SECONDS`, `server.Drain` |
| the reserved routing target | route (`"anthropic"`) | backend, provider | `ctx.route` |

The census of the product spelling exempts this document alone, since its glossary must be able to name what it rejects; the predecessor's names are never written, even here.

## 4. Façades

| Concern | Module | The primitive it hides |
| --- | --- | --- |
| environment reads | `model_gateway/env.py` | `os.environ`, `os.getenv` |
| clock | `model_gateway/clock.py` | `time.time`, `time.sleep`, `time.strftime`, `time.localtime`, `time.monotonic` |
| logging | `model_gateway/log.py` | appending to the log file, rotation, the `logging` module |
| settings | `model_gateway/settings.py` | reading `settings.json` and its file name |
| upstream HTTP | `model_gateway/upstream.py` | `http.client.HTTPConnection`, `HTTPSConnection`, `ssl.create_default_context` |
| capture writes | `model_gateway/capture.py` | the capture file names and their writes |
| test scratch root | `tests/jail.py` | `tempfile.mkdtemp`, `mkstemp`, `TemporaryDirectory`, `NamedTemporaryFile` in tests |

The harness creates its own temporary log file through `tempfile`; it is production code, so the test-jail count does not cover it.

## 5. Registries

| Derived artifact | Source | Regeneration | Tracked or built |
| --- | --- | --- | --- |
| `gateway.py` usage text | `model_gateway/cli.py` `COMMANDS` | built at run time from the table | built |
| the installed file set | `launchd/install.sh` `SHIP=` line | read by `install.sh` and by `tests/launchd` staging | tracked, one line |
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
| stray-runners | Structure | no `*.sh` outside `scripts/` and `launchd/` | `FAIL new: path` | `stray-runners.txt` |
| facade-env | Façade | no `os.environ`/`os.getenv` outside `model_gateway/env.py` | `FAIL path (new N)` | `facade-env.txt` |
| facade-clock | Façade | no `time.*` clock call outside `model_gateway/clock.py` | `FAIL path (new N)` | `facade-clock.txt` |
| facade-log | Façade | no `logging` and no append-mode `open` outside `model_gateway/log.py` | `FAIL path (new N)` | `facade-log.txt` |
| facade-settings | Façade | the `settings.json` name only in `model_gateway/settings.py` among Python sources | `FAIL path (new N)` | `facade-settings.txt` |
| facade-upstream | Façade | no HTTP connection or TLS context outside `model_gateway/upstream.py` | `FAIL path (new N)` | `facade-upstream.txt` |
| facade-capture | Façade | capture file names only in `model_gateway/capture.py` | `FAIL path (new N)` | `facade-capture.txt` |
| spelling-predecessor | Spelling census | zero hits of the predecessor's name family, any case, every file | `FAIL path (new N)` | `spelling-predecessor.txt` |
| spelling-old-name | Spelling census | zero hits of the predecessor's dashed, underscored and env-prefix spellings | `FAIL path (new N)` | `spelling-old-name.txt` |
| spelling-product | Spelling census | the two words appear only as `model-gateway`, `model_gateway`, `MODEL_GATEWAY` | `FAIL path (new N)` | `spelling-product.txt` |
| spelling-env-prefix | Spelling census | every knob a shell script defaults (`${NAME:-…}`) and every name `env.py` reads starts with `MODEL_GATEWAY_` (`HOME` excepted) | `FAIL new: NAME` | `spelling-env-prefix.txt` |
| spelling-kind | Spelling census | no `conv=` in sources | `FAIL path (new N)` | `spelling-kind.txt` |
| dup-functions | Duplicates (free functions) | one top-level `def` per name across sources, hook names and `main` excluded | `FAIL new: name: files` | `dup-functions.txt` |
| registry-parity | Registry parity | every `case` arm a `launchd/*.sh` dispatches appears in its usage line | `FAIL new: script:name`; `ERROR` when no arm or no usage parsed | `undocumented.txt` |
| orientation-truth | Orientation truth | every repo path cited in backticks in `README.md` and any `CLAUDE.md` exists, and every `MODEL_GATEWAY_*` they cite is read by a source | `FAIL new: token`; `ERROR` when README is missing | `dangling.txt` |

`scripts/checks.sh` implements these with the skeleton's `say`, `ratchet` and `ratchet_counts`, over the files git tracks or would add. Its `setup` line is `ERROR` when git lists nothing, no source is listed, a listed file is unreadable, or a path holds a space (the greps word-split the list). `spelling-env-prefix` reads every `*.sh` source and `model_gateway/env.py`. `orientation-truth` checks the first word of each backticked citation: a relative path with a `/` must exist; a bare `*.py`, `*.sh` or `*.md` name must exist at the root or in `launchd/`, since the installed copy is flat. Exit 0 all PASS, 1 any FAIL, 2 any ERROR; `--measure` lowers baselines and never adds. Its tests are `tests/scripts/test_checks.py`.

The gate target, `scripts/gate.sh`, run from the plugin root on the host:

1. `compile`: `py_compile` of every tracked or new `*.py`, into a temporary directory, so the tree gets no bytecode.
2. `shellcheck`: every `*.sh`; a missing `shellcheck` is `ERROR` naming `brew install shellcheck`.
3. `checks`: `scripts/checks.sh`, its lines passed through.
4. `tests`: `/usr/bin/python3 -I -B -m unittest discover -s tests -t .` under `env -i` with a temporary `HOME`, so a test that reached for the real home would find an empty one; `Ran 0 tests`, a missing `Ran` line or any skipped test is `ERROR`.

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

Not applicable: greenfield. The predecessor stays where it is, untouched; nothing moves out of it. Reuse manifest (predecessor → here):

- `gateway.py` → split into `model_gateway/*.py` · rewritten (the passthrough, relay, retry, chunked read, capture, settings reload, health, drain kept; every caching and output-cap rewrite, filter, usage row and stats function dropped)
- `run.sh`, `ctl.sh`, `install.sh`, `uninstall.sh` → `launchd/` · rewritten for the K3 names
- `tests/test_gateway.py` → `tests/model_gateway/test_*.py` · the passthrough, relay, chunked, retry, capture, settings, health, drain and client-reset tests ported; the rest dropped with their code
- `tests/test_run.py` → `tests/launchd/test_run.py` · ported
- `tests/fixtures/` · dropped
- `README.md` → `README.md` · rewritten

## 9. Brief template and work tree

A task in this repository carries:

- anchors: `path:line` quotes for every fact the builder depends on (the hook signature in `model_gateway/hooks.py`, the settings key in `model_gateway/settings.py`)
- commands: `scripts/gate.sh` from the plugin root; one test module as `/usr/bin/python3 -I -B -m unittest discover -s tests -t . -p test_hooks.py` (`-I` keeps the root off `sys.path`, so a dotted module name does not import)
- consumers: a middleware repository built against the contract starts at `model_gateway/hooks.py` (the per-request objects) and `model_gateway/harness.py` (its tests)

The work root lives outside the tree: `{flight directory}/` with `requirements.md` (the brief), `run.md` (progress), `receipts/` (gate and test output), `red/` (watched-failing logs).

## 10. Open rulings

- Size ceilings. Recommendation: 400 lines per source, 600 per test file, slack 5. Alternative: 300 per source would put the largest modules over and force a split of `proxy.py` with no co-change reason. Counts at build time are recorded in `scripts/baselines/` (empty: none over).
- Formatter. Recommendation: none until the owner admits a non-stdlib tool; the stdlib-only rule (K1) excludes `black` and `ruff format`. Alternative: pin `ruff` in a versions file and add a `format` link; cost: a tool install on every host that runs the gate.
- Python linter. Recommendation: same as the formatter — `ruff check` with the classes of § Lint, changed-lines view, once a tool is admitted. Until then `py_compile` and the `dup-functions` census are the only static Python links.
- Clone detector over shell and config. Recommendation: none for four shell scripts; revisit when `launchd/` grows past them.
- Coverage floor. Recommendation: none until a tool is admitted; `trace` lacks a floor mechanism.
- Suite wall time. Recommendation: a 60-second suite budget once the owner sets a tolerance; the suite runs in about 2 minutes today, most of it real processes (drain, restart, `run.sh`) and the `scripts/checks.sh` tests, each a copy of the tree.
- Clean-environment run. Recommendation: the gate's `tests` link already runs under `env -i` with a temporary `HOME`; a container or CI runner joins when the repository gets a remote. Alternative: run the gate in a macOS CI runner; cost: none until a remote exists.
- Publication link. Recommendation: run the parent project's leak gate over every tracked file before each publish, and add a pre-push hook once the repository has a remote.
