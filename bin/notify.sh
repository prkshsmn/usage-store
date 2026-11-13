#!/usr/bin/env bash
# Push one notification to an ntfy server. The reference transport for the
# notifier (see notifier/transport.py), and usable directly from a shell job.
#
# Usage: notify.sh <title> <message> [priority] [tags] [click_url]
#   priority:  min | low | default | high | urgent   (default: default)
#   tags:      comma-separated emoji shortcodes
#   click_url: where tapping the notification goes   (optional)
#
# Environment: NTFY_URL, NTFY_TOPIC, NTFY_TOKEN. Supply them from a secret
# store at run time; they do not belong in a crontab or in this repository.
#
# EXIT CODE. By default this exits 0 whatever happens, because a shell job
# that notifies must not fail on a notification problem. It does not fail
# silently: every non-delivery prints one line on stderr, which lands in the
# calling job's log. With NOTIFY_STRICT=1 it exits 3 on non-delivery, which is
# how the notifier learns the truth. In both modes the outcome is decided by
# the HTTP status the server returned, never by curl merely having run.
#
# Only the HTTP code and curl's exit status are ever printed. Never the URL,
# the topic or the token.
set -uo pipefail

TITLE="${1:-Notification}"
MESSAGE="${2:-}"
PRIORITY="${3:-default}"
TAGS="${4:-}"
CLICK_URL="${5:-}"

fail() {
  echo "notify: $1, push NOT delivered: ${TITLE}" >&2
  [[ "${NOTIFY_STRICT:-}" == 1 ]] && exit 3
  exit 0
}

URL="${NTFY_URL:-}"
TOPIC="${NTFY_TOPIC:-}"
TOKEN="${NTFY_TOKEN:-}"
[[ -n "$URL" && -n "$TOPIC" && -n "$TOKEN" ]] || fail "NTFY_URL, NTFY_TOPIC or NTFY_TOKEN is not set"

# A header cannot carry a line break, and a title built from job output might.
TITLE="${TITLE//[$'\r\n']/ }"

ARGS=(-s --max-time 15 -o /dev/null -w '%{http_code}'
      -H "Authorization: Bearer ${TOKEN}"
      -H "Priority: ${PRIORITY}"
      -H "Title: ${TITLE}")
[[ -n "$TAGS" ]] && ARGS+=(-H "Tags: ${TAGS}")
[[ "$CLICK_URL" == http://* || "$CLICK_URL" == https://* ]] && ARGS+=(-H "Click: ${CLICK_URL}")

CURL_RC=0
HTTP_CODE="$(curl "${ARGS[@]}" --data-binary "${MESSAGE}" "${URL%/}/${TOPIC}" 2>/dev/null)" || CURL_RC=$?

[[ $CURL_RC -eq 0 ]] || fail "curl failed (exit ${CURL_RC})"
[[ "$HTTP_CODE" == 2?? ]] || fail "server returned HTTP ${HTTP_CODE}"
exit 0
