"""Tests for the recurring-reminder "kept" choice (owner decision, 2026-10-07,
after a Pack Confort refund): "l'utilisateur peut choisir lesquels restent
actifs dans sa limite gratuite; sans choix, les plus anciens restent actifs
selon un ordre stable".

Three layers:

* classification - :meth:`Reminder._recurring_archival` passes the stored
  ``kept`` flag through to :func:`tools.premium_archive.classify`, which
  already implements the kept-first ordering (tests/tools/test_premium_archive.py);
  this file only has to prove the PASS-THROUGH, and that no choice at all is
  unchanged from before the feature existed.
* the UI - :class:`_KeepSelect` appears on :class:`RemindersCard` only when
  the member has at least one archived recurring reminder, pre-selects the
  currently active ones, and clamps ``max_values`` to the option count.
* the write - :meth:`Reminder.set_recurring_kept` is author-scoped, one
  transaction, and never touches ``expires`` (so switching a series to
  active can never burst a backlog - it just resumes at whatever future
  occurrence its ``expires`` already pointed to).
"""

import datetime
import types

import discord

from cogs.community.reminders import Reminder, RemindersCard, _KeepSelect

UTC = datetime.timezone.utc
DAY = 86400


def _at(**kwargs):
    return datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC) + datetime.timedelta(
        **kwargs
    )


def _make_cog(pool):
    def _create_task(coro):
        coro.close()
        return types.SimpleNamespace(cancel=lambda: None)

    bot = types.SimpleNamespace(
        db_pool=pool, loop=types.SimpleNamespace(create_task=_create_task)
    )
    return Reminder(bot)


def _listed(**overrides):
    entry = {
        "id": 1,
        "expires": _at(),
        "channel_id": 9,
        "message": "stretch",
        "event": "reminder",
        "repeat_seconds": DAY,
        "archived": False,
    }
    entry.update(overrides)
    return entry


def _container(view):
    return view.children[0]


def _action_rows(container):
    return [c for c in container.children if isinstance(c, discord.ui.ActionRow)]


def _keep_selects(container):
    out = []
    for row in _action_rows(container):
        out.extend(c for c in row.children if isinstance(c, _KeepSelect))
    return out


def _texts(container):
    return [
        c.content for c in container.children if isinstance(c, discord.ui.TextDisplay)
    ]


# ---------------------------------------------------------------------------
# Classification: kept wins an active slot; no choice = unchanged
# ---------------------------------------------------------------------------


async def test_kept_series_wins_an_active_slot_over_an_older_one(fake_pool):
    # Three recurring rows, cap of 2: without a choice the two OLDEST (ids 1
    # and 2) are active and id 3 is archived. Marking id 3 kept flips that -
    # it wins a slot over id 2 (merely older), matching tools.premium_archive's
    # own kept-first ordering.
    fake_pool.fetch_return = [
        {"id": 1, "created": _at(minutes=0), "kept": None},
        {"id": 2, "created": _at(minutes=1), "kept": None},
        {"id": 3, "created": _at(minutes=2), "kept": "true"},
    ]
    cog = _make_cog(fake_pool)
    cog.bot.premium = types.SimpleNamespace(
        for_user=lambda _uid: types.SimpleNamespace(max_recurring_reminders=2)
    )

    result = await cog._recurring_archival(42)

    assert result.is_active(1) is True
    assert result.is_active(3) is True
    assert result.is_active(2) is False  # bumped out by the kept id 3


async def test_with_no_choice_the_oldest_first_order_is_unchanged(fake_pool):
    fake_pool.fetch_return = [
        {"id": 1, "created": _at(minutes=0)},
        {"id": 2, "created": _at(minutes=1)},
        {"id": 3, "created": _at(minutes=2)},
    ]
    cog = _make_cog(fake_pool)
    cog.bot.premium = types.SimpleNamespace(
        for_user=lambda _uid: types.SimpleNamespace(max_recurring_reminders=2)
    )

    result = await cog._recurring_archival(42)

    assert result.is_active(1) is True
    assert result.is_active(2) is True
    assert result.is_active(3) is False


async def test_recurring_rows_query_asks_for_the_kept_flag(fake_pool):
    fake_pool.fetch_return = []
    cog = _make_cog(fake_pool)

    await cog._recurring_archival(1)

    (_method, query, _args), = [c for c in fake_pool.calls if c[0] == "fetch"]
    assert "extra->>'kept' AS kept" in query


# NEGATIVE CONTROL. This test is written to FAIL if the kept pass-through in
# Reminder._recurring_archival regresses (e.g. the "kept" dict key is
# hard-coded to False instead of read off the row). Verified manually during
# this session: temporarily editing the dict comprehension in
# cogs/community/reminders.py to always write ``"kept": False`` made this test
# (and test_kept_series_wins_an_active_slot_over_an_older_one above) fail,
# then the edit was reverted and `git diff` confirmed clean.
async def test_negative_control_without_kept_pass_through_the_choice_is_lost(
    fake_pool,
):
    fake_pool.fetch_return = [
        {"id": 1, "created": _at(minutes=0), "kept": None},
        {"id": 2, "created": _at(minutes=1), "kept": "true"},
    ]
    cog = _make_cog(fake_pool)
    cog.bot.premium = types.SimpleNamespace(
        for_user=lambda _uid: types.SimpleNamespace(max_recurring_reminders=1)
    )

    result = await cog._recurring_archival(1)

    # id 2 is kept, so it wins the single slot over the older id 1.
    assert result.is_active(2) is True
    assert result.is_active(1) is False


# ---------------------------------------------------------------------------
# UI: the control appears only with an archived recurring reminder
# ---------------------------------------------------------------------------


def test_keep_control_absent_without_any_archived_recurring_reminder():
    view = RemindersCard(None, 1, [_listed(archived=False)], False)
    assert _keep_selects(_container(view)) == []


def test_keep_control_appears_with_an_archived_recurring_reminder():
    view = RemindersCard(
        None,
        1,
        [_listed(id=1, archived=True), _listed(id=2, archived=False)],
        False,
    )
    selects = _keep_selects(_container(view))
    assert len(selects) == 1
    assert {opt.value for opt in selects[0].options} == {"1", "2"}
    # Pre-selected: the currently ACTIVE one only.
    defaults = {opt.value for opt in selects[0].options if opt.default}
    assert defaults == {"2"}


def test_keep_control_never_offers_a_one_shot_reminder():
    view = RemindersCard(
        None,
        1,
        [
            _listed(id=1, repeat_seconds=DAY, archived=True),
            _listed(id=2, repeat_seconds=None, archived=False),
        ],
        False,
    )
    selects = _keep_selects(_container(view))
    assert {opt.value for opt in selects[0].options} == {"1"}


def test_free_card_with_no_archived_reminder_is_byte_identical():
    # A member with recurring reminders all within their cap (never archived)
    # must see EXACTLY the pre-feature card: same text, same action rows,
    # no keep-select anywhere.
    before = RemindersCard(None, 1, [_listed(archived=False)], False)
    after = RemindersCard(None, 1, [_listed(archived=False)], False)
    assert _texts(_container(before)) == _texts(_container(after))
    assert len(_action_rows(_container(before))) == len(
        _action_rows(_container(after))
    )
    assert _keep_selects(_container(after)) == []


# ---------------------------------------------------------------------------
# max_values clamp
# ---------------------------------------------------------------------------


def test_keep_select_max_values_clamped_to_the_option_count():
    recurring = [_listed(id=i) for i in range(1, 4)]  # 3 options
    select = _KeepSelect(None, recurring, max_recurring_reminders=10)
    assert select.max_values == 3


def test_keep_select_max_values_uses_the_effective_cap_when_smaller():
    recurring = [_listed(id=i) for i in range(1, 6)]  # 5 options
    select = _KeepSelect(None, recurring, max_recurring_reminders=2)
    assert select.max_values == 2


# ---------------------------------------------------------------------------
# set_recurring_kept: author-scoped, one transaction, never touches `expires`
# ---------------------------------------------------------------------------


class _Tx:
    def __init__(self, pool, is_tx):
        self._pool = pool
        self._is_tx = is_tx

    async def __aenter__(self):
        if self._is_tx:
            self._pool.tx_depth += 1
        return self._pool

    async def __aexit__(self, *exc):
        if self._is_tx:
            self._pool.tx_depth -= 1
        return False


class _TxPool:
    """Records every statement and whether it ran inside a transaction."""

    def __init__(self):
        self.calls = []
        self.tx_depth = 0

    async def execute(self, query, *args):
        self.calls.append(("execute", query.lstrip(), args, self.tx_depth > 0))
        return "UPDATE 1"

    def acquire(self):
        return _Tx(self, is_tx=False)

    def transaction(self):
        return _Tx(self, is_tx=True)


async def test_set_recurring_kept_runs_both_writes_in_one_transaction():
    pool = _TxPool()
    cog = _make_cog(pool)

    await cog.set_recurring_kept(777, kept_ids=[2], candidate_ids=[1, 2, 3])

    calls = [c for c in pool.calls if c[0] == "execute"]
    assert len(calls) == 2
    assert all(call[3] for call in calls)  # both ran inside the transaction


async def test_set_recurring_kept_is_scoped_to_the_given_author():
    # Never trust the id alone: every write this method issues is bound to
    # THIS author's id in its own WHERE clause - another user's id passed as
    # `user_id` is the only thing that can ever change which rows are
    # touched, never the reminder ids themselves.
    pool = _TxPool()
    cog = _make_cog(pool)

    await cog.set_recurring_kept(777, kept_ids=[2], candidate_ids=[1, 2, 3])

    for _method, query, args, _in_tx in pool.calls:
        assert "extra->>'author_id' = $1" in query
        assert args[0] == "777"
        # Never reschedules: switching kept can never cause a delivery burst.
        assert "expires" not in query


async def test_set_recurring_kept_sets_true_for_chosen_false_for_the_rest():
    pool = _TxPool()
    cog = _make_cog(pool)

    await cog.set_recurring_kept(1, kept_ids=[2], candidate_ids=[1, 2, 3])

    by_value = {args[2]: set(args[1]) for _m, _q, args, _tx in pool.calls}
    assert by_value["true"] == {2}
    assert by_value["false"] == {1, 3}


async def test_set_recurring_kept_with_everything_selected_writes_only_true():
    # No candidate is left over to clear, so the "false" write is skipped
    # outright rather than issued as a no-op UPDATE over an empty array.
    pool = _TxPool()
    cog = _make_cog(pool)

    await cog.set_recurring_kept(1, kept_ids=[1, 2], candidate_ids=[1, 2])

    assert len(pool.calls) == 1
    assert pool.calls[0][2][2] == "true"


# ---------------------------------------------------------------------------
# RemindersCard._apply_kept: writes, then re-renders from a fresh read
# ---------------------------------------------------------------------------


class _KeepSpyCog:
    def __init__(self, *, max_recurring_reminders=None):
        self.calls = []
        self.next_reminders = []
        self.next_capped = False
        if max_recurring_reminders is None:
            self.bot = types.SimpleNamespace(premium=None)
        else:
            self.bot = types.SimpleNamespace(
                premium=types.SimpleNamespace(
                    for_user=lambda _uid: types.SimpleNamespace(
                        max_recurring_reminders=max_recurring_reminders
                    )
                )
            )

    async def set_recurring_kept(self, user_id, kept_ids, candidate_ids):
        self.calls.append((user_id, list(kept_ids), list(candidate_ids)))

    async def list_pending_reminders(self, _user_id):
        return self.next_reminders, self.next_capped


async def test_apply_kept_writes_scoped_to_the_cards_author_then_rerenders(
    make_interaction,
):
    cog = _KeepSpyCog()
    cog.next_reminders = [_listed(id=1, archived=False)]
    view = RemindersCard(
        cog, 7, [_listed(id=1, archived=True), _listed(id=2, archived=False)], False
    )
    interaction = make_interaction(user_id=7)

    await view._apply_kept(interaction, ["1"], [1, 2])

    assert cog.calls == [(7, [1], [1, 2])]
    assert view.reminders == cog.next_reminders  # re-rendered from a fresh read
    assert interaction.edits


async def test_apply_kept_clamps_the_write_to_the_current_effective_cap(
    make_interaction,
):
    # The component's own max_values can be stale (a cap that dropped between
    # opening the card and submitting); the write itself must still never
    # honour more picks than the CURRENT effective cap allows.
    cog = _KeepSpyCog(max_recurring_reminders=1)
    view = RemindersCard(cog, 7, [_listed(id=1), _listed(id=2)], False)
    interaction = make_interaction(user_id=7)

    await view._apply_kept(interaction, ["1", "2"], [1, 2])

    assert cog.calls == [(7, [1], [1, 2])]


async def test_archival_orders_by_series_creation_not_by_the_current_row(fake_pool):
    # Series 1 was created first but has just fired, so its CURRENT row is the
    # newest. It must keep its slot over series 2: ordering by the row's own
    # ``created`` would hand the slot over at every firing.
    fake_pool.fetch_return = [
        {
            "id": 10,
            "created": _at(minutes=5),
            "kept": None,
            "series_created": _at(minutes=0).isoformat(),
        },
        {"id": 11, "created": _at(minutes=1), "kept": None, "series_created": None},
    ]
    cog = _make_cog(fake_pool)
    cog.bot.premium = types.SimpleNamespace(
        for_user=lambda _uid: types.SimpleNamespace(max_recurring_reminders=1)
    )

    result = await cog._recurring_archival(42)

    assert result.is_active(10) is True
    assert result.is_active(11) is False
