"""One circuit breaker shared by every AniList POLLER (audit: the 2026-09 outage).

WHAT HAPPENED. From 2026-09-02 to 2026-09-12 AniList answered every request with
HTTP 403 and the GraphQL error "The AniList API has been temporarily disabled due
to severe stability issues." Their maintainers said API traffic had doubled, that
they were resource-constrained, and gave no ETA - so it can recur. Our three
pollers kept going at full rate for ten days: the feed every 120s
(:data:`cogs.anilist.feed.POLL_SECONDS`), airing every 600s and chapters every
1800s, each failed fetch logging its own WARNING. Correctness held (a fetch
failure never advances a cursor, and that stays exactly as it was), but we
hammered a service in distress for nothing and drowned the log.

WHY ONE BREAKER AND NOT THREE. The three pollers are three cogs, but there is one
AniList and one per-IP budget behind them. A per-cog breaker would learn the same
outage three times and probe it three times as often, which is the behaviour this
module exists to stop. So the state lives on the BOT object (:func:`breaker_for`,
lazily attached once and shared), not on any cog: it therefore survives a single
cog failing to load, and a ``?reload anilist`` keeps the same breaker rather than
forgetting an outage in progress.

WHAT IT IS NOT. The INTERACTIVE surface (lookups, account commands, feed card
buttons, admin searches) is deliberately NOT routed through it - see
:mod:`cogs.anilist.throttle`, which bounds that surface instead. Those calls are
user-triggered, they already fail one request at a time in front of the person
who asked, and silently refusing them for up to an hour would turn a remote
outage into a bot that looks broken. A poller has nobody waiting on it; a command
does.

THE THREE STATES.

* CLOSED - normal. Every poller request goes out; each one reports back.
* OPEN - after :data:`FAILURE_THRESHOLD` consecutive HARD failures. Every poller
  request is refused before the socket (:class:`CircuitOpen`), and the pollers
  skip their tick in SILENCE. The wait doubles from
  :data:`OPEN_BASE_SECONDS` and is capped at :data:`OPEN_CAP_SECONDS`.
* HALF-OPEN - once the wait elapses, the FIRST request through takes the single
  probe token; everything else is still refused until that probe reports. So an
  expiry costs exactly one request, not one per poller.

TIME IS MONOTONIC. Every deadline here is a :func:`time.monotonic` delta, never a
wall-clock stamp: an NTP step, a DST change or the suite's 400-day time travel
must not shorten or extend a backoff. The clock is injectable so tests drive it
by hand instead of sleeping.

Pure: standard library only. No discord, no aiohttp, no cog import - which is
what lets :mod:`cogs.system.health` read the state for the ``LOAD`` line.
"""

from __future__ import annotations

import logging
import time
import typing

log = logging.getLogger(__name__)

# Consecutive HARD failures before the circuit opens. One timeout is weather;
# three in a row - across whatever pollers happened to fire - is a pattern. Kept
# low on purpose: during a real outage airing's list refresh alone produces
# several failures inside ONE tick, so the circuit opens in minutes rather than
# hours.
FAILURE_THRESHOLD = 3

# First backoff. Two ticks of the FASTEST poller (the feed, POLL_SECONDS = 120),
# so the first thing an outage costs is two skipped feed ticks and nothing else.
# Pinned against the real feed period by the test suite - this file cannot import
# cogs.anilist.feed without a cycle, so the binding is a test rather than an
# import.
OPEN_BASE_SECONDS = 240.0

# Doubling stops here: one hour. This single number IS the outage request budget
# (see the module's scale note): once saturated, the whole process spends at most
# one probe request per cap-length window, whatever the installed base.
OPEN_CAP_SECONDS = 3600.0

# Where the one shared instance is parked on the bot. Named once, here, so
# :func:`breaker_for` and :func:`breaker_state` cannot drift apart.
BOT_ATTR = "_anilist_breaker"

_Clock = typing.Callable[[], float]


class CircuitOpen(Exception):
    """Raised INSTEAD of making an AniList request while the circuit is open.

    Deliberately NOT a subclass of the pollers' ``_FetchError``: the per-user
    handlers in ``_refresh_lists`` swallow that one and count it against their
    own escape hatches (airing caches an EMPTY list after three straight
    failures). A refused request is not a failed one - nothing was learned about
    that user - so it must fly straight past those handlers to the tick, which
    returns in silence.
    """

    # Read by :func:`guarded_request`: a refusal is not evidence about AniList.
    service_down = False

    def __init__(self, source, seconds_left):
        super().__init__(
            "AniList circuit open; %s request refused (%.0fs left)"
            % (source, seconds_left)
        )
        self.source = source
        self.seconds_left = seconds_left


class AniListBreaker:
    """Consecutive-failure circuit breaker over the shared AniList API.

    Clock-injected and side-effect free apart from two log lines (one WARNING
    when an outage starts, one INFO when it ends). Every method is O(1).
    """

    def __init__(self, *, clock: _Clock = time.monotonic) -> None:
        self._clock = clock
        # Consecutive hard failures since the last success.
        self._consecutive = 0
        # Monotonic deadline; None means closed.
        self._open_until: float | None = None
        # True while a single half-open probe request is in flight.
        self._probing = False
        # Backoff steps taken during THIS outage (drives the doubling), and the
        # lifetime count of OUTAGES - see the `opens` property for the
        # difference, which is the whole reason these are two numbers.
        self._streak = 0
        self._opens = 0
        # Monotonic stamp of the FIRST opening of this outage, for the duration
        # the closing line reports.
        self._outage_started: float | None = None
        # Lifetime count of poller requests refused before the socket.
        self._skipped = 0

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def is_open(self, now: float | None = None) -> bool:
        """Whether a request right now would be refused. Side-effect free.

        True while the backoff is still running AND while another poller already
        holds the half-open probe. The pollers call this at the top of a tick so
        they can return before spending a database round trip, and it must not
        consume the probe to do so - :meth:`require_closed` is what takes it.
        """

        if self._open_until is None:
            return False
        now = self._clock() if now is None else now
        return now < self._open_until or self._probing

    def seconds_left(self, now: float | None = None) -> float:
        """Seconds until the next probe is allowed; 0.0 when closed."""

        if self._open_until is None:
            return 0.0
        now = self._clock() if now is None else now
        return max(0.0, self._open_until - now)

    @property
    def tripped(self) -> bool:
        """Whether the circuit is TRIPPED, which is not the same as refusing.

        It stays tripped from the opening until a request actually succeeds -
        including the half-open gap where the wait has elapsed and the next
        probe has not run yet. :meth:`is_open` answers the narrower "would this
        request be refused", which is what a gate needs and what must go False
        for the probe to happen at all; an operator reading the health line
        needs THIS one, or a mid-outage tick boundary would print
        ``breaker=closed`` and read as all-clear.
        """

        return self._open_until is not None

    @property
    def skipped(self) -> int:
        """Lifetime count of poller requests refused before the socket."""

        return self._skipped

    @property
    def opens(self) -> int:
        """Lifetime count of OUTAGES: closed -> open transitions.

        Counted once per outage and NOT once per backoff step, because this
        number is read by a human. A single outage that takes six failed probes
        to heal is one thing that happened, and reporting ``breaker_opens=6``
        would tell the operator AniList went down six times. The step the
        backoff has reached is already legible live, as ``breaker_left``.

        Survives the healing (:meth:`_reset` leaves it alone), so an outage that
        started and ended overnight is still visible in the morning.
        """

        return self._opens

    def health_fields(self, now: float | None = None) -> dict:
        """Flat ``k=v`` fields for the bot-wide ``LOAD`` line.

        ``breaker=open`` is the grep an operator reaches for mid-incident;
        ``breaker_opens`` / ``breaker_skipped`` keep the trace AFTER it heals, so
        a night-time outage that closed itself is still visible in the morning.
        ``breaker_left`` only appears while open, where it is the only number
        that changes.
        """

        fields = {
            "breaker": "open" if self.tripped else "closed",
            "breaker_opens": self._opens,
            "breaker_skipped": self._skipped,
        }
        if self.tripped:
            fields["breaker_left"] = int(self.seconds_left(now))
        return fields

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------
    def require_closed(self, source, now: float | None = None) -> None:
        """Take the right to make ONE poller request, or raise :class:`CircuitOpen`.

        Closed: returns, and the caller must report back exactly once. Open with
        the wait still running, or with another poller's probe in flight: raises.
        Open with the wait elapsed and no probe out: hands this caller the single
        probe token, so an expiry costs one request for the whole process rather
        than one per poller.
        """

        if self._open_until is None:
            return
        now = self._clock() if now is None else now
        if now < self._open_until or self._probing:
            self._skipped += 1
            raise CircuitOpen(source, max(0.0, self._open_until - now))
        self._probing = True

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    def note_success(self, source, now: float | None = None) -> None:
        """A poller request came back with data: close the circuit.

        The FIRST success ends the outage, whatever the streak - a service that
        answers is a service that is back, and holding the pollers off any longer
        would only lengthen the gap in their cursors.
        """

        now = self._clock() if now is None else now
        was_open = self._open_until is not None
        started = self._outage_started
        streak = self._streak
        skipped = self._skipped
        self._reset()
        if was_open:
            log.info(
                "AniList circuit CLOSED: %s got an answer again after %.0fs open "
                "(%s backoff steps, %s poller requests skipped)",
                source,
                0.0 if started is None else now - started,
                streak,
                skipped,
            )

    def note_failure(self, source, reason, now: float | None = None) -> bool:
        """A poller request failed HARD. Returns True when this opened the circuit.

        ``reason`` is the short description the opening WARNING carries, so the
        operator reads WHY without grepping for the fetch line that produced it.
        """

        now = self._clock() if now is None else now
        self._probing = False
        self._consecutive += 1
        if self._consecutive < FAILURE_THRESHOLD:
            return False

        self._streak += 1
        wait = min(OPEN_BASE_SECONDS * 2 ** (self._streak - 1), OPEN_CAP_SECONDS)
        self._open_until = now + wait
        if self._streak == 1:
            # First step of THIS outage: one outage, counted once. Later steps
            # are the same outage still going, and must not inflate the number
            # an operator reads as "how many times did AniList go down".
            self._opens += 1
            self._outage_started = now
            # ONE warning per outage. The ten-day 403 logged 864 of these a day;
            # every later step of the same outage goes to INFO below, and the
            # live state rides the LOAD line every 60s.
            log.warning(
                "AniList circuit OPEN after %s consecutive hard failures (%s); "
                "every AniList poller stays off the network for %.0fs",
                self._consecutive,
                reason,
                wait,
            )
        else:
            log.info(
                "AniList circuit still open: the probe from %s failed (%s); "
                "backing off %.0fs (step %s)",
                source,
                reason,
                wait,
                self._streak,
            )
        return True

    def note_inconclusive(self) -> None:
        """The request neither proved the service up nor down: release the probe.

        A 429 is the clearest case: AniList answered, so it is not down, but we
        got no data either - and its own ``Retry-After`` embargo already owns
        that backoff. Counting it here would punish the service twice; clearing
        the failure streak would forget an outage on the strength of a rate
        limit. So the counters are left exactly as they were, and only the
        half-open probe token is handed back.
        """

        self._probing = False

    def _reset(self) -> None:
        self._consecutive = 0
        self._open_until = None
        self._probing = False
        self._streak = 0
        self._outage_started = None


def breaker_for(bot) -> AniListBreaker:
    """The ONE breaker for this bot, created on first use.

    Parked on the bot rather than on a cog so the feed, airing and chapter
    pollers - three separate cogs - share one view of one service. Idempotent:
    every poller calls this in its ``__init__`` so the state exists from boot
    (and the health line can say ``breaker=closed`` rather than going quiet),
    and again at every request.
    """

    breaker = getattr(bot, BOT_ATTR, None)
    if breaker is None:
        breaker = AniListBreaker()
        setattr(bot, BOT_ATTR, breaker)
    return breaker


def breaker_state(bot) -> AniListBreaker | None:
    """The breaker if one exists, else None. Never creates one.

    For readers that only observe (:mod:`cogs.system.health`): a monitor must not
    be the thing that brings a piece of state into existence.
    """

    return getattr(bot, BOT_ATTR, None)


# GraphQL error statuses that are about the THING WE ASKED FOR rather than about
# the service: a deleted or renamed AniList account, a malformed id. AniList
# answered correctly - it simply has nothing for that target - so a poller must
# skip that one target and the breaker must not count it. Everything else,
# status 403 (the September disabled-API message) and 5xx included, and anything
# with no status at all, is the service.
TARGET_LEVEL_STATUSES = frozenset({400, 404})


def errors_are_target_level(errors) -> bool:
    """True when EVERY GraphQL error in ``errors`` is about the target, not AniList.

    Deliberately unanimous and deliberately conservative: an empty list, a shape
    that is not a list of dicts, an entry with no ``status``, or a single entry
    outside :data:`TARGET_LEVEL_STATUSES` all answer False, i.e. "treat this as
    the service". Being wrong in that direction costs a skipped tick; being
    wrong the other way is the ten-day hammering this module exists to stop.
    """

    if not isinstance(errors, list) or not errors:
        return False
    for entry in errors:
        if not isinstance(entry, dict):
            return False
        if entry.get("status") not in TARGET_LEVEL_STATUSES:
            return False
    return True


def describe_failure(exc) -> str:
    """A short, bounded description of a failure for the opening WARNING.

    The type name is always present because these exceptions are routinely
    empty-stringed (a timeout's ``str()`` is ``""``), and the cause is followed
    for the same reason the feed poller's own log line follows it.
    """

    detail = exc.__cause__ or exc
    return "{}: {}".format(type(detail).__name__, str(detail)[:120])


async def guarded_request(bot, source, fetch):
    """Run ONE AniList POLLER request under the shared circuit breaker.

    ``fetch`` is a zero-argument coroutine function doing the actual request;
    ``source`` names the poller for the log lines ("feed" / "airing" /
    "chapters").

    The verdict is read from the exception's ``service_down`` attribute, which
    the AniList error types declare explicitly (``_FetchError`` sets it per
    raise site, ``_RateLimited`` and :class:`CircuitOpen` are False). An
    exception carrying no such attribute counts as INCONCLUSIVE on purpose: a
    ``KeyError`` from our own parsing is a bug in this repository, and a bug here
    must never be able to mute the pollers for an hour.
    """

    breaker = breaker_for(bot)
    breaker.require_closed(source)
    try:
        data = await fetch()
    except BaseException as exc:
        if getattr(exc, "service_down", False):
            breaker.note_failure(source, describe_failure(exc))
        else:
            breaker.note_inconclusive()
        raise
    breaker.note_success(source)
    return data
