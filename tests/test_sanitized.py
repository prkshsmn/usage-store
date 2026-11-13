"""The sanitising check must itself be checked: a scanner that matches nothing
passes every tree. Each rule is proven to fire on a planted sample.

The samples are assembled at run time from fragments, so this file does not
contain anything the scanner would flag."""
import importlib.util
import os
import shutil
import tempfile
import unittest

from _util import ROOT

spec = importlib.util.spec_from_file_location(
    "check_sanitized", os.path.join(ROOT, "scripts", "check_sanitized.py"))
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

J = "".join
PLANTED = {
    "private-ipv4": [J(["192", ".168", ".7.20"]), J(["10", ".0.0.5"]), J(["172", ".20", ".1.9"]),
                     J(["100", ".101", ".3.4"])],
    "email": [J(["someone", "@", "mailhost", ".io"])],
    "home-path": [J(["/ho", "me/", "operator/data"]), J(["/Us", "ers/", "operator"]),
                  J(["C:", "\\", "Users", "\\", "operator"])],
    "url-host": [J(["https", "://", "dashboard.", "my-private-site", ".net/x"])],
    "internal-hostname": [J(["nas-box", ".lo", "cal"]), J(["node1.tail1234", ".ts", ".net"])],
    "credential-shape": [J(["gh", "p_", "a" * 36]), J(["AK", "IA", "A" * 16]),
                         J(["-----BEGIN OPENSSH ", "PRIVATE KEY-----"]), J(["tk", "_", "a1" * 14])],
    "bearer-literal": [J(["Bear", "er ", "abcDEF123456", "abcDEF123456"])],
    "long-hex-id": [J(["0123456789abcdef"] * 2)],
    "uuid": [J(["0a1b2c3d", "-1111-4222-8333-", "444455556666"])],
    "dash-style": [chr(0x2014), chr(0x2013)],
}
ALLOWED = [
    "curl http://127.0.0.1:8080/health",
    "see https://github.com/someone/repo and https://example.org/page",
    J(["12345+someone", "@", "users.noreply.github.com"]),
    'Authorization: Bearer ${TOKEN}',
    "version 3.11.2, section 10.2 of the doc, ratio 0.75",
    "a 16 character hex prefix such as 0123456789abcdef is not an identifier",
    "a hyphenated range, 2022-2024, is fine",
]


class Rules(unittest.TestCase):
    def test_every_rule_fires_on_its_planted_sample(self):
        self.assertEqual(set(PLANTED), {name for name, _, _ in cs.RULES}, "a rule with no sample is untested")
        for rule, samples in PLANTED.items():
            for sample in samples:
                hits = [name for _, name, _ in cs.scan_text(f"prefix {sample} suffix")]
                self.assertIn(rule, hits, f"{rule} missed its sample")

    def test_ordinary_text_is_not_flagged(self):
        for line in ALLOWED:
            self.assertEqual(list(cs.scan_text(line)), [], line)

    def test_findings_carry_the_line_number(self):
        text = "clean\nclean\n" + PLANTED["private-ipv4"][0]
        self.assertEqual([n for n, _, _ in cs.scan_text(text)], [3])


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sanitize-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_a_clean_tree_passes_and_a_planted_file_fails(self):
        self.write("a/ok.txt", "nothing to see\n")
        self.assertEqual(cs.main(["--root", self.tmp]), 0)
        self.write("a/deep/bad.sh", "ssh admin@" + PLANTED["private-ipv4"][0] + "\n")
        self.assertEqual(cs.main(["--root", self.tmp]), 1)

    def test_site_terms_match_case_insensitively_and_are_not_echoed(self):
        deny = self.write("../deny-" + os.path.basename(self.tmp), "# comment\nSecretHost\nre:acct-\\d+\n")
        try:
            rules = cs.load_denylist(deny)
            hits = list(cs.scan_text("ping SECRETHOST now\nuse acct-4411\nfine", rules))
            self.assertEqual([(n, name) for n, name, _ in hits], [(1, "site-denylist"), (2, "site-denylist")])
            self.assertTrue(all("secrethost" not in text.lower() for _, _, text in hits))
        finally:
            os.remove(deny)

    def test_a_missing_denylist_fails_when_it_is_required(self):
        self.write("ok.txt", "fine\n")
        self.assertEqual(cs.main(["--root", self.tmp, "--require-denylist"]), 2)

    def test_the_denylist_file_itself_may_never_be_published(self):
        self.write(cs.LOCAL_DENYLIST, "term\n")
        found = cs.scan_tree(self.tmp, [])
        self.assertEqual([f[2] for f in found], ["denylist-published"])

    def test_binary_files_are_refused(self):
        with open(os.path.join(self.tmp, "blob.bin"), "wb") as f:
            f.write(b"\x00\x01\x02")
        self.assertEqual([f[2] for f in cs.scan_tree(self.tmp, [])], ["binary-file"])


class ThisRepository(unittest.TestCase):
    def test_the_tree_is_clean_under_the_generic_rules(self):
        findings = cs.scan_tree(ROOT, [])
        self.assertEqual(findings, [], "\n".join(f"{w}:{n}: {r}" for w, n, r, _ in findings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
