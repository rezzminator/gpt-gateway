#!/bin/sh
# Gateway-owned settings edits, tri-state reads and bounded cleanup retries.

settings_current() {
  if [ ! -e "$SETTINGS" ]; then
    settings_parent=$(dirname "$SETTINGS")
    while [ ! -e "$settings_parent" ] && [ "$settings_parent" != / ] && [ "$settings_parent" != . ]; do
      settings_parent=$(dirname "$settings_parent")
    done
    # A failed search through an existing directory is unreadable, not definitely absent.
    if [ ! -d "$settings_parent" ] || [ -x "$settings_parent" ]; then printf '\n'; return 0; fi
  fi
  "$JQ" -r '.env.ANTHROPIC_BASE_URL // ""' "$SETTINGS" 2>/dev/null
}

settings_linked() {
  settings_value=$(settings_current) || return 2
  [ "$settings_value" = "$URL" ]
}

settings_note() {
  printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(basename "$0")" "$*" >&2
}

settings_edit() {
  settings_tmp=$(mktemp "$SETTINGS.XXXXXX") || return 1
  if "$JQ" --arg url "$URL" "$1" "$SETTINGS" > "$settings_tmp" \
    && chmod 644 "$settings_tmp" && mv "$settings_tmp" "$SETTINGS"; then return 0; fi
  rm -f "$settings_tmp"
  return 1
}

settings_unlink() {
  settings_seconds=${1:-${GPT_GATEWAY_STOP_SECONDS:-630}}
  case "$settings_seconds" in ''|*[!0-9]*) settings_note 'invalid settings cleanup deadline'; return 1 ;; esac
  settings_deadline=$(($(date +%s) + settings_seconds))
  settings_previous=
  while :; do
    if settings_value=$(settings_current); then
      [ "$settings_value" = "$URL" ] || return 0
      # shellcheck disable=SC2016 # $url is a jq variable; a concurrent foreign URL is kept.
      if settings_edit 'if .env.ANTHROPIC_BASE_URL == $url then del(.env.ANTHROPIC_BASE_URL) | if .env.ENABLE_TOOL_SEARCH == "true" then del(.env.ENABLE_TOOL_SEARCH) else . end else . end'; then
        if settings_value=$(settings_current); then
          if [ "$settings_value" != "$URL" ]; then
            settings_note 'unlinked: Claude Code talks to the API directly'
            return 0
          fi
          settings_reason="could not rewrite settings ($SETTINGS)"
        else
          settings_reason="could not read settings ($SETTINGS)"
        fi
      else
        settings_reason="could not rewrite settings ($SETTINGS)"
      fi
    else
      settings_reason="could not read settings ($SETTINGS)"
    fi
    if [ "$settings_reason" != "$settings_previous" ]; then
      settings_note "$settings_reason; retrying until the cleanup deadline"
      settings_previous=$settings_reason
    fi
    if [ "$(date +%s)" -ge "$settings_deadline" ]; then
      settings_note "settings cleanup deadline reached: $settings_reason"
      return 1
    fi
    sleep 0.25
  done
}
