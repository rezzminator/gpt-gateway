#!/bin/sh
# Gateway-owned settings edits, tri-state reads and bounded cleanup retries.
# An install holding a route-secret links Claude Code to $URL/<secret> ($LINK_URL); the secretless $URL of an earlier
# install is still this gateway's own, so reads and unlinks accept either and only a link writes $LINK_URL.

# Sets LINK_URL from the route-secret in install directory $1, or to $URL when it holds none; 1 when it is unreadable
# or not a regular file (a FIFO or device there would hang or flood the read).
settings_route() {
  LINK_URL=$URL
  [ -e "$1/route-secret" ] || [ -L "$1/route-secret" ] || return 0
  [ -f "$1/route-secret" ] || return 1
  settings_secret=$(head -c 65 "$1/route-secret") || return 1
  case "$settings_secret" in
    *[!0-9a-f]*) return 1 ;;
  esac
  [ "${#settings_secret}" -eq 64 ] || return 1
  LINK_URL="$URL/$settings_secret"
}

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

# 0 linked with the credential this install holds, 1 not, 2 unreadable.
settings_linked() {
  settings_value=$(settings_current) || return 2
  [ "$settings_value" = "${LINK_URL-$URL}" ]
}

# 0 when $1 is this gateway's URL: bare, or with any route secret (64 lowercase hex), whether or not this install's
# secret still reads, so a supervisor that cannot read it still removes the link it wrote.
settings_is_ours() {
  { [ "$1" = "${LINK_URL-$URL}" ] || [ "$1" = "$URL" ]; } && return 0
  settings_tail=${1#"$URL/"}
  [ "$settings_tail" != "$1" ] && [ "${#settings_tail}" -eq 64 ] || return 1
  case "$settings_tail" in *[!0-9a-f]*) return 1 ;; esac
}

# 0 this gateway's link, with or without its credential, 1 not, 2 unreadable.
settings_ours() {
  settings_value=$(settings_current) || return 2
  settings_is_ours "$settings_value"
}

settings_note() {
  printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(basename "$0")" "$*" >&2
}

# The rewrite keeps the mode of the file a symlinked path resolves to (GNU stat, then BSD stat), never the link's own,
# so a private settings file stays private.
settings_edit() {
  settings_tmp=$(mktemp "$SETTINGS.XXXXXX") || return 1
  if settings_mode=$(stat -L -c '%a' "$SETTINGS" 2>/dev/null || stat -L -f '%Lp' "$SETTINGS" 2>/dev/null) \
    && "$JQ" --arg url "${LINK_URL-$URL}" --arg legacy "$URL" "$1" "$SETTINGS" > "$settings_tmp" \
    && chmod "$settings_mode" "$settings_tmp" && mv "$settings_tmp" "$SETTINGS"; then return 0; fi
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
      settings_is_ours "$settings_value" || return 0
      # shellcheck disable=SC2016 # $url and $legacy are jq variables; a concurrent foreign URL is kept.
      if settings_edit 'def ours: . == $url or . == $legacy or (type == "string" and startswith($legacy + "/") and (.[($legacy | length) + 1:] | test("^[0-9a-f]{64}$"))); if .env.ANTHROPIC_BASE_URL | ours then del(.env.ANTHROPIC_BASE_URL) | if .env.ENABLE_TOOL_SEARCH == "true" then del(.env.ENABLE_TOOL_SEARCH) else . end else . end'; then
        if settings_value=$(settings_current); then
          if ! settings_is_ours "$settings_value"; then
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
