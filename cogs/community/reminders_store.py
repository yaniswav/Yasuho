"""Pure, DB-free helpers for the reminder listing/cancel surface.

The Reminder cog's "see and cancel my reminders" card leans on a handful of
tiny, side-effect-free functions: the paging math (mirrors
``cogs.community.leveling.engine.leaderboard_page`` so the card's Prev/Next is unit-tested
without any Discord object), the Select/line label truncation, the defensive
type filter (only ``reminder`` timers ever reach a user's card - never a
tempban or any other timer event), and the capped-count label. Keeping them
here means the cog stays thin and every branch is exercised by a plain unit
test.

The recurrence math for repeating reminders lives here too (``parse_repeat``,
``next_occurrence``, ``recurrence_seconds``, ``split_interval``): it is the part
that must be provably drift-free and bounded, so it is written as pure functions
over datetimes and integers with no DB and no Discord object in sight.
"""

from __future__ import annotations

import datetime
import json

from dateutil.relativedelta import relativedelta

from tools.time import ShortTime

# One card page shows at most this many reminders (bounded so a flood of
# pending reminders can never blow the Components V2 budget or a Select's
# 25-option ceiling).
REMINDER_PAGE_SIZE = 10

# Hard ceiling on how many of a user's reminders the card ever lists. It equals
# the per-user pending cap the setter enforces, so in practice every reminder a
# user has fits; the +1 fetch (see the cog) only exists to detect the overflow
# and render it as "25+" rather than silently dropping rows.
REMINDER_LIST_CAP = 25

# Discord's hard limits: a Select option label and description are each capped
# at 100 characters. Reminder text is truncated to fit.
SELECT_LABEL_MAX = 100

# How much reminder text a card line shows before the ASCII ellipsis. Kept well
# under Discord's per-TextDisplay budget so ten lines never overflow.
LINE_TEXT_MAX = 90

# ---------------------------------------------------------------------------
# Recurrence (repeating reminders)
# ---------------------------------------------------------------------------

# Named shorthands the /remind autocomplete suggests. Everything else goes
# through the SAME duration grammar the initial delay already uses
# (tools.time.ShortTime.compiled, e.g. "2d" / "12h" / "1w"), so a user never has
# to learn a second syntax.
REPEAT_PRESETS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
}

# Anti-spam floor: a repeating reminder can never fire more than once an hour.
# The floor is also what makes the series cheap - one timer row and one INSERT
# per occurrence, at most 24 a day per reminder.
MIN_REPEAT_SECONDS = 3600

# Ceiling: a year. Past this the "reminder" is really a calendar entry, and an
# unbounded interval would let a single row squat the table forever.
MAX_REPEAT_SECONDS = 365 * 86400

# Only ever used to turn a relativedelta into a fixed number of seconds. The
# grammar accepts calendar units (months/years) whose length depends on WHEN
# they are measured, and the stored recurrence is a plain second count, so the
# conversion is anchored to one fixed, documented instant: with this reference
# "1mon" is 31 days and "1y" is 365 days, always, on every machine.
_INTERVAL_REFERENCE = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)

# Repeat/refresh glyph shown on a recurring reminder's card line.
REPEAT_GLYPH = "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}"

# The units ``split_interval`` reports, coarsest first. The first one that
# divides the interval exactly wins, so 604800 reads as "1 week" and not
# "168 hours" while 5400 still reads exactly as "90 minutes".
_INTERVAL_UNITS = (
    ("week", 604800),
    ("day", 86400),
    ("hour", 3600),
    ("minute", 60),
)


def parse_extra(value):
    """Decode a timer's ``extra`` column into a plain dict.

    asyncpg hands JSONB back as a ``dict`` when a codec is registered and as raw
    text otherwise, and a NULL/absent value must degrade to ``{}`` rather than
    explode in the dispatch loop. Same three-case handling the cog has always
    done inline, factored out so every reader shares it.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def parse_repeat(text):
    """Parse a user-supplied repeat interval into ``(seconds, problem)``.

    ``text`` empty/None means "no recurrence": returns ``(None, None)`` so the
    caller keeps the one-shot path untouched. A named preset (hourly/daily/
    weekly) or any duration the initial-delay grammar accepts ("2d", "12h",
    "1w30m") yields ``(seconds, None)``. Anything else yields
    ``(None, problem)`` where ``problem`` is one of ``"invalid"``,
    ``"too_short"`` (under :data:`MIN_REPEAT_SECONDS`) or ``"too_long"`` (over
    :data:`MAX_REPEAT_SECONDS`), so the caller picks the message and this stays
    free of any i18n dependency.
    """
    text = (text or "").strip().lower()
    if not text:
        return None, None
    if text in REPEAT_PRESETS:
        return REPEAT_PRESETS[text], None
    match = ShortTime.compiled.fullmatch(text)
    if match is None or not match.group(0):
        return None, "invalid"
    data = {key: int(value) for key, value in match.groupdict(default=0).items()}
    delta = (_INTERVAL_REFERENCE + relativedelta(**data)) - _INTERVAL_REFERENCE
    seconds = int(delta.total_seconds())
    if seconds < MIN_REPEAT_SECONDS:
        return None, "too_short"
    if seconds > MAX_REPEAT_SECONDS:
        return None, "too_long"
    return seconds, None


def recurrence_seconds(extra):
    """The validated recurrence interval stored in a timer's ``extra``, or None.

    This is the ONLY gate the dispatch loop uses to decide whether a fired row
    must be rescheduled, so it re-validates instead of trusting the stored
    value: a missing, non-integer, or out-of-range ``repeat_seconds`` degrades
    the row to a plain one-shot reminder. That matters - a zero or negative
    interval would otherwise schedule the next occurrence in the past forever
    and turn the dispatch loop into a hot spin.
    """
    raw = (extra or {}).get("repeat_seconds")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    seconds = int(raw)
    if seconds < MIN_REPEAT_SECONDS or seconds > MAX_REPEAT_SECONDS:
        return None
    return seconds


def occurrence_number(extra):
    """The 1-based ordinal of the occurrence this timer row represents.

    Total: a missing, non-numeric or nonsensical counter reads as 1 rather than
    raising. This runs inside the reschedule transaction, and an exception there
    would roll the claim back and leave the row pending - i.e. the same reminder
    re-firing every few seconds forever. A wrong counter is cosmetic; a raise is
    not.
    """
    raw = (extra or {}).get("occurrence")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 1
    number = int(raw)
    return number if number >= 1 else 1


def next_occurrence(scheduled, interval_seconds, now):
    """Return ``(next_fire_at, missed)`` for a recurring reminder that just fired.

    The series is anchored to the SCHEDULED time, never to ``now``: the next
    occurrence is ``scheduled + k * interval``. A bot that wakes 30 minutes late
    therefore fires 30 minutes late ONCE and is back on the original grid at the
    next occurrence - the series can never drift forward by the accumulated
    lateness.

    After a long outage the first candidate (``scheduled + interval``) can
    already be in the past. Rather than looping (an outage of a week at an
    hourly interval would be 168 iterations, and a corrupt interval an infinite
    one) the number of steps is computed in closed form: ``k`` is the smallest
    integer with ``scheduled + k * interval > now``, i.e.
    ``k = floor((now - scheduled) / interval) + 1``. The ``k - 1`` occurrences
    that were skipped over are reported as ``missed`` so the delivered message
    can say so. ``missed`` is 0 whenever the bot fired within one interval of
    the scheduled time, which is the normal case.
    """
    behind = max(0.0, (now - scheduled).total_seconds())
    steps = int(behind // interval_seconds) + 1
    return (
        scheduled + datetime.timedelta(seconds=steps * interval_seconds),
        steps - 1,
    )


def split_interval(seconds):
    """Decompose an interval into ``(unit, count)`` on its coarsest exact unit.

    ``604800 -> ("week", 1)``, ``172800 -> ("day", 2)``, ``5400 ->
    ("minute", 90)``. Falls through to seconds when nothing divides it exactly,
    so the rendered label is always exact rather than rounded. The caller owns
    the pluralisation (the unit name is an untranslated key).
    """
    seconds = int(seconds)
    for unit, size in _INTERVAL_UNITS:
        if seconds % size == 0:
            return unit, seconds // size
    return "second", seconds


def paginate(total, page, per_page=REMINDER_PAGE_SIZE):
    """Resolve the paginated slice of ``total`` reminders for ``page``.

    Returns ``(clamped_page, total_pages, start, end)`` where ``[start:end]``
    slices the reminder list for the requested page. ``page`` is 0-indexed and
    clamped into ``[0, total_pages - 1]`` so a list that shrank under the viewer
    (a reminder fired, or the viewer cancelled the last one on a page) never
    lands on a blank page; ``total_pages`` is at least 1 even for an empty list.
    Pure - mirrors ``cogs.community.leveling.engine.leaderboard_page`` so the card's paging math
    is unit-tested without any Discord objects.
    """
    safe_total = max(total, 0)
    total_pages = max(1, (safe_total + per_page - 1) // per_page)
    clamped = max(0, min(page, total_pages - 1))
    start = clamped * per_page
    end = min(start + per_page, safe_total)
    return clamped, total_pages, start, end


def truncate(text, limit):
    """Trim ``text`` to ``limit`` characters with an ASCII ``...`` marker.

    Whitespace is stripped first. A string already within ``limit`` is returned
    unchanged; otherwise it is cut to ``limit`` characters INCLUDING the three
    dots (so the result never exceeds ``limit`` - important for the Select label
    which Discord hard-caps at 100). For a ``limit`` too small to hold the
    ellipsis it degrades to a plain hard cut.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def filter_reminders(rows):
    """Keep only the rows whose timer event is ``reminder``.

    Belt-and-suspenders type scoping: the listing query already filters
    ``event = 'reminder'`` in SQL, but this guarantees - and unit-tests - that a
    tempban (or any future timer event) can never surface on a user's reminder
    card even if the query shape later changes. Accepts any mapping-like row
    (asyncpg ``Record`` or a plain dict) exposing an ``event`` key.
    """
    return [r for r in rows if r["event"] == "reminder"]


def format_count(total, capped, cap=REMINDER_LIST_CAP):
    """Render the pending-count footer, collapsing an overflow to ``"25+"``.

    ``total`` is the number of reminders actually shown (already sliced to
    ``cap``); ``capped`` is True when the user has more than ``cap`` pending, in
    which case the exact number is hidden behind ``"<cap>+"``.
    """
    if capped:
        return "{cap}+".format(cap=cap)
    return str(total)
