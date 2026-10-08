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
  friendly ephemeral. The slash TITLE AUTOCOMPLETE takes the same two windows in
  its own callback (``AccountMixin._autocomplete_slot``) because discord.py runs
  no command check and no cooldown for an autocomplete - without it, one member
  typing was bounded by nothing at all while still spending the global ceiling.

A shared counter records how many interactive responses (lookups and feed card
actions) came back as HTTP 429, so the operator can SEE "AniList is throttling
us" and correlate it with poller embargoes - surfacing the signal without
changing poller behaviour. That counter (and the global window's hit/rejection
counts from :meth:`AniListThrottle.stats`) is folded into the ``anilist=``
segment of the bot-wide ``LOAD`` line that :mod:`cogs.system.health` logs every
60s (:meth:`cogs.system.health.Health._anilist_stats`), the same place the
Music and webhook subsystems already surface theirs - so the promise above is
not just a docstring, it is a grep-able line in production.

:class:`AniListCallRecorder` below is a SEPARATE, purely diagnostic concern: it
does not gate or delay anything. Every AniList HTTP call site (pollers AND
interactive - see :func:`note_response`) reports its response here, so a 429
can be logged with "how many requests did WE make in the last 60s, from every
source" next to it. That is the one question an operator needs answered after
a 429: if ``ours_60s`` is far below the degraded 30/min limit and the last seen
``X-RateLimit-Remaining`` before the 429 was still high, the 429 is AniList's
OWN throttling (a shared-IP neighbour, or AniList having a bad moment) rather
than anything the bot is doing - the opposite reading (``ours_60s`` near the
limit) means it is genuinely our own volume.

Pure and clock-injected (via :mod:`tools.quotas`): pass ``clock`` to drive time
deterministically in tests.
"""

from __future__ import annotations

import collections
import logging
import time
import typing

from tools.quotas import SlidingWindowQuota

log = logging.getLogger(__name__)

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

    def global_available(self, now: float | None = None) -> bool:
        """Whether a process-wide interactive slot is free, WITHOUT taking one.

        For callers that gate a surface whose own request already spends the
        global slot downstream (the slash autocomplete, whose fetches go through
        ``AniListBase._graphql``): they need to know the ceiling is spent so they
        can degrade quietly, but taking a slot here would charge the window twice
        for one request and make the ceiling drift away from the number of calls
        actually put on the wire. Consuming paths keep using
        :meth:`allow_global`.
        """
        return self._global.check(_GLOBAL_KEY, now)

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


# --- 429 diagnostic: ours vs. AniList's own throttling ----------------------
#
# How many requests did the bot ITSELF send to AniList in the last 60s, from
# every source (pollers and interactive alike), and what did the rate-limit
# headers say right before a 429? Bounded (a 60s deque, trimmed on every read
# or write - O(1) amortised, memory never grows past one window's worth of
# events) and process-wide: AniList rate-limits by IP, not by caller, so "ours"
# only means something when every source reports to the SAME recorder.

RECORD_WINDOW = 60.0


class AniListCallRecorder:
    """Bounded in-memory record of every AniList HTTP response, any status.

    :meth:`record` appends ``(timestamp, source)`` to a deque trimmed to the
    last :data:`RECORD_WINDOW` seconds, and remembers the last seen
    ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining`` values (shared across
    sources, since the limit is per-IP). It returns the PREVIOUS last-seen
    pair (before this call updates them) so a 429 handler can log what the
    budget looked like just before the throttling response arrived, rather
    than the 429 response's own headers (which are often empty or already 0).

    Pure and clock-injected like :class:`AniListThrottle`. One instance lives
    on ``AniListBase`` (``self._call_recorder``); every other call site reaches
    it through :func:`recorder_for`, the same ``get_cog("AniList")`` pattern
    ``AniListThrottle`` already uses.
    """

    def __init__(self, *, clock: _Clock = time.monotonic, window: float = RECORD_WINDOW) -> None:
        self._clock = clock
        self._window = window
        self._events: typing.Deque[tuple[float, str]] = collections.deque()
        self._last_limit: str | None = None
        self._last_remaining: str | None = None

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def record(
        self,
        source: str,
        *,
        limit: str | None = None,
        remaining: str | None = None,
        now: float | None = None,
    ) -> tuple[str | None, str | None]:
        """Record one AniList response from ``source``; return the (limit,
        remaining) last seen BEFORE this one (for a 429 line's
        ``remaining_before``)."""
        now = self._clock() if now is None else now
        previous = (self._last_limit, self._last_remaining)
        self._events.append((now, source))
        self._trim(now)
        if limit is not None:
            self._last_limit = limit
        if remaining is not None:
            self._last_remaining = remaining
        return previous

    def snapshot(self, now: float | None = None) -> dict:
        """``{"total_60s": n, "by_source": {source: count, ...}}`` over the window."""
        now = self._clock() if now is None else now
        self._trim(now)
        by_source: dict[str, int] = {}
        for _ts, source in self._events:
            by_source[source] = by_source.get(source, 0) + 1
        return {"total_60s": len(self._events), "by_source": by_source}

    def reset(self) -> None:
        """Test-only: drop every recorded event and the last-seen headers."""
        self._events.clear()
        self._last_limit = None
        self._last_remaining = None


def recorder_for(bot) -> "AniListCallRecorder | None":
    """Resolve the shared :class:`AniListCallRecorder` off the composed
    ``AniList`` cog (``AniListBase.__init__`` owns the one instance), or
    ``None`` when that cog has not loaded. Mirrors
    ``feed_delivery._throttle_for`` - same reasoning: the pollers (separate
    cogs) and the feed card actions need to reach the ONE instance the
    interactive lookup path (``AniListBase._graphql``) already owns, so
    "ours_60s" reflects the bot's real total volume across every source.
    Degrades to None rather than raising, like every other best-effort read
    in this module.
    """
    get_cog = getattr(bot, "get_cog", None)
    if get_cog is None:
        return None
    return getattr(get_cog("AniList"), "_call_recorder", None)


def _or_unknown(value: object) -> str:
    return "?" if value is None else str(value)


def _format_by_source(by_source: dict) -> str:
    return ",".join(f"{source}:{count}" for source, count in sorted(by_source.items()))


def note_response(
    recorder: "AniListCallRecorder | None", source: str, headers: typing.Mapping, status: int
) -> None:
    """Record one AniList HTTP response and, on a 429, log one diagnostic line.

    ``recorder`` is ``None`` when the owning cog has not loaded (or, in a
    test, was never wired) - a no-op, like every other best-effort read here.
    Call this at EVERY AniList call site, on every status (not just 429), so
    the 60s window always reflects the bot's real total volume. On a 429 it
    logs a single greppable WARNING:

        ANILIST-429 source=<source> ours_60s=<n> by_source=<feed:a,airing:b,...>
        remaining_before=<last X-RateLimit-Remaining seen before this 429, or ?>
        limit=<X-RateLimit-Limit on this response, or ?>
        retry_after=<Retry-After on this response, or ?>
        reset=<X-RateLimit-Reset on this response, or ?>

    Interpretation: if ``ours_60s`` is far below ``limit`` and
    ``remaining_before`` was still high, AniList's OWN throttling (not our
    volume) caused this 429. Never raises - a diagnostic must never break the
    request it is observing.
    """
    if recorder is None:
        return
    try:
        limit = headers.get("X-RateLimit-Limit")
        remaining = headers.get("X-RateLimit-Remaining")
        _prev_limit, prev_remaining = recorder.record(
            source, limit=limit, remaining=remaining
        )
        if status != 429:
            return
        snapshot = recorder.snapshot()
        log.warning(
            "ANILIST-429 source=%s ours_60s=%s by_source=%s remaining_before=%s "
            "limit=%s retry_after=%s reset=%s",
            source,
            snapshot["total_60s"],
            _format_by_source(snapshot["by_source"]) or "-",
            _or_unknown(prev_remaining),
            _or_unknown(limit),
            _or_unknown(headers.get("Retry-After")),
            _or_unknown(headers.get("X-RateLimit-Reset")),
        )
    except Exception:
        log.exception("AniList 429 diagnostic failed to record")
