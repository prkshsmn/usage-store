"""The store: schema, idempotent inserts, cursors, and the run ledger.

RULES THIS MODULE HOLDS ITSELF TO

* Idempotent. Every row carries a `uid`. Re-running a collector, or running it
  twice in a day, adds nothing, so a collector that crashed halfway is safe to
  re-run.
* Facts, not conclusions. Collectors store what happened (a job ran, a
  container used N MB). Ranking, pairing and thresholds happen at read time,
  where the logic can change without re-collecting.
* Exit 0 is not proof. Every run is written to `collector_runs` with the rows
  it added, and `check` fails when a collector that should add rows has not.
* No new service. Standard library only, cron driven, one file on disk.
"""
import datetime as dt
import json
import os
import sqlite3

SAMPLE_KINDS = ("container_sample", "host_sample")
SAMPLE_KEEP_DAYS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY,
  ts      TEXT NOT NULL,
  source  TEXT NOT NULL,
  project TEXT,
  kind    TEXT NOT NULL,
  subject TEXT,
  n       REAL,
  dur_s   REAL,
  ok      INTEGER,
  meta    TEXT,
  uid     TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts);
CREATE INDEX IF NOT EXISTS events_subject ON events (subject);
CREATE TABLE IF NOT EXISTS collector_runs (
  id         INTEGER PRIMARY KEY,
  collector  TEXT NOT NULL,
  started    TEXT NOT NULL,
  finished   TEXT,
  rows_added INTEGER,
  error      TEXT
);
CREATE INDEX IF NOT EXISTS collector_runs_c ON collector_runs (collector, started);
CREATE TABLE IF NOT EXISTS cursor (
  name  TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(t=None):
    """Calendar day in the host's own zone, as a date."""
    return (t or dt.datetime.now()).date()


def connect(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    new = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=60)
    conn.executescript(SCHEMA)
    if new:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


def get_cursor(conn, name, default=None):
    row = conn.execute("SELECT value FROM cursor WHERE name = ?", (name,)).fetchone()
    return row[0] if row else default


def set_cursor(conn, name, value):
    conn.execute(
        "INSERT INTO cursor (name, value) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
        (name, str(value)),
    )


def add(conn, rows):
    """Insert event dicts, ignoring any uid already stored. Returns rows added."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO events (ts, source, project, kind, subject, n, dur_s, ok, meta, uid) "
        "VALUES (:ts, :source, :project, :kind, :subject, :n, :dur_s, :ok, :meta, :uid)",
        [
            {
                "ts": r["ts"],
                "source": r["source"],
                "project": r.get("project"),
                "kind": r["kind"],
                "subject": r.get("subject"),
                "n": r.get("n"),
                "dur_s": r.get("dur_s"),
                "ok": r.get("ok"),
                "meta": json.dumps(r["meta"], separators=(",", ":")) if r.get("meta") else None,
                "uid": r["uid"],
            }
            for r in rows
        ],
    )
    return conn.total_changes - before


def prune_samples(conn):
    """Samples are the only kind that grows without a natural bound."""
    cutoff = iso(now_utc() - dt.timedelta(days=SAMPLE_KEEP_DAYS))
    conn.execute(
        "DELETE FROM events WHERE kind IN (%s) AND ts < ?" % ",".join("?" * len(SAMPLE_KINDS)),
        (*SAMPLE_KINDS, cutoff),
    )


def run_collector(name, fn, conn, cfg, out=print):
    """Run one collector, record the run, return an exit code.

    The run row is committed before the collector starts, so a collector that
    is killed outright still leaves evidence: a row with no `finished`.
    """
    started = iso(now_utc())
    cur = conn.execute(
        "INSERT INTO collector_runs (collector, started) VALUES (?, ?)", (name, started)
    )
    run_id = cur.lastrowid
    conn.commit()
    error, rows = None, 0
    try:
        rows = fn(conn, cfg)
        conn.commit()
    except Exception as e:  # recorded, then reported through the exit code
        conn.rollback()
        # Type and a bounded message only. Error text from an upstream service
        # is the classic place for a credential to end up on disk.
        error = f"{type(e).__name__}: {str(e)[:200]}"
    conn.execute(
        "UPDATE collector_runs SET finished = ?, rows_added = ?, error = ? WHERE id = ?",
        (iso(now_utc()), rows if error is None else 0, error, run_id),
    )
    conn.commit()
    out(f"{started} {name}: " + (f"FAILED {error}" if error else f"{rows} rows added"))
    return 1 if error else 0


def check(conn, expect):
    """Return a list of problems. Empty means every collector is keeping up.

    Two different failures are told apart on purpose. "No clean run" is a job
    that crashed or never fired. "Ran N times but added 0 rows" is a job that
    exited 0 every time and produced nothing, which no exit code can report.
    """
    problems = []
    now = now_utc()
    for name, (max_hours, must_add) in expect.items():
        cutoff = iso(now - dt.timedelta(hours=max_hours))
        ok_runs = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(rows_added), 0) FROM collector_runs "
            "WHERE collector = ? AND error IS NULL AND finished IS NOT NULL AND started >= ?",
            (name, cutoff),
        ).fetchone()
        if ok_runs[0] == 0:
            last = conn.execute(
                "SELECT started, error FROM collector_runs WHERE collector = ? ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
            if last:
                why = f"last run {last[0]}" + (f" failed: {last[1]}" if last[1] else "")
            else:
                why = "never ran"
            problems.append(f"{name}: no clean run in {max_hours:g}h ({why})")
        elif must_add and ok_runs[1] == 0:
            problems.append(f"{name}: ran {ok_runs[0]}x in {max_hours:g}h but added 0 rows")
    return problems


def record_gaps(conn, problems):
    """A gap is itself a fact worth keeping: one row per collector per day."""
    day = local_day().isoformat()
    add(conn, [{
        "ts": iso(now_utc()), "source": "usage-store", "project": "platform",
        "kind": "collector_gap", "subject": p.split(":")[0], "n": 1, "ok": 0,
        "meta": {"detail": p[:200]}, "uid": f"gap:{p.split(':')[0]}:{day}",
    } for p in problems])
    conn.commit()


def status(conn, names, db_path, out=print):
    out("collector            last clean run         rows(last)")
    for name in names:
        r = conn.execute(
            "SELECT started, rows_added FROM collector_runs WHERE collector = ? AND error IS NULL "
            "AND finished IS NOT NULL ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        out(f"{name:<20} {(r[0] if r else 'never'):<22} {(r[1] if r else '-')}")
    out("\nrows by kind:")
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY 2 DESC LIMIT 40"):
        out(f"  {kind:<28} {n}")
    out(f"\nstore: {db_path} ({os.path.getsize(db_path) / 1024 / 1024:.1f} MB)")
