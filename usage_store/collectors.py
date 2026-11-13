"""Collectors: one question each.

Every collector exists to answer a named keep-or-remove question, stated in its
docstring. A collector with no question gets switched off once the decision it
served is made. Each one is `fn(conn, cfg) -> rows added` and raises on
failure; `store.run_collector` records the outcome either way.

PRIVACY

* DNS is stored as per-client, per-domain, per-day counts (the top 300 domains
  per client per day, plus one remainder row). No query strings, no page text,
  no timestamps finer than a day.
* Plays are stored by account id, never by a person's name.
* No collector stores error text from another service, request bodies or
  credentials.
"""
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

from .store import add, get_cursor, iso, local_day, now_utc, prune_samples, set_cursor

DNS_TOP_PER_CLIENT_DAY = 300
DNS_FIRST_RUN_BACKFILL_DAYS = 8


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=120, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def copy_sqlite(src):
    """Copy a live SQLite file (and its WAL) so it can be read without touching
    the writer. Returns (tmpdir, path). The caller removes tmpdir."""
    tmp = tempfile.mkdtemp(prefix="usage-store-")
    dst = os.path.join(tmp, os.path.basename(src))
    shutil.copyfile(src, dst)
    if os.path.exists(src + "-wal"):
        try:
            shutil.copyfile(src + "-wal", dst + "-wal")
        except OSError:
            pass
    return tmp, dst


def parse_size_bytes(text):
    """'54.45MiB' -> bytes. Accepts B, kB, MB, GB, TB and the iB forms."""
    m = re.match(r"^\s*([0-9.]+)\s*([A-Za-z]*)", text or "")
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2).lower()
    scale = {
        "": 1, "b": 1,
        "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
        "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
    }.get(unit)
    return None if scale is None else num * scale


def percent(text):
    try:
        return float(str(text).strip().rstrip("%"))
    except ValueError:
        return None


def tail_jsonl(conn, cursor_name, path, to_row):
    """Read the new complete lines of an append-only JSONL file past a byte
    offset. A half written last line is left for the next run, and a file that
    shrank (rotated or truncated) is read again from the start.

    `to_row(record, tag)` gets a tag built from the line's byte offset AND a
    hash of its bytes. Offset alone is not an identity: after a truncation a
    different line can land on an offset that was already used, and a uid made
    from the offset alone would drop it as a duplicate. With both, re-reading
    the same line adds nothing and a new line at an old offset is still new."""
    off = int(get_cursor(conn, cursor_name, "0"))
    size = os.path.getsize(path)
    if size < off:
        off = 0
    rows = []
    with open(path, "rb") as f:
        f.seek(off)
        pos = off
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            here = pos
            pos += len(raw)
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            row = to_row(rec, f"{here}:{hashlib.sha1(raw).hexdigest()[:10]}")
            if row:
                rows.append(row)
    added = add(conn, rows)
    set_cursor(conn, cursor_name, pos)
    return added


# ------------------------------------------------------------- collectors

def collect_containers(conn, cfg):
    """Question: which containers earn their memory?
    `docker stats` is instantaneous, so without a sampler there is no history
    to judge by. One row per container per minute slot, plus one host row."""
    out = run([cfg.docker_bin, "stats", "--no-stream", "--format", "{{json .}}"], timeout=90, check=True)
    ts = iso(now_utc().replace(second=0, microsecond=0))
    rows = []
    for line in out.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        used = parse_size_bytes((d.get("MemUsage") or "").split("/")[0])
        name = d.get("Name")
        if not name or used is None:
            continue
        rows.append({
            "ts": ts, "source": "docker", "project": "host", "kind": "container_sample",
            "subject": name, "n": round(used / 1024 / 1024, 1),
            "meta": {"cpu": percent(d.get("CPUPerc")), "pids": d.get("PIDs")},
            "uid": f"c:{name}:{ts[:16]}",
        })
    meminfo = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                meminfo[k] = int(v.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    load = None
    try:
        with open("/proc/loadavg") as f:
            load = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    if meminfo:
        rows.append({
            "ts": ts, "source": "proc", "project": "host", "kind": "host_sample",
            "subject": cfg.host_label, "n": round(meminfo.get("MemAvailable", 0) / 1024, 1),
            "meta": {
                "mem_total_mb": round(meminfo.get("MemTotal", 0) / 1024),
                "swap_used_mb": round((meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)) / 1024),
                "load1": load,
            },
            "uid": f"h:{ts[:16]}",
        })
    n = add(conn, rows)
    prune_samples(conn)
    return n


def collect_disk(conn, cfg):
    """Question: what is driving disk growth?
    One row per mount, per docker storage type and per watched directory, per
    day. A series of daily sizes answers what a single `du` by hand cannot."""
    day = local_day().isoformat()
    ts = iso(now_utc())
    rows = []
    df = run(["df", "-kP"], timeout=60)
    for line in df.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = parts[5]
        if any(mount == m or (m != "/" and mount.startswith(m)) for m in cfg.disk_mounts):
            rows.append({
                "ts": ts, "source": "df", "project": "host", "kind": "disk_usage",
                "subject": mount, "n": int(parts[2]),
                "meta": {"size_kb": int(parts[1]), "avail_kb": int(parts[3])},
                "uid": f"disk:{mount}:{day}",
            })
    try:
        ddf = run([cfg.docker_bin, "system", "df", "--format", "{{json .}}"], timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        ddf = ""  # a host without docker still has a disk worth measuring
    for line in ddf.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        size = parse_size_bytes(d.get("Size"))
        rec = parse_size_bytes((d.get("Reclaimable") or "").split("(")[0])
        if d.get("Type") and size is not None:
            rows.append({
                "ts": ts, "source": "docker", "project": "host", "kind": "docker_df",
                "subject": d["Type"], "n": size,
                "meta": {"reclaimable": rec, "count": d.get("TotalCount")},
                "uid": f"ddf:{d['Type']}:{day}",
            })
    paths = sorted(p for pattern in cfg.disk_dirs for p in glob.glob(pattern))
    if paths:
        try:
            res = run(["du", "-k", "-s", "--one-file-system"] + paths, timeout=900)
            lines = res.stdout.splitlines()  # a permission error still yields lines
        except (OSError, subprocess.TimeoutExpired):
            lines = []
        for line in lines:
            kb, _, path = line.partition("\t")
            if kb.isdigit() and path:
                rows.append({
                    "ts": ts, "source": "du", "project": "host", "kind": "dir_size",
                    "subject": path, "n": int(kb) * 1024, "uid": f"dir_size:{path}:{day}",
                })
    return add(conn, rows)


def _local_midnight_epoch(d):
    return time.mktime(dt.datetime(d.year, d.month, d.day).timetuple())


def collect_dns(conn, cfg):
    """Question: which self-hosted services does anyone actually open?
    Reads a Pi-hole FTL database. Per-client, per-domain, per-day counts for
    each finished day only, so a day's numbers are final when they are stored."""
    if not cfg.dns_db:
        raise RuntimeError("USAGE_DNS_DB is not set")
    today = local_day()
    last = get_cursor(conn, "dns_day")
    start = (
        dt.date.fromisoformat(last) + dt.timedelta(days=1)
        if last else today - dt.timedelta(days=DNS_FIRST_RUN_BACKFILL_DAYS)
    )
    if start >= today:
        return 0
    tmp, path = copy_sqlite(cfg.dns_db)
    rows = []
    try:
        db = sqlite3.connect(path)
        d = start
        while d < today:
            lo, hi = _local_midnight_epoch(d), _local_midnight_epoch(d + dt.timedelta(days=1))
            by_client = {}
            for client, domain, n in db.execute(
                "SELECT client, domain, COUNT(*) FROM queries "
                "WHERE timestamp >= ? AND timestamp < ? GROUP BY client, domain", (lo, hi)
            ):
                by_client.setdefault(client, []).append((domain, n))
            ts = f"{d.isoformat()}T12:00:00Z"
            for client, items in by_client.items():
                items.sort(key=lambda x: -x[1])
                for domain, n in items[:DNS_TOP_PER_CLIENT_DAY]:
                    rows.append({
                        "ts": ts, "source": "dns", "project": "host", "kind": "dns_domain_day",
                        "subject": domain, "n": n, "meta": {"client": client},
                        "uid": f"dns:{d}:{client}:{domain}",
                    })
                rest = sum(n for _, n in items[DNS_TOP_PER_CLIENT_DAY:])
                rows.append({
                    "ts": ts, "source": "dns", "project": "host", "kind": "dns_client_day",
                    "subject": client, "n": sum(n for _, n in items),
                    "meta": {"domains": len(items), "rest": rest},
                    "uid": f"dnsd:{d}:{client}",
                })
            d += dt.timedelta(days=1)
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    added = add(conn, rows)
    set_cursor(conn, "dns_day", (today - dt.timedelta(days=1)).isoformat())
    return added


def collect_media_plays(conn, cfg):
    """Question: what in the media library is watched, how often, for how long?
    Reads the Jellyfin Playback Reporting plugin's database. One row per play,
    by account id."""
    if not cfg.playback_db or not os.path.exists(cfg.playback_db):
        raise RuntimeError("playback database missing (set USAGE_PLAYBACK_DB; is the plugin installed?)")
    tmp, path = copy_sqlite(cfg.playback_db)
    rows, top = [], int(get_cursor(conn, "media_plays_rowid", "0"))
    try:
        db = sqlite3.connect(path)
        last = top
        for rid, when, user, item, itype, name, method, client, device, dur in db.execute(
            "SELECT rowid, DateCreated, UserId, ItemId, ItemType, ItemName, PlaybackMethod, "
            "ClientName, DeviceName, PlayDuration FROM PlaybackActivity WHERE rowid > ? ORDER BY rowid", (last,)
        ):
            top = max(top, rid)
            rows.append({
                "ts": str(when or "")[:19].replace(" ", "T") + "Z", "source": "media",
                "project": "media", "kind": "media_play", "subject": (name or "")[:200],
                "n": 1, "dur_s": dur,
                "meta": {"type": itype, "method": method, "client": client, "device": device,
                         "user": user, "item": item},
                "uid": f"pb:{when}:{user}:{item}:{rid}",
            })
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    added = add(conn, rows)
    set_cursor(conn, "media_plays_rowid", top)
    return added


def collect_jobs(conn, cfg):
    """Question: which cron jobs run, for how long, and how often do they fail?
    Imports the end events that bin/run-job.sh writes, one file per month."""
    files = sorted(glob.glob(os.path.join(cfg.events_dir, "*.jsonl")))
    if not files:
        # Zero files is a broken wrapper or a wrong path, never a quiet day.
        raise RuntimeError(f"no event files under {cfg.events_dir}")
    added = 0
    for path in files:
        base = os.path.basename(path)

        def to_row(rec, tag, base=base):
            if rec.get("event") != "end" or not rec.get("job"):
                return None
            return {
                "ts": rec.get("ts") or iso(now_utc()), "source": "run-job", "project": "platform",
                "kind": "job_run", "subject": rec["job"], "n": 1, "dur_s": rec.get("duration_s"),
                "ok": int(rec.get("rc", 0) == 0),
                "meta": {"rc": rec.get("rc"), "status": rec.get("status")},
                "uid": f"job:{base}:{tag}",
            }
        added += tail_jsonl(conn, f"jobs_off:{base}", path, to_row)
    return added


def collect_notifications(conn, cfg):
    """Question: which senders are noise?
    Imports every notification the notifier handled: sender, tier, severity,
    delivered or suppressed. Headline and detail text are not stored."""

    def to_row(rec, tag):
        job = rec.get("job")
        if not job:
            return None
        return {
            "ts": rec.get("ts") or iso(now_utc()), "source": "notifier", "project": "platform",
            "kind": "notification", "subject": job, "n": 1,
            "ok": int(bool(rec.get("delivered"))),
            "meta": {
                "tier": rec.get("tier"), "severity": rec.get("severity"),
                "action": bool(rec.get("action")), "suppressed": bool(rec.get("suppressed")),
            },
            "uid": f"notif:{rec.get('id') or tag}",
        }
    return tail_jsonl(conn, "notif_off", cfg.notify_store, to_row)


def _github_rows(repo, what, data):
    return [{
        "ts": b["timestamp"], "source": "github", "project": repo,
        "kind": f"github_{what}", "subject": repo, "n": b.get("count", 0),
        "meta": {"uniques": b.get("uniques", 0)},
        "uid": f"gh:{what}:{repo}:{b['timestamp']}",
    } for b in data.get(what, [])]


def collect_github(conn, cfg):
    """Question: does anyone outside look at these repositories?
    GitHub keeps traffic for 14 days only, so this must run at least weekly or
    the history is gone."""
    if not cfg.gh_owner:
        raise RuntimeError("USAGE_GH_OWNER is not set")
    listed = run([cfg.gh_bin, "repo", "list", cfg.gh_owner, "--limit", "100",
                  "--json", "name", "-q", ".[].name"], timeout=120, check=True)
    repos = [r for r in listed.stdout.split() if r]
    rows, failed = [], 0
    for repo in repos:
        for what in ("views", "clones"):
            res = run([cfg.gh_bin, "api", f"repos/{cfg.gh_owner}/{repo}/traffic/{what}"], timeout=60)
            if res.returncode != 0:
                failed += 1
                continue
            try:
                data = json.loads(res.stdout)
            except ValueError:
                failed += 1
                continue
            rows += _github_rows(repo, what, data)
    # Always write a marker row, so a week with no traffic is still a measurement
    # and can be told apart from a week when the collector did not run.
    rows.append({
        "ts": iso(now_utc()), "source": "github", "project": "all", "kind": "github_sweep",
        "subject": cfg.gh_owner, "n": len(repos), "meta": {"failed_calls": failed},
        "uid": f"ghs:{local_day().isoformat()}",
    })
    if repos and failed >= len(repos) * 2:
        raise RuntimeError(f"every GitHub traffic call failed ({failed})")
    return add(conn, rows)


def _safe_extract(tar, dest):
    """Extract only plain files whose names stay inside dest. Links, devices,
    absolute paths and ../ escapes are dropped."""
    root = os.path.realpath(dest)
    for m in tar:
        target = os.path.realpath(os.path.join(dest, m.name))
        if not m.isfile() or not target.startswith(root + os.sep):
            continue
        with tar.extractfile(m) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def load_github(conn, cfg, src):
    """Load traffic fetched on another machine: a directory, a .tgz, or '-' for
    a .tgz on stdin, holding <repo>__views.json and <repo>__clones.json as the
    API returned them. Same uids as the live collector, so loading twice adds
    nothing. This exists for the case where the collecting host's token cannot
    read traffic and a workstation's can."""
    tmp = tempfile.mkdtemp(prefix="usage-github-")
    try:
        if src == "-":
            with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as t:
                _safe_extract(t, tmp)
            root = tmp
        elif os.path.isdir(src):
            root = src
        else:
            with tarfile.open(src) as t:
                _safe_extract(t, tmp)
            root = tmp
        rows, repos = [], set()
        for path in sorted(glob.glob(os.path.join(root, "*__views.json"))
                           + glob.glob(os.path.join(root, "*__clones.json"))):
            repo, _, rest = os.path.basename(path).partition("__")
            what = rest.split(".")[0]
            repos.add(repo)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except ValueError:
                continue
            if "error" in data:
                continue
            rows += _github_rows(repo, what, data)
        if not repos:
            raise RuntimeError("no <repo>__views.json or <repo>__clones.json files found")
        rows.append({
            "ts": iso(now_utc()), "source": "github", "project": "all", "kind": "github_sweep",
            "subject": cfg.gh_owner or "loaded", "n": len(repos), "meta": {"loaded": True},
            "uid": f"ghs:{local_day().isoformat()}",
        })
        return add(conn, rows)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def collect_backup(conn, cfg):
    """Question: would the measurements survive the disk they describe?
    Snapshots the store to another volume and keeps the newest N. The copy is
    opened and counted afterwards: a backup is the file you can read back, not
    the command that returned 0."""
    if not cfg.backup_dir:
        raise RuntimeError("USAGE_BACKUP_DIR is not set")
    parent = os.path.dirname(cfg.backup_dir.rstrip("/\\")) or os.sep
    if not os.path.isdir(parent):
        raise RuntimeError("backup parent directory missing (is the volume mounted?)")
    os.makedirs(cfg.backup_dir, exist_ok=True)
    dest = os.path.join(cfg.backup_dir, f"usage-{local_day().isoformat()}.db")
    conn.commit()
    expected = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
    verify = sqlite3.connect(dest)
    try:
        got = verify.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        verify.close()
    if got != expected:
        raise RuntimeError(f"backup holds {got} events, the store holds {expected}")
    for old in sorted(glob.glob(os.path.join(cfg.backup_dir, "usage-*.db")))[:-cfg.backup_keep]:
        os.remove(old)
    return 1


COLLECTORS = {
    "containers": collect_containers,
    "disk": collect_disk,
    "dns": collect_dns,
    "media_plays": collect_media_plays,
    "jobs": collect_jobs,
    "notifications": collect_notifications,
    "github": collect_github,
    "backup": collect_backup,
}
