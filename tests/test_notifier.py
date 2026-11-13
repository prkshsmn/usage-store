"""The envelope, dedup, the store-failure exit, the digest and its one retry."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

from _util import ROOT

from notifier import digest, event, paths, store, transport


class NotifierCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="notifier-test-")
        self._env = dict(os.environ)
        os.environ["NOTIFY_HOME"] = self.tmp
        os.environ["NOTIFY_POLICY"] = os.path.join(ROOT, "config", "policy.example.json")
        os.environ["NOTIFY_JOBS"] = os.path.join(ROOT, "config", "jobs.example.json")
        for k in ("NOTIFY_JOB", "NOTIFY_TRANSPORT", "NOTIFY_FALLBACK"):
            os.environ.pop(k, None)
        self.sent = []
        self.deliver = True
        self._send = transport.send

        def fake_send(title, body, priority, tags, click=""):
            self.sent.append({"title": title, "body": body, "priority": priority, "click": click})
            return self.deliver
        transport.send = fake_send

    def tearDown(self):
        transport.send = self._send
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def notify(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = event.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def run_digest(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = digest.main([])
        return rc, out.getvalue(), err.getvalue()


class Envelope(NotifierCase):
    def test_a_live_push_carries_the_five_fields_in_order(self):
        rc, out, _ = self.notify("--job", "disk-check", "--headline", "root volume at 91%",
                                 "--detail", "58 of 63 GB used", "--severity", "warn")
        self.assertEqual(rc, 0)
        self.assertIn("tier=live", out)
        self.assertIn("delivered", out)
        push = self.sent[0]
        self.assertEqual(push["title"], "[!] host: root volume at 91%")
        lines = push["body"].splitlines()
        self.assertTrue(lines[0].startswith("Runs: every 6 hours"))
        self.assertTrue(lines[1].startswith("Does: "))
        self.assertIn("58 of 63 GB used", lines)
        self.assertIn("Action needed: YES", lines)
        self.assertTrue(lines[-1].startswith("Suggested: "))
        rec = store.read()[0]
        self.assertTrue(rec["delivered"])
        self.assertEqual(rec["tier_why"], "the standing policy for this job")

    def test_the_job_id_comes_from_the_wrapper_when_not_given(self):
        os.environ["NOTIFY_JOB"] = "nightly-backup"
        self.notify("--headline", "done")
        self.assertEqual(store.read()[0]["job"], "nightly-backup")

    def test_a_digest_tier_event_is_recorded_and_not_pushed(self):
        rc, out, _ = self.notify("--job", "daily-summary", "--headline", "41 runs, 0 failed")
        self.assertEqual((rc, self.sent), (0, []))
        self.assertEqual(store.read()[0]["tier"], "digest")

    def test_a_failure_escalates_a_digest_sender_to_live(self):
        self.notify("--job", "nightly-backup", "--headline", "dump failed", "--severity", "fail")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["priority"], "high")
        self.assertIn("escalated from digest", store.read()[0]["tier_why"])

    def test_a_chronic_sender_never_pushes_even_on_failure(self):
        self.notify("--job", "token-expiry-check", "--headline", "1 token expired", "--severity", "fail")
        self.assertEqual(self.sent, [])
        self.assertEqual(store.read()[0]["tier"], "digest")

    def test_a_silent_sender_is_recorded_only(self):
        self.notify("--job", "renewal-reminder", "--headline", "2 renewals", "--severity", "fail")
        self.assertEqual(self.sent, [])
        self.assertEqual(store.read()[0]["tier"], "silent")

    def test_an_unknown_sender_can_still_notify(self):
        rc, _, _ = self.notify("--job", "brand-new-job", "--headline", "hello", "--severity", "fail")
        self.assertEqual(rc, 0)
        self.assertEqual(self.sent[0]["title"], "brand-new-job: hello")
        self.assertFalse(store.read()[0]["policy_known"])

    def test_only_http_links_are_kept(self):
        self.notify("--job", "disk-check", "--headline", "a", "--click", "javascript:alert(1)")
        self.notify("--job", "disk-check", "--headline", "b", "--click", "https://example.org/x")
        self.assertEqual([p["click"] for p in self.sent], ["", "https://example.org/x"])

    def test_no_envelope_sends_the_detail_alone(self):
        self.notify("--job", "disk-check", "--headline", "h", "--detail", "just this", "--no-envelope")
        self.assertEqual(self.sent[0]["body"], "just this")

    def test_dry_run_records_and_pushes_nothing(self):
        rc, out, _ = self.notify("--job", "disk-check", "--headline", "h", "--dry-run")
        self.assertEqual((rc, self.sent, store.read()), (0, [], []))
        self.assertIn("tier=live", out)


class Dedup(NotifierCase):
    def test_the_same_fact_three_times_is_one_push_and_three_records(self):
        for _ in range(3):
            self.notify("--job", "nightly-backup", "--headline", "dump failed", "--severity", "fail")
        self.assertEqual(len(self.sent), 1)
        rows = store.read()
        self.assertEqual([r["suppressed"] for r in rows], [False, True, True])
        self.assertEqual(rows[1]["suppressed_by"], rows[0]["id"])
        self.assertEqual(len({r["id"] for r in rows}), 3, "a duplicate still gets its own id")

    def test_a_changing_number_needs_a_stable_key(self):
        for pct in (86, 87):
            self.notify("--job", "disk-check", "--headline", f"root volume at {pct}%")
        self.assertEqual(len(self.sent), 2, "different text, different content hash")
        for pct in (88, 89):
            self.notify("--job", "disk-check", "--headline", f"root volume at {pct}%",
                        "--dedup-key", "disk-root-85-89")
        self.assertEqual(len(self.sent), 3, "one standing condition, one push")

    def test_the_window_comes_from_the_policy(self):
        old = (datetime.now().astimezone() - timedelta(hours=10)).isoformat()
        for job in ("nightly-backup", "disk-check"):  # 6 hour window, 72 hour window
            store.append({"id": f"old-{job}", "ts": old, "job": job, "dedup_key": "k", "suppressed": False})
        self.notify("--job", "nightly-backup", "--headline", "h", "--severity", "fail", "--dedup-key", "k")
        self.assertEqual(len(self.sent), 1, "10 hours is outside a 6 hour window")
        self.sent.clear()
        store.rewrite([r for r in store.read() if r["job"] == "disk-check"])
        self.notify("--job", "disk-check", "--headline", "h", "--dedup-key", "k")
        self.assertEqual(self.sent, [], "10 hours is inside a 72 hour window")


class Failure(NotifierCase):
    def test_an_undelivered_live_push_is_recorded_as_such_and_exits_zero(self):
        self.deliver = False
        rc, out, err = self.notify("--job", "disk-check", "--headline", "full")
        self.assertEqual(rc, 0, "a failed push must not fail the calling job")
        self.assertIn("NOT DELIVERED", out)
        self.assertFalse(store.read()[0]["delivered"])

    def test_a_failed_store_write_exits_nonzero_and_prints_no_success_line(self):
        real = store.append
        store.append = lambda rec: False
        try:
            rc, out, err = self.notify("--job", "disk-check", "--headline", "full")
        finally:
            store.append = real
        self.assertEqual(rc, event.EXIT_STORE_FAILED)
        self.assertNotIn("tier=", out)
        self.assertIn("NOT recorded", err)

    def test_the_fallback_fires_once_per_key_per_day(self):
        calls = []
        real = transport.fallback
        transport.fallback = lambda title, body: calls.append((title, body)) or "issue-1"
        self.deliver = False
        try:
            self.notify("--job", "disk-check", "--headline", "full", "--detail", "from outside `x`",
                        "--dedup-key", "k1")
            store.rewrite([])  # as if the dedup window had passed
            self.notify("--job", "disk-check", "--headline", "full", "--dedup-key", "k1")
            self.notify("--job", "disk-check", "--headline", "other", "--dedup-key", "k2")
        finally:
            transport.fallback = real
        self.assertEqual(len(calls), 2, "k1 once, k2 once")
        self.assertIn("```\nfrom outside `x`\n```", calls[0][1], "outside text is fenced, not interpolated")
        ledger = json.loads(paths.fallback_ledger().read_text())
        self.assertEqual(set(ledger), {"k1", "k2"})


class RealTransport(NotifierCase):
    """One pass through a real subprocess, to pin the strict exit contract."""

    def setUp(self):
        super().setUp()
        transport.send = self._send
        self.script = os.path.join(self.tmp, "fake_transport.py")
        with open(self.script, "w") as f:
            f.write("import json, os, sys\n"
                    "json.dump({'argv': sys.argv[1:], 'strict': os.environ.get('NOTIFY_STRICT')},\n"
                    "          open(os.environ['FAKE_OUT'], 'w'))\n"
                    "sys.exit(int(os.environ.get('FAKE_RC', '0')))\n")
        os.environ["FAKE_OUT"] = os.path.join(self.tmp, "sent.json")
        os.environ["NOTIFY_TRANSPORT"] = f'"{sys.executable}" "{self.script}"'

    def test_exit_zero_under_strict_is_delivered(self):
        self.assertTrue(transport.send("t", "line one\nline two", "high", "warning", "https://example.org"))
        with open(os.environ["FAKE_OUT"]) as f:
            got = json.load(f)
        self.assertEqual(got["strict"], "1")
        self.assertEqual(got["argv"], ["t", "line one\nline two", "high", "warning", "https://example.org"])

    def test_a_nonzero_exit_is_not_delivered(self):
        os.environ["FAKE_RC"] = "3"
        self.assertFalse(transport.send("t", "b", "low", ""))

    def test_a_transport_that_cannot_start_is_not_delivered(self):
        os.environ["NOTIFY_TRANSPORT"] = os.path.join(self.tmp, "does-not-exist")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(transport.send("t", "b", "low", ""))


class Digest(NotifierCase):
    def test_nothing_held_sends_nothing(self):
        rc, out, _ = self.run_digest()
        self.assertEqual((rc, self.sent), (0, []))
        self.assertIn("sending nothing", out)

    def test_held_items_go_out_once_action_first(self):
        self.notify("--job", "daily-summary", "--headline", "41 runs, 0 failed")
        self.notify("--job", "token-expiry-check", "--headline", "1 token expires in 2 days")
        self.notify("--job", "renewal-reminder", "--headline", "silent, never included")
        rc, out, _ = self.run_digest()
        self.assertEqual(rc, 0)
        push = self.sent[-1]
        self.assertEqual(push["title"], "Morning digest: 1 needs you, 1 FYI")
        body = push["body"]
        self.assertLess(body.index("NEEDS YOU"), body.index("1 token expires"))
        self.assertLess(body.index("1 token expires"), body.index("FYI"))
        self.assertNotIn("silent, never included", body)
        self.sent.clear()
        self.run_digest()
        self.assertEqual(self.sent, [], "a digested item is never sent twice")

    def test_a_failed_digest_push_leaves_everything_held(self):
        self.notify("--job", "daily-summary", "--headline", "41 runs")
        self.deliver = False
        rc, _, err = self.run_digest()
        self.assertEqual(rc, 1)
        self.assertFalse(store.read()[0]["digested"])
        self.deliver = True
        self.assertEqual(self.run_digest()[0], 0)
        self.assertTrue(store.read()[0]["digested"])

    def test_a_live_push_that_did_not_land_is_retried_by_the_digest(self):
        self.deliver = False
        self.notify("--job", "disk-check", "--headline", "root volume at 96%", "--severity", "fail")
        self.deliver = True
        self.sent.clear()
        self.run_digest()
        self.assertIn("root volume at 96%", self.sent[0]["body"])
        self.assertEqual(self.sent[0]["priority"], "high")

    def test_repeats_collapse_so_the_title_counts_problems_not_mentions(self):
        now = datetime.now().astimezone()
        for i in range(7):
            store.append({"id": f"r{i}", "ts": (now - timedelta(hours=7 - i)).isoformat(),
                          "job": "usage-check", "tier": "digest", "action": True, "severity": "warn",
                          "headline": "collector quiet", "detail": f"age {i}", "dedup_key": "quiet-dns",
                          "facts": {"project": "platform"}})
        store.append({"id": "other", "ts": now.isoformat(), "job": "usage-check", "tier": "digest",
                      "action": True, "severity": "warn", "headline": "another", "dedup_key": "quiet-disk"})
        self.run_digest()
        self.assertEqual(self.sent[0]["title"], "Morning digest: 2 need you")
        self.assertIn("(x7 since", self.sent[0]["body"])
        self.assertIn("age 6", self.sent[0]["body"], "the newest row carries the current state")
        self.assertTrue(all(r["digested"] for r in store.read()), "folded rows are marked too")

    def test_items_older_than_the_lookback_are_left_alone(self):
        old = (datetime.now().astimezone() - timedelta(hours=digest.LOOKBACK_H + 1)).isoformat()
        store.append({"id": "old", "ts": old, "job": "j", "tier": "digest", "headline": "stale"})
        self.run_digest()
        self.assertEqual(self.sent, [])

    def test_shorten_never_cuts_a_word_in_half(self):
        text = "The first sentence is long enough to keep on its own line here. " + "word " * 40
        self.assertEqual(digest.shorten(text), "The first sentence is long enough to keep on its own line here.")
        cut = digest.shorten("alpha " * 40)
        self.assertTrue(cut.endswith("alpha…"))
        self.assertLessEqual(len(cut), 111)


class StoreFile(NotifierCase):
    def test_a_truncated_line_is_skipped(self):
        store.append({"id": "a"})
        with paths.store().open("a", encoding="utf-8") as f:
            f.write('{"id": "half\n')
        store.append({"id": "b"})
        self.assertEqual([r["id"] for r in store.read()], ["a", "b"])

    def test_mark_touches_only_the_named_records(self):
        for i in "abc":
            store.append({"id": i})
        self.assertTrue(store.mark({"a", "c"}, {"digested": True}))
        self.assertEqual([r.get("digested", False) for r in store.read()], [True, False, True])

    def test_the_store_is_trimmed_only_well_past_the_cap(self):
        real = store.MAX_LINES, store.TRIM_AT
        store.MAX_LINES, store.TRIM_AT = 10, 15
        try:
            for i in range(15):
                store.append({"id": i})
            self.assertEqual(len(store.read()), 15)
            store.append({"id": 15})
            self.assertEqual([r["id"] for r in store.read()], list(range(6, 16)))
        finally:
            store.MAX_LINES, store.TRIM_AT = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
