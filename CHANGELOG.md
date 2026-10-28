# Changelog

## [Unreleased]

### Changed

- `status_label` answers report zero usage throughout: the `output_tokens` figure that counted the label's words is gone, since no model produced the label and the gateway counts no tokens for it.
- Every log the gateway writes lives in the plugin log directory: `$PLUGIN_LOG_DIR/gpt-gateway/`, else `~/.local/state/gpt-gateway/logs/`. The level rows go through the shared plugin-log library, vendored as `gpt_gateway/plugin_log.py`: rotated level files keep five generations, and a row that cannot be written is held for the next write.
- stderr shows only error lines and one line per failing streak of the log: `gateway.py mw` reports a middleware without a cli and an unknown middleware as error rows, and a failing text log is a `text-log-failed` error row.

### Removed

- Migration: the `GPT_GATEWAY_LOG` variable is gone; the text log now lives at `$PLUGIN_LOG_DIR/gpt-gateway/gpt-gateway.log`, else `~/.local/state/gpt-gateway/logs/gpt-gateway.log`. Reinstall with `service/install.sh` to drop the variable from the service definition; the old file (`~/Library/Logs/gpt-gateway.log` on macOS, `~/.local/state/gpt-gateway/gpt-gateway.log` on Linux) is no longer written, and `service/uninstall.sh --purge` still deletes it.
