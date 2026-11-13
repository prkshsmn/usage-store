"""The run ledger and the check: the part that refuses to trust an exit code."""
import datetime as dt
import unittest

from _util import TmpCase

from usage_store import cli, store
from usage_store.collectors import COLLECTORS


def row(uid, kind="thing", ts="2026-01-01T00:00:00Z"):
    return {"ts": ts, "source": "test", "kind": kind, "subject": "s", "n": 1, "uid": uid}


class Idempotence(TmpCase, unittest.TestCase):
    def test_the_same_uid_is_stored_once(self):
        self.assertEqual(store.add(self.conn, [row("a"), row("b")]), 2)
        self.assertEqual(store.add(self.conn, [row("a"), row("b"), row("c")]), 1)
        self.assertEqual(self.count(), 3)

    def test_cursor_round_trip(self):
        self.assertEqual(store.get_cursor(self.conn, "x", "0"), "0")
        store.set_cursor(self.conn, "x", 5)
        store.set_cursor(self.conn, "x", 9)
        self.assertEqual(store.get_cursor(self.conn, "x"), "9")

    def test_old_samples_are_pruned_and_other_kinds_are_kept(self):
        old = store.iso(store.now_utc() - dt.timedelta(days=store.SAMPLE_KEEP_DAYS + 1))
        store.add(self.conn, [row("s1", "container_sample", old), row("j1", "job_run", old),
                              row("s2", "container_sample", store.iso(store.now_utc()))])
        store.prune_samples(self.conn)
        self.assertEqual(self.count("container_sample"), 1)
        self.assertEqual(self.count("job_run"), 1, "only the unbounded kinds expire")


class RunAndCheck(TmpCase, unittest.TestCase):
    def run_ok(self, name, rows=1):
        return store.run_collector(name, lambda c, g: rows, self.conn, self.cfg, out=lambda s: None)

    def all_ok(self, rows=1):
        for name in self.cfg.expect:
            self.run_ok(name, rows)

    def test_check_flags_everything_when_nothing_has_run(self):
        problems = store.check(self.conn, self.cfg.expect)
        self.assertEqual(len(problems), len(self.cfg.expect))
        self.assertTrue(all("never ran" in p for p in problems))

    def test_a_failing_collector_is_recorded_and_returns_nonzero(self):
        def boom(conn, cfg):
            store.add(conn, [row("half")])
            raise RuntimeError("source unreachable")
        rc = store.run_collector("disk", boom, self.conn, self.cfg, out=lambda s: None)
        self.assertEqual(rc, 1)
        started, finished, rows, error = self.conn.execute(
            "SELECT started, finished, rows_added, error FROM collector_runs").fetchone()
        self.assertIsNotNone(finished)
        self.assertEqual(rows, 0)
        self.assertIn("RuntimeError: source unreachable", error)
        self.assertEqual(self.count(), 0, "a failed run rolls back its partial rows")

    def test_a_long_error_is_bounded_before_it_is_stored(self):
        def boom(conn, cfg):
            raise RuntimeError("x" * 5000)
        store.run_collector("disk", boom, self.conn, self.cfg, out=lambda s: None)
        error = self.conn.execute("SELECT error FROM collector_runs").fetchone()[0]
        self.assertLess(len(error), 250)

    def test_check_passes_when_every_collector_is_current(self):
        self.all_ok()
        self.assertEqual(store.check(self.conn, self.cfg.expect), [])

    def test_a_collector_that_exits_zero_but_adds_nothing_is_flagged(self):
        # The case no exit code can report: every run "succeeded".
        self.all_ok()
        self.conn.execute("UPDATE collector_runs SET rows_added = 0 WHERE collector = 'disk'")
        problems = store.check(self.conn, self.cfg.expect)
        self.assertEqual(len(problems), 1)
        self.assertIn("disk: ran 1x", problems[0])
        self.assertIn("added 0 rows", problems[0])

    def test_a_quiet_collector_may_add_nothing(self):
        self.all_ok()
        self.conn.execute("UPDATE collector_runs SET rows_added = 0 WHERE collector = 'notifications'")
        self.assertEqual(store.check(self.conn, self.cfg.expect), [])

    def test_rows_from_any_run_in_the_window_count(self):
        self.all_ok()
        self.run_ok("disk", rows=0)  # a second run the same day legitimately adds nothing
        self.assertEqual(store.check(self.conn, self.cfg.expect), [])

    def test_a_stale_clean_run_is_flagged(self):
        self.all_ok()
        old = store.iso(store.now_utc() - dt.timedelta(hours=3))
        self.conn.execute("UPDATE collector_runs SET started = ? WHERE collector = 'containers'", (old,))
        problems = store.check(self.conn, self.cfg.expect)
        self.assertEqual(len(problems), 1)
        self.assertIn("containers: no clean run in 1h", problems[0])

    def test_a_run_that_never_finished_is_not_a_clean_run(self):
        self.all_ok()
        self.conn.execute("UPDATE collector_runs SET finished = NULL WHERE collector = 'jobs'")
        self.assertTrue(any(p.startswith("jobs:") for p in store.check(self.conn, self.cfg.expect)))

    def test_the_cli_check_exits_nonzero_and_writes_the_gap_to_the_store(self):
        self.all_ok()
        self.conn.execute("UPDATE collector_runs SET rows_added = 0 WHERE collector = 'disk'")
        self.conn.commit()
        self.assertEqual(cli.main(["check"], cfg=self.cfg), 1)
        self.assertEqual(cli.main(["check"], cfg=self.cfg), 1)
        gaps = self.conn.execute("SELECT subject FROM events WHERE kind = 'collector_gap'").fetchall()
        self.assertEqual(gaps, [("disk",)], "one gap row per collector per day, however often check runs")

    def test_the_cli_check_exits_zero_when_all_is_well(self):
        self.all_ok()
        self.conn.commit()
        self.assertEqual(cli.main(["check"], cfg=self.cfg), 0)

    def test_every_default_expectation_names_a_real_collector(self):
        self.assertLessEqual(set(self.cfg.expect), set(COLLECTORS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
