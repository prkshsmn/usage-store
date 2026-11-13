"""Configuration: environment variables, read once into a plain object.

Everything has a default under the invoking user's home directory, so the store
runs with no configuration at all. Collectors receive the Config explicitly,
which is what lets the tests build one that points at a temporary directory.
"""
import dataclasses
import json
import os
import socket

STATE = os.path.join(os.path.expanduser("~"), ".local", "state")

# collector -> (max hours since the last clean run, must it have added rows
# somewhere in that window). A quiet day legitimately adds nothing for the
# play and notification collectors, so those only have to run. The rest must
# produce: a disk collector that ran and stored nothing is broken, whatever
# its exit code said.
#
# `github` is deliberately absent from the defaults. The traffic API needs a
# token with repository administration read access, and a deployment whose
# token lacks it would show a permanent red here. A check that is always red
# teaches everyone to ignore it. Add it through USAGE_EXPECT once it can pass.
DEFAULT_EXPECT = {
    "containers": (1.0, True),
    "disk": (30, True),
    "dns": (30, True),
    "media_plays": (30, False),
    "jobs": (30, True),
    "notifications": (30, False),
    "backup": (30, True),
}


def _split(value):
    return [p for p in (value or "").split(os.pathsep) if p]


def load_expect(path):
    """An expectations file replaces the defaults entirely, so a deployment
    lists exactly the collectors it schedules and nothing else.

        {"containers": {"max_hours": 1, "must_add": true}, ...}
    """
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out = {}
    for name, entry in doc.items():
        if name.startswith("_"):
            continue
        out[name] = (float(entry["max_hours"]), bool(entry["must_add"]))
    return out


@dataclasses.dataclass
class Config:
    db: str = os.path.join(os.path.expanduser("~"), "telemetry", "usage.db")
    events_dir: str = os.path.join(STATE, "run-job", "events")
    notify_store: str = os.path.join(STATE, "notify", "notifications.jsonl")
    dns_db: str = ""
    playback_db: str = ""
    backup_dir: str = ""
    backup_keep: int = 14
    disk_mounts: list = dataclasses.field(default_factory=lambda: ["/"])
    disk_dirs: list = dataclasses.field(default_factory=list)
    docker_bin: str = "docker"
    gh_bin: str = "gh"
    gh_owner: str = ""
    host_label: str = "host"
    expect: dict = dataclasses.field(default_factory=lambda: dict(DEFAULT_EXPECT))

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        cfg = cls()
        cfg.db = env.get("USAGE_DB", cfg.db)
        cfg.events_dir = env.get("USAGE_EVENTS_DIR", cfg.events_dir)
        cfg.notify_store = env.get("USAGE_NOTIFY_STORE", cfg.notify_store)
        cfg.dns_db = env.get("USAGE_DNS_DB", "")
        cfg.playback_db = env.get("USAGE_PLAYBACK_DB", "")
        cfg.backup_dir = env.get("USAGE_BACKUP_DIR", "")
        cfg.backup_keep = int(env.get("USAGE_BACKUP_KEEP", "14"))
        cfg.disk_mounts = _split(env.get("USAGE_DISK_MOUNTS")) or ["/"]
        cfg.disk_dirs = _split(env.get("USAGE_DISK_DIRS"))
        # Absolute paths matter here: cron runs with PATH=/usr/bin:/bin, so a
        # binary that resolves in a terminal can be missing on a schedule.
        cfg.docker_bin = env.get("USAGE_DOCKER_BIN", "docker")
        cfg.gh_bin = env.get("USAGE_GH_BIN", "gh")
        cfg.gh_owner = env.get("USAGE_GH_OWNER", "")
        cfg.host_label = env.get("USAGE_HOST_LABEL") or socket.gethostname() or "host"
        if env.get("USAGE_EXPECT"):
            cfg.expect = load_expect(env["USAGE_EXPECT"])
        return cfg
