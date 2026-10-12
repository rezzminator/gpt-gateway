#!/bin/sh
# Removes gpt-gateway. Stopping the agent makes run.sh point Claude Code back
# at the API at once and lets in-flight streams finish; if the setting is still
# there afterwards (run.sh was not running), this script removes it itself.
# Then it deletes the launchd agent and the installed copy, keeping settings.json
# and middleware/ in ~/.local/state/gpt-gateway/backups/ first.
# --purge also deletes the logs and the database.
set -eu

PURGE=no
case "${1:-}" in
  --purge) PURGE=yes ;;
  '') ;;
  *) echo "usage: uninstall.sh [--purge]" >&2; exit 2 ;;
esac

LABEL=${GPT_GATEWAY_LABEL:-local.gpt-gateway}
DEST="$HOME/.local/share/gpt-gateway"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs"
BACKUPS="$HOME/.local/state/gpt-gateway/backups"
DB="$HOME/.local/state/gpt-gateway/gateway.db"
DOMAIN="gui/$(id -u)"

PORT=18787
SETTINGS=${GPT_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
JQ=${GPT_GATEWAY_JQ:-jq}
if [ -f "$PLIST" ]; then
  PORT=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_PORT raw "$PLIST" 2>/dev/null || echo "$PORT")
  SETTINGS=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_CLAUDE_SETTINGS raw "$PLIST" 2>/dev/null || echo "$SETTINGS")
  JQ=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_JQ raw "$PLIST" 2>/dev/null || echo "$JQ")
fi
URL="http://127.0.0.1:$PORT"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "stopping the agent; in-flight streams finish first"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; do sleep 0.25; done
  echo "agent stopped"
else
  echo "agent was not running"
fi
launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true  # drop any off-state record

if [ -f "$SETTINGS" ] && [ "$("$JQ" -r '.env.ANTHROPIC_BASE_URL // ""' "$SETTINGS")" = "$URL" ]; then
  mkdir -p "$BACKUPS"
  cp -p "$SETTINGS" "$BACKUPS/settings.json.pre-uninstall-$(date +%Y%m%d-%H%M%S)"
  TMP=$(mktemp "$SETTINGS.XXXXXX")
  "$JQ" 'del(.env.ANTHROPIC_BASE_URL) | if .env.ENABLE_TOOL_SEARCH == "true" then del(.env.ENABLE_TOOL_SEARCH) else . end' "$SETTINGS" > "$TMP"
  chmod 644 "$TMP"
  mv "$TMP" "$SETTINGS"
fi
echo "Claude Code talks to the API directly ($SETTINGS)"

if [ -f "$DEST/settings.json" ] || [ -d "$DEST/middleware" ]; then
  KEEP="$BACKUPS/uninstall-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$KEEP"
  [ -f "$DEST/settings.json" ] && cp -p "$DEST/settings.json" "$KEEP/"
  [ -d "$DEST/middleware" ] && cp -Rp "$DEST/middleware" "$KEEP/"
  echo "kept settings.json and middleware/ in $KEEP"
fi
rm -f "$PLIST"
rm -rf "$DEST"
if [ "$PURGE" = yes ]; then
  rm -f "$LOGS/gpt-gateway.log" "$LOGS/gpt-gateway.log.1" "$LOGS/gpt-gateway.stderr.log" "$LOGS/gpt-gateway-install-tests.log"
  rm -f "$DB" "$DB-wal" "$DB-shm"
  echo "logs and database deleted"
fi
echo "gpt-gateway removed"
