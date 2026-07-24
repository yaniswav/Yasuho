"""Interactive AniList API-abuse throttle (audit P-2).

The background pollers (airing / feed / chapters) share AniList's per-IP 429
budget with every user-driven lookup and interactive button click. A promo spike
of clicks or ``/search`` could burn that shared budget and silently degrade the
alert pollers for ALL guilds. This module bounds the INTERACTIVE surface only -
it never touches the pollers' own request budget or their embargo logic - built
entirely from the pure primitives in :mod:`tools.quotas`.

Two layers, deliberately separate:

* A process-wide aggregate ceiling on user-driven interactive calls (a single-
  key :class:`~tools.quotas.SlidingWindowQuota`). This is the hard backstop: no
  burst of interactive calls can sustain more than ``GLOBAL_LIMIT`` requests per
  window across the WHOLE process, so the pollers always keep their share of the
  per-IP budget. It spans the ENTIRE interactive surface - the lookup commands
  (``AniListBase._graphql``), the feed card actions (like / reply / add, which
  act as the clicking user through ``feed_delivery._authed_graphql``) and the
  admin feed searches (follow-lookup and title-search). The airing / feed /
  chapter pollers use their own authenticated fetch, are excluded by design and
  never touch this ceiling.
* Per-user and per-guild sliding windows checked at the top of the expensive
  interactive callbacks (lookup components and feed card buttons), so one member
  (or one hyped guild) is told to slow down BEFORE the expensive fetch, with a
  friendly ephemeral.

A shared counter records how many interactive responses (lookups and feed card
actions) came back as HTTP 429, so the operator can SEE "AniList is throttling
us" and correlate it with poller embargoes - surfacing the signal without
changing poller behaviour. That counter (and the global window's hit/rejection
counts from :meth:`AniListThrottle.stats`) is folded into the ``anilist=``
segment of the bot-wide ``LOAD`` line that :mod:`cogs.system.health` logs every
60s (:meth:`cogs.system.health.Health._anilist_stats`), the same place the
Music and webhook subsystems already surface theirs - so the promise above is
not just a docstring, it is a grep-able line in production.

Pure and clock-injected (via :mod:`tools.quotas`): pass ``clock`` to drive time
deterministically in tests.
"""

from __future__ import annotations

import time
import typing

from tools.quotas import SlidingWindowQuota

# Per-IP AniList allows ~90 requests/min (30 when degraded), SHARED with the
# pollers. The interactive backstop sits well under that ceiling so the pollers
# always keep headroom: even a sustained click/search storm cannot burn more
# than GLOBAL_LIMIT requests per window across the whole process.
GLOBAL_LIMIT = 60
GLOBAL_WINDOW = 60.0

# One member hammering buttons: a slot roughly every ~5s mirrors the
# @commands.cooldown(1, 5) rhythm the lookup commands already use.
USER_LIMIT = 12
USER_WINDOW = 60.0

# One hyped guild (a promo drop) across all of its members at once, kept well
# above the per-user limit so a busy-but-legitimate guild is not starved by it.
GUILD_LIMIT = 30
GUILD_WINDOW = 60.0

# Single constant key for the process-wide window (one shared bucket).
_GLOBAL_KEY = "anilist:interactive"

_Clock = typing.Callable[[], float]


class AniListThrottle:
    """Bounds the interactive AniList surface; leaves the pollers untouched."""

    def __init__(self, *, clock: _Clock = time.monotonic) -> None:
        self._clock = clock
        self._global = SlidingWindowQuota(GLOBAL_LIMIT, GLOBAL_WINDOW, clock=clock)
        self._user = SlidingWindowQuota(USER_LIMIT, USER_WINDOW, clock=clock)
        self._guild = SlidingWindowQuota(GUILD_LIMIT, GUILD_WINDOW, clock=clock)
        self._throttled_429 = 0

    def allow_global(self, now: float | None = None) -> bool:
        """Consume one process-wide interactive slot; False when the ceiling is hit.

        This is the backstop wired into every interactive path (the lookup
        ``_graphql``, the feed card actions and the admin feed searches): a False
        here means the whole interactive surface is already at capacity for this
        window, so the call is dropped rather than added to the shared per-IP
        budget the pollers depend on.
        """
        return self._global.hit(_GLOBAL_KEY, now)

    def allow_interactive(
        self, user_id: typing.Any, guild_id: typing.Any, now: float | None = None
    ) -> bool:
        """True if a per-user AND per-guild slot is free, consuming one of each.

        Checked at the top of the expensive button callbacks so a rejected click
        is refused BEFORE any AniList fetch. A rejection consumes nothing on the
        axis that rejected it (the check precedes the hit), so a throttled user
        does not also burn the guild's budget. ``guild_id`` may be ``None`` (a DM),
        in which case only the per-user window applies.
        """
        now = self._clock() if now is None else now
        if not self._user.check(user_id, now):
            return False
        if guild_id is not None and not self._guild.check(guild_id, now):
            return False
        self._user.hit(user_id, now)
        if guild_id is not None:
            self._guild.hit(guild_id, now)
        return True

    def note_throttled(self) -> None:
        """Record one interactive AniList response (lookup or feed action) as 429."""
        self._throttled_429 += 1

    @property
    def throttled_count(self) -> int:
        """Lifetime count of interactive HTTP 429 responses (for the operator)."""
        return self._throttled_429

    def stats(self) -> dict:
        """Cheap snapshot for periodic operator logging."""
        return {
            "global": self._global.stats(),
            "user": self._user.stats(),
            "guild": self._guild.stats(),
            "throttled_429": self._throttled_429,
        }
