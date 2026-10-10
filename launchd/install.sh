#!/bin/sh
# Installs or updates model-gateway for the current macOS user. Re-runnable.
#
# Copies the SHIP files to ~/.local/share/model-gateway, creates middleware/ there and
# copies the shipped middleware into it (each file replaces only its own copy), writes
# settings.json listing status_label on a first install (an update validates the existing
# one and keeps it), and writes the launchd agent. A first install turns the gateway on.
# An update keeps the current on/off state:
#   - on: the change applies through a graceful restart, so no stream is cut;
#   - off: only the files change, and `ctl.sh on` turns it on.
#
# Options: --port N (default 18787).
set -eu

PORT=18787
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT=${2:-}; shift 2 ;;
    *) echo "usage: install.sh [--port N]" >&2; exit 2 ;;
  esac
done
case "$PORT" in ''|*[!0-9]*) echo "install.sh: --port must be a number" >&2; exit 2 ;; esac

SRC=$(cd "$(dirname "$0")/.." && pwd)
# What an install copies; top-level items are flat, package subdirectories are preserved.
SHIP="gateway.py model_gateway launchd/run.sh launchd/ctl.sh README.md LICENSE THIRD_PARTY_NOTICES.md"
LABEL=${MODEL_GATEWAY_LABEL:-local.model-gateway}
DEST="$HOME/.local/share/model-gateway"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs"
BACKUPS="$HOME/.local/state/model-gateway/backups"
PY=${MODEL_GATEWAY_PYTHON:-/usr/bin/python3}
URL="http://127.0.0.1:$PORT"
DOMAIN="gui/$(id -u)"
# A stop drains in-flight requests for up to DRAIN seconds; launchd must wait longer than that.
DRAIN=600

JQ=${MODEL_GATEWAY_JQ:-$(command -v jq)} || { echo "install.sh: jq is required" >&2; exit 1; }
[ -x "$JQ" ] || { echo "install.sh: jq is required" >&2; exit 1; }
"$PY" -I -c 'import sys; assert sys.version_info >= (3, 9)' || { echo "install.sh: $PY must be Python 3.9+" >&2; exit 1; }
# run.sh rewrites the file the setting lives in, so it gets the real path, not a symlink to it.
SETTINGS=$("$PY" -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${MODEL_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}")
[ -f "$SETTINGS" ] || { echo "install.sh: no Claude Code settings at $SETTINGS" >&2; exit 1; }
"$PY" -I -B -m unittest discover -s "$SRC/tests" -t "$SRC" >/dev/null 2>&1 \
  || { echo "install.sh: tests fail; run: $PY -I -B -m unittest discover -s $SRC/tests -t $SRC" >&2; exit 1; }

# settings.json: an existing file is kept once it validates against the middleware installed beside it.
if [ -f "$DEST/settings.json" ]; then
  "$PY" -I "$SRC/gateway.py" check "$DEST/settings.json" >/dev/null \
    || { echo "install.sh: $DEST/settings.json is invalid (above); fix or delete it, then run again" >&2; exit 1; }
fi

CUR=$("$JQ" -r '.env.ANTHROPIC_BASE_URL // ""' "$SETTINGS")
if [ -n "$CUR" ] && [ "$CUR" != "$URL" ]; then
  # Another URL (an earlier port, another proxy) would keep run.sh from linking; this install replaces it,
  # and the tool search switch that rode that link.
  mkdir -p "$BACKUPS"
  cp -p "$SETTINGS" "$BACKUPS/settings.json.pre-install-$(date +%Y%m%d-%H%M%S)"
  TMP=$(mktemp "$SETTINGS.XXXXXX")
  "$JQ" 'del(.env.ANTHROPIC_BASE_URL) | if .env.ENABLE_TOOL_SEARCH == "true" then del(.env.ENABLE_TOOL_SEARCH) else . end' "$SETTINGS" > "$TMP"
  chmod 644 "$TMP"
  mv "$TMP" "$SETTINGS"
  echo "removed the earlier ANTHROPIC_BASE_URL $CUR (backup in $BACKUPS)"
fi

LOADED=no
launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && LOADED=yes
DISABLED=no
launchctl print-disabled "$DOMAIN" 2>/dev/null | grep -q "\"$LABEL\" => \(disabled\|true\)" && DISABLED=yes
# run.sh supervises from memory: a new run.sh needs the agent itself restarted, not just a HUP.
RUN_CHANGED=yes
cmp -s "$SRC/launchd/run.sh" "$DEST/run.sh" 2>/dev/null && RUN_CHANGED=no

mkdir -p "$DEST" "$DEST/middleware" "$HOME/Library/LaunchAgents" "$LOGS"
for f in $SHIP; do
  name=$(basename "$f")
  if [ -d "$SRC/$f" ]; then
    # A package goes whole, its sources only; the running gateway has already imported its copy.
    rm -rf "$DEST/$name.new"
    mkdir "$DEST/$name.new"
    "$PY" -I - "$SRC/$f" "$DEST/$name.new" <<'PY'
import os
import shutil
import sys

src, dest = sys.argv[1:]
for directory, _, names in os.walk(src):
    target = os.path.join(dest, os.path.relpath(directory, src))
    for name in names:
        if name.endswith(".py"):
            os.makedirs(target, exist_ok=True)
            shutil.copy(os.path.join(directory, name), target)
PY
    rm -rf "${DEST:?}/$name"
    mv "$DEST/$name.new" "$DEST/$name"
  else
    cp "$SRC/$f" "$DEST/$name.new" && mv "$DEST/$name.new" "$DEST/$name"
  fi
done
chmod +x "$DEST/run.sh" "$DEST/ctl.sh"
# The shipped middleware: each file replaces only its own copy, so the user's own modules stay.
for f in "$SRC"/middleware/*.py; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  cp "$f" "$DEST/middleware/$name.new" && mv "$DEST/middleware/$name.new" "$DEST/middleware/$name"
done
if [ ! -f "$DEST/settings.json" ]; then
  # The running gateway re-reads settings.json when it changes, so it is replaced whole, never edited in place.
  printf '%s\n' '{"capture_dir": null, "middleware": [{"module": "status_label"}]}' > "$DEST/settings.json.new"
  "$PY" -I "$DEST/gateway.py" check "$DEST/settings.json.new" >/dev/null \
    || { rm -f "$DEST/settings.json.new"; echo "install.sh: the settings it built are invalid (above)" >&2; exit 1; }
  mv "$DEST/settings.json.new" "$DEST/settings.json"
fi
echo "settings: $(tr -d '\n ' < "$DEST/settings.json") ($DEST/settings.json)"

cat > "$PLIST.new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>$DEST/run.sh</string><string>serve</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MODEL_GATEWAY_PORT</key><string>$PORT</string>
    <key>MODEL_GATEWAY_LOG</key><string>$LOGS/model-gateway.log</string>
    <key>MODEL_GATEWAY_DRAIN_SECONDS</key><string>$DRAIN</string>
    <key>MODEL_GATEWAY_PYTHON</key><string>$PY</string>
    <key>MODEL_GATEWAY_JQ</key><string>$JQ</string>
    <key>MODEL_GATEWAY_CLAUDE_SETTINGS</key><string>$SETTINGS</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>2</integer>
  <key>ExitTimeOut</key><integer>$((DRAIN + 30))</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$LOGS/model-gateway.stderr.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST.new" >/dev/null
PLIST_CHANGED=yes
cmp -s "$PLIST.new" "$PLIST" 2>/dev/null && PLIST_CHANGED=no
mv "$PLIST.new" "$PLIST"

if [ "$DISABLED" = yes ]; then
  echo "installed; the gateway is off. Turn it on with: $DEST/ctl.sh on"
  exit 0
fi

if [ "$LOADED" = yes ] && [ "$PLIST_CHANGED" = no ] && [ "$RUN_CHANGED" = no ]; then
  BEFORE=$(curl -sf --max-time 1 "$URL/__gateway/health" | sed -n 's/^ok pid=\([0-9]*\).*/\1/p')
  launchctl kill HUP "$DOMAIN/$LABEL"
  i=0
  until AFTER=$(curl -sf --max-time 1 "$URL/__gateway/health" | sed -n 's/^ok pid=\([0-9]*\).*/\1/p') \
      && [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ]; do
    i=$((i + 1))
    if [ $i -ge 80 ]; then
      echo "install.sh: the graceful restart did not take over within 20 seconds; the old gateway keeps serving" >&2
      tail -5 "$LOGS/model-gateway.stderr.log" >&2 2>/dev/null || true
      exit 1
    fi
    sleep 0.25
  done
  echo "updated through a graceful restart: $(curl -sf "$URL/__gateway/health")"
  exit 0
fi

if [ "$LOADED" = yes ]; then
  # The agent itself must reload. Stopping unlinks Claude Code first, so new requests go
  # straight to the API while the old gateway drains its streams; none is cut.
  echo "reloading the agent (the old gateway drains in the background)"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null &
  i=0
  while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && [ $i -lt $((DRAIN * 4 + 120)) ]; do sleep 0.25; i=$((i + 1)); done
fi
launchctl bootstrap "$DOMAIN" "$PLIST"

i=0
until curl -sf --max-time 1 "$URL/__gateway/health" >/dev/null 2>&1 \
  && [ "$("$JQ" -r '.env.ANTHROPIC_BASE_URL // ""' "$SETTINGS")" = "$URL" ]; do
  i=$((i + 1))
  if [ $i -ge 80 ]; then
    echo "install.sh: the gateway did not come up within 20 seconds; Claude Code still talks to the API directly" >&2
    tail -5 "$LOGS/model-gateway.stderr.log" 2>/dev/null >&2 || true
    exit 1
  fi
  sleep 0.25
done
echo "gateway on: $(curl -sf "$URL/__gateway/health")"
echo "Claude Code uses $URL ($SETTINGS)"
