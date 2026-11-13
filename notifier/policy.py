"""The policy file: how loud each sender is allowed to be.

TIERS

  live    pushes to the phone the moment it happens. Reserved for "a human has
          to do something, and sooner is better than tomorrow".
  digest  held, and rolled into one push the next morning. The default.
  silent  recorded, never pushed.

`escalate_on` lists severities that override the tier and force a live push. A
job that is normally a digest line but whose failure is urgent sets
tier=digest with escalate_on=["fail"].

THE DEFAULT IS THE DANGEROUS PART

An unlisted sender gets the defaults, and the defaults escalate on failure. So
a sender nobody wrote an entry for goes live every time it fails. Two chronic
conditions that no code change could clear (an expired third-party token, a
standing list of stuck items) produced 49 live pushes in 30 days exactly this
way. The fix is an explicit entry with `"escalate_on": []`, and an empty list
has to be honoured as "never", not treated as "unset". `validate` and the
tests in tests/test_policy.py pin that.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TIERS = ("live", "digest", "silent")
SEVERITIES = ("ok", "info", "warn", "fail")

BUILTIN_DEFAULTS = {
    "tier": "digest",
    "action": "no",
    "escalate_on": ["fail"],
    "dedup_hours": 6,
    "suggest": "No standing action. This sender has no policy entry yet, so it is "
               "treated as informational. Add one to the policy file.",
}


def load(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except Exception as exc:  # noqa: BLE001
        # An unreadable policy must not stop a notification. It falls back to
        # the built-in defaults and says so in the job's log.
        print(f"notify-policy: could not read {path.name}: {exc}", file=sys.stderr)
        return {}


def job_policy(doc: dict, job_id: str) -> tuple[dict, bool]:
    """Merged policy for a job, and whether it had an explicit entry."""
    merged = dict(BUILTIN_DEFAULTS)
    merged.update(doc.get("defaults") or {})
    entry = (doc.get("jobs") or {}).get(job_id)
    if entry is None:
        return merged, False
    merged.update(entry)
    return merged, True


def resolve_tier(policy: dict, severity: str, override: str | None = None) -> tuple[str, str]:
    """Returns (tier, why). The why is stored on the record so that a push can
    always be traced back to the rule that allowed it."""
    if override in TIERS:
        return override, "the caller asked for this tier explicitly"
    escalate = policy.get("escalate_on") or []
    base = policy.get("tier", "digest")
    if base not in TIERS:
        base = "digest"
    if severity in escalate and base != "live":
        return "live", f"escalated from {base}: severity is {severity}"
    return base, "the standing policy for this job"


def validate(doc: dict) -> list[str]:
    """Shape problems in a policy document. Empty means it is well formed."""
    problems = []
    entries = {"defaults": doc.get("defaults") or {}}
    entries.update({f"jobs.{k}": v for k, v in (doc.get("jobs") or {}).items()})
    for name, e in entries.items():
        if not isinstance(e, dict):
            problems.append(f"{name}: not an object")
            continue
        if "tier" in e and e["tier"] not in TIERS:
            problems.append(f"{name}: unknown tier {e['tier']!r}")
        if "action" in e and e["action"] not in ("yes", "no"):
            problems.append(f"{name}: action must be yes or no")
        esc = e.get("escalate_on", [])
        if not isinstance(esc, list) or not set(esc) <= set(SEVERITIES):
            problems.append(f"{name}: escalate_on must be a list of {', '.join(SEVERITIES)}")
        if name.startswith("jobs.") and e.get("tier") == "silent" and e.get("action") == "yes":
            # A silent row that claims to need a human is a contradiction: it
            # can never reach one.
            problems.append(f"{name}: a silent sender cannot have action=yes")
    return problems
