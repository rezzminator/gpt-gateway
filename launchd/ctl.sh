#!/bin/sh
# Turns model-gateway on or off by hand. The state holds across logins
# until changed here; nothing else turns it on or off.
#
#   ctl.sh on        start it; Claude Code uses it once it answers
#   ctl.sh off       Claude Code goes straight to the API at once; in-flight
#                    streams finish on the gateway, then it stops
#   ctl.sh restart   graceful restart onto the gateway.py and middleware on disk,
#                    no stream is cut; a settings.json edit needs none, it applies
#                    at once (a changed middleware list needs this restart)
#   ctl.sh status    on or off, its health, and where Claude Code points
set -eu

LABEL=${MODEL_GATEWAY_LABEL:-local.model-gateway}
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
[ -f "$PLIST" ] || { echo "ctl.sh: not installed ($PLIST missing); run launchd/install.sh" >&2; exit 1; }
PORT=$(plutil -extract EnvironmentVariables.MODEL_GATEWAY_PORT raw "$PLIST")
SETTINGS=$(plutil -extract EnvironmentVariables.MODEL_GATEWAY_CLAUDE_SETTINGS raw "$PLIST")
JQ=$(plutil -extract EnvironmentVariables.MODEL_GATEWAY_JQ raw "$PLIST")
URL="http://127.0.0.1:$PORT"

health() { curl -sf --max-time 1 "$URL/__gateway/health" 2>/dev/null; }
loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
linked() { [ "$("$JQ" -r '.env.ANTHROPIC_BASE_URL // ""' "$SETTINGS")" = "$URL" ]; }

case "${1:-}" in
  on)
    launchctl enable "$DOMAIN/$LABEL"
    loaded || launchctl bootstrap "$DOMAIN" "$PLIST"
    i=0
    until health >/dev/null && linked; do
      i=$((i + 1))
      [ $i -ge 80 ] && { echo "ctl.sh: the gateway did not come up within 20 seconds; see ~/Library/Logs/model-gateway.stderr.log" >&2; exit 1; }
      sleep 0.25
    done
    echo "on: $(health)"
    ;;
  off)
    launchctl disable "$DOMAIN/$LABEL"
    if loaded; then
      # bootout returns once in-flight streams have drained; Claude Code is unlinked within a second.
      launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 &
      i=0
      while linked && [ $i -lt 40 ]; do sleep 0.25; i=$((i + 1)); done
    fi
    if linked; then
      echo "ctl.sh: the setting is still there; remove env.ANTHROPIC_BASE_URL from $SETTINGS by hand" >&2
      exit 1
    fi
    echo "off: Claude Code talks to the API directly; in-flight streams finish on the gateway first"
    ;;
  restart)
    loaded || { echo "ctl.sh: the gateway is off; use: ctl.sh on" >&2; exit 1; }
    before=$(health | sed -n 's/^ok pid=\([0-9]*\).*/\1/p')
    launchctl kill HUP "$DOMAIN/$LABEL"
    i=0
    until after=$(health | sed -n 's/^ok pid=\([0-9]*\).*/\1/p') && [ -n "$after" ] && [ "$after" != "$before" ]; do
      i=$((i + 1))
      [ $i -ge 80 ] && { echo "ctl.sh: no new gateway took over within 20 seconds; the old one keeps serving" >&2; exit 1; }
      sleep 0.25
    done
    echo "restarted: $(health)"
    ;;
  status)
    if launchctl print-disabled "$DOMAIN" 2>/dev/null | grep -q "\"$LABEL\" => \(disabled\|true\)"; then state=off
    elif loaded; then state=on
    else state="off until the next login (not disabled)"
    fi
    echo "state:  $state"
    echo "health: $(health || echo 'not answering')"
    if linked; then echo "Claude Code: through the gateway ($URL)"; else echo "Claude Code: straight to the API"; fi
    ;;
  *) echo "usage: ctl.sh on|off|restart|status" >&2; exit 2 ;;
esac
