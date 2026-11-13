"""Command line for the usage store.

    usage-store <collector>            run one collector
    usage-store check                  exit 1 if a collector has gone quiet
    usage-store status                 what each collector last did, rows by kind
    usage-store init                   create the store
    usage-store github-load <src>      load traffic JSON fetched on another machine
    usage-store export-stats --out F   publish job-run stats, or refuse if stale
"""
import argparse
import datetime as dt
import json
import sys

from . import export
from .collectors import COLLECTORS, load_github
from .config import Config
from .store import check, connect, record_gaps, run_collector, status


def main(argv=None, cfg=None):
    ap = argparse.ArgumentParser(prog="usage-store", description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=sorted(COLLECTORS) + ["check", "status", "init", "github-load", "export-stats"])
    ap.add_argument("path", nargs="?", help="github-load: a directory, a .tgz, or - for a .tgz on stdin")
    ap.add_argument("--out", help="export-stats: file to write (default: print to stdout)")
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--max-age-days", type=float, default=3)
    args = ap.parse_args(argv)
    cfg = cfg or Config.from_env()

    if args.what == "export-stats":
        try:
            stats = export.job_stats(cfg.db, dt.datetime.now(dt.timezone.utc),
                                     args.window_days, args.max_age_days)
        except export.Refuse as e:
            print(f"usage-store export-stats: REFUSED: {e}", file=sys.stderr)
            return export.EXIT_REFUSED
        if args.out:
            export.write_stats(stats, args.out)
            print(f"usage-store export-stats: wrote {args.out} ({stats['runs']} runs, "
                  f"{stats['jobs_ran']} jobs)")
        else:
            print(json.dumps(stats, indent=2, sort_keys=True))
        return 0

    conn = connect(cfg.db)
    try:
        if args.what == "init":
            print(f"store ready: {cfg.db}")
            return 0
        if args.what == "status":
            status(conn, list(COLLECTORS), cfg.db)
            return 0
        if args.what == "check":
            problems = check(conn, cfg.expect)
            if not problems:
                print("usage-store check: every collector is keeping up")
                return 0
            record_gaps(conn, problems)
            for p in problems:
                print("PROBLEM", p)
            return 1
        if args.what == "github-load":
            if not args.path:
                ap.error("github-load needs a path (a directory, a .tgz, or -)")
            return run_collector("github-load", lambda c, g: load_github(c, g, args.path), conn, cfg)
        return run_collector(args.what, COLLECTORS[args.what], conn, cfg)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
