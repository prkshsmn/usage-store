"""Shared test helpers. Everything runs against temporary directories: no
network beyond loopback, no docker, no real databases."""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from usage_store.config import Config  # noqa: E402
from usage_store.store import connect  # noqa: E402


def make_cfg(tmp):
    return Config(
        db=os.path.join(tmp, "usage.db"),
        events_dir=os.path.join(tmp, "events"),
        notify_store=os.path.join(tmp, "notifications.jsonl"),
        dns_db=os.path.join(tmp, "dns.db"),
        playback_db=os.path.join(tmp, "playback.db"),
        backup_dir=os.path.join(tmp, "backups", "usage"),
        host_label="test-host",
    )


class TmpCase:
    """Mixin: a temp dir, a Config pointing into it, and an open store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage-store-test-")
        self.cfg = make_cfg(self.tmp)
        self.conn = connect(self.cfg.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def count(self, kind=None):
        if kind:
            return self.conn.execute("SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def find_bash():
    """A POSIX bash, or None. On Windows a bare `bash` can resolve to a
    different subsystem with a different filesystem, so there it must be named
    explicitly through TEST_BASH."""
    if os.environ.get("TEST_BASH"):
        return os.environ["TEST_BASH"]
    if os.name == "nt":
        return None
    return shutil.which("bash")


def completed(cmd, stdout="", returncode=0):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
