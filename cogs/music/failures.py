"""Coalescing a BURST of track failures into a bounded number of messages.

THE INCIDENT (production, 2026-08-20). ``on_sonolink_track_exception`` posted one
message per failed track. When YouTube broke and a queued session of forty tracks
failed one after another - which is exactly how that outage presents, every track
failing on the same cause within a couple of seconds - the cog posted forty
messages into one channel and Discord answered with 429s. The bot's whole HTTP
client is shared, so the rate limit it earned announcing dead tracks was paid by
every other command in every other guild.

THE SHAPE CHOSEN: leading edge, then one trailing summary.

* the FIRST failure of a burst is announced immediately, WITH its title - the
  overwhelmingly common case is a single dead track, and that case must keep the
  message it has always had, un-delayed and un-degraded. It also means no
  translated string is lost.
* every further failure inside :data:`BURST_WINDOW` seconds is silently tallied.
* when the window closes, ONE more message states how many were swallowed.

So a burst of N costs 2 messages instead of N, and a lone failure still costs
exactly 1. The ceiling is 2 messages per window per guild - with a 5 second
window that is well inside Discord's per-channel budget, where 40 in two seconds
was not. A pure trailing-only design would cost 1 instead of 2, at the price of
delaying and stripping the title from the ordinary single-failure case; that
trade was not worth one message.

SCALE. State is one small entry per guild that currently has an OPEN burst, and
an entry deletes itself when its window closes - so the map is bounded by the
number of guilds failing tracks in the same five seconds, not by the number of
guilds, and it is empty at rest. One ``asyncio`` task per open burst, likewise.
Nothing here is a timer that runs when nothing is failing.

The accounting (:meth:`TrackFailureBursts.record` / :meth:`close`) is pure and
synchronous, so the whole decision is unit-tested without a clock; only
:meth:`arm` touches the event loop, and it hands the task back so a test can
await it instead of sleeping.
"""

from __future__ import annotations

import asyncio
import logging
import typing

log = logging.getLogger(__name__)

# How long a burst stays open after its first failure. A few seconds: long enough
# that a queue draining through a common failure lands in ONE window, short
# enough that the summary still reads as part of what just happened.
BURST_WINDOW = 5.0


class TrackFailureBursts:
    """Per-key burst accounting for track-failure announcements.

    ``key`` is a guild id in production (one player, one home channel per guild).
    """

    def __init__(self, window: float = BURST_WINDOW) -> None:
        self.window = window
        # key -> failures suppressed since this burst's leading edge. Present
        # ONLY while a burst is open; popped by close().
        self._open: typing.Dict[typing.Any, int] = {}
        # key -> the ARMED summary task, so a second arm() for the same key can
        # never leave two summaries running. A key is disarmed by close(), which
        # the summary itself calls the moment it takes the count - BEFORE it
        # sends - so a failure landing during that send opens a fresh burst that
        # gets its own summary instead of falling into a silent hole.
        self._tasks: typing.Dict[typing.Any, asyncio.Task] = {}
        # Strong references to summaries still RUNNING, which is not the same set:
        # close() drops a task from _tasks while it is still suspended inside its
        # send, and a task with no reference is collectable mid-await (the trap
        # Music._refill_tasks guards against the same way). Each task removes
        # itself here on completion, so this is empty at rest too.
        self._live: typing.Set[asyncio.Task] = set()

    def record(self, key: typing.Any) -> bool:
        """Count one failure under ``key``; True iff it OPENS a new burst.

        True means the caller owns the announcement (post the per-track message,
        then :meth:`arm` the summary). False means a burst is already open and
        this failure is now nothing but a number in that burst's summary - the
        caller must send NOTHING, which is the entire point.
        """
        if key in self._open:
            self._open[key] += 1
            return False
        self._open[key] = 0
        return True

    def close(self, key: typing.Any) -> int:
        """End ``key``'s burst and return how many failures it swallowed.

        Zero means the leading edge was the only failure, and the caller must not
        post a summary for it - it was already announced with its title. Disarms
        the key as well, so the very next failure starts a clean burst.
        """
        self._tasks.pop(key, None)
        return self._open.pop(key, 0)

    def _disarm(self, key: typing.Any, task: asyncio.Task) -> None:
        """Drop ``key`` only if ``task`` is still the one armed under it.

        ``close`` normally disarms first, so this is the tidy-up for a summary
        that died before getting there - and the identity check keeps it from
        stealing a NEWER burst's task out of the map.
        """
        if self._tasks.get(key) is task:
            del self._tasks[key]

    def is_open(self, key: typing.Any) -> bool:
        """Whether a burst is currently open for ``key`` (tests / diagnostics)."""
        return key in self._open

    def arm(
        self, key: typing.Any, coro: typing.Awaitable[None]
    ) -> typing.Optional[asyncio.Task]:
        """Schedule ``coro`` as this burst's summary, keeping a strong reference.

        Returns the task (so a test can await it deterministically), or None when
        a summary is already armed for ``key`` - in which case ``coro`` is closed
        rather than left as a never-awaited coroutine. Both maps drop the task on
        completion, which is what keeps them empty at rest.
        """
        if key in self._tasks:
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return None
        task = asyncio.ensure_future(coro)
        self._tasks[key] = task
        self._live.add(task)
        task.add_done_callback(self._live.discard)
        task.add_done_callback(lambda done, k=key: self._disarm(k, done))
        return task

    def shutdown(self) -> None:
        """Cancel every summary, armed or mid-send, and drop all state (cog unload)."""
        for task in list(self._live) + list(self._tasks.values()):
            task.cancel()
        self._live.clear()
        self._tasks.clear()
        self._open.clear()
