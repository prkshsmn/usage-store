"""Hand one rendered notification to the transport and report whether it landed.

The transport is any command that takes

    <title> <body> <priority> <tags> <click_url>

and, when NOTIFY_STRICT=1 is in its environment, exits 0 only if the message
was accepted by the server. bin/notify.sh is the reference implementation: it
posts to an ntfy server and derives its exit code from the HTTP status.

WHY STRICT IS A SWITCH AND NOT THE DEFAULT

A shell job that calls the transport directly must never fail because a push
did not land: a notification problem must not become a skipped backup. So the
transport exits 0 for those callers whatever happens. The notifier is the one
caller that needs the truth, because it records `delivered` and the digest
retries on it, so it asks for the strict exit code. An exit code that was not
derived from the effect (here, the HTTP status) is never trusted as delivery.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_TRANSPORT = Path(__file__).resolve().parent.parent / "bin" / "notify.sh"


def _split(value: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(value)
    # Non-POSIX splitting keeps backslashes in Windows paths, and also keeps
    # the quotes around each token, so those are removed here.
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in shlex.split(value, posix=False)]


def command() -> list[str] | None:
    custom = os.environ.get("NOTIFY_TRANSPORT")
    if custom:
        return _split(custom)
    if DEFAULT_TRANSPORT.exists():
        return ["bash", str(DEFAULT_TRANSPORT)]
    return None


def send(title: str, body: str, priority: str, tags: str, click: str = "") -> bool:
    cmd = command()
    if cmd is None:
        print("notify-transport: no transport configured (set NOTIFY_TRANSPORT)", file=sys.stderr)
        return False
    env = dict(os.environ, NOTIFY_STRICT="1")
    try:
        proc = subprocess.run(cmd + [title, body, priority, tags, click], env=env,
                              timeout=45, check=False, capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001
        print(f"notify-transport: could not run the transport: {exc}", file=sys.stderr)
        return False
    err = (proc.stderr or "").strip()
    if err:
        # Passed through: under cron, stderr lands in the job's own log, which
        # is where somebody will look.
        print(err, file=sys.stderr)
    return proc.returncode == 0


def fallback(title: str, body: str) -> str:
    """Last resort for a live notification whose push failed.

    NOTIFY_FALLBACK names a command that receives <title> <body-file> and
    prints a reference (for example an issue URL) on stdout. It should use a
    channel that does not share a failure domain with the primary one: if the
    push server runs on the host that is down, the fallback must not.
    Returns the reference, or an empty string when nothing was filed.
    """
    custom = os.environ.get("NOTIFY_FALLBACK")
    if not custom:
        return ""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="notify-fallback-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        proc = subprocess.run(_split(custom) + [title, tmp], timeout=45, check=False,
                              capture_output=True, text=True)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip().splitlines()[0] if (proc.stdout or "").strip() else ""
