"""The export publishes fresh numbers or nothing."""
import datetime as dt
import json
import os
import unittest

from _util import TmpCase

from usage_store import cli, export, store

NOW = dt.datetime(2026, 1, 20, 12, 0, tzinfo=dt.timezone.utc)
OFFSET = dt.timezone(dt.timedelta(hours=-5))


def ago(days):
    # run-job.sh writes local-offset timestamps, so the fixtures do too
    return (NOW - dt.timedelta(days=days)).astimezone(OFFSET).isoformat()


class Export(TmpCase, unittest.TestCase):
    def runs(self, rows):
        store.add(self.conn, [{"ts": ts, "source": "run-job", "kind": kind, "subject": job, "n": 1,
                               "ok": ok, "uid": f"r{i}"} for i, (ts, kind, job, ok) in enumerate(rows)])
        self.conn.commit()

    def test_counts_runs_inside_the_window_only(self):
        self.runs([(ago(1), "job_run", "a", 1), (ago(2), "job_run", "a", 1), (ago(3), "job_run", "b", 0),
                   (ago(13.9), "job_run", "c", 1),
                   (ago(15), "job_run", "old", 1),          # outside the window
                   (ago(1), "container_sample", "x", 1)])   # a different kind of event
        s = export.job_stats(self.cfg.db, NOW)
        self.assertEqual((s["runs"], s["jobs_ran"], s["success_rate"]), (4, 3, 0.75))

    def test_a_store_that_stopped_updating_is_refused(self):
        self.runs([(ago(4), "job_run", "a", 1), (ago(5), "job_run", "b", 1)])
        with self.assertRaises(export.Refuse) as ctx:
            export.job_stats(self.cfg.db, NOW)
        self.assertIn("days old", str(ctx.exception))

    def test_a_store_updated_two_days_ago_is_still_accepted(self):
        self.runs([(ago(2), "job_run", "a", 1)])
        self.assertEqual(export.job_stats(self.cfg.db, NOW)["runs"], 1)

    def test_empty_and_missing_stores_are_refused(self):
        with self.assertRaises(export.Refuse):
            export.job_stats(self.cfg.db, NOW)
        with self.assertRaises(export.Refuse):
            export.job_stats(os.path.join(self.tmp, "nope.db"), NOW)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope.db")),
                         "looking for a store must not create one")

    def test_a_row_with_an_unreadable_timestamp_is_skipped_not_fatal(self):
        self.runs([("not a date", "job_run", "z", 1), (ago(1), "job_run", "a", 1)])
        self.assertEqual(export.job_stats(self.cfg.db, NOW)["runs"], 1)

    def test_a_refused_export_leaves_the_previous_file_untouched(self):
        out = os.path.join(self.tmp, "stats.json")
        with open(out, "w") as f:
            f.write('{"previous": true}\n')
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
        self.runs([(old, "job_run", "a", 1)])
        self.assertEqual(cli.main(["export-stats", "--out", out], cfg=self.cfg), export.EXIT_REFUSED)
        with open(out) as f:
            self.assertEqual(json.load(f), {"previous": True})

    def test_a_fresh_export_is_written(self):
        out = os.path.join(self.tmp, "stats.json")
        self.runs([(dt.datetime.now(dt.timezone.utc).isoformat(), "job_run", "a", 1)])
        self.assertEqual(cli.main(["export-stats", "--out", out], cfg=self.cfg), 0)
        with open(out) as f:
            self.assertEqual(json.load(f)["runs"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
