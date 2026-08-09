"""Tests for RECURRING reminders (cogs/community/reminders*.py).

Three layers, matching where the risk actually lives:

* the pure recurrence math in ``reminders_store`` - interval parsing with its
  floor/ceiling, the drift-free next-occurrence formula and its closed-form
  fast-forward after an outage, and the re-validation that stops a corrupt
  ``repeat_seconds`` from spinning the dispatch loop;
* the dispatch path - the claim/reschedule ORDERING that decides whether a
  crash costs one delivery or the whole series, and the proof that a delivery
  failure can never double-fire nor silently end the series;
* inertness - every non-recurring row (one-shot reminders, tempban,
  vote_reminder and any other generic ``*_timer_complete`` event) must still
  take the exact single-statement path it took before this feature existed.
"""

import asyncio
import datetime
import json
import types

import discord
import pytest

from cogs.community import reminders_store as rem
from cogs.community.reminders import (
    _RECURRING_LOCK_CLASS,
    MAX_RECURRING_REMINDERS,
    Reminder,
    ReminderChannelGone,
    RemindersCard,
    format_interval,
)

UTC = datetime.timezone.utc
HOUR = 3600
DAY = 86400
WEEK = 604800


def _at(**kwargs):
    return datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC) + datetime.timedelta(
        **kwargs
    )


# ---------------------------------------------------------------------------
# parse_repeat: presets, the shared duration grammar, floor and ceiling
# ---------------------------------------------------------------------------


def test_parse_repeat_absent_is_a_one_shot_not_an_error():
    # The whole feature is opt-in: nothing supplied must be indistinguishable
    # from the pre-feature behaviour, and must NOT raise a validation error.
    assert rem.parse_repeat(None) == (None, None)
    assert rem.parse_repeat("") == (None, None)
    assert rem.parse_repeat("   ") == (None, None)


def test_parse_repeat_named_presets():
    assert rem.parse_repeat("hourly") == (HOUR, None)
    assert rem.parse_repeat("daily") == (DAY, None)
    assert rem.parse_repeat("weekly") == (WEEK, None)


def test_parse_repeat_is_case_and_whitespace_insensitive():
    assert rem.parse_repeat("  DAILY  ") == (DAY, None)
    assert rem.parse_repeat(" 2D ") == (2 * DAY, None)


def test_parse_repeat_uses_the_same_duration_grammar_as_the_initial_delay():
    # Same tools.time.ShortTime grammar /remind already parses "10m buy milk"
    # with - a member never learns a second syntax.
    assert rem.parse_repeat("2d") == (2 * DAY, None)
    assert rem.parse_repeat("12h") == (12 * HOUR, None)
    assert rem.parse_repeat("1w") == (WEEK, None)
    assert rem.parse_repeat("1h30m") == (HOUR + 1800, None)


def test_parse_repeat_calendar_units_are_anchored_to_a_fixed_reference():
    # Months/years have no fixed length, so the conversion to stored seconds is
    # anchored (2001-01-01): 1mon is always 31 days and 1y always 365 days,
    # deterministically, on every machine and at every call site.
    assert rem.parse_repeat("1mon") == (31 * DAY, None)
    assert rem.parse_repeat("1y") == (365 * DAY, None)


def test_parse_repeat_enforces_the_one_hour_floor():
    assert rem.parse_repeat("30m") == (None, "too_short")
    assert rem.parse_repeat("1s") == (None, "too_short")
    # Exactly the floor is accepted.
    assert rem.parse_repeat("1h") == (rem.MIN_REPEAT_SECONDS, None)


def test_parse_repeat_enforces_the_one_year_ceiling():
    assert rem.parse_repeat("366d") == (None, "too_long")
    assert rem.parse_repeat("2y") == (None, "too_long")
    # Exactly the ceiling is accepted.
    assert rem.parse_repeat("365d") == (rem.MAX_REPEAT_SECONDS, None)


def test_parse_repeat_rejects_nonsense_without_raising():
    assert rem.parse_repeat("whenever") == (None, "invalid")
    assert rem.parse_repeat("2 days-ish") == (None, "invalid")
    assert rem.parse_repeat("-1d") == (None, "invalid")


# ---------------------------------------------------------------------------
# recurrence_seconds: the gate that keeps the dispatch loop safe
# ---------------------------------------------------------------------------


def test_recurrence_seconds_reads_a_valid_interval():
    assert rem.recurrence_seconds({"repeat_seconds": DAY}) == DAY


def test_recurrence_seconds_absent_means_one_shot():
    assert rem.recurrence_seconds({}) is None
    assert rem.recurrence_seconds(None) is None
    assert rem.recurrence_seconds({"message": "hi"}) is None


def test_recurrence_seconds_rejects_corrupt_values():
    # A zero/negative interval would schedule the next occurrence in the past
    # forever - a hot spin in the dispatch loop. It must degrade to a one-shot,
    # and so must any non-numeric or out-of-range value.
    for bad in (0, -1, 60, "daily", True, None, [86400], 400 * DAY):
        assert rem.recurrence_seconds({"repeat_seconds": bad}) is None


# ---------------------------------------------------------------------------
# next_occurrence: drift-free scheduling and bounded fast-forward
# ---------------------------------------------------------------------------


def test_next_occurrence_on_time_is_exactly_one_interval_later():
    scheduled = _at()
    nxt, missed = rem.next_occurrence(scheduled, DAY, scheduled)
    assert nxt == scheduled + datetime.timedelta(days=1)
    assert missed == 0


def test_next_occurrence_does_not_drift_when_the_bot_fires_late():
    # Bot was down 30 minutes: the occurrence fires late ONCE, but the next one
    # is measured from the SCHEDULED time, so the series snaps back to its
    # original grid instead of sliding 30 minutes later every day.
    scheduled = _at()
    now = scheduled + datetime.timedelta(minutes=30)
    nxt, missed = rem.next_occurrence(scheduled, DAY, now)
    assert nxt == scheduled + datetime.timedelta(days=1)
    assert nxt != now + datetime.timedelta(days=1)  # the drifting answer
    assert missed == 0


def test_next_occurrence_fast_forwards_past_a_long_outage():
    # Down for 3h10m on an hourly series: occurrences at +1h, +2h, +3h were all
    # swallowed. Exactly those three are reported missed, and the next one is
    # the first slot strictly in the future.
    scheduled = _at()
    now = scheduled + datetime.timedelta(hours=3, minutes=10)
    nxt, missed = rem.next_occurrence(scheduled, HOUR, now)
    assert missed == 3
    assert nxt == scheduled + datetime.timedelta(hours=4)
    assert nxt > now


def test_next_occurrence_skips_a_slot_that_lands_exactly_on_now():
    # now sits exactly on the 3rd slot. Delivering it too would be a double
    # send in the same instant, so it counts as missed and we jump to slot 4.
    scheduled = _at()
    now = scheduled + datetime.timedelta(hours=3)
    nxt, missed = rem.next_occurrence(scheduled, HOUR, now)
    assert missed == 3
    assert nxt == scheduled + datetime.timedelta(hours=4)
    assert nxt > now


def test_next_occurrence_stays_on_the_original_grid_after_the_outage():
    scheduled = _at()
    now = scheduled + datetime.timedelta(hours=3, minutes=10)
    nxt, _missed = rem.next_occurrence(scheduled, HOUR, now)
    # Still an exact multiple of the interval away from the series origin.
    assert (nxt - scheduled).total_seconds() % HOUR == 0


def test_next_occurrence_is_closed_form_not_a_catch_up_loop():
    # Ten years of outage on an hourly series. A loop would run ~87600 times;
    # the closed form answers in one step, and the answer is still exact.
    scheduled = _at()
    now = scheduled + datetime.timedelta(days=3650)
    nxt, missed = rem.next_occurrence(scheduled, HOUR, now)
    assert missed == 3650 * 24
    assert nxt == scheduled + datetime.timedelta(days=3650, hours=1)
    assert nxt > now


def test_next_occurrence_tolerates_an_early_clock():
    # A now earlier than the scheduled time (clock skew) must never produce a
    # negative step count and schedule the series into the past.
    scheduled = _at()
    nxt, missed = rem.next_occurrence(scheduled, DAY, scheduled - datetime.timedelta(hours=2))
    assert nxt == scheduled + datetime.timedelta(days=1)
    assert missed == 0


# ---------------------------------------------------------------------------
# split_interval / format_interval / parse_extra
# ---------------------------------------------------------------------------


def test_split_interval_picks_the_coarsest_exact_unit():
    assert rem.split_interval(WEEK) == ("week", 1)
    assert rem.split_interval(2 * DAY) == ("day", 2)
    assert rem.split_interval(12 * HOUR) == ("hour", 12)
    assert rem.split_interval(5400) == ("minute", 90)
    assert rem.split_interval(3601) == ("second", 3601)


def test_format_interval_reads_naturally_for_one_and_many():
    assert format_interval(HOUR) == "every hour"
    assert format_interval(2 * DAY) == "every 2 days"
    assert format_interval(WEEK) == "every week"


def test_occurrence_number_never_raises_on_a_corrupt_counter():
    # It runs inside the reschedule transaction: a raise there would roll the
    # claim back and re-fire the same reminder every few seconds forever.
    assert rem.occurrence_number({"occurrence": 7}) == 7
    for bad in ({}, None, {"occurrence": "x"}, {"occurrence": 0}, {"occurrence": -3},
                {"occurrence": True}, {"occurrence": None}):
        assert rem.occurrence_number(bad) == 1


async def test_corrupt_occurrence_counter_does_not_stall_the_series():
    row = _due_row(extra=_recurring_extra(occurrence="banana"))
    result = await _run_one_dispatch(row)

    stored = json.loads(result.pool.inserts[0][2][3])
    assert stored["occurrence"] == 2  # recovered, series continues
    assert len(result.delivered) == 1


def test_parse_extra_handles_text_dict_and_null():
    assert rem.parse_extra('{"a": 1}') == {"a": 1}
    assert rem.parse_extra({"a": 1}) == {"a": 1}
    assert rem.parse_extra(None) == {}


# ---------------------------------------------------------------------------
# Creation: caps and stored shape
# ---------------------------------------------------------------------------


def _make_cog(pool):
    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    bot = types.SimpleNamespace(
        db_pool=pool, loop=types.SimpleNamespace(create_task=_create_task)
    )
    return Reminder(bot)


async def test_recurring_count_query_rides_the_existing_author_index(fake_pool):
    fake_pool.fetchval_return = 2
    cog = _make_cog(fake_pool)

    assert await cog._pending_recurring_count(77) == 2

    (_method, query, args), = [c for c in fake_pool.calls if c[0] == "fetchval"]
    # Equality on (event, author) - the leading columns of
    # timers_reminder_author_idx - then a recheck on the few rows that match.
    assert "event = 'reminder'" in query
    assert "extra->>'author_id' = $1" in query
    assert "extra->>'repeat_seconds' IS NOT NULL" in query
    assert args == ("77",)


async def test_recurring_limit_is_reached_only_at_the_cap(fake_pool):
    cog = _make_cog(fake_pool)

    fake_pool.fetchval_return = MAX_RECURRING_REMINDERS - 1
    assert await cog._recurring_limit_reached(1) is False

    fake_pool.fetchval_return = MAX_RECURRING_REMINDERS
    assert await cog._recurring_limit_reached(1) is True


async def test_one_shot_timer_extra_is_unchanged_by_this_feature(fake_pool):
    cog = _make_cog(fake_pool)

    await cog.create_timer(
        _at(),
        "reminder",
        author_id=1,
        channel_id=2,
        guild_id=3,
        message="hi",
    )

    (_method, _query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert json.loads(args[3]) == {
        "author_id": 1,
        "channel_id": 2,
        "guild_id": 3,
        "message": "hi",
    }


async def test_recurring_timer_stores_interval_and_first_occurrence(fake_pool):
    from cogs.community.reminders import recurrence_extra

    cog = _make_cog(fake_pool)

    await cog.create_timer(
        _at(),
        "reminder",
        author_id=1,
        channel_id=2,
        guild_id=3,
        message="hi",
        **recurrence_extra(DAY),
    )

    (_method, _query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    extra = json.loads(args[3])
    assert extra["repeat_seconds"] == DAY
    assert extra["occurrence"] == 1


def test_recurrence_extra_is_empty_for_a_one_shot():
    from cogs.community.reminders import recurrence_extra

    assert recurrence_extra(None) == {}


class _CapPool:
    """Records every statement of a guarded creation, and whether it was in a
    transaction - which is the only thing that makes the cap unraceable."""

    def __init__(self, count=0):
        self.count = count
        self.calls = []
        self.tx_depth = 0

    def _record(self, method, query, args):
        self.calls.append((method, query.lstrip(), args, self.tx_depth > 0))

    async def execute(self, query, *args):
        self._record("execute", query, args)
        return "SELECT 1"

    async def fetchval(self, query, *args):
        self._record("fetchval", query, args)
        return self.count

    async def fetchrow(self, query, *args):
        self._record("fetchrow", query, args)
        return {"id": 7}

    def acquire(self):
        return _TxContext(self, is_transaction=False)

    def transaction(self):
        return _TxContext(self, is_transaction=True)


async def test_the_recurring_cap_counts_and_inserts_under_one_lock():
    """The cap has to be unraceable in a way the pending cap does not: a
    one-shot overshoot fires once and drains, a recurring one re-inserts itself
    for ever and nothing re-checks the cap afterwards."""
    pool = _CapPool(count=MAX_RECURRING_REMINDERS - 1)
    cog = _make_cog(pool)

    row = await cog.create_reminder_timer(
        _at(),
        repeat_seconds=DAY,
        author_id=77,
        channel_id=2,
        guild_id=3,
        message="hi",
    )

    assert row == {"id": 7}
    queries = [c[1] for c in pool.calls]
    assert queries[0].startswith("SELECT pg_advisory_xact_lock")
    assert queries[1].startswith("SELECT COUNT(*) FROM timers")
    assert queries[2].startswith("INSERT INTO timers")
    # The lock is HELD across the count and the insert: all three in one
    # transaction, so a second submit waits instead of reading a stale count.
    assert all(call[3] for call in pool.calls)
    # Two-argument (namespaced) form, keyed by this member alone.
    assert pool.calls[0][2] == (_RECURRING_LOCK_CLASS, 77)
    # ...and the stored shape is the one the unguarded path wrote.
    assert json.loads(pool.calls[2][2][3]) == {
        "author_id": 77,
        "channel_id": 2,
        "guild_id": 3,
        "message": "hi",
        "repeat_seconds": DAY,
        "occurrence": 1,
    }


async def test_the_recurring_cap_refuses_inside_the_lock_and_writes_nothing():
    pool = _CapPool(count=MAX_RECURRING_REMINDERS)
    cog = _make_cog(pool)

    created = await cog.create_reminder_timer(
        _at(),
        repeat_seconds=DAY,
        author_id=77,
        channel_id=2,
        guild_id=3,
        message="hi",
    )

    assert created is None
    assert not any(c[1].startswith("INSERT") for c in pool.calls)


async def test_a_one_shot_creation_never_locks_nor_counts(fake_pool):
    """The guarded path is for series only: a one-shot is byte-identical to what
    it was before recurrence existed, and pays for none of this."""
    cog = _make_cog(fake_pool)

    await cog.create_reminder_timer(
        _at(), author_id=1, channel_id=2, guild_id=3, message="hi"
    )

    (_method, _query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert json.loads(args[3]) == {
        "author_id": 1,
        "channel_id": 2,
        "guild_id": 3,
        "message": "hi",
    }
    assert not any("pg_advisory" in call[1] for call in fake_pool.calls)
    assert not any("COUNT(*)" in call[1] for call in fake_pool.calls)


# ---------------------------------------------------------------------------
# Dispatch: claim + reschedule ordering
# ---------------------------------------------------------------------------


class _TxContext:
    def __init__(self, pool, *, is_transaction):
        self._pool = pool
        self._is_transaction = is_transaction

    async def __aenter__(self):
        if self._is_transaction:
            self._pool.tx_depth += 1
        return self._pool

    async def __aexit__(self, exc_type, exc, tb):
        if self._is_transaction:
            self._pool.tx_depth -= 1
            if exc_type is not None:
                self._pool.rolled_back = True
        return False


class _DispatchPool:
    """Serves exactly one due timer, then nothing; records every statement.

    Each recorded call carries whether it ran inside a transaction, so a test
    can prove the claim and the reschedule are atomic with respect to each
    other. The claim (``DELETE ... RETURNING``) returns the row when the claim
    is won and None when a cancel or another worker got there first.
    """

    def __init__(self, row, *, claim_won=True):
        self.row = row
        self._served = False
        self._claim_won = claim_won
        self.calls = []
        self.tx_depth = 0
        self.rolled_back = False

    def _record(self, method, query, args):
        self.calls.append((method, query.lstrip(), args, self.tx_depth > 0))

    async def fetchrow(self, query, *args):
        if query.lstrip().startswith("SELECT"):
            if self._served:
                return None
            self._served = True
            return self.row
        self._record("fetchrow", query, args)
        return self.row if self._claim_won else None

    async def execute(self, query, *args):
        self._record("execute", query, args)
        return "INSERT 0 1"

    async def fetchval(self, query, *args):
        self._record("fetchval", query, args)
        if query.lstrip().startswith("INSERT INTO timers"):
            return 99  # the id of the next occurrence
        return 0

    async def fetch(self, query, *args):
        self._record("fetch", query, args)
        return []

    def acquire(self):
        return _TxContext(self, is_transaction=False)

    def transaction(self):
        return _TxContext(self, is_transaction=True)

    # -- assertions helpers --------------------------------------------------
    @property
    def claims(self):
        return [c for c in self.calls if c[1].startswith("DELETE FROM timers")]

    @property
    def inserts(self):
        return [c for c in self.calls if c[1].startswith("INSERT INTO timers")]


class _DispatchBot:
    def __init__(self, pool):
        self.db_pool = pool
        self.loop = types.SimpleNamespace(
            create_task=lambda coro: (
                coro.close(),
                types.SimpleNamespace(cancel=lambda: None),
            )[1]
        )
        self._closed = False
        self.dispatched = []

    async def wait_until_ready(self):
        return None

    def is_closed(self):
        return self._closed

    def dispatch(self, event, *args):
        self.dispatched.append((event, args))

    def get_channel(self, channel_id):
        return None


def _due_row(event="reminder", *, extra=None, late_seconds=1, timer_id=1):
    now = datetime.datetime.now(UTC)
    return {
        "id": timer_id,
        "event": event,
        "expires": now - datetime.timedelta(seconds=late_seconds),
        "created": now - datetime.timedelta(days=1),
        "attempts": 0,
        "extra": extra if extra is not None else {"author_id": 5, "channel_id": 9},
    }


def _recurring_extra(seconds=DAY, occurrence=1, **overrides):
    extra = {
        "author_id": 5,
        "channel_id": 9,
        "guild_id": 3,
        "message": "stretch",
        "repeat_seconds": seconds,
        "occurrence": occurrence,
    }
    extra.update(overrides)
    return extra


async def _run_one_dispatch(row, *, claim_won=True, delivery_error=None):
    pool = _DispatchPool(row, claim_won=claim_won)
    bot = _DispatchBot(pool)
    cog = Reminder(bot)
    delivered = []
    statements_at_delivery = []

    async def _spy_call_timer(r):
        statements_at_delivery.append(list(pool.calls))
        delivered.append(r)
        if delivery_error is not None:
            raise delivery_error

    cog.call_timer = _spy_call_timer

    task = asyncio.ensure_future(cog.dispatch_timers())
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return types.SimpleNamespace(
        pool=pool,
        delivered=delivered,
        statements_at_delivery=statements_at_delivery,
    )


async def test_recurring_reminder_reschedules_before_delivering():
    # The crash-safety choice, asserted: the next occurrence is committed BEFORE
    # the message is sent. Crashing after the INSERT costs one delivery; the
    # opposite order would cost the whole series, silently.
    result = await _run_one_dispatch(_due_row(extra=_recurring_extra()))

    assert len(result.delivered) == 1
    assert len(result.pool.inserts) == 1
    before = result.statements_at_delivery[0]
    assert any(c[1].startswith("DELETE FROM timers") for c in before)
    assert any(c[1].startswith("INSERT INTO timers") for c in before)


async def test_claim_and_reschedule_are_one_transaction():
    # The only window that could kill a series is a crash between the claim and
    # the reschedule; both statements are in the same transaction, so it does
    # not exist - either both commit or the row stays pending and fires again.
    result = await _run_one_dispatch(_due_row(extra=_recurring_extra()))

    claim = result.pool.claims[0]
    insert = result.pool.inserts[0]
    assert claim[3] is True  # ran inside a transaction
    assert insert[3] is True


def _unwinds(pool):
    """The DELETEs that end a series (the next occurrence's id, never the
    claim's)."""
    return [
        call
        for call in pool.calls
        if call[1].startswith("DELETE FROM timers WHERE id = $1 AND claimed_at")
        and call[2] == (99,)
    ]


async def test_a_recurring_reminder_into_a_deleted_channel_ends_the_series():
    """The one thing the reschedule-before-deliver ordering has to pay back.

    The next occurrence is already COMMITTED when the delivery discovers the
    channel is gone. Left alone it would re-fire into a channel that can never
    exist again - hourly, for ever, with nothing to stop it, since a series
    re-inserts itself and no other timer kind survives this path.
    """
    result = await _run_one_dispatch(
        _due_row(extra=_recurring_extra()), delivery_error=ReminderChannelGone(9)
    )

    assert len(result.pool.inserts) == 1  # the ordering is unchanged...
    assert len(_unwinds(result.pool)) == 1  # ...and then unwound


async def test_a_live_recurring_delivery_never_unwinds_the_series():
    result = await _run_one_dispatch(_due_row(extra=_recurring_extra()))

    assert len(result.pool.inserts) == 1
    assert _unwinds(result.pool) == []


async def test_an_ordinary_delivery_failure_still_leaves_the_series_alone():
    """Only a dead CHANNEL ends a series. Any other failure (a permission
    hiccup, a network blip) costs one delivery and the series lives - the
    at-most-once trade this path always made."""
    result = await _run_one_dispatch(
        _due_row(extra=_recurring_extra()), delivery_error=RuntimeError("boom")
    )

    assert len(result.pool.inserts) == 1
    assert _unwinds(result.pool) == []


async def test_a_one_shot_into_a_deleted_channel_is_simply_dropped():
    """Unchanged behaviour: its row died with the claim, so there is nothing to
    unwind and nothing extra to run."""
    result = await _run_one_dispatch(_due_row(), delivery_error=ReminderChannelGone(9))

    assert result.pool.inserts == []
    assert len(result.pool.claims) == 1  # the claim, and nothing after it


async def test_recurring_claim_is_still_a_delete_so_it_cannot_double_fire():
    result = await _run_one_dispatch(_due_row(extra=_recurring_extra()))

    claim_query = result.pool.claims[0][1]
    assert claim_query.startswith("DELETE FROM timers")
    assert "claimed_at IS NULL" in claim_query
    assert "RETURNING" in claim_query


async def test_recurring_delivery_failure_loses_one_send_but_keeps_the_series():
    result = await _run_one_dispatch(
        _due_row(extra=_recurring_extra()),
        delivery_error=RuntimeError("Discord down"),
    )

    assert len(result.delivered) == 1  # fired once, never retried, never doubled
    assert len(result.pool.inserts) == 1  # the next occurrence survives the crash


async def test_lost_claim_never_creates_an_orphan_next_occurrence():
    # A cancel won the row: nothing is delivered AND nothing is rescheduled,
    # otherwise cancelling a series would resurrect it.
    result = await _run_one_dispatch(
        _due_row(extra=_recurring_extra()), claim_won=False
    )

    assert result.delivered == []
    assert result.pool.inserts == []


async def test_next_row_is_scheduled_one_interval_after_the_scheduled_time():
    row = _due_row(extra=_recurring_extra(seconds=HOUR), late_seconds=120)
    result = await _run_one_dispatch(row)

    _method, _query, args, _in_tx = result.pool.inserts[0]
    assert args[0] == "reminder"
    # Anchored to expires, NOT to now: exactly one interval after the slot that
    # just fired, even though delivery happened two minutes late.
    assert args[1] == row["expires"] + datetime.timedelta(seconds=HOUR)


async def test_next_row_carries_the_recurrence_and_advances_the_counter():
    row = _due_row(extra=_recurring_extra(occurrence=4))
    result = await _run_one_dispatch(row)

    _method, _query, args, _in_tx = result.pool.inserts[0]
    stored = json.loads(args[3])
    assert stored["repeat_seconds"] == DAY
    assert stored["occurrence"] == 5
    assert stored["message"] == "stretch"
    assert stored["channel_id"] == 9


async def test_delivery_only_keys_are_never_persisted():
    # 'missed' and 'next_at' exist for the message text alone; writing them into
    # the next row would make them stick to the series forever.
    row = _due_row(extra=_recurring_extra(seconds=HOUR), late_seconds=4 * HOUR)
    result = await _run_one_dispatch(row)

    stored = json.loads(result.pool.inserts[0][2][3])
    assert "missed" not in stored
    assert "next_at" not in stored


async def test_outage_fast_forwards_the_counter_by_the_skipped_slots():
    # 3h10m late on an hourly series: 3 slots skipped, so the ordinal of the
    # next slot is current + 1 + 3.
    row = _due_row(
        extra=_recurring_extra(seconds=HOUR, occurrence=10),
        late_seconds=3 * HOUR + 600,
    )
    result = await _run_one_dispatch(row)

    stored = json.loads(result.pool.inserts[0][2][3])
    assert stored["occurrence"] == 14
    assert result.delivered[0]["extra"]["missed"] == 3


async def test_next_occurrence_written_after_an_outage_is_in_the_future():
    row = _due_row(extra=_recurring_extra(seconds=HOUR), late_seconds=50 * HOUR)
    result = await _run_one_dispatch(row)

    scheduled_next = result.pool.inserts[0][2][1]
    assert scheduled_next > datetime.datetime.now(UTC)


# ---------------------------------------------------------------------------
# Inertness for every other timer event
# ---------------------------------------------------------------------------


async def test_one_shot_reminder_keeps_the_single_statement_path():
    result = await _run_one_dispatch(_due_row(extra={"author_id": 5, "channel_id": 9}))

    assert len(result.delivered) == 1
    assert result.pool.inserts == []  # nothing rescheduled
    claim = result.pool.claims[0]
    assert claim[1].startswith("DELETE FROM timers")
    assert claim[3] is False  # NOT wrapped in a transaction - unchanged path


async def test_tempban_path_is_untouched_by_recurrence():
    # A tempban carries no author_id/repeat_seconds and takes the durable
    # claim -> deliver -> delete path; recurrence must not even be consulted.
    result = await _run_one_dispatch(
        _due_row("tempban", extra={"guild_id": 10, "user_id": 20})
    )

    assert len(result.delivered) == 1
    assert result.pool.inserts == []
    # The durable path claims with an UPDATE and only deletes AFTER a successful
    # delivery - it never takes the at-most-once DELETE-as-claim.
    assert not any("RETURNING" in c[1] for c in result.pool.claims)
    assert any(
        c[1].startswith("UPDATE timers SET claimed_at = now()")
        for c in result.pool.calls
    )
    assert all(c[3] is False for c in result.pool.calls)  # no transaction wrapper


async def test_generic_events_take_the_unchanged_at_most_once_path():
    # vote_reminder (cogs/community/votes.py), announcement and temprole all go
    # through the generic dispatch. None of them may gain a reschedule.
    for event in ("vote_reminder", "announcement", "temprole"):
        result = await _run_one_dispatch(
            _due_row(event, extra={"user_id": 20, "voted_at": "2026-01-01"})
        )
        assert len(result.delivered) == 1, event
        assert result.pool.inserts == [], event
        assert result.pool.claims[0][3] is False, event


async def test_a_reminder_shaped_event_name_is_not_enough_to_recur():
    # Guard against an event whose extra happens to carry repeat_seconds but
    # which is not a reminder: only event == 'reminder' may recur.
    result = await _run_one_dispatch(
        _due_row("vote_reminder", extra={"user_id": 1, "repeat_seconds": DAY})
    )

    assert result.pool.inserts == []


async def test_corrupt_repeat_seconds_degrades_to_a_one_shot():
    # A hand-edited or legacy row with a nonsense interval must fire once and
    # stop, never spin the loop rescheduling itself in the past.
    for bad in (0, -5, 60, "daily"):
        result = await _run_one_dispatch(
            _due_row(extra=_recurring_extra(seconds=bad))
        )
        assert len(result.delivered) == 1, bad
        assert result.pool.inserts == [], bad


# ---------------------------------------------------------------------------
# Delivered message
# ---------------------------------------------------------------------------


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


def _delivery_cog(channel):
    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    bot = types.SimpleNamespace(
        db_pool=None,
        loop=types.SimpleNamespace(create_task=_create_task),
        get_channel=lambda _cid: channel,
    )
    return Reminder(bot)


def _delivery_row(extra):
    return {
        "id": 1,
        "event": "reminder",
        "created": datetime.datetime.now(UTC) - datetime.timedelta(days=1),
        "extra": extra,
    }


async def test_one_shot_delivery_text_is_unchanged():
    channel = _Channel()
    cog = _delivery_cog(channel)

    await cog.call_timer(
        _delivery_row({"author_id": 5, "channel_id": 9, "message": "milk"})
    )

    assert len(channel.sent) == 1
    assert channel.sent[0].endswith(": milk")
    assert "-#" not in channel.sent[0]  # no footer at all


async def test_recurring_delivery_announces_the_next_occurrence():
    channel = _Channel()
    cog = _delivery_cog(channel)
    next_at = datetime.datetime.now(UTC) + datetime.timedelta(days=1)

    await cog.call_timer(
        _delivery_row(
            _recurring_extra(missed=0, next_at=next_at.isoformat(), message="milk")
        )
    )

    body = channel.sent[0]
    assert "every day" in body
    assert "missed" not in body  # nothing was skipped


async def test_recurring_delivery_reports_missed_occurrences():
    channel = _Channel()
    cog = _delivery_cog(channel)
    next_at = datetime.datetime.now(UTC) + datetime.timedelta(hours=1)

    await cog.call_timer(
        _delivery_row(
            _recurring_extra(
                seconds=HOUR, missed=3, next_at=next_at.isoformat(), message="milk"
            )
        )
    )

    body = channel.sent[0]
    assert "3 occurrences were missed" in body
    assert "every hour" in body


async def test_missed_note_is_singular_for_one_occurrence():
    channel = _Channel()
    cog = _delivery_cog(channel)

    await cog.call_timer(
        _delivery_row(_recurring_extra(seconds=HOUR, missed=1, message="milk"))
    )

    assert "1 occurrence was missed" in channel.sent[0]


async def test_a_corrupt_next_at_never_breaks_the_delivery():
    channel = _Channel()
    cog = _delivery_cog(channel)

    await cog.call_timer(
        _delivery_row(_recurring_extra(next_at="not-a-date", message="milk"))
    )

    assert len(channel.sent) == 1  # delivered anyway, just without the footer


async def test_a_corrupt_missed_count_never_breaks_the_delivery():
    """The same total parse as `next_at`, for the same reason: by the time the
    footer is built the row is already deleted, so a ValueError here would cost
    the delivery outright. Unreachable while the key stays in-process only -
    which is exactly when an asymmetry is cheap to remove."""
    channel = _Channel()
    cog = _delivery_cog(channel)
    next_at = datetime.datetime.now(UTC) + datetime.timedelta(days=1)

    await cog.call_timer(
        _delivery_row(
            _recurring_extra(
                missed="banana", next_at=next_at.isoformat(), message="milk"
            )
        )
    )

    body = channel.sent[0]
    assert "missed" not in body  # the corrupt count is simply not spoken...
    assert "every day" in body  # ...and the rest of the footer still renders


async def test_a_deleted_channel_is_reported_as_such_not_swallowed():
    """call_timer RAISES on a 404 so the recurring path can unwind its already
    committed next occurrence; a swallowed return would be silently ignored."""

    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    async def _fetch_channel(_channel_id):
        raise discord.NotFound(
            types.SimpleNamespace(status=404, reason="Not Found"), "unknown channel"
        )

    bot = types.SimpleNamespace(
        db_pool=None,
        loop=types.SimpleNamespace(create_task=_create_task),
        get_channel=lambda _cid: None,
        fetch_channel=_fetch_channel,
    )
    cog = Reminder(bot)

    with pytest.raises(ReminderChannelGone):
        await cog.call_timer(_delivery_row(_recurring_extra(message="milk")))


# ---------------------------------------------------------------------------
# Listing and the card
# ---------------------------------------------------------------------------


async def test_list_surfaces_the_recurrence_for_the_card(fake_pool):
    fake_pool.fetch_return = [
        {
            "id": 1,
            "expires": _at(),
            "extra": {"author_id": 1, "channel_id": 9, "message": "a"},
        },
        {
            "id": 2,
            "expires": _at(hours=1),
            "extra": json.dumps(
                {
                    "author_id": 1,
                    "channel_id": 9,
                    "message": "b",
                    "repeat_seconds": DAY,
                }
            ),
        },
    ]
    cog = _make_cog(fake_pool)

    reminders_list, _capped = await cog.list_pending_reminders(1)

    assert reminders_list[0]["repeat_seconds"] is None
    assert reminders_list[1]["repeat_seconds"] == DAY


def _card_lines(view):
    container = view.children[0]
    texts = [
        c.content
        for c in container.children
        if isinstance(c, discord.ui.TextDisplay)
    ]
    return "\n".join(texts)


def _listed(**overrides):
    entry = {
        "id": 1,
        "expires": _at(),
        "channel_id": 9,
        "message": "stretch",
        "event": "reminder",
        "repeat_seconds": None,
    }
    entry.update(overrides)
    return entry


def test_card_marks_a_recurring_reminder_with_glyph_and_interval():
    view = RemindersCard(None, 1, [_listed(repeat_seconds=2 * DAY)], False)
    body = _card_lines(view)
    assert rem.REPEAT_GLYPH in body
    assert "every 2 days" in body
    assert "in <#9>" in body  # the channel note is still there


def test_card_line_for_a_one_shot_is_byte_identical_to_before():
    view = RemindersCard(None, 1, [_listed()], False)
    body = _card_lines(view)
    expected = "{when} - stretch\n-# in <#9>".format(
        when=discord.utils.format_dt(_at(), "R")
    )
    assert expected in body
    assert rem.REPEAT_GLYPH not in body


def test_card_line_without_a_channel_has_no_subtext_when_one_shot():
    view = RemindersCard(None, 1, [_listed(channel_id=None)], False)
    body = _card_lines(view)
    assert "-# in <#" not in body


def test_card_line_without_a_channel_still_marks_recurrence():
    view = RemindersCard(
        None, 1, [_listed(channel_id=None, repeat_seconds=WEEK)], False
    )
    body = _card_lines(view)
    assert rem.REPEAT_GLYPH in body
    assert "every week" in body


# ---------------------------------------------------------------------------
# The modal surface (also the ONLY recurrence entry point for prefix users)
# ---------------------------------------------------------------------------


class _ModalCog:
    def __init__(self, *, pending=0, limit_reached=False):
        self.created = []
        self._pending = pending
        self._limit_reached = limit_reached
        self.limit_checks = 0

    async def get_tzinfo(self, _user_id):
        return UTC

    async def _pending_reminder_count(self, _user_id):
        return self._pending

    async def create_reminder_timer(self, when, *, repeat_seconds=None, **extra):
        # Models the real seam: the recurring cap is enforced INSIDE the
        # creation (one locked count-and-insert), so a refusal is a None return
        # and a one-shot never consults the cap at all.
        from cogs.community.reminders import recurrence_extra

        if repeat_seconds is not None:
            self.limit_checks += 1
            if self._limit_reached:
                return None
        self.created.append(
            (when, "reminder", {**extra, **recurrence_extra(repeat_seconds)})
        )
        return {"id": 1}


def _modal(cog, repeat_field, *, when="10m", message="stretch", prefill=None):
    from cogs.community.reminders import RemindModal

    modal = RemindModal(cog, 9, 5, 3, repeat=prefill)
    modal.when_input._value = when
    modal.message_input._value = message
    modal.repeat_input._value = repeat_field
    return modal


def _interaction(make_interaction):
    interaction = make_interaction(user_id=5)
    interaction.created_at = datetime.datetime.now(UTC)
    return interaction


async def test_modal_blank_repeat_creates_the_same_one_shot_as_before(
    make_interaction,
):
    cog = _ModalCog()
    interaction = _interaction(make_interaction)

    await _modal(cog, "").on_submit(interaction)

    (_when, event, extra), = cog.created
    assert event == "reminder"
    assert "repeat_seconds" not in extra
    assert "occurrence" not in extra
    assert cog.limit_checks == 0  # the recurring cap is not even consulted


async def test_modal_repeat_field_creates_a_series(make_interaction):
    cog = _ModalCog()
    interaction = _interaction(make_interaction)

    await _modal(cog, "daily").on_submit(interaction)

    (_when, _event, extra), = cog.created
    assert extra["repeat_seconds"] == DAY
    assert extra["occurrence"] == 1
    assert "every day" in interaction.sent[0][0][0]


async def test_modal_rejects_a_bad_interval_before_touching_the_db(
    make_interaction,
):
    cog = _ModalCog()
    interaction = _interaction(make_interaction)

    await _modal(cog, "5m").on_submit(interaction)

    assert cog.created == []
    assert "once an hour" in interaction.sent[0][0][0]


async def test_modal_enforces_the_recurring_cap(make_interaction):
    cog = _ModalCog(limit_reached=True)
    interaction = _interaction(make_interaction)

    await _modal(cog, "weekly").on_submit(interaction)

    assert cog.created == []
    assert str(MAX_RECURRING_REMINDERS) in interaction.sent[0][0][0]


def test_modal_prefills_the_repeat_picked_on_the_slash_command():
    cog = _ModalCog()
    modal = _modal(cog, "", prefill="daily")
    assert modal.repeat_input.default == "daily"


def test_launcher_view_forwards_the_repeat_to_the_modal():
    from cogs.community.reminders import RemindLauncherView

    view = RemindLauncherView(_ModalCog(), 5, 9, 3, repeat="weekly")
    assert view.repeat == "weekly"


# ---------------------------------------------------------------------------
# The /remind command surface
# ---------------------------------------------------------------------------


class _CommandCtx:
    def __init__(self, *, interaction=None):
        self.sends = []
        self.author = types.SimpleNamespace(id=5)
        self.guild = types.SimpleNamespace(id=3)
        self.channel = types.SimpleNamespace(id=9)
        self.interaction = interaction

    async def send(self, *args, **kwargs):
        self.sends.append(args[0] if args else kwargs.get("content"))


async def test_remind_rejects_a_bad_repeat_before_parsing_anything(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _CommandCtx()

    await cog.remind.callback(cog, ctx, when="10m stretch", repeat="soon")

    assert "repeat interval" in ctx.sends[0]
    # Nothing was written and no cap query ran: the refusal is the first gate.
    assert fake_pool.calls == []


async def test_remind_with_a_repeat_but_no_time_prefills_the_form(fake_pool):
    from cogs.community.reminders import RemindModal

    cog = _make_cog(fake_pool)
    modals = []

    async def _send_modal(modal):
        modals.append(modal)

    ctx = _CommandCtx(
        interaction=types.SimpleNamespace(
            response=types.SimpleNamespace(send_modal=_send_modal)
        )
    )

    await cog.remind.callback(cog, ctx, repeat="weekly")

    assert isinstance(modals[0], RemindModal)
    assert modals[0].repeat_default == "weekly"  # the option is not lost


def test_recurring_reminders_count_against_the_list_cap_like_any_other():
    # Nothing special: the card slices to REMINDER_LIST_CAP regardless of type.
    entries = [_listed(id=i, repeat_seconds=DAY) for i in range(rem.REMINDER_PAGE_SIZE + 3)]
    view = RemindersCard(None, 1, entries, False)
    container = view.children[0]
    selects = [
        child
        for row in container.children
        if isinstance(row, discord.ui.ActionRow)
        for child in row.children
        if isinstance(child, discord.ui.Select)
    ]
    assert len(selects[0].options) == rem.REMINDER_PAGE_SIZE
