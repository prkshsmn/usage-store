"""Roll every held notification into one morning push.

THE POINT

The tiering in event.py only helps if the held items actually arrive
somewhere. A digest tier that nothing drains is a silent drop with extra
steps, and a system that has quietly stopped telling you things is worse than
a noisy one, because you go on trusting it.

So this runs once a morning, takes everything recorded since the last digest
that was tiered `digest` and not suppressed, and sends exactly one
notification. Items needing action are listed first and counted in the title,
because the title is the only part read on a lock screen.

An empty digest sends NOTHING. A daily "nothing to report" push is the exact
habit-forming noise this exists to remove.

It also picks up any live notification whose push did not land, which is the
one retry in the system. See pending().
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

from . import store, transport

# Wider than the 24 hours between runs on purpose: a digest that was itself
# missed (the host was down, cron did not fire) must not swallow that day's
# items. Anything already marked digested is skipped, so the overlap is free.
LOOKBACK_H = 72
MAX_LINES = 18
RANK = {"fail": 0, "warn": 1, "info": 2, "ok": 3}


def warn(msg: str) -> None:
    print(f"notify-digest: {msg}", file=sys.stderr)


def pending(rows: list[dict], now: datetime) -> list[dict]:
    """Everything held for this digest.

    Two sources. The obvious one is tier=digest. The other is a tier=live
    notification whose push did not land: it is recorded with delivered=false,
    and before this nothing ever looked at that field again, so the loudest
    class of notification was also the only one with no retry.
    """
    cutoff = now - timedelta(hours=LOOKBACK_H)
    out = []
    for row in rows:
        held = row.get("tier") == "digest"
        missed = row.get("tier") == "live" and not row.get("delivered")
        if not (held or missed) or row.get("suppressed") or row.get("digested"):
            continue
        try:
            ts = datetime.fromisoformat(row["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            out.append(row)
    return out


def collapse(items: list[dict]) -> list[dict]:
    """One line per distinct thing, not one line per time it was mentioned.

    A check that re-alerts while a job stays broken put seven identical lines
    into four consecutive digests, each counted separately in the "N need you"
    title, so the count read as five problems when there were two. A number
    that grows with the age of a problem instead of its size is worse than no
    number: the mornings that need action then look like the ones that do not.

    Keeps the newest row of each group, since that one carries the current
    state, and records how many were folded into it. Rows with no dedup_key
    are never grouped: an absent key means the sender made no claim about
    sameness, and inventing one would merge things that are not the same.
    """
    out: list[dict] = []
    by_key: dict[str, dict] = {}
    for row in sorted(items, key=lambda r: r.get("ts", "")):
        key = row.get("dedup_key")
        if not key:
            out.append(row)
            continue
        prior = by_key.get(key)
        if prior is None:
            row = dict(row, repeats=1, first_ts=row.get("ts", ""))
            by_key[key] = row
            out.append(row)
        else:
            repeats, first_ts = prior["repeats"] + 1, prior["first_ts"]
            prior.clear()
            prior.update(row)
            prior["repeats"], prior["first_ts"] = repeats, first_ts
    return out


def shorten(text: str, limit: int = 110) -> str:
    """Fit text in limit characters without cutting a word in half. Prefers the
    last sentence end inside the limit when that keeps at least half the text."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut >= limit * 0.5:
        return head[: cut + 1]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip(" ,;:-") + "…"


def line_for(row: dict) -> str:
    project = (row.get("facts", {}).get("project") or row.get("job", "")).strip()
    headline = " ".join(str(row.get("headline", "")).split())
    detail = shorten(row.get("detail") or "")
    repeats = row.get("repeats", 1)
    since = str(row.get("first_ts", ""))[5:16].replace("T", " ")
    again = f" (x{repeats} since {since})" if repeats > 1 else ""
    head = f"• {project}: {headline}{again}"
    return f"{head}\n  {detail}" if detail and detail != headline else head


def sort_key(row: dict):
    # Action needed first, then by severity, then oldest first. The order is
    # the message: whatever is at the top is what to do.
    return (not row.get("action"), RANK.get(row.get("severity", "info"), 2), row.get("ts", ""))


def render_digest(lines: list[dict], max_lines: int = MAX_LINES) -> tuple[str, str]:
    """(title, body). Plain text: a push banner does not render Markdown."""
    need = [r for r in lines if r.get("action")]
    fyi = [r for r in lines if not r.get("action")]
    shown_need = need[:max_lines]
    shown_fyi = fyi[: max(0, max_lines - len(shown_need))]
    hidden = len(lines) - len(shown_need) - len(shown_fyi)

    parts: list[str] = []
    if shown_need:
        parts += ["NEEDS YOU", *[line_for(r) for r in shown_need], ""]
    if shown_fyi:
        if shown_need:
            parts.append("FYI")
        parts += [line_for(r) for r in shown_fyi]
    if hidden:
        parts += ["", f"+{hidden} more not shown"]
    body = "\n".join(parts).strip("\n")

    if need:
        title = f"Morning digest: {len(need)} {'needs' if len(need) == 1 else 'need'} you"
        title += f", {len(fyi)} FYI" if fyi else ""
    else:
        title = f"Morning digest: {len(lines)} {'item' if len(lines) == 1 else 'items'}, none need you"
    return title, body


def main(argv=None) -> int:
    now = datetime.now().astimezone()
    rows = store.read()
    items = pending(rows, now)
    if not items:
        print("notify-digest: nothing held, sending nothing")
        return 0

    # Collapse before counting, so the title counts problems and not mentions.
    # `items` stays the full list: every row in it is marked digested at the
    # end, whether or not it earned a line of its own. Otherwise the folded
    # rows would be held forever and folded again every morning.
    lines = sorted(collapse(items), key=sort_key)
    need = [r for r in lines if r.get("action")]
    title, body = render_digest(lines)

    delivered = transport.send(title, body, "high" if need else "low",
                               "warning" if need else "newspaper")
    if not delivered:
        # Marking first, or marking on an unverified send, would lose these
        # items for good: every later digest skips what is marked.
        warn(f"the digest push did not land, leaving all {len(items)} items held for the next run")
        return 1

    ids = {r["id"] for r in items if "id" in r}
    if not store.mark(ids, {"digested": True, "digested_at": now.isoformat()}):
        # The push landed, so nothing was lost, but the bookkeeping was: these
        # items still read as held and the next digest will repeat them.
        warn(f"sent {len(items)} items but could not mark them digested; they will be sent again")
        return 1

    print(f"notify-digest: sent {len(items)} held item(s) as {len(lines)} line(s), "
          f"{len(need)} needing action")
    return 0


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
