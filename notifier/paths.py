"""Where the notifier keeps its files. Resolved at call time, from the environment.

    NOTIFY_HOME       state directory (default ~/.local/state/notify)
    NOTIFY_POLICY     policy file      (default $NOTIFY_HOME/policy.json)
    NOTIFY_JOBS       job registry     (default $NOTIFY_HOME/jobs.json)
"""
import os
from pathlib import Path


def home() -> Path:
    default = Path(os.path.expanduser("~")) / ".local" / "state" / "notify"
    return Path(os.environ.get("NOTIFY_HOME") or default)


def store() -> Path:
    return home() / "notifications.jsonl"


def lock() -> Path:
    return home() / ".notifications.lock"


def policy() -> Path:
    return Path(os.environ.get("NOTIFY_POLICY") or home() / "policy.json")


def jobs() -> Path:
    return Path(os.environ.get("NOTIFY_JOBS") or home() / "jobs.json")


def fallback_ledger() -> Path:
    return home() / "fallback-issued.json"
