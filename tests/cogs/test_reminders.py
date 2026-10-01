"""Tests for the reminders listing/cancel surface (cogs/community/reminders.py).

Covers three things the pure-logic suite (tests/cogs/test_reminders_store.py) cannot:

* :class:`RemindersCard` rendering + navigation + confirm-less cancel (the
  Components V2 card), driven against the FakeInteraction/fake-cog stand-ins.
* The cog's DB seams: ``list_pending_reminders`` (author + type scoping,
  str/dict ``extra`` parsing, the +1 overflow -> ``capped``) and
  ``cancel_reminder`` (scoped DELETE, dispatch-loop wake, existed/not-existed).
* The dispatch-loop race-safety guard: a timer whose conditional UPDATE returns
  no row (another worker already claimed it) is NOT fired.
"""

import asyncio
import datetime
import json
import types

import discord

from cogs.community import reminders as reminders_mod
from cogs.community import reminders_store as rem
from cogs.community.reminders import Reminder, RemindersCard, timer_retry_delay

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_reminder_bot(fake_pool):
    """A bot stand-in whose loop.create_task neutralises the cog's dispatch task.

    ``Reminder.__init__`` spawns ``dispatch_timers`` via ``bot.loop.create_task``;
    the tests never want that background loop running, so create_task closes the
    coroutine (no "never awaited" warning) and hands back a dummy task object.
    """

    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    return types.SimpleNamespace(
        db_pool=fake_pool,
        loop=types.SimpleNamespace(create_task=_create_task),
    )


def _make_cog(fake_pool):
    return Reminder(_make_reminder_bot(fake_pool))


def _future(minutes):
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=minutes
    )


def _reminders(n, channel_id=100):
    """N parsed reminder dicts, soonest first, in the cog's internal shape."""
    return [
        {
            "id": i,
            "expires": _future(i + 1),
            "channel_id": channel_id,
            "message": f"reminder text {i}",
            "event": "reminder",
        }
        for i in range(1, n + 1)
    ]


def _container(view):
    return view.children[0]


def _action_rows(container):
    return [c for c in container.children if isinstance(c, discord.ui.ActionRow)]


def _selects(container):
    out = []
    for row in _action_rows(container):
        out.extend(c for c in row.children if isinstance(c, discord.ui.Select))
    return out


def _buttons(container):
    out = []
    for row in _action_rows(container):
        out.extend(c for c in row.children if isinstance(c, discord.ui.Button))
    return out


def _texts(container):
    return [
        c.content
        for c in container.children
        if isinstance(c, discord.ui.TextDisplay)
    ]


# ---------------------------------------------------------------------------
# RemindersCard rendering
# ---------------------------------------------------------------------------


def test_card_empty_state_has_no_select_or_pager():
    view = RemindersCard(None, 1, [], False)
    container = _container(view)
    assert _selects(container) == []
    assert _buttons(container) == []
    assert any("no reminders" in t.lower() for t in _texts(container))


def test_card_single_page_has_select_but_no_pager():
    view = RemindersCard(None, 1, _reminders(5), False)
    container = _container(view)
    assert len(_selects(container)) == 1
    # The cancel select lists every reminder on the (only) page.
    assert len(_selects(container)[0].options) == 5
    assert _buttons(container) == []  # no pager on a single page


def test_card_multipage_has_pager_and_page_sized_select():
    view = RemindersCard(None, 1, _reminders(rem.REMINDER_PAGE_SIZE + 4), False)
    container = _container(view)
    buttons = _buttons(container)
    assert len(buttons) == 2
    assert buttons[0].disabled is True  # Prev disabled on page 0
    assert buttons[1].disabled is False  # Next enabled
    # The select only offers the page's reminders, never the whole list.
    assert len(_selects(container)[0].options) == rem.REMINDER_PAGE_SIZE


def test_card_capped_footer_shows_overflow_marker():
    view = RemindersCard(None, 1, _reminders(rem.REMINDER_LIST_CAP), True)
    footer = " ".join(_texts(_container(view)))
    assert "25+" in footer


async def test_card_next_shows_the_remaining_reminders(make_interaction):
    view = RemindersCard(None, 1, _reminders(rem.REMINDER_PAGE_SIZE + 2), False)
    interaction = make_interaction(user_id=1)

    await view._next(interaction)

    assert view.page == 1
    container = _container(view)
    # Page 1 carries the two overflow reminders and disables Next.
    assert len(_selects(container)[0].options) == 2
    assert _buttons(container)[1].disabled is True
    assert interaction.edits  # edited in place


def test_card_is_author_gated():
    view = RemindersCard(None, 4242, _reminders(3), False)
    assert view.author_id == 4242


# ---------------------------------------------------------------------------
# Confirm-less cancel
# ---------------------------------------------------------------------------


class _CancelSpyCog:
    def __init__(self, existed=True):
        self.calls = []
        self._existed = existed

    async def cancel_reminder(self, reminder_id, user_id):
        self.calls.append((reminder_id, user_id))
        return self._existed


async def test_cancel_removes_the_reminder_and_rerenders(make_interaction):
    cog = _CancelSpyCog()
    view = RemindersCard(cog, 7, _reminders(3), False)
    interaction = make_interaction(user_id=7)

    await view._cancel(interaction, 2)

    assert cog.calls == [(2, 7)]  # scoped to the card's author
    assert [r["id"] for r in view.reminders] == [1, 3]  # id 2 dropped
    assert interaction.edits  # re-rendered in place


async def test_cancel_of_an_already_fired_reminder_still_drops_it(make_interaction):
    # cancel_reminder returns False (row already gone), but the card must still
    # remove it from the visible list - it no longer exists either way.
    cog = _CancelSpyCog(existed=False)
    view = RemindersCard(cog, 7, _reminders(2), False)

    await view._cancel(make_interaction(user_id=7), 1)

    assert [r["id"] for r in view.reminders] == [2]


async def test_cancelling_last_reminder_on_a_page_clamps_not_blank(make_interaction):
    # Two pages; on page 1 cancel its only reminder -> paginate must clamp back
    # to page 0 rather than render an empty page.
    view = RemindersCard(_CancelSpyCog(), 7, _reminders(rem.REMINDER_PAGE_SIZE + 1), False)
    view.page = 1
    view._build()

    await view._cancel(make_interaction(user_id=7), rem.REMINDER_PAGE_SIZE + 1)

    assert view.page == 0
    assert len(view.reminders) == rem.REMINDER_PAGE_SIZE


# ---------------------------------------------------------------------------
# cog.list_pending_reminders
# ---------------------------------------------------------------------------


async def test_list_scopes_query_to_author_and_reminder_type(fake_pool):
    fake_pool.fetch_return = []
    cog = _make_cog(fake_pool)

    await cog.list_pending_reminders(555)

    (_method, query, args), = [c for c in fake_pool.calls if c[0] == "fetch"]
    assert "event = 'reminder'" in query
    assert "extra->>'author_id' = $1" in query
    assert "ORDER BY expires" in query
    assert args[0] == "555"  # author id compared as text (matches jsonb ->>)
    assert args[1] == rem.REMINDER_LIST_CAP + 1  # +1 to detect the overflow


async def test_list_parses_both_str_and_dict_extra(fake_pool):
    fake_pool.fetch_return = [
        {
            "id": 1,
            "expires": _future(5),
            "extra": json.dumps({"author_id": 1, "channel_id": 42, "message": "a"}),
        },
        {
            "id": 2,
            "expires": _future(6),
            "extra": {"author_id": 1, "channel_id": 43, "message": "b"},
        },
    ]
    cog = _make_cog(fake_pool)

    reminders_list, capped = await cog.list_pending_reminders(1)

    assert capped is False
    assert [(r["id"], r["channel_id"], r["message"]) for r in reminders_list] == [
        (1, 42, "a"),
        (2, 43, "b"),
    ]


async def test_list_flags_overflow_and_slices_to_cap(fake_pool):
    fake_pool.fetch_return = [
        {
            "id": i,
            "expires": _future(i),
            "extra": {"author_id": 1, "channel_id": 1, "message": "x"},
        }
        for i in range(rem.REMINDER_LIST_CAP + 1)
    ]
    cog = _make_cog(fake_pool)

    reminders_list, capped = await cog.list_pending_reminders(1)

    assert capped is True
    assert len(reminders_list) == rem.REMINDER_LIST_CAP


# ---------------------------------------------------------------------------
# cog.cancel_reminder
# ---------------------------------------------------------------------------


async def test_cancel_reminder_scopes_delete_and_wakes_loop(fake_pool):
    fake_pool.fetchrow_return = {"id": 9}
    cog = _make_cog(fake_pool)
    cog._have_data.clear()

    result = await cog.cancel_reminder(9, 777)

    assert result is True
    (_method, query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert query.startswith("DELETE FROM timers")
    assert "event = 'reminder'" in query
    assert "extra->>'author_id' = $2" in query
    assert "claimed_at IS NULL" in query
    assert args == (9, "777")
    assert cog._have_data.is_set()  # dispatch loop woken to re-sleep


async def test_cancel_reminder_missing_row_returns_false_and_no_wake(fake_pool):
    fake_pool.fetchrow_return = None
    cog = _make_cog(fake_pool)
    cog._have_data.clear()

    result = await cog.cancel_reminder(9, 777)

    assert result is False
    assert not cog._have_data.is_set()  # nothing changed, loop not disturbed


# ---------------------------------------------------------------------------
# Dispatch-loop delivery semantics
#
# Two contracts, split by event (cogs/community/reminders.py):
#   * reminders + generic ``*_timer_complete`` events -> DELETE-as-atomic-claim
#     BEFORE delivery (at-most-once): a crash mid-delivery loses at most one
#     firing and can NEVER double-fire.
#   * tempban -> durable claim -> deliver -> delete with bounded retries and a
#     dead-letter at MAX_TIMER_ATTEMPTS (at-least-once; the unban is idempotent).
# ---------------------------------------------------------------------------


class _RaceBot:
    """A bot whose dispatch loop sees exactly one due timer, then nothing."""

    def __init__(self, pool):
        self.db_pool = pool
        self.loop = types.SimpleNamespace(
            create_task=lambda coro: (coro.close(), types.SimpleNamespace(cancel=lambda: None))[1]
        )
        self._closed = False

    async def wait_until_ready(self):
        return None

    def is_closed(self):
        return self._closed


class _RacePool:
    """Serves one due timer, then nothing; records claims and executes.

    A single ``get_active_timer`` SELECT returns the seeded row once (and None
    forever after, modelling the row being gone once claimed/deleted). The claim
    fetchrow - the at-most-once ``DELETE ... RETURNING`` or the durable
    ``UPDATE ... claimed_at`` - returns the row when the claim is won, else None.
    """

    def __init__(self, row, *, claim_won=True):
        self._served = False
        self._claim_won = claim_won
        self.row = row
        self.executes = []
        self.claims = []

    async def fetchrow(self, query, *args):
        stripped = query.lstrip()
        if stripped.startswith("SELECT"):
            if self._served:
                return None
            self._served = True
            return self.row
        # Both claim shapes (DELETE-as-claim and UPDATE-claim) land here.
        self.claims.append((query, args))
        return self.row if self._claim_won else None

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return "DELETE 1"


def _due_row(event, *, attempts=0, timer_id=1):
    return {
        "id": timer_id,
        "event": event,
        "expires": datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=1),
        "attempts": attempts,
        "extra": {"guild_id": 10, "user_id": 20},
    }


async def _run_one_dispatch(row, *, claim_won=True, delivery_error=None):
    pool = _RacePool(row, claim_won=claim_won)
    bot = _RaceBot(pool)
    cog = Reminder(bot)
    fired = []
    claims_at_delivery = []

    async def _spy_call_timer(r):
        # Record how many claims had already run when delivery started, so a
        # test can prove the DELETE claim happens BEFORE delivery.
        claims_at_delivery.append(len(pool.claims))
        fired.append(r["id"])
        if delivery_error is not None:
            raise delivery_error

    cog.call_timer = _spy_call_timer

    task = asyncio.ensure_future(cog.dispatch_timers())
    # Let it process the single due timer, then it blocks on _have_data.wait().
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return types.SimpleNamespace(
        fired=fired,
        executes=pool.executes,
        claims=pool.claims,
        claims_at_delivery=claims_at_delivery,
    )


# --- at-most-once path (reminders + generic dispatched events) --------------


async def test_reminder_deletes_as_claim_before_delivering():
    result = await _run_one_dispatch(_due_row("reminder"))

    assert result.fired == [1]
    # The claim is a DELETE ... RETURNING, and it ran BEFORE delivery.
    assert len(result.claims) == 1
    claim_query, _args = result.claims[0]
    assert claim_query.startswith("DELETE FROM timers")
    assert "RETURNING" in claim_query
    assert result.claims_at_delivery == [1]  # delete had already happened
    # No post-delivery execute DELETE: the claim WAS the delete.
    assert result.executes == []


async def test_reminder_crash_after_delete_cannot_double_fire():
    # Delivery raises AFTER the row was deleted-as-claim. The dispatch loop keeps
    # running (get_active_timer now returns nothing), so the reminder is neither
    # rescheduled nor re-delivered: at most one firing, never two.
    result = await _run_one_dispatch(
        _due_row("reminder"), delivery_error=RuntimeError("Discord down")
    )

    assert result.fired == [1]  # fired exactly once, never twice
    # Nothing is rescheduled and nothing re-deleted - the firing is dropped.
    assert result.executes == []
    assert not any(
        q.startswith("UPDATE timers SET claimed_at = NULL")
        for q, _a in result.executes
    )


async def test_reminder_skips_when_another_worker_or_cancel_won_the_claim():
    result = await _run_one_dispatch(_due_row("reminder"), claim_won=False)

    assert result.fired == []
    assert result.executes == []


# --- durable path (tempban) -------------------------------------------------


async def test_tempban_claims_delivers_then_deletes():
    result = await _run_one_dispatch(_due_row("tempban"))

    assert result.fired == [1]
    # Durable claim is the conditional UPDATE (claim BEFORE delivery).
    claim_query, _args = result.claims[0]
    assert claim_query.startswith("UPDATE timers SET claimed_at = now()")
    # The delete only happens AFTER a successful delivery.
    assert result.claims_at_delivery == [1]
    assert any(q.startswith("DELETE FROM timers") for q, _a in result.executes)


async def test_tempban_skips_firing_when_another_worker_holds_the_claim():
    result = await _run_one_dispatch(_due_row("tempban"), claim_won=False)

    assert result.fired == []
    assert result.executes == []


async def test_tempban_releases_and_reschedules_failed_delivery():
    result = await _run_one_dispatch(
        _due_row("tempban", attempts=0),
        delivery_error=RuntimeError("Discord unavailable"),
    )

    assert result.fired == [1]
    retry_query, retry_args = next(
        (query, args)
        for query, args in result.executes
        if query.startswith("UPDATE timers SET claimed_at = NULL")
    )
    assert "attempts = attempts + 1" in retry_query
    assert retry_args == (1, "Discord unavailable", 60)
    # Not deleted: durable delivery is retried, not dropped.
    assert not any(
        query.startswith("DELETE FROM timers") for query, _args in result.executes
    )


async def test_tempban_dead_letters_at_max_attempts(caplog):
    # The 12th consecutive failure (prior attempts = MAX - 1) exhausts the retry
    # budget: the row is deleted and a grep-able dead-letter line is logged.
    from cogs.community.reminders import MAX_TIMER_ATTEMPTS

    with caplog.at_level("ERROR", logger="cogs.community.reminders"):
        result = await _run_one_dispatch(
            _due_row("tempban", attempts=MAX_TIMER_ATTEMPTS - 1),
            delivery_error=RuntimeError("Forbidden forever"),
        )

    assert result.fired == [1]
    # Dead-lettered: deleted, and NOT rescheduled.
    assert any(
        query.startswith("DELETE FROM timers") for query, _args in result.executes
    )
    assert not any(
        query.startswith("UPDATE timers SET claimed_at = NULL")
        for query, _args in result.executes
    )
    dead_letter = " ".join(r.getMessage() for r in caplog.records)
    assert "dead-letter" in dead_letter
    assert "tempban" in dead_letter  # event is grep-able
    assert "id=1" in dead_letter  # id is grep-able


def test_timer_retry_delay_is_exponential_and_bounded():
    assert timer_retry_delay(0) == 60
    assert timer_retry_delay(1) == 120
    assert timer_retry_delay(99) == 3600


async def test_tempban_fetches_uncached_guild_before_unban(fake_pool):
    class _Guild:
        def __init__(self):
            self.unbanned = []

        async def unban(self, user, *, reason):
            self.unbanned.append((user.id, reason))

    guild = _Guild()
    bot = _make_reminder_bot(fake_pool)
    bot.get_guild = lambda _guild_id: None

    async def fetch_guild(guild_id):
        assert guild_id == 123
        return guild

    bot.fetch_guild = fetch_guild
    cog = Reminder(bot)

    await cog.call_timer(
        {
            "id": 9,
            "event": "tempban",
            "extra": {"guild_id": 123, "user_id": 456},
        }
    )

    assert guild.unbanned == [(456, "Temp-ban expired")]


# ---------------------------------------------------------------------------
# Dispatch task lifecycle: cog_load/cog_unload seam + crash-restart
#
# The task that runs dispatch_timers forever is started in cog_load (not
# __init__) and torn down in cog_unload; if it ever dies with an exception
# (dispatch_timers' own loop already catches everything it can, so this is a
# backstop - see the module-level comment above DISPATCH_RESTART_BACKOFF_*),
# a done callback logs it and restarts it with a bounded exponential backoff.
# ---------------------------------------------------------------------------


def _loop_bot(fake_pool):
    """A bot stand-in with a REAL event loop, for tests that drive real tasks.

    Unlike ``_make_reminder_bot`` (whose fake ``create_task`` closes the coro
    immediately so no background loop ever runs), these tests need actual
    asyncio scheduling: a crashing task, a done callback, and a restart task.
    """
    return types.SimpleNamespace(db_pool=fake_pool, loop=asyncio.get_event_loop())


def _patch_fast_sleep(monkeypatch):
    """Replace reminders.asyncio.sleep with one that records the delay asked
    for but only actually waits one real event-loop tick, so a test can see
    the whole restart-backoff dance without any real waiting.
    """
    real_sleep = asyncio.sleep
    calls = []

    async def fake_sleep(delay):
        calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(reminders_mod.asyncio, "sleep", fake_sleep)
    return calls


async def test_cog_load_sets_the_seam_and_starts_the_dispatch_task(fake_pool):
    bot = _loop_bot(fake_pool)
    cog = Reminder(bot)
    assert not hasattr(bot, "reminder")
    assert cog._task is None

    cog.dispatch_timers = lambda: asyncio.Event().wait()  # never finishes
    await cog.cog_load()

    assert bot.reminder is cog
    assert cog._task is not None
    assert not cog._task.done()

    cog.cog_unload()
    try:
        await cog._task
    except asyncio.CancelledError:
        pass


async def test_cog_unload_clears_the_seam_only_if_still_its_own(fake_pool):
    bot = _loop_bot(fake_pool)
    cog = Reminder(bot)
    cog.dispatch_timers = lambda: asyncio.Event().wait()
    await cog.cog_load()

    # Simulate a reload: a second instance already took the seam over.
    other = types.SimpleNamespace()
    bot.reminder = other
    cog.cog_unload()
    assert bot.reminder is other  # the old instance must not clobber it

    try:
        await cog._task
    except asyncio.CancelledError:
        pass


async def test_dispatch_crash_is_logged_and_restarted_with_backoff(
    fake_pool, monkeypatch, caplog
):
    sleep_calls = _patch_fast_sleep(monkeypatch)
    bot = _loop_bot(fake_pool)
    cog = Reminder(bot)

    runs = []
    second_run_started = asyncio.Event()

    async def fake_dispatch():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("boom")
        second_run_started.set()
        await asyncio.Event().wait()  # the "healthy" second run just stays up

    cog.dispatch_timers = fake_dispatch

    with caplog.at_level("ERROR"):
        await cog.cog_load()
        await asyncio.wait_for(second_run_started.wait(), timeout=2)

    assert len(runs) == 2  # it crashed once and ran a second time
    assert sleep_calls == [reminders_mod.DISPATCH_RESTART_BACKOFF_INITIAL]
    assert "crashed" in caplog.text.lower()

    cog.cog_unload()
    try:
        await cog._task
    except asyncio.CancelledError:
        pass


async def test_dispatch_backoff_doubles_on_a_second_crash(fake_pool, monkeypatch):
    sleep_calls = _patch_fast_sleep(monkeypatch)
    bot = _loop_bot(fake_pool)
    cog = Reminder(bot)

    runs = []
    done = asyncio.Event()

    async def fake_dispatch():
        runs.append(1)
        if len(runs) <= 2:
            raise RuntimeError("boom again")
        done.set()
        await asyncio.Event().wait()

    cog.dispatch_timers = fake_dispatch

    await cog.cog_load()
    await asyncio.wait_for(done.wait(), timeout=2)

    assert len(runs) == 3
    assert sleep_calls == [
        reminders_mod.DISPATCH_RESTART_BACKOFF_INITIAL,
        reminders_mod.DISPATCH_RESTART_BACKOFF_INITIAL * 2,
    ]

    cog.cog_unload()
    try:
        await cog._task
    except asyncio.CancelledError:
        pass


async def test_cog_unload_cancellation_does_not_restart(fake_pool, monkeypatch):
    sleep_calls = _patch_fast_sleep(monkeypatch)
    bot = _loop_bot(fake_pool)
    cog = Reminder(bot)

    async def fake_dispatch():
        await asyncio.Event().wait()  # hangs until cancelled

    cog.dispatch_timers = fake_dispatch

    await cog.cog_load()
    await asyncio.sleep(0)
    task = cog._task
    cog.cog_unload()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Give the done callback a chance to (not) fire a restart.
    await asyncio.sleep(0)

    assert bot.reminder is None
    assert cog._restart_task is None
    # No backoff delay was ever scheduled (the test's own ticks also go
    # through the patched sleep, hence checking for the backoff VALUE rather
    # than an empty list).
    assert reminders_mod.DISPATCH_RESTART_BACKOFF_INITIAL not in sleep_calls


# ---------------------------------------------------------------------------
# Hot-loop check: a lost claim must still AWAIT the DB each iteration, never
# spin. If dispatch_timers ever regressed into a path that loops without
# awaiting anything, this test would hang (and the gate's own "watch for
# hangs" instruction would catch it) rather than fail cleanly.
# ---------------------------------------------------------------------------


async def test_dispatch_loop_awaits_the_db_even_when_every_claim_is_lost():
    row = _due_row("reminder")
    claims = []

    class _AlwaysDueLostClaimPool:
        async def fetchrow(self, query, *args):
            # A real DB call always suspends on real IO; a fake that returned
            # synchronously would let a non-awaiting loop spin forever without
            # ever giving this test's own ticks a chance to run (which is
            # exactly the bug this test exists to catch, just one layer up),
            # so this stand-in yields once per call like the real one does.
            await asyncio.sleep(0)
            stripped = query.lstrip()
            if stripped.startswith("SELECT"):
                return row  # synthetic: always "due", to stress the loop
            claims.append((query, args))
            return None  # every claim attempt loses

        async def execute(self, query, *args):
            return "DELETE 0"

    pool = _AlwaysDueLostClaimPool()
    bot = _RaceBot(pool)
    cog = Reminder(bot)
    cog.call_timer = lambda r: None

    task = asyncio.ensure_future(cog.dispatch_timers())
    try:
        await asyncio.wait_for(_spin_n_ticks(50), timeout=2)
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2)
        except asyncio.CancelledError:
            pass

    # Each iteration awaited the DB for its own claim attempt: many distinct
    # claims were recorded, not a single tight non-yielding loop.
    assert len(claims) > 5


async def _spin_n_ticks(n):
    for _ in range(n):
        await asyncio.sleep(0)
