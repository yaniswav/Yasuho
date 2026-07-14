"""Tests for the reminders listing/cancel surface (cogs/community/reminders.py).

Covers three things the pure-logic suite (tests/tools/test_reminders.py) cannot:

* :class:`RemindersCard` rendering + navigation + confirm-less cancel (the
  Components V2 card), driven against the FakeInteraction/fake-cog stand-ins.
* The cog's DB seams: ``list_pending_reminders`` (author + type scoping,
  str/dict ``extra`` parsing, the +1 overflow -> ``capped``) and
  ``cancel_reminder`` (scoped DELETE, dispatch-loop wake, existed/not-existed).
* The dispatch-loop race-safety guard: a timer whose DELETE removed zero rows
  (a concurrent cancel already claimed it) is NOT fired.
"""

import asyncio
import datetime
import json
import types

import discord

from cogs.community.reminders import Reminder, RemindersCard
from tools import reminders as rem

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
# Dispatch-loop race safety: DELETE is the atomic claim
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
    """Serves one due timer to the first get_active_timer, None afterwards, and
    a configurable DELETE status."""

    def __init__(self, delete_status):
        self._served = False
        self._delete_status = delete_status
        self.executes = []

    async def fetchrow(self, query, *args):
        if self._served:
            return None
        self._served = True
        return {
            "id": 1,
            "expires": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=1),
        }

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return self._delete_status


async def _run_one_dispatch(delete_status):
    pool = _RacePool(delete_status)
    bot = _RaceBot(pool)
    cog = Reminder(bot)
    fired = []

    async def _spy_call_timer(row):
        fired.append(row["id"])

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
    return fired


async def test_dispatch_fires_when_it_wins_the_delete():
    """DELETE removed the row (status "DELETE 1") -> this loop owns it, fires."""
    fired = await _run_one_dispatch("DELETE 1")
    assert fired == [1]


async def test_dispatch_skips_firing_when_cancel_won_the_delete():
    """A concurrent cancel already removed the row ("DELETE 0") -> do NOT fire.

    This is the race-safety guarantee: cancelling the exact timer the loop is
    about to fire means the loop's DELETE affects zero rows and the reminder is
    never delivered.
    """
    fired = await _run_one_dispatch("DELETE 0")
    assert fired == []
