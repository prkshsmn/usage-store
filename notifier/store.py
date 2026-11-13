"""The notification store: the one module allowed to read or write it.

WHY THIS IS A MODULE AND NOT A FEW LINES IN EACH SCRIPT

`notifications.jsonl` is appended to by every scheduled job that notifies, and
rewritten in full by two paths: the trim when it outgrows its cap, and the
digest when it marks items as sent. Both rewrites are read-modify-write, and
several jobs share a cron minute.

An unlocked rewrite racing an append loses the append silently, and a lost
record is a notification that never happened as far as anything downstream is
concerned. So the rewrite path holds an exclusive lock and the append path
holds a shared one, in one place.

WINDOWS

fcntl does not exist there. The store only matters on the Linux host that runs
the cron jobs; a workstation runs this code to test it, one process at a time.
So the lock degrades to a no-op instead of an ImportError.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager

from . import paths

try:
    import fcntl
    HAVE_FLOCK = True
except ImportError:  # Windows
    HAVE_FLOCK = False

# Keep the store bounded. It is runtime data, not history: the usage store
# imports every record daily and that is where the long view lives.
MAX_LINES = 4000
# Trim only once well past the cap, not on every append. The trim is the
# expensive, lock-holding path, and running it on every append past the cap
# would serialise every notifying job behind a full file rewrite.
TRIM_AT = int(MAX_LINES * 1.25)


def warn(msg: str) -> None:
    print(f"notify-store: {msg}", file=sys.stderr)


@contextmanager
def _locked(exclusive: bool):
    if not HAVE_FLOCK:
        yield
        return
    fh = None
    try:
        paths.lock().parent.mkdir(parents=True, exist_ok=True)
        fh = paths.lock().open("a+")
        fcntl.flock(fh, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError as exc:
        # A lock that cannot be taken must not stop a notification being
        # recorded. The unlocked write risks a rare lost line; refusing to
        # write guarantees one.
        warn(f"could not lock the store ({exc}), proceeding unlocked")
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


def _parse(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a truncated line from an earlier crash is skipped, not fatal
    return rows


def read() -> list[dict]:
    """Every record, oldest first."""
    store = paths.store()
    if not store.exists():
        return []
    with _locked(exclusive=False):
        try:
            return _parse(store.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            warn(f"could not read the store: {exc}")
            return []


def append(record: dict) -> bool:
    """Add one record. Returns whether it landed on disk.

    The return value is the point. A caller that ignores it can print a success
    line for a record that does not exist anywhere.
    """
    store = paths.store()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _locked(exclusive=False):
        try:
            store.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND makes a single short write atomic on Linux, so concurrent
            # appends are safe against each other. The lock is what makes them
            # safe against a rewrite.
            with store.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            warn(f"could not append to the store: {exc}")
            return False
    _maybe_trim()
    return True


def rewrite(rows: list[dict]) -> bool:
    """Replace the whole store. Held exclusively: nothing may append during it."""
    store = paths.store()
    tmp = store.with_suffix(".jsonl.tmp")
    with _locked(exclusive=True):
        try:
            with tmp.open("w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            # Write then replace, never truncate then write. A crash midway
            # through an in-place rewrite would leave the store half written.
            tmp.replace(store)
            return True
        except OSError as exc:
            warn(f"could not rewrite the store: {exc}")
            return False


def mark(ids: set, fields: dict) -> bool:
    """Set `fields` on every record whose id is in `ids`.

    Read and rewrite happen under ONE exclusive lock. Reading first and
    rewriting later, as two separate steps, would silently drop any record
    appended in between.
    """
    store = paths.store()
    tmp = store.with_suffix(".jsonl.tmp")
    with _locked(exclusive=True):
        try:
            rows = _parse(store.read_text(encoding="utf-8", errors="replace"))
            with tmp.open("w", encoding="utf-8") as f:
                for row in rows:
                    if row.get("id") in ids:
                        row.update(fields)
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tmp.replace(store)
            return True
        except OSError as exc:
            warn(f"could not update the store: {exc}")
            return False


def _maybe_trim() -> None:
    store = paths.store()
    try:
        with store.open("rb") as f:
            if sum(1 for _ in f) <= TRIM_AT:
                return
    except OSError:
        return
    with _locked(exclusive=True):
        try:
            lines = store.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if len(lines) <= TRIM_AT:
            return  # another process trimmed first
        tmp = store.with_suffix(".jsonl.tmp")
        try:
            tmp.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")
            tmp.replace(store)
        except OSError as exc:
            warn(f"could not trim the store: {exc}")
