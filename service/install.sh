#!/bin/sh
# Installs or updates gpt-gateway for the current macOS or Linux user. Re-runnable.
#
# Copies the SHIP files to ~/.local/share/gpt-gateway, creates middleware/ there and
# copies the shipped middleware into it (each file replaces only its own copy), writes
# settings.json listing status_label on a first install (an update validates the existing
# one and keeps it), and writes the platform service. A first install turns the gateway on.
# An update keeps the current on/off state:
#   - on: the change applies through a graceful restart, so no stream is cut;
#   - off: only the files change, and `ctl.sh on` turns it on.
#
# A former install under the old product name is migrated once: its settings and own middleware seed a first
# install, its agent stops and is removed, its state moves here unless this install has its own, its copy goes.
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
SHIP="gateway.py gpt_gateway service/run.sh service/ctl.sh service/platform.sh service/settings.sh service/listener.py README.md LICENSE THIRD_PARTY_NOTICES.md"
# shellcheck source=service/platform.sh
. "$SRC/service/platform.sh"
# shellcheck source=service/settings.sh
. "$SRC/service/settings.sh"
service_init
if [ "$PLATFORM" = Linux ]; then service_require_linux; fi
PY=${GPT_GATEWAY_PYTHON:-/usr/bin/python3}
URL="http://127.0.0.1:$PORT"
# A stop drains in-flight requests for up to DRAIN seconds; the service manager must wait longer than that.
DRAIN=600
STOP_SECONDS=${GPT_GATEWAY_STOP_SECONDS:-$((DRAIN + 30))}
case "$STOP_SECONDS" in ''|*[!0-9]*) echo 'install.sh: invalid stop deadline' >&2; exit 1 ;; esac

service_jq
"$PY" -I -c 'import sys; assert sys.version_info >= (3, 9)' || { echo "install.sh: $PY must be Python 3.9+" >&2; exit 1; }
# run.sh rewrites the file the setting lives in, so it gets the real path, not a symlink to it.
SETTINGS=$("$PY" -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${GPT_GATEWAY_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}")
[ -f "$SETTINGS" ] || { echo "install.sh: no Claude Code settings at $SETTINGS" >&2; exit 1; }
# The suite's whole output is kept in TEST_LOG. A failure names its tests and the log. A run still going after
# TEST_SECONDS dumps every thread's stack into the log and exits (faulthandler's own exit, so no crash report).
TEST_SECONDS=${GPT_GATEWAY_TEST_SECONDS:-1800}
TEST_LOG="$LOGS/gpt-gateway-install-tests.log"
mkdir -p "$LOGS"
TEST_RC=0
"$PY" -I -B -c 'import faulthandler, sys, unittest
faulthandler.dump_traceback_later(float(sys.argv[1]), exit=True)
unittest.main(module=None, argv=["unittest", "discover", "-v", "-s", sys.argv[2] + "/tests", "-t", sys.argv[2]])' \
  "$TEST_SECONDS" "$SRC" >"$TEST_LOG" 2>&1 || TEST_RC=$?
if [ "$TEST_RC" -ne 0 ]; then
  if grep -q 'Timeout (0:' "$TEST_LOG"; then
    echo "install.sh: tests hung past ${TEST_SECONDS}s; last test started: $(grep ' \.\.\. ' "$TEST_LOG" | tail -n 1)" >&2
    echo "install.sh: every thread's stack is at the end of $TEST_LOG" >&2
  else
    echo "install.sh: tests fail; output: $TEST_LOG" >&2
    { grep -E '^(FAIL|ERROR): ' "$TEST_LOG" || tail -n 15 "$TEST_LOG"; } | sed 's/^/install.sh:   /' >&2
  fi
  exit 1
fi

# The former install. Its settings and own middleware seed a first install here, so they are validated below
# while it still serves.
LEGACY_NAME=model-gateway
if [ "$PLATFORM" = Darwin ]; then
LEGACY_LABEL="local.$LEGACY_NAME"
LEGACY_DEST="$HOME/.local/share/$LEGACY_NAME"
LEGACY_STATE="$HOME/.local/state/$LEGACY_NAME"
LEGACY_PLIST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
if [ -d "$LEGACY_DEST" ] && [ ! -e "$DEST" ]; then
  mkdir -p "$DEST/middleware"
  if [ -f "$LEGACY_DEST/settings.json" ]; then cp -p "$LEGACY_DEST/settings.json" "$DEST/settings.json"; fi
  for f in "$LEGACY_DEST"/middleware/*.py; do
    [ -f "$f" ] || continue
    cp -p "$f" "$DEST/middleware/"
  done
  echo "carried the settings and middleware of $LEGACY_DEST into $DEST"
fi

# settings.json: an existing file is kept once it validates against the middleware installed beside it.
if [ -f "$DEST/settings.json" ]; then
  "$PY" -I "$SRC/gateway.py" check "$DEST/settings.json" >/dev/null \
    || { echo "install.sh: $DEST/settings.json is invalid (above); fix or delete it, then run again" >&2; exit 1; }
fi

# The former agent stops before this one starts: it holds the port, the database, and unlinks Claude Code as it goes.
if [ -f "$LEGACY_PLIST" ] || launchctl print "$DOMAIN/$LEGACY_LABEL" >/dev/null 2>&1; then
  echo "stopping the former agent $LEGACY_LABEL (it drains in the background)"
  launchctl bootout "$DOMAIN/$LEGACY_LABEL" 2>/dev/null &
  i=0
  while launchctl print "$DOMAIN/$LEGACY_LABEL" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ $i -ge $((DRAIN * 4 + 120)) ]; then
      echo "install.sh: the former agent $LEGACY_LABEL did not stop; its state and copy are left as they were" >&2
      exit 1
    fi
    sleep 0.25
  done
  rm -f "$LEGACY_PLIST"
fi
if [ -d "$LEGACY_STATE" ] && [ ! -e "$STATE" ]; then
  mkdir -p "$(dirname "$STATE")"
  mv "$LEGACY_STATE" "$STATE"
  echo "moved $LEGACY_STATE to $STATE"
fi
if [ -d "$LEGACY_DEST" ]; then
  rm -rf "$LEGACY_DEST"
  echo "removed the former copy $LEGACY_DEST"
fi

fi  # macOS's former install migration

# Validate Linux updates before replacing any installed code.
if [ "$PLATFORM" = Linux ] && [ -f "$DEST/settings.json" ]; then
  "$PY" -I -B "$SRC/gateway.py" check "$DEST/settings.json" >/dev/null \
    || { echo "install.sh: $DEST/settings.json is invalid (above); fix or delete it, then run again" >&2; exit 1; }
fi

# The route secret: created once, kept by every update. Claude Code reaches the gateway at $URL/<secret>, and the GPT
# route answers only requests that carry it, so no other local process spends the ChatGPT subscription.
mkdir -p "$DEST"
"$PY" -I - "$DEST/route-secret" <<'PY' || { echo "install.sh: the route secret could not be created or validated" >&2; exit 1; }
import os
import re
import secrets
import stat
import sys

path = sys.argv[1]
if not os.path.lexists(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write(secrets.token_hex(32) + "\n")
info = os.lstat(path)
if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
    raise ValueError("route secret must be a regular 0600 file")
with open(path, encoding="ascii") as source:
    if not re.fullmatch(r"[0-9a-f]{64}", source.read(65).strip()):
        raise ValueError("route secret must contain 64 lowercase hex digits")
PY
settings_route "$DEST" || { echo "install.sh: the route secret is unreadable ($DEST/route-secret)" >&2; exit 1; }
CUR=$("$JQ" -r '.env.ANTHROPIC_BASE_URL // ""' "$SETTINGS") || { echo "install.sh: cannot read Claude Code settings at $SETTINGS" >&2; exit 1; }
if [ -n "$CUR" ] && [ "$CUR" != "$URL" ] && [ "$CUR" != "$LINK_URL" ]; then
  # Another URL (an earlier port, another proxy) would keep run.sh from linking; this install replaces it,
  # and the tool search switch that rode that link.
  mkdir -p "$BACKUPS"
  cp -p "$SETTINGS" "$BACKUPS/settings.json.pre-install-$(date +%Y%m%d-%H%M%S)"
  settings_edit 'del(.env.ANTHROPIC_BASE_URL) | if .env.ENABLE_TOOL_SEARCH == "true" then del(.env.ENABLE_TOOL_SEARCH) else . end' \
    || { echo "install.sh: could not rewrite Claude Code settings at $SETTINGS" >&2; exit 1; }
  echo "removed the earlier ANTHROPIC_BASE_URL (backup in $BACKUPS)"
fi

LOADED=no
service_loaded && LOADED=yes
DISABLED=no
service_disabled && DISABLED=yes
# run.sh supervises from memory: a new run.sh needs the agent itself restarted, not just a HUP.
RUN_CHANGED=yes
cmp -s "$SRC/service/run.sh" "$DEST/run.sh" 2>/dev/null && RUN_CHANGED=no

mkdir -p "$DEST" "$DEST/middleware" "$LOGS"
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
  (umask 077; printf '%s\n' '{"capture_dir": null, "middleware": [{"module": "status_label"}]}' > "$DEST/settings.json.new")
  BYTECODE=
  [ "$PLATFORM" = Linux ] && BYTECODE=-B
  # shellcheck disable=SC2086 # The optional Linux -B flag expands to zero or one argument.
  "$PY" -I $BYTECODE "$DEST/gateway.py" check "$DEST/settings.json.new" >/dev/null \
    || { rm -f "$DEST/settings.json.new"; echo "install.sh: the settings it built are invalid (above)" >&2; exit 1; }
  mv "$DEST/settings.json.new" "$DEST/settings.json"
fi
echo "settings: $(tr -d '\n ' < "$DEST/settings.json") ($DEST/settings.json)"

if [ "$PLATFORM" = Linux ]; then
  service_write_linux
else
mkdir -p "$(dirname "$PLIST")"
PLUGIN_LOG_ENV=$("$PY" -I - <<'PYLOG'
import os
from xml.sax.saxutils import escape
if "PLUGIN_LOG_DIR" in os.environ:
    print("    <key>PLUGIN_LOG_DIR</key><string>" + escape(os.environ["PLUGIN_LOG_DIR"]) + "</string>")
PYLOG
)
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
    <key>GPT_GATEWAY_PORT</key><string>$PORT</string>
    <key>GPT_GATEWAY_LOG</key><string>$LOGS/gpt-gateway.log</string>
    <key>GPT_GATEWAY_DRAIN_SECONDS</key><string>$DRAIN</string>
    <key>GPT_GATEWAY_STOP_SECONDS</key><string>$STOP_SECONDS</string>
    <key>GPT_GATEWAY_PYTHON</key><string>$PY</string>
    <key>GPT_GATEWAY_JQ</key><string>$JQ</string>
    <key>GPT_GATEWAY_CLAUDE_SETTINGS</key><string>$SETTINGS</string>
$PLUGIN_LOG_ENV
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>2</integer>
  <key>ExitTimeOut</key><integer>$STOP_SECONDS</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$LOGS/gpt-gateway.stderr.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST.new" >/dev/null
fi
SERVICE_CHANGED=yes
cmp -s "$AGENT.new" "$AGENT" 2>/dev/null && SERVICE_CHANGED=no
mv "$AGENT.new" "$AGENT"
if [ "$PLATFORM" = Linux ]; then systemctl --user daemon-reload; fi

if [ "$DISABLED" = yes ]; then
  echo "installed; the gateway is off. Turn it on with: $DEST/ctl.sh on"
  exit 0
fi

if [ "$LOADED" = yes ] && [ "$SERVICE_CHANGED" = no ] && [ "$RUN_CHANGED" = no ]; then
  BEFORE=$(curl -sf --max-time 1 "$URL/__gateway/health" | sed -n 's/^ok pid=\([0-9]*\).*/\1/p')
  service_hup
  i=0
  until AFTER=$(curl -sf --max-time 1 "$URL/__gateway/health" | sed -n 's/^ok pid=\([0-9]*\).*/\1/p') \
      && [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ]; do
    i=$((i + 1))
    if [ $i -ge 80 ]; then
      echo "install.sh: the graceful restart did not take over within 20 seconds; the old gateway keeps serving" >&2
      tail -5 "$LOGS/gpt-gateway.stderr.log" >&2 2>/dev/null || true
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
  if [ "$PLATFORM" = Linux ]; then
    echo "reloading the service (waiting for the old gateway to drain)"
    service_stop
  else
    echo "reloading the agent (the old gateway drains in the background)"
    service_stop 2>/dev/null &
  fi
  i=0
  while service_loaded && [ $i -lt $((DRAIN * 4 + 120)) ]; do sleep 0.25; i=$((i + 1)); done
fi
if [ "$PLATFORM" = Linux ]; then service_enable; fi
service_start

i=0
while :; do
  if curl -sf --max-time 1 "$URL/__gateway/health" >/dev/null 2>&1; then
    # A settings file that cannot be read is no answer: fail loudly, never wait it out as "not linked yet".
    if settings_linked; then break
    elif [ "$?" -eq 2 ]; then echo "install.sh: cannot read Claude Code settings at $SETTINGS" >&2; exit 1; fi
  fi
  i=$((i + 1))
  if [ $i -ge 80 ]; then
    echo "install.sh: the gateway did not come up within 20 seconds; Claude Code still talks to the API directly" >&2
    tail -5 "$LOGS/gpt-gateway.stderr.log" 2>/dev/null >&2 || true
    exit 1
  fi
  sleep 0.25
done
echo "gateway on: $(curl -sf "$URL/__gateway/health")"
echo "Claude Code uses $URL with its route secret ($SETTINGS)"
