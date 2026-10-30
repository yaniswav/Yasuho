"""Purpose: the CSV EXPORT (Yasuho+) of the daily aggregates a server already
sees in /serverstats: one row per UTC day, nothing else.

Columns: day, messages, joins, leaves, net, member_count_start. Per-guild totals
only - no channel, no user, no content - so the file holds exactly what the
card and the weekly digest already show, in a spreadsheet-friendly shape.

* A day nobody observed (no ``server_stats_days`` row) keeps its row with EMPTY
  cells: a hole stays a hole, never a 0. A NULL member_count snapshot is empty
  too.
* SIZE BOUND: at most :data:`MAX_EXPORT_DAYS` rows (the Yasuho+ retention
  ceiling) of six short cells, i.e. well under 20 KB; building it is a few
  hundred microseconds, so it runs inline rather than in an executor.
* FORMULA INJECTION: :func:`safe_cell` prefixes any TEXT cell starting with
  = + - @ (or a tab / CR) with an apostrophe. Today every cell is a number or an
  ISO date, so nothing needs it; it is the single choke point any future text
  column (a channel name is user data) MUST go through, and it is test-pinned.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import csv
import io

from . import rollups

MAX_EXPORT_DAYS = rollups.PREMIUM_MAX_WINDOW_DAYS
DEFAULT_EXPORT_DAYS = 30
FILENAME = "serverstats.csv"
# member_count_start: the daily snapshot is taken on the first flush of the
# UTC day, so it is the count at the START of that day.
HEADER = ("day", "messages", "joins", "leaves", "net", "member_count_start")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    """A CSV cell that no spreadsheet will evaluate. ``None`` is an empty cell;
    numbers pass through untouched (a negative net is a number, not a formula)."""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def build_csv(activity_rows, growth_rows, start, end):
    """The CSV text for the days ``start..end`` inclusive. Pure."""
    growth_by_day = {row["day"]: row for row in growth_rows or ()}
    messages_by_day = {
        row["day"]: int(row["messages"] or 0) for row in activity_rows or ()
    }
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for day in rollups.day_span(start, end):
        row = growth_by_day.get(day)
        if row is None:
            cells = [day.isoformat(), None, None, None, None, None]
        else:
            joins = int(row["joins"] or 0)
            leaves = int(row["leaves"] or 0)
            cells = [
                day.isoformat(),
                messages_by_day.get(day, 0),
                joins,
                leaves,
                joins - leaves,
                row["member_count"],
            ]
        writer.writerow([safe_cell(cell) for cell in cells])
    return out.getvalue()


async def collect(pool, guild_id, start, end):
    activity_rows = await pool.fetch(rollups.ACTIVITY_SERIES, guild_id, start, end)
    growth_rows = await pool.fetch(rollups.GROWTH, guild_id, start, end)
    return build_csv(activity_rows, growth_rows, start, end)
