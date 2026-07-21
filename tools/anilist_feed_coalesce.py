"""Pure, testable core for coalescing consecutive AniList list-progress cards.

AniList emits a SEPARATE activity every time a user saves progress on a title, so
a reader who saves ch.50 then ch.54 on the same manga produces two feed activities
where the second simply supersedes the first - repetitive spam in a feed channel.
This module owns the ONE decision the delivery layer needs to fold those into a
single card that is EDITED in place (a Discord edit is silent = zero notification):
given the incoming activity, the live coalescing record for its
``(channel, user, media)`` slot (or ``None``), and "now", should the caller POST a
fresh card (and record it) or EDIT the existing one (and update the record)?

It is deliberately free of any discord.py / database / await use - it only shapes
and compares data - so the cog stays a thin I/O shell and the tests need no
network or DB. It is also translation-free: no user-facing wording lives here.

The coalescing window has two independent clocks, both anchored on the live
record and compared against "now":

* SESSION_GAP - time since the card was LAST touched (``record.updated_at``). If a
  user goes quiet for longer than this, their next save opens a FRESH card rather
  than resurrecting a stale one. 30 min.
* AGE_CAP - total age of the card since it was FIRST posted
  (``record.created_at``). Even an unbroken reading session gets a fresh card once
  the current one is this old, so a single card never edits forever. 6 h.

SCOPE: only same-status progress INCREMENTS coalesce. A status change (e.g.
CURRENT -> COMPLETED), progress going BACKWARDS, or a non-list (text) activity all
open a fresh card. Only ``ListActivity`` carrying a progress value is ever a
coalescing candidate; ``TextActivity`` is always POST_NEW and never recorded.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

# --- Coalescing window constants (seconds) ----------------------------------
# No update for this long -> the next update starts a fresh card.
SESSION_GAP = 1800  # 30 minutes
# A card older than this gets a fresh card even if the session continues.
AGE_CAP = 21600  # 6 hours
# Extra slack past AGE_CAP before the sweep is allowed to delete a dead record.
PRUNE_GRACE = 3600  # 1 hour

# --- Decision actions -------------------------------------------------------
POST_NEW = "post_new"  # send a fresh card and (if record=True) record it
EDIT = "edit"          # edit the existing card at message_id and update its record


class CoalesceRecord(NamedTuple):
    """The live coalescing card for one ``(channel, user, media)`` slot.

    Built by the caller from an ``anilist_feed_posts`` row. ``created_at`` is the
    first-post time (the AGE_CAP clock); ``updated_at`` is the last-edit time (the
    SESSION_GAP clock, and the sweep's prune key). ``last_progress`` is the raw
    AniList progress string of the newest fold; ``status`` is the list status the
    card is currently coalescing.
    """

    message_id: int
    status: Optional[str]
    last_progress: Optional[object]
    created_at: object  # datetime
    updated_at: object  # datetime


class Decision(NamedTuple):
    """What the delivery layer should do with an incoming activity.

    * ``action`` - :data:`POST_NEW` or :data:`EDIT`.
    * ``message_id`` - for :data:`EDIT`, the existing card's message id to edit;
      always ``None`` for :data:`POST_NEW`.
    * ``record`` - whether the caller should upsert an ``anilist_feed_posts`` row
      for this activity. ``True`` for every coalescing candidate (both a fresh
      POST_NEW that opens a new card and an EDIT that advances one); ``False`` for
      a non-coalescing activity (text posts, list activities with no progress),
      which are posted but never tracked.
    """

    action: str
    message_id: Optional[int]
    record: bool


_DIGITS = re.compile(r"\d+")


def progress_value(progress) -> Optional[int]:
    """Reduce an AniList progress value to a single comparable integer.

    AniList progress is a string that may be a single number (``"54"``) or a range
    (``"50 - 54"``); the meaningful "current" progress is the HIGHEST number in it.
    Returns that max integer, or ``None`` when nothing numeric is present (an
    unparseable value can never be judged an increment, so the caller treats it as
    non-coalescing). An ``int`` passes straight through.
    """

    if progress is None:
        return None
    if isinstance(progress, bool):  # guard: bool is an int subclass
        return None
    if isinstance(progress, int):
        return progress
    nums = _DIGITS.findall(str(progress))
    if not nums:
        return None
    return max(int(n) for n in nums)


def is_coalescible(activity) -> bool:
    """True when an activity is a list-progress card eligible to be folded.

    Only a ``ListActivity`` that carries a progress value qualifies; everything
    else (text posts, list activities with no progress) is always POST_NEW and is
    never recorded.
    """

    return (
        activity.get("kind") == "ListActivity"
        and activity.get("progress") is not None
    )


def _seconds_since(then, now) -> Optional[float]:
    """Whole seconds between two datetimes, or ``None`` if ``then`` is missing.

    A missing anchor is treated by callers as "past every window" (force a fresh
    card / prunable), so the decision stays safe rather than editing a card whose
    age is unknown.
    """

    if then is None:
        return None
    return (now - then).total_seconds()


def decide_delivery(
    activity,
    existing_record,
    now,
    *,
    session_gap: int = SESSION_GAP,
    age_cap: int = AGE_CAP,
) -> Decision:
    """Decide whether to POST a fresh card or EDIT the live one for an activity.

    ``activity`` is a normalised feed activity (``kind``, ``status``, ``progress``,
    ... - see ``cogs/anilist/feed._normalize``). ``existing_record`` is the
    :class:`CoalesceRecord` for this activity's ``(channel, user, media)`` slot, or
    ``None`` when none is live. ``now`` is a timezone-aware datetime.

    A fresh card (:data:`POST_NEW`) is returned unless EVERY coalescing condition
    holds against an existing record: same list status, progress advanced (>=),
    last touch within ``session_gap``, and card age within ``age_cap``. A status
    change, backwards/unparseable progress, a lapsed session or an over-age card
    each open a fresh card. Non-coalescing activities (text, progress-less) are
    POST_NEW with ``record=False``; every coalescing candidate carries
    ``record=True`` so the caller tracks it either way.
    """

    if not is_coalescible(activity):
        return Decision(POST_NEW, None, record=False)

    if existing_record is None:
        return Decision(POST_NEW, None, record=True)

    # Status change (CURRENT -> COMPLETED, dropped, ...) -> fresh card.
    if activity.get("status") != existing_record.status:
        return Decision(POST_NEW, None, record=True)

    # Progress must advance (or hold); backwards or unparseable -> fresh card.
    new_progress = progress_value(activity.get("progress"))
    old_progress = progress_value(existing_record.last_progress)
    if new_progress is None or old_progress is None or new_progress < old_progress:
        return Decision(POST_NEW, None, record=True)

    # Session lapsed since the last edit -> fresh card.
    since_touch = _seconds_since(existing_record.updated_at, now)
    if since_touch is None or since_touch > session_gap:
        return Decision(POST_NEW, None, record=True)

    # Card too old since first post -> fresh card even mid-session.
    since_post = _seconds_since(existing_record.created_at, now)
    if since_post is None or since_post > age_cap:
        return Decision(POST_NEW, None, record=True)

    # All conditions met: silently edit the existing card in place.
    return Decision(EDIT, existing_record.message_id, record=True)


def is_prunable(
    record,
    now,
    *,
    age_cap: int = AGE_CAP,
    grace: int = PRUNE_GRACE,
) -> bool:
    """True when a coalescing record is dead and the sweep may delete it.

    A card can no longer be edited once its session has lapsed, and an active card
    is touched at least every ``SESSION_GAP`` (< ``age_cap``); so a record whose
    last touch (``updated_at``) is older than ``age_cap + grace`` is certainly
    dead. Mirrors the sweep query ``WHERE updated_at < now - (age_cap + grace)``.
    """

    since_touch = _seconds_since(record.updated_at, now)
    if since_touch is None:
        return True
    return since_touch > age_cap + grace
