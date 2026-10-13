#!/bin/sh
# Turns gpt-gateway on or off by hand. The state holds across logins
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

DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=service/platform.sh
. "$DIR/platform.sh"
service_init
[ -f "$AGENT" ] || { echo "ctl.sh: not installed ($AGENT missing); run service/install.sh" >&2; exit 1; }
service_jq
service_read
URL="http://127.0.0.1:$PORT"
# shellcheck source=service/settings.sh
. "$DIR/settings.sh"

health() { curl -sf --max-time 1 "$URL/__gateway/health" 2>/dev/null; }

case "${1:-}" in
  start|on)
    service_enable
    service_loaded || service_start
    i=0
    until health >/dev/null && settings_linked; do
      i=$((i + 1))
      if [ $i -ge 80 ]; then
        settings_current >/dev/null || { echo "ctl.sh: could not read settings ($SETTINGS)" >&2; exit 1; }
        echo "ctl.sh: the gateway did not come up within 20 seconds; see $LOGS/gpt-gateway.stderr.log" >&2
        exit 1
      fi
      sleep 0.25
    done
    echo "on: $(health)"
    ;;
  stop|off)
    service_disable
    if service_loaded; then
      # bootout returns once in-flight streams have drained; Claude Code is unlinked within a second.
      service_stop >/dev/null 2>&1 &
      i=0
      while settings_linked && [ $i -lt 40 ]; do sleep 0.25; i=$((i + 1)); done
    fi
    if settings_linked; then
      echo "ctl.sh: the setting is still there; remove env.ANTHROPIC_BASE_URL, and env.ENABLE_TOOL_SEARCH if it is \"true\", from $SETTINGS by hand" >&2
      exit 1
    elif [ "$?" -eq 2 ]; then
      echo "ctl.sh: could not read settings ($SETTINGS)" >&2
      exit 1
    fi
    echo "off: Claude Code talks to the API directly; in-flight streams finish on the gateway first"
    ;;
  restart)
    service_loaded || { echo "ctl.sh: the gateway is off; use: ctl.sh start" >&2; exit 1; }
    before=$(health | sed -n 's/^ok pid=\([0-9]*\).*/\1/p')
    service_hup
    i=0
    until after=$(health | sed -n 's/^ok pid=\([0-9]*\).*/\1/p') && [ -n "$after" ] && [ "$after" != "$before" ]; do
      i=$((i + 1))
      [ $i -ge 80 ] && { echo "ctl.sh: no new gateway took over within 20 seconds; the old one keeps serving" >&2; exit 1; }
      sleep 0.25
    done
    echo "restarted: $(health)"
    ;;
  status)
    result=0
    if [ "$PLATFORM" = Linux ]; then
      if service_activity; then
        if [ "$SERVICE_ACTIVITY" = active ] || [ "$SERVICE_ACTIVITY" = reloading ]; then state=on
        elif [ "$SERVICE_ACTIVITY" = activating ]; then state='on (starting)'
        elif [ "$SERVICE_ACTIVITY" = deactivating ]; then state='off (stopping)'
        else state=off; fi
      else
        state=unknown
        SERVICE_ACTIVITY=unknown
        SERVICE_ENABLED=unknown
        result=1
      fi
    elif service_disabled; then state=off
    elif service_loaded; then state=on
    else state="off until the next login (not disabled)"
    fi
    echo "state:  $state"
    if [ "$PLATFORM" = Linux ]; then
      echo "activity: $SERVICE_ACTIVITY"
      echo "enabled: $SERVICE_ENABLED"
    fi
    echo "health: $(health || echo 'not answering')"
    if settings_linked; then echo "Claude Code: through the gateway ($URL)"
    elif [ "$?" -eq 2 ]; then echo "Claude Code: could not read settings ($SETTINGS)"; result=1
    elif [ ! -e "$SETTINGS" ]; then echo "Claude Code: settings file is missing ($SETTINGS); straight to the API"
    else echo "Claude Code: straight to the API"; fi
    exit "$result"
    ;;
  *) echo "usage: ctl.sh start|stop|restart|status|on|off" >&2; exit 2 ;;
esac
