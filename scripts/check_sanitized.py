#!/usr/bin/env python3
"""Fail if anything private-looking is in the tree (or, with --history, in any
commit's metadata).

This repository was extracted from a private one that sits next to real
infrastructure. The extraction is only as good as the check that nothing came
along with it, so the check is code, it runs in CI, and it fails closed.

TWO LAYERS

1. Generic patterns, below. They know nothing about any particular site:
   private and carrier-grade address ranges, email addresses, home directory
   paths, credential shapes, long hex identifiers, internal-looking hostnames,
   and any URL whose host is not on a short allowlist. That last rule is the
   strong one: a domain does not have to be known to be caught, it only has to
   be absent from the allowlist.

2. A site denylist: hostnames, account names and project names from the
   private estate. It is read from a file that is NOT in this repository
   (--denylist, or .sanitize-denylist, which is gitignored), because a
   committed list of the things that must not be published would publish
   them. CI therefore runs layer 1 only, and layer 2 runs before every push
   from the machine that has the list.

    python3 scripts/check_sanitized.py                # the working tree
    python3 scripts/check_sanitized.py --history      # plus commit metadata
    python3 scripts/check_sanitized.py --require-denylist   # fail if layer 2 is missing

Exit 0 means no finding. Exit 1 lists every finding as path:line: rule: text.
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCAL_DENYLIST = ".sanitize-denylist"

# Hosts a URL may point at. Anything else is a finding.
URL_HOSTS = {
    "github.com", "docs.github.com", "img.shields.io",
    "example.com", "example.org", "example.net",
    "127.0.0.1", "localhost",
    "docs.ntfy.sh", "ntfy.sh", "www.sqlite.org", "docs.python.org", "opensource.org",
}
EMAIL_OK = re.compile(r"@(users\.noreply\.github\.com|example\.(com|org|net)|noreply\.anthropic\.com|anthropic\.com)$")

# name, pattern, optional predicate on the match that returns True when it is fine
RULES = [
    ("private-ipv4", re.compile(
        r"(?<![\d.])(10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})(?![\d.])"), None),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+"),
     lambda m: bool(EMAIL_OK.search(m.group(0)))),
    ("home-path", re.compile(r"(/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+|[A-Za-z]:\\+Users\\+[A-Za-z0-9_.-]+)"), None),
    ("url-host", re.compile(r"https?://([A-Za-z0-9.-]+)"),
     lambda m: m.group(1).lower().rstrip(".") in URL_HOSTS),
    ("internal-hostname", re.compile(r"\b[A-Za-z0-9-]+\.(local|lan|internal|home\.arpa|ts\.net)\b", re.I), None),
    ("credential-shape", re.compile(
        r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|tk_[a-z0-9]{20,}"
        r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|AGE-SECRET-KEY-1[A-Z0-9]{20,})"), None),
    ("bearer-literal", re.compile(r"Bearer\s+[A-Za-z0-9_.=-]{24,}"), None),
    ("long-hex-id", re.compile(r"\b[0-9a-f]{32,}\b"), None),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), None),
    # House style, enforced here because it is cheap: no em or en dashes.
    ("dash-style", re.compile("[%s%s]" % (chr(0x2014), chr(0x2013))), None),
]


def load_denylist(path):
    """One term per line. Blank lines and # comments are skipped. A plain term
    matches case-insensitively as a substring; `re:` starts a regex."""
    rules = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            term = raw.strip()
            if not term or term.startswith("#"):
                continue
            if term.startswith("re:"):
                rules.append((term, re.compile(term[3:], re.I)))
            else:
                rules.append((term, re.compile(re.escape(term), re.I)))
    return rules


def scan_text(text, denylist=()):
    """Yield (line number, rule, matched text) for every finding in text."""
    for n, line in enumerate(text.splitlines(), 1):
        for name, pattern, ok in RULES:
            for m in pattern.finditer(line):
                if ok and ok(m):
                    continue
                yield n, name, m.group(0)
        for term, pattern in denylist:
            for m in pattern.finditer(line):
                # The term itself is not echoed: findings are printed in CI logs.
                yield n, "site-denylist", f"<term #{[t for t, _ in denylist].index(term) + 1}>"


def tracked_files(root):
    """Everything git would publish: tracked files plus untracked files that
    are not ignored. Falls back to walking the tree when this is not a repo."""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
                             capture_output=True, check=True).stdout.decode("utf-8", "replace")
        return sorted(p for p in out.split("\0") if p)
    except (OSError, subprocess.CalledProcessError):
        found = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            found += [os.path.relpath(os.path.join(base, f), root).replace(os.sep, "/") for f in files]
        return sorted(found)


def scan_tree(root, denylist=()):
    findings = []
    for rel in tracked_files(root):
        if os.path.basename(rel) == LOCAL_DENYLIST:
            findings.append((rel, 0, "denylist-published", "the site denylist must never be in the tree"))
            continue
        for n, name, text in scan_text(rel, denylist):
            findings.append((rel, 0, name + " (in file name)", text))
        path = os.path.join(root, rel)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            findings.append((rel, 0, "binary-file", "binary files cannot be checked, so none are allowed"))
            continue
        for n, name, text in scan_text(data.decode("utf-8", "replace"), denylist):
            findings.append((rel, n, name, text))
    return findings


def scan_history(root, denylist=()):
    """Author, committer and message of every commit on every ref. A clean
    tree with a personal address in `git log` is still a leak."""
    out = subprocess.run(
        ["git", "-C", root, "log", "--all", "--format=%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e"],
        capture_output=True, check=True).stdout.decode("utf-8", "replace")
    findings = []
    for entry in out.split("\x1e"):
        parts = entry.strip().split("\x1f")
        if len(parts) < 4:
            continue
        sha = parts[0][:10]
        for n, name, text in scan_text("\n".join(parts[1:]), denylist):
            if name == "dash-style":
                continue
            findings.append((f"commit {sha}", n, name, text))
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--denylist", default=None, help=f"site term file (default: {LOCAL_DENYLIST} if present)")
    ap.add_argument("--require-denylist", action="store_true",
                    help="fail when no site denylist is available (use before a push)")
    ap.add_argument("--history", action="store_true", help="also check commit metadata on every ref")
    args = ap.parse_args(argv)

    deny_path = args.denylist or os.path.join(args.root, LOCAL_DENYLIST)
    denylist = []
    if os.path.exists(deny_path):
        denylist = load_denylist(deny_path)
    elif args.denylist or args.require_denylist:
        print(f"check_sanitized: no site denylist at {deny_path}", file=sys.stderr)
        return 2

    findings = scan_tree(args.root, denylist)
    if args.history:
        findings += scan_history(args.root, denylist)
    for where, n, name, text in findings:
        print(f"{where}:{n}: {name}: {text}")
    layers = f"{len(RULES)} generic rules" + (f", {len(denylist)} site terms" if denylist else ", no site denylist")
    scope = f"{len(tracked_files(args.root))} files" + (" and commit history" if args.history else "")
    if findings:
        print(f"check_sanitized: FAIL, {len(findings)} finding(s) across {scope} ({layers})")
        return 1
    print(f"check_sanitized: clean, {scope} ({layers})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
