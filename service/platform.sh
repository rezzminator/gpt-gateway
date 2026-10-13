#!/bin/sh
# The service-manager boundary: paths, stored environment and lifecycle on macOS and Linux.
# shellcheck disable=SC2034 # The entry scripts consume these shared path variables.

service_init() {
  PLATFORM=$(uname -s)
  DEST="$HOME/.local/share/gpt-gateway"
  STATE="$HOME/.local/state/gpt-gateway"
  BACKUPS="$STATE/backups"
  case "$PLATFORM" in
    Darwin)
      LABEL=${GPT_GATEWAY_LABEL:-local.gpt-gateway}
      DOMAIN="gui/$(id -u)"
      PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
      AGENT="$PLIST"
      LOGS="$HOME/Library/Logs"
      ;;
    Linux)
      UNIT=gpt-gateway.service
      UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
      AGENT="$UNIT_DIR/$UNIT"
      LOGS="$STATE"
      ;;
    *) echo "gpt-gateway: unsupported platform $PLATFORM (macOS or Linux required)" >&2; exit 1 ;;
  esac
}

service_jq() {
  JQ=${GPT_GATEWAY_JQ:-$(command -v jq)} || { echo "$(basename "$0"): jq is required" >&2; return 1; }
  [ -x "$JQ" ] || { echo "$(basename "$0"): jq is required" >&2; return 1; }
}

service_require_linux() {
  version=$(systemctl --version) || return 1
  version=$(printf '%s\n' "$version" | sed -n 's/^systemd \([0-9][0-9]*\).*/\1/p' | head -n 1)
  case "$version" in ''|*[!0-9]*) version=0 ;; esac
  [ "$version" -ge 252 ] || { echo 'install.sh: systemd 252+ is required' >&2; return 1; }
  systemctl --user show-environment >/dev/null \
    || { echo 'install.sh: could not reach the systemd user bus' >&2; return 1; }
}

service_activity() {
  activity=$(systemctl --user show "$UNIT" --property=ActiveState --property=UnitFileState) || return 2
  SERVICE_ACTIVITY=$(printf '%s\n' "$activity" | sed -n 's/^ActiveState=//p')
  SERVICE_ENABLED=$(printf '%s\n' "$activity" | sed -n 's/^UnitFileState=//p')
  [ -n "$SERVICE_ACTIVITY" ] && [ -n "$SERVICE_ENABLED" ] || return 2
}

service_loaded() {
  if [ "$PLATFORM" = Linux ]; then systemctl --user is-active --quiet "$UNIT"
  else launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; fi
}

service_disabled() {
  if [ "$PLATFORM" = Linux ]; then
    [ -f "$AGENT" ] && ! systemctl --user is-enabled --quiet "$UNIT"
  else
    launchctl print-disabled "$DOMAIN" 2>/dev/null | grep -q "\"$LABEL\" => \(disabled\|true\)"
  fi
}

service_enable() {
  if [ "$PLATFORM" = Linux ]; then systemctl --user enable "$UNIT"
  else launchctl enable "$DOMAIN/$LABEL"; fi
}

service_disable() {
  if [ "$PLATFORM" = Linux ]; then systemctl --user disable "$UNIT"
  else launchctl disable "$DOMAIN/$LABEL"; fi
}

service_start() {
  if [ "$PLATFORM" = Linux ]; then systemctl --user start "$UNIT"
  else launchctl bootstrap "$DOMAIN" "$PLIST"; fi
}

service_stop() {
  if [ "$PLATFORM" = Linux ]; then systemctl --user stop "$UNIT"
  else launchctl bootout "$DOMAIN/$LABEL"; fi
}

service_hup() {
  if [ "$PLATFORM" = Linux ]; then systemctl --user kill -s HUP --kill-whom=main "$UNIT"
  else launchctl kill HUP "$DOMAIN/$LABEL"; fi
}

service_read() {
  if [ "$PLATFORM" = Linux ]; then
    CONFIG="$DEST/service.json"
    record=$("$JQ" -ce 'select(type == "object" and
      ([.GPT_GATEWAY_PORT, .GPT_GATEWAY_CLAUDE_SETTINGS, .GPT_GATEWAY_JQ] | all(type == "string" and length > 0)))' "$CONFIG" 2>/dev/null) || record=$(
      sed -n 's/^Environment=//p' "$AGENT" | "$JQ" -Rsc '
        split("\n") | map(select(length > 0) | fromjson | gsub("%%"; "%")
          | capture("^(?<key>[^=]+)=(?<value>.*)$")) | from_entries'
    ) || return 1
    recorded_port=$(printf '%s\n' "$record" | "$JQ" -er '.GPT_GATEWAY_PORT | select(type == "string" and length > 0)') || return 1
    recorded_settings=$(printf '%s\n' "$record" | "$JQ" -er '.GPT_GATEWAY_CLAUDE_SETTINGS | select(type == "string" and length > 0)') || return 1
    recorded_jq=$(printf '%s\n' "$record" | "$JQ" -er '.GPT_GATEWAY_JQ | select(type == "string" and length > 0)') || return 1
    PORT=$recorded_port
    SETTINGS=$recorded_settings
    if [ -x "$recorded_jq" ]; then JQ=$recorded_jq; fi
  else
    PORT=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_PORT raw "$PLIST")
    SETTINGS=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_CLAUDE_SETTINGS raw "$PLIST")
    JQ=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_JQ raw "$PLIST")
  fi
}

service_write_linux() {
  mkdir -p "$UNIT_DIR"
  "$PY" -I - "$AGENT.new" "$DEST" "$PORT" "$LOGS" "$DRAIN" "$PY" "$JQ" "$SETTINGS" "$HOME" "$STOP_SECONDS" <<'PY'
import json
import os
import sys

unit, dest, port, logs, drain, python, jq, settings, home, stop_seconds = sys.argv[1:]
config = {
    "GPT_GATEWAY_PORT": port,
    "GPT_GATEWAY_LOG": os.path.join(logs, "gpt-gateway.log"),
    "GPT_GATEWAY_DRAIN_SECONDS": drain,
    "GPT_GATEWAY_STOP_SECONDS": stop_seconds,
    "GPT_GATEWAY_PYTHON": python,
    "GPT_GATEWAY_JQ": jq,
    "GPT_GATEWAY_CLAUDE_SETTINGS": settings,
}
with open(os.path.join(dest, "service.json.new"), "w") as output:
    json.dump(config, output, indent=2)
    output.write("\n")

def quoted(value):
    return json.dumps(value.replace("%", "%%"), ensure_ascii=False)

with open(unit, "w") as output:
    output.write("[Unit]\nDescription=gpt-gateway\n\n[Service]\nType=simple\n")
    # ':' disables systemd's dollar expansion in the installed script path.
    output.write("ExecStart=:/bin/sh " + quoted(os.path.join(dest, "run.sh")) + " serve\n")
    for name, value in dict(config, HOME=home).items():
        output.write("Environment=" + quoted(name + "=" + value) + "\n")
    output.write("Restart=on-failure\nRestartSec=2\nKillMode=mixed\nTimeoutStopSec=" + stop_seconds + "\n")
    # StandardError takes a literal path after append:, rather than a quoted word.
    stderr = os.path.join(logs, "gpt-gateway.stderr.log").replace("%", "%%")
    output.write("StandardOutput=null\nStandardError=append:" + stderr + "\n")
    output.write("\n[Install]\nWantedBy=default.target\n")
PY
  mv "$DEST/service.json.new" "$DEST/service.json"
}
