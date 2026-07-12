"""A pure, mutation-stable round-robin scheduler for constant per-tick budgets.

The AniList/MangaDex pollers must spend a CONSTANT number of requests per tick
no matter how many manga/users/guilds are tracked (the whole point of lot B2):
an O(M) "one request per tracked thing every tick" design 429-storms and dies at
scale. The fix is to serve only a fixed ``budget`` slice of the tracked set each
tick and rotate through the rest over successive ticks, so the request count is
bounded and the per-item poll interval degrades linearly and predictably instead
of the request count exploding.

This module owns just that rotation and nothing else. It is PURE: no clock, no
network, no database, no discord, no i18n. The caller holds the wheel's position
(a single "resume after this item" marker) between ticks and threads it back in;
an in-memory marker is fine, and a restart simply restarts the wheel at the top
(a documented, harmless reset - the worst case is one item polled a tick early or
late right after a restart).

Why a marker VALUE and not an index. The tracked set mutates between ticks
(manga get mapped/unsubscribed, users opt in/out). An index into a re-sorted list
would silently point at a different item after any insertion or removal. Resuming
"after the marker value" via :func:`bisect.bisect_right` instead is stable under
add/remove: a removed marker simply resumes at the next larger item, an inserted
item slots into the canonical order and is served within one full cycle, and
nothing already in the set can be starved.

Fairness guarantee. With a static set of ``n`` items and a positive ``budget``,
:func:`next_batch` advances the marker by ``budget`` positions each call (wrapping
at the end), so every item is served at least once within
``ceil(n / budget)`` calls - exactly what :func:`poll_interval_ticks` reports. No
item is served twice in one batch (a batch is capped at ``n``), so there is no
starvation and no wasted slot.
"""

from __future__ import annotations

import bisect
import math
import typing

T = typing.TypeVar("T")


def next_batch(
    items: typing.Iterable[T],
    after: T | None,
    budget: int,
) -> tuple[list[T], T | None]:
    """Return the next ``budget`` items of a fair rotation, plus the new marker.

    ``items`` is the current tracked set (any iterable of sortable, comparable
    values - ints for user ids, str UUIDs for manga); duplicates collapse via the
    canonical sort. ``after`` is the marker returned by the previous call (``None``
    to start at the top). ``budget`` is the maximum number of items to serve this
    tick.

    Returns ``(batch, new_after)``:

    * ``batch`` - up to ``min(budget, len(set(items)))`` items in ascending order,
      resuming at the first item strictly greater than ``after`` and wrapping past
      the end exactly once. Never contains a duplicate, so a small set with a large
      budget yields each item once (not the same item repeatedly).
    * ``new_after`` - the last item in ``batch`` (feed it back next tick). When
      nothing is served (empty ``items`` or a non-positive ``budget``) the marker
      is returned UNCHANGED, so an empty tick never resets the wheel.

    Stable under mutation: because the resume point is computed from the marker
    VALUE (via :func:`bisect.bisect_right`), a marker that was removed from the set
    between calls still resumes cleanly at the next larger item, and a freshly
    added item is picked up within one cycle. Pure and total: it never raises on an
    empty set or a zero/negative budget.
    """

    ordered = sorted(set(items))
    n = len(ordered)
    if n == 0 or budget <= 0:
        return [], after

    if after is None:
        start = 0
    else:
        # First index whose value is strictly greater than the marker. When the
        # marker is at or past the last item, wrap to the top.
        start = bisect.bisect_right(ordered, after)
        if start >= n:
            start = 0

    take = budget if budget < n else n  # never serve an item twice in one batch
    batch = [ordered[(start + i) % n] for i in range(take)]
    return batch, batch[-1]


def poll_interval_ticks(n: int, budget: int) -> int:
    """Ticks for the wheel to serve every one of ``n`` items once, at ``budget``/tick.

    This is ``ceil(n / budget)`` - the number of ticks a full rotation takes and
    therefore the worst-case per-item poll interval (in ticks). Multiply by the
    poll cadence to get the wall-clock interval a caller logs
    ("125 manga tracked, each polled every ~2.5h"). Returns ``0`` for an empty set
    and, defensively, ``n`` for a non-positive budget (which would never drain).
    Pure and total.
    """

    if n <= 0:
        return 0
    if budget <= 0:
        return n
    return math.ceil(n / budget)
