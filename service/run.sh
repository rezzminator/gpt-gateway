#!/bin/sh
# Supervises gateway.py for launchd or systemd and keeps Claude Code pointed at it only
# while it answers. Once the gateway is healthy, it sets env.ANTHROPIC_BASE_URL
# in Claude Code's settings. It removes that setting again (fail open: Claude
# Code talks to the API directly) when Python is missing, when the gateway
# fails to start or exits, and when the agent is stopped. A URL other than
# this gateway's is never touched. An install holding a route-secret links the URL with that secret as its first
# path segment: the gateway strips it, and a GPT route request without it is refused.
#
# Signals:
#   HUP   graceful restart: starts a new gateway from the gateway.py on disk, on
#         the inherited listener on Linux, or the same port (SO_REUSEPORT) on macOS; once
#         it answers, tells the old one to drain. No in-flight stream is cut.
#   TERM  stop: removes the setting first, then lets every gateway drain and exits.
#
# Usage: run.sh serve. Configuration: the service manager's environment. The gateway reads
# settings.json next to it itself and applies an edit there without a restart;
# a settings.json or a middleware it rejects at start fails open.
set -u
trap 'reload=1' HUP

PY=${GPT_GATEWAY_PYTHON:-/usr/bin/python3}
JQ=${GPT_GATEWAY_JQ:-/usr/bin/jq}
SETTINGS=${GPT_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
PORT=${GPT_GATEWAY_PORT:-18787}
RETRY=${GPT_GATEWAY_RETRY_SECONDS:-60}
URL="http://127.0.0.1:$PORT"
DIR=$(cd "$(dirname "$0")" && pwd)
pid=
reload=

log() { printf '%s run.sh %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

# shellcheck source=service/settings.sh
. "$DIR/settings.sh"
route_failed=
if [ -e "$DIR/route-secret" ]; then
  GPT_GATEWAY_ROUTE_SECRET_PATH="$DIR/route-secret"
  export GPT_GATEWAY_ROUTE_SECRET_PATH
fi
settings_route "$DIR" || route_failed=yes

# Tool search rides the link: Claude Code turns it off behind a non-Anthropic base URL unless this is set. A value the
# user chose is kept, linked or not; a "true" reads as the one this script wrote and is removed with the URL.
tool_search() { "$JQ" -r '.env.ENABLE_TOOL_SEARCH // ""' "$SETTINGS" 2>/dev/null; }

link() {
  cur=$(settings_current) || { log "could not read settings ($SETTINGS)"; return 1; }
  if [ -n "$cur" ] && ! settings_is_ours "$cur"; then log "settings point at another URL, not this gateway; left unchanged"; return 0; fi
  if [ "$cur" = "$LINK_URL" ] && [ -n "$(tool_search)" ]; then return 0; fi
  # shellcheck disable=SC2016 # $url is a jq variable bound by --arg, not a shell expansion
  settings_edit '.env = ((.env // {}) + {ANTHROPIC_BASE_URL: $url}) | if .env | has("ENABLE_TOOL_SEARCH") then . else .env.ENABLE_TOOL_SEARCH = "true" end' \
    && log "linked: Claude Code uses $URL with tool search on"
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

close_listener() {
  if [ -n "${GPT_GATEWAY_LISTENER_FD:-}" ]; then exec 3<&-; fi
}

stop() {
  trap '' HUP
  stopped=0
  stop_seconds=${GPT_GATEWAY_STOP_SECONDS:-630}
  case "$stop_seconds" in ''|*[!0-9]*) log 'invalid settings stop deadline'; stop_seconds=0; stopped=1 ;; esac
  stop_deadline=$(($(date +%s) + stop_seconds - 1))
  # Try once before TERM; failed cleanup retries alongside the full gateway drain.
  if settings_unlink 0; then unlinked=yes; else unlinked=no; fi
  close_listener
  # Every gateway still running is a child of this script: the serving one, any still draining
  # after a restart, and one still starting. Signalling by parent never hits an exited one's reused pid.
  pkill -TERM -P $$ 2>/dev/null
  log "stopping: gateways draining their in-flight requests"
  if [ "$unlinked" = no ]; then
    unlink_seconds=$((stop_deadline - $(date +%s)))
    [ "$unlink_seconds" -ge 0 ] || unlink_seconds=0
    settings_unlink "$unlink_seconds" || stopped=1
  fi
  wait
  log "stopped"
  exit "$stopped"
}

# Fails open, then waits before exiting so the service manager's restarts stay slow.
fail() {
  log "$1"
  close_listener
  settings_unlink || true
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
  [ -z "$route_failed" ] || fail "cannot read the installed route secret ($DIR/route-secret)"
  # Without the developer tools, /usr/bin/python3 is a stub that opens an install dialog.
  if [ "$(uname -s)" = Darwin ] && [ "$PY" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
    fail "no command line developer tools, so no $PY"
  fi
  "$PY" -I -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1 \
    || fail "$PY is missing or older than Python 3.9"
  if [ "$(uname -s)" = Linux ] && [ -z "${GPT_GATEWAY_LISTENER_FD:-}" ]; then
    trap '' HUP  # inherited by the interpreter until listener.py installs its startup handler
    export GPT_GATEWAY_LISTENER_FD=3
    exec "$PY" -I "$DIR/listener.py" "$PORT" /bin/sh "$DIR/run.sh" serve
  fi
  start_gateway || fail "the gateway could not start"
  pid=$new
  link

  while :; do
    # Checked before the wait too: a HUP trapped while run.sh was busy (linking, logging, restarting) interrupts no wait.
    if [ -n "$reload" ]; then
      reload=
      restart
      continue
    fi
    wait "$pid"
    status=$?
    if [ -n "$reload" ]; then continue; fi
    if kill -0 "$pid" 2>/dev/null; then continue; fi  # wait was interrupted, the gateway still runs
    log "gateway exited with status $status"
    close_listener
    settings_unlink || true
    exit 1
  done
}

case "${1:-}" in
  serve) supervise ;;
  cleanup) stop ;;
  failed) trap stop TERM INT; fail "${2:-listener could not start}" ;;
  *) echo "usage: run.sh serve|cleanup|failed" >&2; exit 2 ;;
esac
