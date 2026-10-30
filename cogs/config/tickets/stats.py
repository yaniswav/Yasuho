"""Ticket statistics and their CSV export (lot V5a): pure computation, no Discord.

Everything is derived from ONE bounded fetch (storage.fetch_stats_rows: one
guild, ``opened_at >= cutoff``, newest first, at most :data:`ROW_CAP` rows,
served by ``tickets_guild_opened_idx``). A big server's 365 days is therefore
one index range scan of a few tens of thousands of small rows, at the command's
own cooldown - and the same rows feed the CSV, so it costs nothing extra.

* "No data" is ``None``, never 0: a median over zero claimed tickets is
  ``None`` (shown as a dash), and a ticket claimed before ``claimed_at`` existed
  contributes no sample rather than a bogus 0 seconds.
* The window counts tickets by when they were OPENED.
* Staff attribution (claims per member) is the server's record of its own team;
  PRIVACY.md states that the claiming staff member is stored.
* The CSV holds: ``ticket`` (the per-server number, already shown in the thread
  name), the three timestamps, ``claimed_by`` (the same staff id the stats show)
  and ``rating``. It has NO opener id (the member is not needed to analyse
  service quality), NO comment, NO transcript and NO ``closed_by``.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import csv
import io
import statistics

from cogs.community.serverstats.export import safe_cell

ROW_CAP = 20000
DEFAULT_DAYS = 30
MAX_DAYS = 365
TOP_STAFF = 5
FILENAME = "tickets.csv"
HEADER = ("ticket", "opened_at", "claimed_at", "closed_at", "claimed_by", "rating")


def _seconds(start, end):
    """Whole seconds ``start -> end``, or ``None`` when either is missing/negative."""
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds()
    return delta if delta >= 0 else None


def _central(samples):
    """``(median, mean)`` of the samples, or ``(None, None)`` when there are none."""
    if not samples:
        return None, None
    return statistics.median(samples), statistics.fmean(samples)


def summarize(rows):
    """Aggregate the fetched rows. Pure; every "no data" is ``None``."""
    to_claim = []
    to_close = []
    ratings = []
    claims = {}
    closed = 0
    for row in rows:
        if row["closed_at"] is not None:
            closed += 1
        sample = _seconds(row["opened_at"], row["claimed_at"])
        if sample is not None:
            to_claim.append(sample)
        sample = _seconds(row["opened_at"], row["closed_at"])
        if sample is not None:
            to_close.append(sample)
        if row["rating"] is not None:
            ratings.append(int(row["rating"]))
        if row["claimed_by"] is not None:
            claims[row["claimed_by"]] = claims.get(row["claimed_by"], 0) + 1
    claim_median, claim_mean = _central(to_claim)
    close_median, close_mean = _central(to_close)
    top = sorted(claims.items(), key=lambda item: (-item[1], item[0]))
    return {
        "opened": len(rows),
        "closed": closed,
        "claim_median": claim_median,
        "claim_mean": claim_mean,
        "close_median": close_median,
        "close_mean": close_mean,
        "rated": len(ratings),
        "rating_mean": statistics.fmean(ratings) if ratings else None,
        "distribution": {n: ratings.count(n) for n in range(1, 6)},
        "staff": top[:TOP_STAFF],
        "staff_total": len(top),
    }


def _iso(value):
    return "" if value is None else value.isoformat()


def build_csv(rows):
    """The CSV text for the fetched rows, oldest first. Pure and formula-safe."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for row in reversed(list(rows)):
        writer.writerow(
            [
                safe_cell(row["ticket_number"]),
                safe_cell(_iso(row["opened_at"])),
                safe_cell(_iso(row["claimed_at"])),
                safe_cell(_iso(row["closed_at"])),
                safe_cell(row["claimed_by"]),
                safe_cell(row["rating"]),
            ]
        )
    return out.getvalue()
