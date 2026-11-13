# usage-store

[![ci](https://github.com/prkshsmn/usage-store/actions/workflows/ci.yml/badge.svg)](https://github.com/prkshsmn/usage-store/actions/workflows/ci.yml)

One SQLite file fed by cron collectors, a daily check that fails when a collector runs but writes nothing, and a notification envelope that sorts every alert into live, digest or silent. Standard library Python and two shell scripts. No daemon, no dependencies.

## The problem

I run about 30 containers and about 45 scheduled jobs on one small Linux host (8 GB of RAM, no swap). By October 2026 it had two problems that turned out to be the same problem.

**It paged me 10.7 times a day.** The push server's own counters showed 322 pushes in the 30 days to 2026-10-28. About two thirds came from five chronic conditions that nobody had fixed: a disk that was honestly full (94 pushes), an expired third-party token (31), a partially failing sync (31), a standing list of stuck queue items (16), and a daily digest whose title said "1 needs you" every single morning. A channel that fires ten times a day is a channel you stop reading.

**I could not say what was safe to remove.** Every proposed cut rested on "I do not think I use this". The numbers that would settle it either expired quietly (the DNS log keeps 91 days, GitHub traffic keeps 14) or had never been recorded at all (what a container costs in memory over a week, since `docker stats` only shows this instant).

Both are missing measurement. One is measurement of what the system does, the other of what the system tells me.

## The result

| | Before | After |
|---|---|---|
| Live pushes | 322 in 30 days, about 10.7 a day | 3 in the 48 hours measured on 2026-11-11, so 1 to 2 a day |
| Largest sender (disk alert) | 94 pushes in 30 days | 0, because the disk went from 90% to 69% used |
| Longest alert streak (expired token) | 38 consecutive days, 31 live pushes in 30 days | 0, root cause removed |
| Usage history | none | 111,897 rows from 10 collectors by day four, including an import of earlier job-run history |
| A collector that exits 0 and writes nothing | invisible | fails the 06:00 check by name |

Two honest notes on that table. The drop in pushes came from three things together: the policy file (which senders may page), fixing the chronic conditions at their root, and cutting or demoting senders nobody acted on. The envelope alone would not have done it. And the removal decisions the store exists to inform were not made yet when this was written: the rules below require 30 days of data, and the collectors went live on 2026-11-03.

## Design decisions

### 1. Measure before removing

Every collector exists to answer one named keep-or-remove question, and the question is the first line of its docstring in [`usage_store/collectors.py`](usage_store/collectors.py). "Which containers earn their memory?" is a sampler. "What is driving disk growth?" is a daily size series. A collector with no question is switched off once its decision is made.

Collectors store facts, not conclusions: a job ran for 14 seconds, a container used 812 MB. Ranking and thresholds happen at read time, so the logic can change without collecting again.

The store is one SQLite file because the host had about 1.3 GB of free memory. A metrics stack would have been the largest thing on the machine it was measuring. Measured cost of the busiest collector: one `docker stats` pass took about 2.2 seconds, so it runs every ten minutes, not five.

### 2. Rules are written before the data arrives

A threshold chosen after looking at the numbers is a rationalisation. These were committed first, with the instruction "change them now or not at all":

| Subject | Metric | Window | Rule |
|---|---|---|---|
| Container | user-initiated activity, average memory | 30 days | no activity and over 100 MB: stop it (reversible) |
| Cron job (not a guard or a backup) | consumers of its output | 30 days | none: remove |
| Notification sender | taps and actions taken | 30 days | under 10% acted on: demote to digest. Zero: remove |
| Feature or page | views | 60 and 90 days | zero for 60: hide. Zero for 90: remove |
| Media | plays since import | 180 days | unplayed: listed for approval, never deleted automatically |

The notification rule has a stated caveat: a push can be read on a lock screen without a tap, so the rule may demote a page-now alert and may never delete one.

### 3. Verify by effect, not by exit code

The failure this estate kept rediscovering was never "a job failed". It was a job that succeeded and produced nothing: cron fired, the process exited 0, the dashboard was green, and the output had been empty for weeks. So nothing here treats exit 0 as evidence.

- Every collector run is written to a `collector_runs` ledger with the rows it added. `usage-store check` reports two different failures: "no clean run in 30h" (it crashed or never fired) and "ran 3x in 30h but added 0 rows" (it succeeded at nothing). See [`store.check`](usage_store/store.py).
- The backup collector opens the copy and compares its row count with the source. A backup is the file you can read back.
- The push transport derives its exit code from the HTTP status the server returned. The notifier records `delivered` from that, and the morning digest retries any live alert that did not land. Before this, the loudest class of alert was the only one with no retry.
- The digest marks items as sent only after the push is confirmed. Marking first would lose them for good on a morning the push server was down.
- If the notification record itself cannot be written, the notifier exits 2 and prints no success line. This is its one deliberate exception to "never fail the calling job", because a record that was not written does not exist anywhere.

### 4. A stale source is refused, not published

An export job published job-run statistics from a summary file that another process rebuilt. That process was switched off. For eight weeks the export kept publishing the same run count and success rate, under a "last verified" date that moved forward every day it ran. The arithmetic was right. The input had stopped changing and nothing asked how old it was.

[`usage_store/export.py`](usage_store/export.py) now reads the store directly and checks the age of the newest row first. Past the limit it writes nothing, leaves the previous output untouched, and exits 3. A stale source may make the job skip a day. It may not be published as current.

### 5. Every notification answers the same five questions

Senders used to compose their own title and body. One evening that produced the same sentence three times in 13 minutes, with a title that named neither the job nor whether anyone had to act. Now the caller supplies one field, the thing only it knows, and the envelope supplies the rest from two small files:

```
[!] host: root volume at 91%

Runs: every 6 hours
Does: Compares free space on the root volume and the backup volume against fixed bands.

58 of 63 GB used

Action needed: YES
Suggested: Free space on the volume named in the body.
```

The policy file sorts each sender into a tier: `live` pushes now, `digest` is held for one morning push, `silent` is recorded only. An empty digest sends nothing.

The dangerous part is the default. An unlisted sender inherits "escalate to live on failure", which is how two conditions that nobody could clear at 03:00 produced 49 live pushes in 30 days. The fix is an explicit entry with an empty `escalate_on` list, and [`tests/test_policy.py`](tests/test_policy.py) pins that an empty list means never.

Deduplication is per sender and per key, with the window set in the policy. Replaying the 184 live pushes from the audit month against different windows: 24 hours keeps 97, 72 hours keeps 51, 168 hours keeps 32. A duplicate is still recorded with `suppressed: true`, because "this fired eleven times" is a fact about a job. The same audit showed that deduplication was never going to be the whole fix: two senders bypassed the envelope entirely, and no window collapses a condition that should have been repaired.

### 6. The wrapper cannot hurt the job it wraps

[`bin/run-job.sh`](bin/run-job.sh) sits in front of every scheduled job, so its contract is short and tested: it never prevents the command from running, it passes the exit code through exactly, it does not touch stdout or stderr, and when its own bookkeeping fails it says so on stderr and carries on. It also prepends to `PATH`, because cron's `PATH` is `/usr/bin:/bin` and a script that works in a terminal can fail on a schedule for that reason alone.

### 7. Privacy limits are in the collectors, not in a policy document

DNS is stored as per-client, per-domain, per-day counts: the top 300 domains per client per day and one remainder row. No query strings, no page text, nothing finer than a day. Plays are stored by account id. The notification collector keeps sender, tier and outcome, and a test asserts that headline and detail text never reach the store. Collector errors are stored as an exception type and a bounded message, because error text from an upstream service is the classic place for a credential to end up on disk.

## Run it

Requires Python 3.9 or newer. Nothing to install for a first look.

```bash
git clone https://github.com/prkshsmn/usage-store.git
```

```bash
cd usage-store
```

```bash
python3 -m unittest discover -s tests
```

A two minute tour with no docker and no push server. The first block wraps two commands and imports their run events:

```bash
export USAGE_DB=/tmp/usage-demo/usage.db USAGE_EVENTS_DIR=/tmp/usage-demo/events RUN_JOB_EVENTS_DIR=/tmp/usage-demo/events
bin/run-job.sh demo-ok -- true
bin/run-job.sh demo-fail -- false
python3 -m usage_store jobs
python3 -m usage_store status
python3 -m usage_store export-stats
```

`check` then fails, correctly, because most collectors have never run on this machine:

```bash
python3 -m usage_store check
```

The notifier, rendered without sending anything:

```bash
NOTIFY_POLICY=config/policy.example.json NOTIFY_JOBS=config/jobs.example.json python3 -m notifier.event --job disk-check --headline "root volume at 91%" --detail "58 of 63 GB used" --dry-run
```

### Commands

| Command | What it does |
|---|---|
| `usage-store <collector>` | run one of `containers`, `disk`, `dns`, `media_plays`, `jobs`, `notifications`, `github`, `backup` |
| `usage-store check` | exit 1 and name each collector that stopped running or ran without adding rows |
| `usage-store status` | last clean run per collector, rows by kind, file size |
| `usage-store export-stats [--out FILE]` | job-run statistics, or exit 3 if the store is stale |
| `usage-store github-load <dir or .tgz>` | load traffic JSON fetched on a machine whose token can read it |
| `notify-event --headline ... [--severity fail]` | record one notification and push it if the policy says live |
| `notify-digest` | send everything held as one push, or nothing if nothing is held |

`pip install .` provides those names. Without installing, use `python3 -m usage_store`, `python3 -m notifier.event` and `python3 -m notifier.digest`.

### Configuration

Everything is an environment variable with a default under the invoking user's home directory.

| Variable | Default | Meaning |
|---|---|---|
| `USAGE_DB` | `~/telemetry/usage.db` | the store (created mode 600, directory mode 700) |
| `USAGE_EVENTS_DIR` | `~/.local/state/run-job/events` | where `run-job.sh` writes, read by the `jobs` collector |
| `USAGE_NOTIFY_STORE` | `~/.local/state/notify/notifications.jsonl` | read by the `notifications` collector |
| `USAGE_DNS_DB` | unset | a Pi-hole FTL database |
| `USAGE_PLAYBACK_DB` | unset | a Jellyfin Playback Reporting database |
| `USAGE_BACKUP_DIR` | unset | snapshot directory on another volume (`USAGE_BACKUP_KEEP`, default 14) |
| `USAGE_DISK_MOUNTS`, `USAGE_DISK_DIRS` | `/`, none | mounts and directory globs to size, separated by `:` |
| `USAGE_DOCKER_BIN`, `USAGE_GH_BIN` | `docker`, `gh` | use absolute paths under cron |
| `USAGE_GH_OWNER` | unset | account whose repository traffic to collect |
| `USAGE_EXPECT` | built in | JSON file of expectations, see [`config/expect.example.json`](config/expect.example.json) |
| `NOTIFY_HOME` | `~/.local/state/notify` | notification records and lock |
| `NOTIFY_POLICY`, `NOTIFY_JOBS` | `$NOTIFY_HOME/policy.json`, `jobs.json` | see [`config/`](config) |
| `NOTIFY_TRANSPORT` | `bin/notify.sh` | any command taking title, body, priority, tags, click URL |
| `NOTIFY_FALLBACK` | unset | command for a live alert whose push failed, on a channel that does not share the push server's failure domain |
| `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` | unset | read by `bin/notify.sh`. Supply from a secret store at run time |

A cron schedule is in [`config/crontab.example`](config/crontab.example).

## What this repository is

This is an extraction from a private repository that sits next to real infrastructure, made to stand alone. The design, the incidents in the comments and the numbers above are from the running system. Some things differ from what runs there, and it would be misleading not to say so:

- Collectors that only make sense for my own applications are left out. Eight of the eleven are here.
- Paths, job names and the example policy are generic.
- Two behaviours were tightened during the extraction, each with a test. The transport reports delivery through an exit code derived from the HTTP status (the original printed a line on stderr that the caller matched). And an imported log line is identified by its byte offset plus a hash of its bytes, after a test written for this repository showed that offset alone drops a new line that lands on an old offset after a truncation.

[`scripts/check_sanitized.py`](scripts/check_sanitized.py) is part of the work, not an afterthought. It fails the build on private address ranges, email addresses, home directory paths, credential shapes, long hex identifiers, internal hostnames and any URL whose host is not on a short allowlist, and it can check commit metadata as well as files. Its own tests plant a sample for every rule, because a scanner that matches nothing passes every tree.

## Layout

```
usage_store/   store.py (schema, ledger, check)  collectors.py  export.py  cli.py  config.py
notifier/      event.py (envelope)  policy.py (tiers)  digest.py  store.py (locked JSONL)  transport.py
bin/           run-job.sh (cron wrapper)  notify.sh (ntfy transport)
config/        example policy, job registry, expectations, crontab
scripts/       check_sanitized.py
tests/         standard library unittest, loopback only
```

## License

MIT. See [LICENSE](LICENSE).
