#!/bin/sh
# Supervises gateway.py for launchd and keeps Claude Code pointed at it only
# while it answers. Once the gateway is healthy, it sets env.ANTHROPIC_BASE_URL
# in Claude Code's settings. It removes that setting again (fail open: Claude
# Code talks to the API directly) when Python is missing, when the gateway
# fails to start or exits, and when the agent is stopped. A URL other than
# this gateway's is never touched.
#
# Signals:
#   HUP   graceful restart: starts a new gateway from the gateway.py on disk, on
#         the same port (SO_REUSEPORT; new connections go to the newest), and once
#         it answers, tells the old one to drain. No in-flight stream is cut.
#   TERM  stop: removes the setting first, then lets every gateway drain and exits.
#
# Usage: run.sh serve. Configuration: the plist's environment. The gateway reads
# settings.json next to it itself and applies an edit there without a restart;
# a settings.json or a middleware it rejects at start fails open.
set -u

PY=${MODEL_GATEWAY_PYTHON:-/usr/bin/python3}
JQ=${MODEL_GATEWAY_JQ:-/usr/bin/jq}
SETTINGS=${MODEL_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
PORT=${MODEL_GATEWAY_PORT:-18787}
RETRY=${MODEL_GATEWAY_RETRY_SECONDS:-60}
URL="http://127.0.0.1:$PORT"
DIR=$(cd "$(dirname "$0")" && pwd)
pid=
reload=

log() { printf '%s run.sh %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

current() { "$JQ" -r '.env.ANTHROPIC_BASE_URL // ""' "$SETTINGS" 2>/dev/null; }

# Rewrites the settings file through jq with $1 as the filter, atomically.
edit() {
  tmp=$(mktemp "$SETTINGS.XXXXXX") || return 1
  if "$JQ" --arg url "$URL" "$1" "$SETTINGS" > "$tmp"; then
    chmod 644 "$tmp" && mv "$tmp" "$SETTINGS"
  else
    rm -f "$tmp"
    return 1
  fi
}

# Tool search rides the link: Claude Code turns it off behind a non-Anthropic base URL unless this is set. A value the
# user chose is kept, linked or not; a "true" reads as the one this script wrote and is removed with the URL.
tool_search() { "$JQ" -r '.env.ENABLE_TOOL_SEARCH // ""' "$SETTINGS" 2>/dev/null; }

link() {
  cur=$(current)
  if [ -n "$cur" ] && [ "$cur" != "$URL" ]; then log "settings point at $cur, not this gateway; left unchanged"; return 0; fi
  if [ "$cur" = "$URL" ] && [ -n "$(tool_search)" ]; then return 0; fi
  # shellcheck disable=SC2016 # $url is a jq variable bound by --arg, not a shell expansion
  edit '.env = ((.env // {}) + {ANTHROPIC_BASE_URL: $url}) | if .env | has("ENABLE_TOOL_SEARCH") then . else .env.ENABLE_TOOL_SEARCH = "true" end' \
    && log "linked: Claude Code uses $URL with tool search on"
}

unlink() {
  if [ "$(current)" != "$URL" ]; then return 0; fi
  edit 'del(.env.ANTHROPIC_BASE_URL) | if .env.ENABLE_TOOL_SEARCH == "true" then del(.env.ENABLE_TOOL_SEARCH) else . end' \
    && log "unlinked: Claude Code talks to the API directly"
}

health_pid() {
  curl -sf --max-time 1 "$URL/__gateway/health" 2>/dev/null | sed -n 's/^ok pid=\([0-9]*\).*/\1/p'
}

# Starts a gateway and waits until it answers on the port.
# Sets $new on success; on failure kills it and returns 1.
start_gateway() {
  "$PY" -I "$DIR/gateway.py" &
  new=$!
  i=0
  until [ "$(health_pid)" = "$new" ]; do
    if ! kill -0 "$new" 2>/dev/null; then
      wait "$new"
      log "gateway exited with status $? before answering $URL"
      return 1
    fi
    i=$((i + 1))
    if [ $i -ge 60 ]; then
      kill -TERM "$new" 2>/dev/null; wait "$new" 2>/dev/null
      log "gateway did not answer $URL/__gateway/health within 15 seconds"
      return 1
    fi
    sleep 0.25
  done
}

stop() {
  unlink
  # Every gateway still running is a child of this script: the serving one, any still draining
  # after a restart, and one still starting. Signalling by parent never hits an exited one's reused pid.
  pkill -TERM -P $$ 2>/dev/null
  log "stopping: gateways draining their in-flight requests"
  wait
  log "stopped"
  exit 0
}

# Fails open, then waits before exiting so launchd's restarts stay slow.
fail() {
  log "$1"
  unlink
  sleep "$RETRY" & wait $!
  exit 1
}

restart() {
  if start_gateway; then
    kill -TERM "$pid" 2>/dev/null
    log "restarted: gateway $new took over, $pid drains its in-flight requests"
    pid=$new
  else
    log "restart failed; gateway $pid keeps serving"
  fi
}

supervise() {
  trap stop TERM INT
  trap 'reload=1' HUP
  # Without the developer tools, /usr/bin/python3 is a stub that opens an install dialog.
  if [ "$PY" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
    fail "no command line developer tools, so no $PY"
  fi
  "$PY" -I -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1 \
    || fail "$PY is missing or older than Python 3.9"
  start_gateway || fail "the gateway could not start"
  pid=$new
  link

  while :; do
    wait "$pid"
    status=$?
    if [ -n "$reload" ]; then
      reload=
      restart
      continue
    fi
    if kill -0 "$pid" 2>/dev/null; then continue; fi  # wait was interrupted, the gateway still runs
    log "gateway exited with status $status"
    unlink
    exit 1
  done
}

case "${1:-}" in
  serve) supervise ;;
  *) echo "usage: run.sh serve" >&2; exit 2 ;;
esac
