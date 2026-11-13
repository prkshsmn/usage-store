"""Collectors, against fixture files and a faked subprocess. No docker, no network."""
import datetime as dt
import io
import json
import os
import sqlite3
import tarfile
import time
import unittest

from _util import TmpCase, completed

from usage_store import collectors as col
from usage_store import store


class Helpers(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(col.parse_size_bytes("1kB"), 1000)
        self.assertEqual(col.parse_size_bytes("1KiB"), 1024)
        self.assertAlmostEqual(col.parse_size_bytes("54.45MiB"), 54.45 * 1024 ** 2)
        self.assertEqual(col.parse_size_bytes("0B"), 0)
        self.assertIsNone(col.parse_size_bytes("12 parsecs"))
        self.assertIsNone(col.parse_size_bytes(None))

    def test_percent(self):
        self.assertEqual(col.percent("0.21%"), 0.21)
        self.assertIsNone(col.percent("--"))


class Jobs(TmpCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.cfg.events_dir)
        self.path = os.path.join(self.cfg.events_dir, "2026-01.jsonl")

    def write(self, lines, mode="a"):
        with open(self.path, mode, encoding="utf-8", newline="\n") as f:
            f.write(lines)

    @staticmethod
    def end(job, rc=0, dur=3):
        return json.dumps({"ts": "2026-01-05T04:00:00+00:00", "job": job, "event": "end",
                           "status": "ok" if rc == 0 else "fail", "rc": rc, "duration_s": dur}) + "\n"

    def test_jobs_are_idempotent_and_incremental(self):
        self.write(json.dumps({"job": "a", "event": "start"}) + "\n" + self.end("a") + self.end("b", rc=1))
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 2, "start events are not runs")
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 0)
        self.write(self.end("a"))
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 1)
        ok = dict(self.conn.execute("SELECT subject, MIN(ok) FROM events GROUP BY subject").fetchall())
        self.assertEqual(ok, {"a": 1, "b": 0})

    def test_a_half_written_line_is_left_for_the_next_run(self):
        self.write(self.end("a") + self.end("b")[:-10])
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 1)
        self.write(self.end("b")[-10:])
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 1)
        self.assertEqual(self.count("job_run"), 2)

    def test_a_file_that_shrinks_does_not_break_the_reader(self):
        self.write(self.end("a") + self.end("b") + self.end("c"))
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 3)
        self.write(self.end("a"), mode="w")  # truncated: same first line at the same offset
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 0)
        # A NEW line now lands on the offset the old "b" line used. An identity
        # made from the offset alone would drop it as a duplicate.
        self.write(self.end("d"))
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 1)
        self.assertEqual(self.count("job_run"), 4)

    def test_a_corrupt_line_is_skipped_not_fatal(self):
        self.write(self.end("a") + "{not json\n" + self.end("b"))
        self.assertEqual(col.collect_jobs(self.conn, self.cfg), 2)

    def test_no_event_files_is_a_failure_not_silence(self):
        os.remove(self.path) if os.path.exists(self.path) else None
        with self.assertRaises(RuntimeError):
            col.collect_jobs(self.conn, self.cfg)


class Notifications(TmpCase, unittest.TestCase):
    def test_tier_and_delivery_are_kept_and_text_is_not(self):
        recs = [
            {"id": "n1", "ts": "2026-01-05T03:00:00+00:00", "job": "disk-check", "tier": "live",
             "severity": "fail", "action": True, "delivered": True, "headline": "SECRET-HEADLINE",
             "detail": "SECRET-DETAIL"},
            {"id": "n2", "ts": "2026-01-05T03:05:00+00:00", "job": "disk-check", "tier": "live",
             "severity": "fail", "action": True, "delivered": False, "suppressed": True},
            {"ts": "2026-01-05T03:06:00+00:00"},  # no job: not a notification
        ]
        with open(self.cfg.notify_store, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        self.assertEqual(col.collect_notifications(self.conn, self.cfg), 2)
        self.assertEqual(col.collect_notifications(self.conn, self.cfg), 0)
        rows = self.conn.execute("SELECT ok, meta FROM events ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 0])
        self.assertTrue(json.loads(rows[1][1])["suppressed"])
        dump = "\n".join(self.conn.iterdump())
        self.assertNotIn("SECRET", dump, "headline and detail text never reach the usage store")


class Dns(TmpCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.today = store.local_day()
        db = sqlite3.connect(self.cfg.dns_db)
        db.execute("CREATE TABLE queries (timestamp INTEGER, client TEXT, domain TEXT)")
        self.src = db

    def tearDown(self):
        self.src.close()
        super().tearDown()

    def noon(self, days_ago):
        d = self.today - dt.timedelta(days=days_ago)
        return time.mktime(dt.datetime(d.year, d.month, d.day, 12).timetuple())

    def insert(self, days_ago, client, domain, n=1):
        self.src.executemany("INSERT INTO queries VALUES (?,?,?)",
                             [(self.noon(days_ago), client, domain)] * n)
        self.src.commit()

    def test_aggregates_finished_days_only_and_is_idempotent(self):
        self.insert(1, "client-a", "example.org", 3)
        self.insert(1, "client-a", "example.com", 1)
        self.insert(0, "client-a", "example.net", 5)  # today is not finished
        self.assertEqual(col.collect_dns(self.conn, self.cfg), 3)  # 2 domains + 1 client total
        self.assertEqual(col.collect_dns(self.conn, self.cfg), 0)
        got = dict(self.conn.execute("SELECT subject, n FROM events WHERE kind = 'dns_domain_day'").fetchall())
        self.assertEqual(got, {"example.org": 3, "example.com": 1})
        self.assertEqual(self.conn.execute(
            "SELECT n FROM events WHERE kind = 'dns_client_day'").fetchone()[0], 4)

    def test_the_long_tail_is_rolled_into_a_remainder(self):
        real = col.DNS_TOP_PER_CLIENT_DAY
        col.DNS_TOP_PER_CLIENT_DAY = 2
        try:
            for i, n in enumerate((5, 4, 3, 2)):
                self.insert(1, "client-a", f"site{i}.example.org", n)
            col.collect_dns(self.conn, self.cfg)
        finally:
            col.DNS_TOP_PER_CLIENT_DAY = real
        self.assertEqual(self.count("dns_domain_day"), 2)
        n, meta = self.conn.execute("SELECT n, meta FROM events WHERE kind = 'dns_client_day'").fetchone()
        self.assertEqual(n, 14)
        self.assertEqual(json.loads(meta), {"domains": 4, "rest": 5})

    def test_an_unconfigured_source_is_a_failure(self):
        self.cfg.dns_db = ""
        with self.assertRaises(RuntimeError):
            col.collect_dns(self.conn, self.cfg)


class MediaPlays(TmpCase, unittest.TestCase):
    def test_plays_carry_duration_and_are_collected_once(self):
        db = sqlite3.connect(self.cfg.playback_db)
        db.execute("CREATE TABLE PlaybackActivity (DateCreated TEXT, UserId TEXT, ItemId TEXT, ItemType TEXT, "
                   "ItemName TEXT, PlaybackMethod TEXT, ClientName TEXT, DeviceName TEXT, PlayDuration INTEGER)")
        db.execute("INSERT INTO PlaybackActivity VALUES ('2026-01-05 20:00:00.123','u1','i1','Movie',"
                   "'A Film','DirectPlay','tv-app','living room',5400)")
        db.commit()
        self.assertEqual(col.collect_media_plays(self.conn, self.cfg), 1)
        self.assertEqual(col.collect_media_plays(self.conn, self.cfg), 0)
        ts, dur = self.conn.execute("SELECT ts, dur_s FROM events").fetchone()
        self.assertEqual((ts, dur), ("2026-01-05T20:00:00Z", 5400))
        db.execute("INSERT INTO PlaybackActivity VALUES ('2026-01-06 20:00:00','u1','i2','Episode',"
                   "'A Show','Transcode','tv-app','living room',1300)")
        db.commit()
        db.close()
        self.assertEqual(col.collect_media_plays(self.conn, self.cfg), 1)

    def test_a_missing_database_is_a_failure_not_silence(self):
        with self.assertRaises(RuntimeError):
            col.collect_media_plays(self.conn, self.cfg)


class ShellOut(TmpCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.real_run = col.run

    def tearDown(self):
        col.run = self.real_run
        super().tearDown()

    def fake(self, handler):
        col.run = lambda cmd, timeout=120, check=False: handler(cmd)

    def test_containers(self):
        def line(name, mem):
            return json.dumps({"Name": name, "MemUsage": f"{mem} / 7.755GiB", "CPUPerc": "0.21%", "PIDs": "12"})
        self.fake(lambda cmd: completed(cmd, "\n".join([line("web", "812MiB"), line("db", "300.5MiB"), "not json"])))
        self.assertGreaterEqual(col.collect_containers(self.conn, self.cfg), 2)
        mb = dict(self.conn.execute("SELECT subject, n FROM events WHERE kind = 'container_sample'").fetchall())
        self.assertAlmostEqual(mb["web"], 812.0, delta=0.1)
        self.assertAlmostEqual(mb["db"], 300.5, delta=0.1)
        # Same minute slot, same uids. Skipped in the rare case the clock
        # crossed a minute boundary between the two calls.
        if store.iso(store.now_utc())[:16] in {r[0][:16] for r in self.conn.execute("SELECT ts FROM events")}:
            self.assertEqual(col.collect_containers(self.conn, self.cfg), 0)

    def test_disk_records_configured_mounts_docker_types_and_directories(self):
        target = os.path.join(self.tmp, "data")
        os.makedirs(target)
        self.cfg.disk_mounts = ["/", "/mnt/backup"]
        self.cfg.disk_dirs = [os.path.join(self.tmp, "da*")]

        def handler(cmd):
            if cmd[0] == "df":
                return completed(cmd, "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                                      "/dev/a 1000 600 400 60% /\n"
                                      "/dev/b 5000 100 4900 2% /mnt/backup\n"
                                      "tmpfs 10 1 9 10% /run\n")
            if cmd[0] == "du":
                return completed(cmd, f"42\t{target}\ndu: cannot read\n")
            return completed(cmd, json.dumps({"Type": "Images", "Size": "4.7GB",
                                              "Reclaimable": "1.2GB (25%)", "TotalCount": "31"}))
        self.fake(handler)
        self.assertEqual(col.collect_disk(self.conn, self.cfg), 4)
        self.assertEqual(col.collect_disk(self.conn, self.cfg), 0, "one row per subject per day")
        used = dict(self.conn.execute("SELECT subject, n FROM events WHERE kind = 'disk_usage'").fetchall())
        self.assertEqual(used, {"/": 600, "/mnt/backup": 100})
        self.assertEqual(self.conn.execute(
            "SELECT n FROM events WHERE kind = 'dir_size'").fetchone()[0], 42 * 1024)

    def test_github_writes_a_marker_even_with_no_traffic(self):
        self.cfg.gh_owner = "octo"

        def handler(cmd):
            if "list" in cmd:
                return completed(cmd, "repo-one\nrepo-two\n")
            return completed(cmd, json.dumps({"count": 0, "uniques": 0, "views": [], "clones": []}))
        self.fake(handler)
        self.assertEqual(col.collect_github(self.conn, self.cfg), 1)
        n, meta = self.conn.execute("SELECT n, meta FROM events WHERE kind = 'github_sweep'").fetchone()
        self.assertEqual((n, json.loads(meta)), (2, {"failed_calls": 0}))

    def test_github_fails_when_every_call_is_refused(self):
        self.cfg.gh_owner = "octo"
        self.fake(lambda cmd: completed(cmd, "repo-one\n") if "list" in cmd else completed(cmd, "", 1))
        with self.assertRaises(RuntimeError):
            col.collect_github(self.conn, self.cfg)


class GithubLoad(TmpCase, unittest.TestCase):
    VIEWS = {"count": 3, "uniques": 2, "views": [
        {"timestamp": "2026-01-04T00:00:00Z", "count": 2, "uniques": 1},
        {"timestamp": "2026-01-05T00:00:00Z", "count": 1, "uniques": 1}]}

    def test_load_from_a_directory_is_idempotent_and_skips_errors(self):
        src = os.path.join(self.tmp, "traffic")
        os.makedirs(src)
        with open(os.path.join(src, "repo-one__views.json"), "w") as f:
            json.dump(self.VIEWS, f)
        with open(os.path.join(src, "repo-two__clones.json"), "w") as f:
            json.dump({"error": "403"}, f)
        self.assertEqual(col.load_github(self.conn, self.cfg, src), 3)  # 2 buckets + marker
        self.assertEqual(col.load_github(self.conn, self.cfg, src), 0)

    def test_load_from_a_tarball_drops_escaping_members(self):
        path = os.path.join(self.tmp, "t.tgz")
        with tarfile.open(path, "w:gz") as t:
            for name in ("repo-one__views.json", "../escape__views.json"):
                data = json.dumps(self.VIEWS).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        self.assertEqual(col.load_github(self.conn, self.cfg, path), 3)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "escape__views.json")))
        self.assertEqual({r[0] for r in self.conn.execute(
            "SELECT subject FROM events WHERE kind = 'github_views'")}, {"repo-one"})

    def test_an_empty_source_is_a_failure(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(RuntimeError):
            col.load_github(self.conn, self.cfg, empty)


class Backup(TmpCase, unittest.TestCase):
    def test_the_copy_is_read_back_and_old_copies_are_pruned(self):
        os.makedirs(os.path.dirname(self.cfg.backup_dir))
        store.add(self.conn, [{"ts": "2026-01-01T00:00:00Z", "source": "t", "kind": "k", "uid": "1"}])
        os.makedirs(self.cfg.backup_dir)
        for i in range(1, 20):
            open(os.path.join(self.cfg.backup_dir, f"usage-2000-01-{i:02d}.db"), "w").close()
        self.assertEqual(col.collect_backup(self.conn, self.cfg), 1)
        kept = sorted(os.listdir(self.cfg.backup_dir))
        self.assertEqual(len(kept), self.cfg.backup_keep)
        copy = sqlite3.connect(os.path.join(self.cfg.backup_dir, kept[-1]))
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        copy.close()

    def test_an_unmounted_volume_is_a_failure_not_a_new_directory(self):
        with self.assertRaises(RuntimeError):
            col.collect_backup(self.conn, self.cfg)
        self.assertFalse(os.path.exists(self.cfg.backup_dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
