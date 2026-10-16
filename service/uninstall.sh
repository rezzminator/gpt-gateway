#!/bin/sh
# Removes gpt-gateway. Stopping the agent makes run.sh point Claude Code back
# at the API at once and lets in-flight streams finish; if the setting is still
# there afterwards (run.sh was not running), this script removes it itself.
# Then it deletes the service and the installed copy, keeping settings.json
# and middleware/ in ~/.local/state/gpt-gateway/backups/ first.
# --purge also deletes the logs and the database.
set -eu

PURGE=no
case "${1:-}" in
  --purge) PURGE=yes ;;
  '') ;;
  *) echo "usage: uninstall.sh [--purge]" >&2; exit 2 ;;
esac

DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=service/platform.sh
. "$DIR/platform.sh"
service_init
DB="$HOME/.local/state/gpt-gateway/gateway.db"

PORT=18787
SETTINGS=${GPT_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
service_jq
if [ "$PLATFORM" = Linux ] && [ -f "$AGENT" ]; then
  service_read || echo 'uninstall.sh: could not read the service environment; using supplied settings and port' >&2
elif [ "$PLATFORM" = Darwin ] && [ -f "$PLIST" ]; then
  PORT=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_PORT raw "$PLIST" 2>/dev/null || echo "$PORT")
  SETTINGS=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_CLAUDE_SETTINGS raw "$PLIST" 2>/dev/null || echo "$SETTINGS")
  JQ=$(plutil -extract EnvironmentVariables.GPT_GATEWAY_JQ raw "$PLIST" 2>/dev/null || echo "$JQ")
fi
URL="http://127.0.0.1:$PORT"
# shellcheck source=service/settings.sh
. "$DIR/settings.sh"
settings_route "$DEST" || { echo "uninstall.sh: cannot read the route secret ($DEST/route-secret); remove env.ANTHROPIC_BASE_URL from $SETTINGS by hand, or delete that file and run again" >&2; exit 1; }

if [ "$PLATFORM" = Linux ] && [ -f "$AGENT" ]; then service_disable; fi
if service_loaded; then
  echo "stopping the agent; in-flight streams finish first"
  if [ "$PLATFORM" = Linux ]; then service_stop
  else service_stop 2>/dev/null || true; fi
  while service_loaded; do sleep 0.25; done
  echo "agent stopped"
else
  echo "agent was not running"
fi
if [ "$PLATFORM" = Darwin ]; then service_enable 2>/dev/null || true; fi  # drop any off-state record

if settings_ours; then
  mkdir -p "$BACKUPS"
  cp -p "$SETTINGS" "$BACKUPS/settings.json.pre-uninstall-$(date +%Y%m%d-%H%M%S)"
fi
settings_unlink "${GPT_GATEWAY_STOP_SECONDS:-630}" || exit 1
echo "Claude Code talks to the API directly ($SETTINGS)"

if [ -f "$DEST/settings.json" ] || [ -d "$DEST/middleware" ]; then
  KEEP="$BACKUPS/uninstall-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$KEEP"
  [ -f "$DEST/settings.json" ] && cp -p "$DEST/settings.json" "$KEEP/"
  [ -d "$DEST/middleware" ] && cp -Rp "$DEST/middleware" "$KEEP/"
  echo "kept settings.json and middleware/ in $KEEP"
fi
rm -f "$AGENT"
if [ "$PLATFORM" = Linux ]; then
  systemctl --user daemon-reload
  systemctl --user reset-failed "$UNIT" 2>/dev/null || true
fi
rm -rf "$DEST"
if [ "$PURGE" = yes ]; then
  rm -f "$LOGS/gpt-gateway.log" "$LOGS/gpt-gateway.log.1" "$LOGS/gpt-gateway.stderr.log" "$LOGS/gpt-gateway-install-tests.log"
  rm -f "$DB" "$DB-wal" "$DB-shm"
  echo "logs and database deleted"
fi
echo "gpt-gateway removed"
