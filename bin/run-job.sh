#!/usr/bin/env bash
# Transparent cron wrapper: records a start and an end event for every run
# without changing what the wrapped job does.
#
# Usage in a crontab:
#   30 2 * * * /opt/usage-store/bin/run-job.sh nightly-backup -- /opt/jobs/backup.sh >> /var/log/jobs/backup.log 2>&1
#
# Events go to $RUN_JOB_EVENTS_DIR/YYYY-MM.jsonl (default
# ~/.local/state/run-job/events), one JSON object per line. The usage store's
# `jobs` collector imports the end events.
#
# CONTRACT. This wrapper sits in front of every scheduled job on the host,
# several of which guard against incidents that already happened once. So:
#
#   * It NEVER prevents the wrapped command from running. Every bookkeeping
#     step is best effort and cannot abort the run.
#   * It passes the exit code through EXACTLY. Callers and cron see what they
#     would have seen unwrapped.
#   * It does not touch stdout or stderr. Cron's own `>> log 2>&1` still owns
#     those, so existing logs keep their shape.
#   * No `set -e`. An arithmetic slip or a full disk must not become a skipped
#     backup.
#
# When bookkeeping fails it complains on stderr, which lands in the job's own
# log. A wrapper that quietly stops recording would show "no runs" for a job
# that is running fine, and that reads as an outage.
set -u

# Cron gives every job a bare PATH (/usr/bin:/bin). Tools installed under
# ~/.local/bin or /usr/local/bin resolve in a terminal and vanish on a
# schedule, and the failure only shows up unattended. Prepended, not replaced:
# a wrapped job may depend on whatever PATH its own caller already set.
export PATH="$HOME/.local/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

JOB_ID="${1:-unknown}"
shift || true
[[ "${1:-}" == "--" ]] && shift

if [[ $# -eq 0 ]]; then
  echo "run-job.sh: no command given for job '$JOB_ID'" >&2
  exit 2
fi

EVENTS_DIR="${RUN_JOB_EVENTS_DIR:-$HOME/.local/state/run-job/events}"
EVENTS_FILE="$EVENTS_DIR/$(date +%Y-%m).jsonl"

mkdir -p "$EVENTS_DIR" 2>/dev/null \
  || echo "run-job.sh: WARNING cannot create $EVENTS_DIR; not recording events" >&2

# JSON-escape a string for the characters that actually occur here.
#
# The control-character range starts at \x01, not \x00, and that is load
# bearing. A NUL cannot exist in a shell string, so bash truncates $'\x00' to
# empty, and a range written as [$'\x00'-$'\x1f'] collapses to the two literal
# characters `-` and \x1f. That version ate every hyphen out of every
# timestamp this function touched. tests/test_run_job.py pins it.
esc() {
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//[$'\x01'-$'\x1f']/ }"
  printf '%s' "$s"
}

emit() {
  [[ -d "$EVENTS_DIR" ]] || return 0
  printf '%s\n' "$1" >>"$EVENTS_FILE" 2>/dev/null \
    || echo "run-job.sh: WARNING could not append to $EVENTS_FILE" >&2
  return 0
}

# Every notification raised from inside this job knows which job raised it,
# without the job having to be told. Exported, not passed as an argument: an
# argument has to be threaded through every caller, and a forgotten one goes
# quiet instead of failing.
export NOTIFY_JOB="$JOB_ID"

HOST_S="$(hostname 2>/dev/null || echo unknown)"
START_EPOCH="$(date +%s)"

emit "{\"ts\":\"$(esc "$(date -Is)")\",\"job\":\"$(esc "$JOB_ID")\",\"event\":\"start\",\"host\":\"$(esc "$HOST_S")\"}"

# Run it. Nothing above this line may abort, and nothing below may alter $rc.
"$@"
rc=$?

DUR=$(( $(date +%s) - START_EPOCH ))
[[ $DUR -lt 0 ]] && DUR=0   # the clock stepped mid-run; a negative duration is worse than 0

if [[ $rc -eq 0 ]]; then STATUS=ok; else STATUS=fail; fi

emit "{\"ts\":\"$(esc "$(date -Is)")\",\"job\":\"$(esc "$JOB_ID")\",\"event\":\"end\",\"status\":\"$STATUS\",\"rc\":$rc,\"duration_s\":$DUR,\"host\":\"$(esc "$HOST_S")\"}"

exit $rc
