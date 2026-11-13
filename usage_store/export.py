"""Publish job-run statistics from the store, or refuse.

WHY THIS REFUSES

The first version of this export read a summary file that another process was
supposed to rebuild. That process was switched off, the file stopped changing,
and for eight weeks the export kept publishing the same run count and success
rate under a "last verified" date that moved forward every day it ran. Nothing
was wrong with the arithmetic. The input had stopped changing and nothing asked
how old it was.

So the export now reads the store directly and checks the age of the newest
row before it computes anything. A stale source may make this job skip a day.
It may never again be published as current. On refusal nothing is written, the
previous output file is left untouched, and the exit code is nonzero so the
wrapper records a failure someone will see.
"""
import datetime as dt
import json
import os
import sqlite3

EXIT_REFUSED = 3


class Refuse(Exception):
    """The source cannot back a published number."""


def _parse_ts(value):
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def job_stats(db_path, now, window_days=14, max_age_days=3):
    """Run count, distinct jobs and success rate over the window.

    Raises Refuse when the store is missing, unreadable, empty, or when its
    newest job run is older than max_age_days.
    """
    if not os.path.exists(db_path):
        raise Refuse(f"usage store not found at {db_path}")
    try:
        con = sqlite3.connect(db_path, timeout=10)
        try:
            rows = con.execute(
                "SELECT ts, subject, ok FROM events WHERE kind = 'job_run'").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise Refuse(f"could not read the usage store: {e}")

    cutoff = now - dt.timedelta(days=window_days)
    newest = None
    total = ok = 0
    jobs = set()
    for raw_ts, subject, ok_flag in rows:
        ts = _parse_ts(raw_ts)
        if ts is None:
            continue  # one unreadable row is skipped, it does not sink the export
        if newest is None or ts > newest:
            newest = ts
        if ts >= cutoff:
            total += 1
            ok += 1 if ok_flag == 1 else 0
            jobs.add(subject)
    if newest is None:
        raise Refuse("the usage store holds no job_run events")
    age_days = (now - newest).total_seconds() / 86400
    if age_days > max_age_days:
        raise Refuse(
            f"the newest job run in the store is {age_days:.1f} days old, over the "
            f"{max_age_days:g} day limit; refusing to publish stale numbers as current")
    return {
        "generated": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "newest_run": newest.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs_ran": len(jobs),
        "runs": total,
        "success_rate": round(ok / total, 4) if total else None,
    }


def write_stats(stats, out_path):
    """Write then replace, so a reader never sees half a file."""
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, out_path)
