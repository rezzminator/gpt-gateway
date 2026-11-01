# Changelog

## [0.5.0] - 2026-11-01

### Added

- GPT models run as the Claude Code main session, not only as sub-agents: start Claude Code on a catalog slug, for example `claude --model gpt-6.1-sol`. The installed secret URL prefix is required for main sessions and sub-agents alike; a main session never receives the sub-agent delivery contract or the sub-agent routing header.
- `install.sh` lists GPT models in Claude Code's `/model` picker while the ChatGPT route is on. The setting `model_picker_models` (default `["gpt-6.1-sol","gpt-6-astra"]`, `[]` for none; `GPT_GATEWAY_MODEL_PICKER_MODELS` overrides one install) names them; the installer adopts identical rows, never touches rows it does not own, and `uninstall.sh` removes only its own. The local catalog cache only labels the rows: a model it does not list is refused by name when the cache reads whole; a cache that cannot be read, an entry that does not parse or a `display_name` that is no label costs only itself, with a warning on stderr, and leaves the default label.
- `status_label` gains the setting `subcommand_tools` (env `GPT_GATEWAY_STATUS_LABEL_SUBCOMMAND_TOOLS`), which adds tools whose subcommand the `Running {command}` label shows.

### Changed

- Every ChatGPT-route model is served in standard Responses mode, so GPT models can return several tool calls in one reply. `parallel_tool_calls` is on unless Claude Code disables parallel tool use; every tool, including deferred and mid-conversation tools, goes in the request's `tools` list.
- ChatGPT-route requests match Codex's: a follow-up turn is sent as a delta only after its prefix and the prior output are proven unchanged (otherwise the full request goes), requests carry Codex's session, thread, turn and routing headers, reasoning-effort changes ride a continuation, and the WebSocket uses bounded compression. Uncached input tokens drop sharply on multi-turn conversations.
- Lane keys for prompt-cache affinity hash the session and agent ids unambiguously, so a main session and a sub-agent never share a lane. Upgrading resets cache affinity once per open lane.
- The built-in `status_label` subcommand list names no particular launcher; add your own with `subcommand_tools`.
- `status_label` answers report zero usage throughout: the `output_tokens` figure that counted the label's words is gone, since no model produced the label and the gateway counts no tokens for it.
- Every log the gateway writes lives in the plugin log directory: `$PLUGIN_LOG_DIR/gpt-gateway/`, else `~/.local/state/gpt-gateway/logs/`. The level rows go through the shared plugin-log library, vendored as `gpt_gateway/plugin_log.py`: rotated level files keep five generations, and a row that cannot be written is held for the next write.
- A middleware `on_request` that raises, changes `body["model"]` or leaves an edit that does not serialize costs only its own edits and note: the body goes back to what the modules before it left, and the later modules' `on_request` still runs, where before the original bytes went upstream with every module's edits discarded. A note that is neither a string nor `None` costs only that note. A module's `check()` may return a string, written to the text log as a warning while its settings are kept.
- The `advisor_cache` middleware keeps an `advisor_agents` entry that no agent file holds and warns in the text log, instead of refusing the start or the settings edit; an agent directory that cannot be read still refuses the settings.
- A capture file that cannot be written costs only that file; only a capture folder that cannot be made stops capture for the exchange.
- stderr shows only error lines and one line per failing streak of the log: `gateway.py mw` reports a middleware without a cli and an unknown middleware as error rows, and a failing text log is a `text-log-failed` error row.

### Removed

- Migration: the `GPT_GATEWAY_LOG` variable is gone; the text log now lives at `$PLUGIN_LOG_DIR/gpt-gateway/gpt-gateway.log`, else `~/.local/state/gpt-gateway/logs/gpt-gateway.log`. Reinstall with `service/install.sh` to drop the variable from the service definition; the old file (`~/Library/Logs/gpt-gateway.log` on macOS, `~/.local/state/gpt-gateway/gpt-gateway.log` on Linux) is no longer written, and `service/uninstall.sh --purge` still deletes it.

### Fixed

- A GPT sub-agent writes the report files its instructions name: Claude Code's note against writing report files no longer overrides the agent's own save step.
- `service/uninstall.sh --purge` deletes the logs where the service wrote them, reading the recorded `PLUGIN_LOG_DIR`: the text log and every level file with its rotations.
- A ChatGPT-route stream event the gateway cannot translate costs only that event, noted in `translation_notes`, instead of ending the stream with a protocol error; notes are written for every final attempt, including a stream that closed without its completion event, and a usage count that does not read is NULL in the row and 0 for the client.
- A ChatGPT catalog entry that cannot be read is skipped, recorded as a new `catalog_fault` event, while the rest of the catalog serves.
- A tool schema whose `type` names nothing valid loses only that key; its description and other keys reach the model.
