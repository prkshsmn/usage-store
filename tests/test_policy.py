"""Gate for the policy file. These pin the decisions that stopped the noise,
and the shape that keeps a later edit from quietly undoing them."""
import json
import os
import unittest

from _util import ROOT

from notifier import policy

with open(os.path.join(ROOT, "config", "policy.example.json"), encoding="utf-8") as f:
    DOC = json.load(f)
with open(os.path.join(ROOT, "config", "jobs.example.json"), encoding="utf-8") as f:
    REGISTRY = json.load(f)["jobs"]

# Senders reporting a standing condition nobody can clear at 03:00.
NEVER_LIVE = ("token-expiry-check", "queue-repair")
PAGE_NOW = ("disk-check",)


class ExamplePolicy(unittest.TestCase):
    def test_the_example_is_well_formed(self):
        self.assertEqual(policy.validate(DOC), [])

    def test_every_registered_job_has_an_explicit_entry(self):
        # An unlisted sender inherits live-on-failure. That default is how a
        # sender nobody thought about ends up paging.
        for job in REGISTRY:
            self.assertIn(job, DOC["jobs"], f"{job} has no policy entry")

    def test_chronic_senders_can_never_escalate_to_live(self):
        for job in NEVER_LIVE:
            pol, known = policy.job_policy(DOC, job)
            self.assertTrue(known)
            for severity in policy.SEVERITIES:
                self.assertNotEqual(policy.resolve_tier(pol, severity)[0], "live", f"{job} on {severity}")

    def test_the_page_now_senders_are_still_live(self):
        for job in PAGE_NOW:
            pol, _ = policy.job_policy(DOC, job)
            self.assertEqual(policy.resolve_tier(pol, "warn")[0], "live")


class Resolution(unittest.TestCase):
    def test_an_unlisted_sender_is_digest_until_it_fails(self):
        pol, known = policy.job_policy(DOC, "never-heard-of-it")
        self.assertFalse(known)
        self.assertEqual(policy.resolve_tier(pol, "info")[0], "digest")
        tier, why = policy.resolve_tier(pol, "fail")
        self.assertEqual(tier, "live")
        self.assertIn("escalated from digest", why)

    def test_an_empty_escalate_list_overrides_the_default_and_means_never(self):
        doc = {"defaults": {"escalate_on": ["fail"]}, "jobs": {"j": {"escalate_on": []}}}
        pol, _ = policy.job_policy(doc, "j")
        self.assertEqual(policy.resolve_tier(pol, "fail")[0], "digest")

    def test_an_explicit_override_wins_and_a_bad_one_is_ignored(self):
        pol, _ = policy.job_policy(DOC, "daily-summary")
        self.assertEqual(policy.resolve_tier(pol, "info", "live")[0], "live")
        self.assertEqual(policy.resolve_tier(pol, "info", "loudest")[0], "digest")

    def test_an_unknown_tier_in_the_file_falls_back_to_digest(self):
        self.assertEqual(policy.resolve_tier({"tier": "shout"}, "info")[0], "digest")

    def test_a_missing_policy_file_still_yields_working_defaults(self):
        pol, known = policy.job_policy(policy.load(__import__("pathlib").Path("no-such-file.json")), "j")
        self.assertFalse(known)
        self.assertEqual(pol["tier"], "digest")
        self.assertEqual(pol["escalate_on"], ["fail"])


class Validation(unittest.TestCase):
    def test_shape_problems_are_named(self):
        bad = {"defaults": {"tier": "loud"},
               "jobs": {"a": {"escalate_on": ["boom"]}, "b": {"action": "maybe"},
                        "c": {"tier": "silent", "action": "yes"}, "d": "nope"}}
        problems = "\n".join(policy.validate(bad))
        for needle in ("defaults: unknown tier", "jobs.a: escalate_on", "jobs.b: action",
                       "jobs.c: a silent sender", "jobs.d: not an object"):
            self.assertIn(needle, problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
