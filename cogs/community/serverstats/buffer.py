"""Purpose: the pure, in-memory side of the collectors - bounded counters, UTC
day arithmetic, and the array payload the flush hands to asyncpg.

Nothing here imports discord or touches the DB, so every rule that matters (the
key cap, the day a counter belongs to, what a drain returns) is testable as
plain data.

PRIVACY: the only things this module can hold are ids of GUILDS and CHANNELS
plus integer counts. No user id, no message content, no author, ever - that is
the whole point of the aggregate design and it is enforced by the shape of the
keys below.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass

# One UTC day in seconds. The buffer speaks in "days since the epoch" (a plain
# int) rather than dates: deriving it on the hot path costs one float divide
# instead of building a datetime, and int(t // 86400) IS the UTC day because
# time.time() is defined as seconds since 1970-01-01T00:00:00Z.
SECONDS_PER_DAY = 86400
_EPOCH = datetime.date(1970, 1, 1)

# Hard ceiling on distinct (guild, channel, day) keys held between two flushes.
# 16384 keys is ~40x the design's realistic peak (1000 active guilds each with a
# handful of channels busy inside the same 5-minute window), so legitimate load
# never reaches it; only a pathological fan-out (or a wedged flush) does, and
# then keys are DROPPED and counted instead of growing the process without
# bound. A dropped key costs a few uncounted messages in one channel-day - the
# deliberate trade against unbounded memory.
MESSAGE_KEY_CAP = 16384

# Same backstop for the (guild, day) join/leave keys. This one is naturally
# bounded by the guild count already, so the cap is only a leak backstop.
DAY_KEY_CAP = 8192


def utc_day(now=None):
    """The UTC day as a plain int (days since 1970-01-01); ``now`` is a unix ts."""
    return int((time.time() if now is None else now) // SECONDS_PER_DAY)


def day_to_date(day):
    """Turn a day int back into the ``DATE`` value the flush writes."""
    return _EPOCH + datetime.timedelta(days=day)


@dataclass
class DrainedStats:
    """One flush's worth of counters, detached from the live buffer.

    ``messages`` is a list of ``(guild_id, channel_id, day, count)`` and ``days``
    a list of ``(guild_id, day, joins, leaves)``. Each row carries its OWN day,
    so a flush that straddles midnight UTC writes each counter onto the day it
    was actually collected on.
    """

    messages: list
    days: list
    dropped_messages: int = 0
    dropped_days: int = 0

    @property
    def is_empty(self):
        return not self.messages and not self.days


class StatsBuffer:
    """Bounded in-memory counters for messages and joins/leaves, keyed by UTC day.

    Every mutator is synchronous, O(1) and allocation-light (one tuple key on
    first sight of a key, nothing afterwards): the listeners call these from the
    hot gateway path and must never await.
    """

    __slots__ = (
        "_messages",
        "_days",
        "_message_cap",
        "_day_cap",
        "_dropped_messages",
        "_dropped_days",
    )

    def __init__(self, message_cap=MESSAGE_KEY_CAP, day_cap=DAY_KEY_CAP):
        # (guild_id, channel_id, day) -> message count
        self._messages: dict[tuple[int, int, int], int] = {}
        # (guild_id, day) -> [joins, leaves]
        self._days: dict[tuple[int, int], list[int]] = {}
        self._message_cap = message_cap
        self._day_cap = day_cap
        self._dropped_messages = 0
        self._dropped_days = 0

    # ------------------------------------------------------------------
    # Hot path (called from the listeners; never awaits, never allocates
    # beyond the one tuple key)
    # ------------------------------------------------------------------
    def record_message(self, guild_id, channel_id, day, count=1):
        """Count ``count`` message(s) in a channel-day. False == dropped at the cap."""
        key = (guild_id, channel_id, day)
        current = self._messages.get(key)
        if current is None:
            if len(self._messages) >= self._message_cap:
                self._dropped_messages += 1
                return False
            self._messages[key] = count
            return True
        self._messages[key] = current + count
        return True

    def record_join(self, guild_id, day, count=1):
        """Count ``count`` member join(s). False == dropped at the cap."""
        return self._bump_day(guild_id, day, 0, count)

    def record_leave(self, guild_id, day, count=1):
        """Count ``count`` member departure(s). False == dropped at the cap."""
        return self._bump_day(guild_id, day, 1, count)

    def _bump_day(self, guild_id, day, slot, count):
        key = (guild_id, day)
        counts = self._days.get(key)
        if counts is None:
            if len(self._days) >= self._day_cap:
                self._dropped_days += 1
                return False
            counts = [0, 0]
            self._days[key] = counts
        counts[slot] += count
        return True

    # ------------------------------------------------------------------
    # Flush path
    # ------------------------------------------------------------------
    @property
    def is_empty(self):
        return not self._messages and not self._days

    @property
    def key_count(self):
        """Live key count, for the instrumentation line in the flush log."""
        return len(self._messages) + len(self._days)

    def drain(self):
        """Detach everything collected so far and reset the live counters.

        The buffer is cleared BEFORE the write so the listeners keep counting
        into a fresh generation while the flush is in flight; a failed write
        hands the result back via :meth:`restore`.
        """
        drained = DrainedStats(
            messages=[
                (guild_id, channel_id, day, count)
                for (guild_id, channel_id, day), count in self._messages.items()
            ],
            days=[
                (guild_id, day, counts[0], counts[1])
                for (guild_id, day), counts in self._days.items()
            ],
            dropped_messages=self._dropped_messages,
            dropped_days=self._dropped_days,
        )
        self._messages = {}
        self._days = {}
        self._dropped_messages = 0
        self._dropped_days = 0
        return drained

    def restore(self, drained):
        """Fold a failed flush's counters back in, still respecting both caps.

        A DB blip must not silently eat 5 minutes of counters, but it must not
        be able to grow the buffer without bound either: the restore goes
        through the same capped mutators, so at worst the oldest keys are
        dropped and counted like any other overflow.

        The drain's OWN overflow tallies are deliberately NOT folded back in.
        They were already reported (and logged) by the flush that drained them,
        so re-adding them here would make a multi-tick outage report the same
        drops again on every retry - a drop RATE that is not happening. Only
        drops caused by this restore itself are counted, by the mutators above.
        """
        for guild_id, channel_id, day, count in drained.messages:
            self.record_message(guild_id, channel_id, day, count)
        for guild_id, day, joins, leaves in drained.days:
            if joins:
                self.record_join(guild_id, day, joins)
            if leaves:
                self.record_leave(guild_id, day, leaves)


def build_flush_payload(drained):
    """Turn a drain into the eight parallel arrays the batched upsert unnests.

    Returns ``(guild_ids, channel_ids, message_days, counts, day_guild_ids,
    day_days, joins, leaves)``. Day ints become real ``date`` objects here (the
    single conversion point), and the ordering is stable per array because each
    is built in one pass over the same list.
    """
    message_guild_ids = []
    message_channel_ids = []
    message_days = []
    message_counts = []
    for guild_id, channel_id, day, count in drained.messages:
        message_guild_ids.append(guild_id)
        message_channel_ids.append(channel_id)
        message_days.append(day_to_date(day))
        message_counts.append(count)

    day_guild_ids = []
    day_days = []
    joins = []
    leaves = []
    for guild_id, day, joined, left in drained.days:
        day_guild_ids.append(guild_id)
        day_days.append(day_to_date(day))
        joins.append(joined)
        leaves.append(left)

    return (
        message_guild_ids,
        message_channel_ids,
        message_days,
        message_counts,
        day_guild_ids,
        day_days,
        joins,
        leaves,
    )
