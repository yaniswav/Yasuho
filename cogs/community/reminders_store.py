"""Pure, DB-free helpers for the reminder listing/cancel surface.

The Reminder cog's "see and cancel my reminders" card leans on a handful of
tiny, side-effect-free functions: the paging math (mirrors
``tools.leveling.leaderboard_page`` so the card's Prev/Next is unit-tested
without any Discord object), the Select/line label truncation, the defensive
type filter (only ``reminder`` timers ever reach a user's card - never a
tempban or any other timer event), and the capped-count label. Keeping them
here means the cog stays thin and every branch is exercised by a plain unit
test.
"""

from __future__ import annotations

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


def paginate(total, page, per_page=REMINDER_PAGE_SIZE):
    """Resolve the paginated slice of ``total`` reminders for ``page``.

    Returns ``(clamped_page, total_pages, start, end)`` where ``[start:end]``
    slices the reminder list for the requested page. ``page`` is 0-indexed and
    clamped into ``[0, total_pages - 1]`` so a list that shrank under the viewer
    (a reminder fired, or the viewer cancelled the last one on a page) never
    lands on a blank page; ``total_pages`` is at least 1 even for an empty list.
    Pure - mirrors ``tools.leveling.leaderboard_page`` so the card's paging math
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
