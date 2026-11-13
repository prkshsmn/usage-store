"""Build, record and deliver one standardised notification.

WHAT THIS REPLACED

Every notifying job used to compose its own title and body and call the push
server directly. Twelve hours of the push server's cache made the case for
this file:

    18:42  59 items still missing after 14d
    18:54  59 items still missing after 14d
    18:55  59 items still missing after 14d

Three pushes, one fact, and a title that names neither the job that produced it
nor whether anybody has to do anything. Nine notifications in that window, of
which one needed a human.

So this module owns the whole shape of a notification and the callers stop
deciding it. Every notification answers the same five questions, in the same
order:

    1. How often does this run          <- the job registry
    2. What does the job do             <- the job registry
    3. What did it actually find        <- the caller, and only this
    4. Does it need a human, yes or no  <- the policy file, caller may override
    5. What should the human do         <- the policy file, caller may override

The caller is left with the one field it is the only thing that knows.

DEDUP

The same key from the same job inside the dedup window collapses to the first
one. The duplicate is still recorded, with suppressed=true, because "this
fired eleven times" is a fact about a job. Only the push is dropped.

NEVER FAILS ITS CALLER, WITH ONE DELIBERATE EXCEPTION

This sits in front of every notifying job, several of which guard incidents
that already happened once. A notification problem must not become a skipped
backup, so this exits 0 on a failed push, an unknown job id or a missing
policy entry, and complains on stderr.

The exception is EXIT_STORE_FAILED (2). A push that did not land is recorded
with delivered=false and the digest retries it. A store write that failed
means the record does not exist anywhere, and printing a success line for it
would be reporting an effect that did not happen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta

from . import paths, policy as policy_mod, store, transport

FALLBACK_WINDOW_H = 24
EXIT_STORE_FAILED = 2

# `urgent` is deliberately unused: it bypasses the phone's own quiet hours, and
# nothing here is a fire alarm. The escalation this system has is live versus
# digest.
PRIORITY = {"ok": "low", "info": "default", "warn": "default", "fail": "high"}
TAGS = {"ok": "white_check_mark", "info": "information_source",
        "warn": "warning", "fail": "rotating_light"}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def warn(msg: str) -> None:
    print(f"notify-event: {msg}", file=sys.stderr)


def job_facts(job_id: str) -> dict:
    """Cadence and purpose for a job id, from the registry.

    Returns empty strings for an unregistered job instead of raising. An
    unknown sender must still be able to notify: that is exactly the moment
    you want to hear from it.
    """
    empty = {"project": "", "cadence": "", "what": ""}
    try:
        with paths.jobs().open(encoding="utf-8") as f:
            entry = (json.load(f).get("jobs") or {}).get(job_id) or {}
    except Exception:  # noqa: BLE001
        return empty
    return {k: str(entry.get(k, "")) for k in empty}


def content_hash(job_id: str, title: str, body: str) -> str:
    h = hashlib.sha256()
    for part in (job_id, title.strip(), body.strip()):
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()[:12]


def recent_duplicate(rows: list[dict], key: str, now: datetime, window_h: float) -> dict | None:
    """The newest unsuppressed record with this key, if it is inside the window."""
    cutoff = now - timedelta(hours=window_h)
    for row in reversed(rows):
        if row.get("dedup_key") != key or row.get("suppressed"):
            continue
        try:
            ts = datetime.fromisoformat(row["ts"])
        except (KeyError, ValueError):
            continue
        return row if ts >= cutoff else None
    return None


def make_id(job_id: str, now: datetime, key: str) -> str:
    """A readable, unique id.

    Seconds and milliseconds are both in the stamp. The first version used
    minutes plus the content hash, and the first test of the dedup path
    produced a duplicate carrying the same id as the notification it
    duplicated: identical content in the same minute hashes the same and
    stamps the same.
    """
    stamp = now.strftime("%m%d%H%M%S") + f"{now.microsecond:06d}"[:3]
    safe = job_id if ID_RE.match(job_id) else "unknown"
    return f"{safe}-{stamp}-{key[:4]}"[:80]


def trim(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_body(rec: dict) -> str:
    """The envelope, as it appears on the phone.

    Fixed order and label prefixed on purpose. After a week you stop reading it
    and scan for the one line you care about, which is "Action needed", and it
    is always in the same place.
    """
    detail = rec["detail"].strip() or "(the job reported no detail)"
    if not rec.get("envelope", True):
        return detail
    f = rec["facts"]
    lines = []
    if f.get("cadence"):
        lines.append(f"Runs: {f['cadence']}")
    if f.get("what"):
        lines.append(f"Does: {trim(f['what'], 200)}")
    lines += ["", detail, "", "Action needed: " + ("YES" if rec["action"] else "no")]
    if rec.get("suggest"):
        lines.append(f"Suggested: {trim(rec['suggest'], 300)}")
    return "\n".join(lines).strip("\n")


def render_title(rec: dict) -> str:
    project = rec["facts"].get("project") or rec["job"]
    return f"{'[!] ' if rec['action'] else ''}{project}: {rec['headline']}"


def fire_fallback(rec: dict) -> None:
    """File the fallback for an undelivered live notification, at most once per
    dedup key per FALLBACK_WINDOW_H, so a long outage of the primary channel
    does not open hundreds of issues and get the fallback muted too.

    Wrapped in one broad try on purpose: a fallback that raises must never turn
    a recorded notification into a crashed notifier.
    """
    try:
        ledger_path = paths.fallback_ledger()
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = {}
        prev = ledger.get(rec["dedup_key"])
        now = datetime.now().astimezone()
        if prev:
            last = datetime.fromisoformat(prev["last_issued"])
            if now - last < timedelta(hours=FALLBACK_WINDOW_H):
                warn(f"{rec['id']}: fallback already filed for this key within {FALLBACK_WINDOW_H}h")
                return
        # The detail can carry text from outside (an upstream release note, a
        # third party's error message). It is fenced as a code block, never
        # interpolated as prose, and it is never used to build a command.
        body = (f"{rec['headline']}\n\n```\n{rec.get('detail') or '(no detail)'}\n```\n\n"
                f"notification id: {rec['id']}\npush failed at {rec['ts']}\n")
        ref = transport.fallback(f"undelivered: {trim(rec['headline'], 200)}", body)
        if not ref:
            return
        print(f"notify-event: {rec['id']} fallback filed {ref}")
        ledger[rec["dedup_key"]] = {"last_issued": now.isoformat(), "ref": ref}
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        warn(f"{rec.get('id', '?')}: fallback raised and was swallowed: {exc}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="notify-event", description="Send one standardised notification.")
    p.add_argument("--job", default=os.environ.get("NOTIFY_JOB"),
                   help="job id. Defaults to NOTIFY_JOB, which bin/run-job.sh exports, so a "
                        "wrapped job never has to be told its own name.")
    p.add_argument("--headline", required=True, help="a few words: what happened")
    p.add_argument("--detail", default="", help="the actual output, the one field only the caller knows")
    p.add_argument("--severity", default="info", choices=policy_mod.SEVERITIES)
    p.add_argument("--action", choices=("yes", "no"), default=None,
                   help="override the policy's answer to 'does this need a human'")
    p.add_argument("--suggest", default=None, help="override the policy's standing suggested action")
    p.add_argument("--tier", choices=policy_mod.TIERS, default=None,
                   help="override the policy tier. Use sparingly: the policy file is where "
                        "these decisions are supposed to be visible.")
    p.add_argument("--dedup-key", default=None,
                   help="collapse on this key instead of the content hash. Use when the body "
                        "carries a changing number but the fact is the same (a disk at 86%%, "
                        "then 87%%, is one condition).")
    p.add_argument("--click", default="", help="where tapping the push goes (http or https only)")
    p.add_argument("--no-envelope", dest="envelope", action="store_false", default=True,
                   help="send the detail alone, for a message that is already complete")
    p.add_argument("--dry-run", action="store_true", help="render and print, record nothing, push nothing")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.job:
        warn("no --job and no NOTIFY_JOB in the environment, recording as 'unknown'")
        args.job = "unknown"

    now = datetime.now().astimezone()
    facts = job_facts(args.job)
    pol, had_policy = policy_mod.job_policy(policy_mod.load(paths.policy()), args.job)

    action = (args.action == "yes") if args.action else (pol.get("action") == "yes")
    suggest = args.suggest if args.suggest is not None else pol.get("suggest", "")
    tier, tier_why = policy_mod.resolve_tier(pol, args.severity, args.tier)

    chash = content_hash(args.job, args.headline, args.detail)
    key = args.dedup_key or chash
    rec = {
        "id": make_id(args.job, now, chash),
        "ts": now.isoformat(),
        "job": args.job,
        "headline": args.headline,
        "detail": args.detail,
        "severity": args.severity,
        "action": action,
        "suggest": suggest,
        "tier": tier,
        "tier_why": tier_why,
        "dedup_key": key,
        "facts": facts,
        "envelope": args.envelope,
        "policy_known": had_policy,
        # Only http(s): this is handed to a phone's browser.
        "click": args.click if args.click.startswith(("http://", "https://")) else "",
        "suppressed": False,
        "delivered": False,
        "digested": False,
    }
    nid = rec["id"]

    if args.dry_run:
        print(render_title(rec))
        print("-" * 60)
        print(render_body(rec))
        print("-" * 60)
        print(f"tier={tier} ({tier_why})")
        return 0

    try:
        window_h = float(pol.get("dedup_hours", 6))
    except (TypeError, ValueError):
        window_h = 6.0
    dup = recent_duplicate(store.read(), key, now, window_h)
    if dup is not None:
        rec["suppressed"] = True
        rec["suppressed_by"] = dup.get("id", "")
        if not store.append(rec):
            warn(f"{nid} duplicates {dup.get('id')} but was NOT recorded (store write failed)")
            return EXIT_STORE_FAILED
        print(f"notify-event: {nid} duplicates {dup.get('id')} inside {window_h:g}h, recorded but not pushed")
        return 0

    if tier == "live":
        rec["delivered"] = transport.send(
            render_title(rec), render_body(rec),
            PRIORITY.get(args.severity, "default"), TAGS.get(args.severity, "bell"), rec["click"])
        if not rec["delivered"]:
            warn(f"{nid} was tier=live but the push did not land; it is recorded and "
                 f"the next digest will carry it")
            fire_fallback(rec)

    if not store.append(rec):
        warn(f"{nid} was NOT recorded (store write failed); it does not exist anywhere, "
             f"whatever happened to the push above")
        return EXIT_STORE_FAILED

    print(f"notify-event: {nid} tier={tier} action={'yes' if action else 'no'}"
          + ("" if tier != "live" else (" delivered" if rec["delivered"] else " NOT DELIVERED")))
    return 0


def cli() -> None:
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # The never-fail-the-caller contract, held at the outermost edge.
        warn(f"unhandled error, dropping the notification: {exc}")
        sys.exit(0)


if __name__ == "__main__":
    cli()
