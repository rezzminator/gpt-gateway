# Linux service

The owner runs these commands from the reviewed checkout during a quiet window. Each block runs in a shell with `-eu` and stops at its first unexpected failure. Every Claude Code session reading the selected settings file is routed when the gateway becomes healthy. Rollback preserves the current shared settings; only the gateway lifecycle changes its two owned keys.

## Contents

- [Prepare and switch on](#prepare-and-switch-on)
- [Verify](#verify)
- [Switch off](#switch-off)
- [Roll back](#roll-back)

## Prepare and switch on

Pause new Claude Code requests and allow active work to settle. Linux requires systemd 252+, Python 3.9+, jq and curl. Without linger the user manager stops at logout; an owner requiring service between logins enables it separately with `loginctl enable-linger "$USER"`.

This block saves the gateway's installed files, unit and enabled state before stopping an existing gateway. It never snapshots or replaces the shared Claude Code settings. Keep the printed rollback directory. `GPT_GATEWAY_CLAUDE_SETTINGS` selects another settings file; `GPT_GATEWAY_PORT` selects another port.

```sh
/bin/sh -eu <<'SH'
for gateway_command in python3 jq curl shellcheck systemctl ss loginctl; do
  command -v "$gateway_command" >/dev/null
done
loginctl show-user "$USER" -p Linger
systemctl --user show-environment >/dev/null
gateway_settings=${GPT_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
gateway_port=${GPT_GATEWAY_PORT:-18787}
test -f "$gateway_settings"
scripts/gate.sh
gateway_backup="$HOME/.local/state/gpt-gateway/backups/pre-linux-$(date +%Y%m%d-%H%M%S)"
gateway_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$gateway_backup"
if [ -d "$HOME/.local/share/gpt-gateway" ]; then
  tar -C "$HOME/.local/share" -czf "$gateway_backup/install.tgz" gpt-gateway
fi
if [ -f "$gateway_unit_dir/gpt-gateway.service" ]; then
  cp -p "$gateway_unit_dir/gpt-gateway.service" "$gateway_backup/gpt-gateway.service"
  if gateway_enabled=$(systemctl --user is-enabled gpt-gateway.service); then
    printf 'on\n' > "$gateway_backup/state"
  else
    case "$gateway_enabled" in
      disabled) printf 'off\n' > "$gateway_backup/state" ;;
      *) echo 'Could not determine the previous enabled state' >&2; exit 1 ;;
    esac
  fi
  "$HOME/.local/share/gpt-gateway/ctl.sh" stop
  systemctl --user stop gpt-gateway.service
else
  printf 'absent\n' > "$gateway_backup/state"
fi
printf 'Rollback directory: %s\n' "$gateway_backup"
gateway_listener=$(ss -H -ltn "sport = :$gateway_port")
if [ -n "$gateway_listener" ]; then
  printf 'Chosen port is already occupied: %s\n' "$gateway_listener" >&2
  exit 1
fi
service/install.sh --port "$gateway_port"
"$HOME/.local/share/gpt-gateway/ctl.sh" start
"$HOME/.local/share/gpt-gateway/ctl.sh" status
SH
```

The gate must end `GATE PASS`. Installation validates the systemd minimum and user bus before changing the install. If a step fails, keep the quiet window open and use the rollback block; successful earlier steps are not automatically reversed.

## Verify

```sh
/bin/sh -eu <<'SH'
gateway_settings=${GPT_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
gateway_port=${GPT_GATEWAY_PORT:-18787}
gateway_url="http://127.0.0.1:$gateway_port"
"$HOME/.local/share/gpt-gateway/ctl.sh" status
systemctl --user is-enabled gpt-gateway.service
systemctl --user is-active gpt-gateway.service
curl -fsS --max-time 2 "$gateway_url/__gateway/health"
jq -e --arg url "$gateway_url" '.env.ANTHROPIC_BASE_URL == $url' "$gateway_settings"
jq '.env | {ANTHROPIC_BASE_URL, ENABLE_TOOL_SEARCH}' "$gateway_settings"
SH
```

Expect `state:  on`, `activity: active`, an `ok pid=...` health line, `enabled`, `active`, and a successful URL check. Tool search is `"true"` unless a user-selected value already exists. Resume the waiting sessions after verification. Supervisor stderr and the installation-test log live under `~/.local/state/gpt-gateway/`; the text log `gpt-gateway.log` and the level logs under `~/.local/state/gpt-gateway/logs/` (or `$PLUGIN_LOG_DIR/gpt-gateway/`).

An update uses the same install command and preserves the enabled state. Linux HUP hands the successor the same listening socket and accept queue, preserving queued connections and accepted requests through the drain limit. An accepted client that has sent no request data closes as idle at drain start. Existing streams finish on their original gateway.

## Switch off

```sh
/bin/sh -eu <<'SH'
"$HOME/.local/share/gpt-gateway/ctl.sh" stop
systemctl --user stop gpt-gateway.service
"$HOME/.local/share/gpt-gateway/ctl.sh" status
gateway_settings=${GPT_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
gateway_url="http://127.0.0.1:${GPT_GATEWAY_PORT:-18787}"
jq -e --arg url "$gateway_url" '.env.ANTHROPIC_BASE_URL != $url' "$gateway_settings"
SH
```

Expect `state:  off` and a successful URL inequality check. The setting leaves before in-flight streams finish; systemctl waits for the drain. An unreadable settings file is reported as `could not read settings`, and cleanup retries alongside the full gateway drain until one second before the stop deadline instead of reporting success. A missing settings file is already unlinked and needs no retry.

## Roll back

Keep the quiet window open. Set `gateway_backup` in your shell to the directory printed during preparation. Validate that directory before removing the current install. All settings changes below occur through uninstall or the restored gateway's start; later edits made by other sessions stay intact.

```sh
/bin/sh -eu -s -- "${gateway_backup:?set gateway_backup to the printed rollback directory}" <<'SH'
gateway_backup=$1
gateway_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
gateway_previous=$(cat "$gateway_backup/state")
case "$gateway_previous" in on|off|absent) ;; *) echo 'Invalid rollback state' >&2; exit 1 ;; esac
service/uninstall.sh
if [ -f "$gateway_backup/install.tgz" ]; then
  mkdir -p "$HOME/.local/share"
  tar -C "$HOME/.local/share" -xzf "$gateway_backup/install.tgz"
fi
if [ -f "$gateway_backup/gpt-gateway.service" ]; then
  mkdir -p "$gateway_unit_dir"
  cp -p "$gateway_backup/gpt-gateway.service" "$gateway_unit_dir/gpt-gateway.service"
fi
systemctl --user daemon-reload
if [ "$gateway_previous" = on ]; then
  "$HOME/.local/share/gpt-gateway/ctl.sh" start
fi
if [ "$gateway_previous" != absent ]; then
  "$HOME/.local/share/gpt-gateway/ctl.sh" status
fi
printf 'Rollback completed: %s\n' "$gateway_previous"
SH
```

A previously on install re-links only after its health check succeeds. A failed start stops rollback immediately, leaving the gateway URL unlinked. A prior off state stays disabled; an absent service stays uninstalled. Backups remain under `~/.local/state/gpt-gateway/backups/`; rollback preserves logs and the database.
