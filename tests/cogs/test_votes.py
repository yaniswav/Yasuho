"""Unit tests for cogs.community.votes (top.gg vote rewards).

V1 covered the ledger's silent infrastructure: one listener, one upsert, one
cross-cog hand-off. This file now also covers V2, the surfaces built on top of
it:

* the payload boundary - a test vote is dropped, a string user id is int()ed,
  a missing/junk one is refused, and ``is_weekend`` picks the boost window;
* the write itself - ONE statement, on the right table, with the right
  parameters (the SQL's own semantics are proven against a real Postgres, see
  the lot's probe transcript, not re-implemented here);
* the hand-off to the Leveling cog's in-memory boost map, and its refusal to
  turn any failure of that into a failure of the vote;
* the erasure seam forget_vote_boost, the twin of
  presence.forget_collected_presence;
* V2: the thank-you DM, the opt-in "vote again" reminder (schedule, cancel-
  and-reschedule, fire-time preconditions), the lazy top.gg catch-up poll at
  ``/vote`` open, and the pure ``is_recent_supporter`` badge window.
"""

import datetime
import logging
import re
import types

import discord
import pytest

from cogs.community import votes
from cogs.community.votes import Votes
from tools import settings

UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """tools.settings caches user blobs in a process-global singleton; the V2
    reminder tests below are the first in this module to exercise
    settings.get_user, so an entry left by an earlier test must never leak
    in (see tests/cogs/test_leveling.py for the same pattern)."""
    settings._cache.clear()
    yield
    settings._cache.clear()


class _FakeLeveling:
    """The one method the vote cog calls on the Leveling cog, plus a record."""

    def __init__(self, raises=False):
        self.raises = raises
        self.armed = []
        self.forgotten = []

    def note_vote_boost(self, user_id, expires_at):
        if self.raises:
            raise RuntimeError("boom")
        self.armed.append((user_id, expires_at))

    def forget_vote_boost(self, user_id):
        if self.raises:
            raise RuntimeError("boom")
        self.forgotten.append(user_id)
        return True


def _make_bot(fake_pool, leveling=_FakeLeveling):
    """A bot exposing exactly the two seams the V1 cog uses: db_pool and
    get_cog. No get_user/fetch_user/user - the V2 DM and reminder helpers must
    (and do) treat that as "cannot resolve, skip silently", never a crash;
    tests that need to observe a DM or a scheduled reminder use
    :func:`_make_rich_bot` instead."""
    cog = leveling() if isinstance(leveling, type) else leveling
    return types.SimpleNamespace(
        db_pool=fake_pool,
        get_cog=lambda name: cog if name == "Leveling" else None,
    ), cog


class _FakeUser:
    def __init__(self, user_id, raise_on_send=None):
        self.id = user_id
        self.raise_on_send = raise_on_send
        self.sent = []

    async def send(self, content=None, **kwargs):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(content)


def _forbidden():
    """A real ``discord.Forbidden`` without touching the network."""
    response = types.SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, "cannot send messages to this user")


class _FakeReminder:
    """Records every ``create_timer`` call; never touches a real timers loop."""

    def __init__(self):
        self.created = []

    async def create_timer(self, when, event, **extra):
        self.created.append((when, event, extra))
        return {"id": 1}


class _FakeDBLClient:
    def __init__(self, voted=True, raises=False):
        self.voted = voted
        self.raises = raises
        self.calls = []

    async def get_user_vote(self, user_id):
        self.calls.append(user_id)
        if self.raises:
            raise RuntimeError("top.gg is down")
        return self.voted


class _FakeWebstats:
    def __init__(self, client=None):
        self.dbl_client = client


def _make_rich_bot(
    fake_pool,
    *,
    leveling=None,
    reminder=None,
    webstats=None,
    user=None,
    bot_id=999,
    fetch_error=None,
):
    """A bot exposing every seam the V2 surfaces use, each independently
    omittable (None) so a test only wires up what it is actually exercising -
    an absent cog/user must behave exactly like it does in :func:`_make_bot`."""
    cogs = {"Leveling": leveling, "Reminder": reminder, "Webstats": webstats}

    async def fetch_user(user_id):
        if fetch_error is not None:
            raise fetch_error
        if user is not None and user.id == user_id:
            return user
        raise LookupError("unknown user %s" % user_id)

    bot = types.SimpleNamespace(
        db_pool=fake_pool,
        get_cog=lambda name: cogs.get(name),
        get_user=lambda user_id: user if (user and user.id == user_id) else None,
        fetch_user=fetch_user,
        user=types.SimpleNamespace(id=bot_id),
    )
    return bot


def _vote(user="4242", kind="upvote", **extra):
    """A dbl_vote payload in the shape the topgg lib actually hands over.

    ``user`` is a STRING on purpose: topgg.types.parse_vote_dict int-converts
    only ``bot``/``guild``, whatever VoteDataDict's annotation claims.
    """
    data = {"bot": 111, "user": user, "type": kind}
    data.update(extra)
    return data


def _expiry(hours=12):
    return datetime.datetime.now(UTC) + datetime.timedelta(hours=hours)


def _row(streak=1, total=1, expires=None, replayed=False, last_vote_at=None):
    return {
        "last_vote_at": last_vote_at or datetime.datetime.now(UTC),
        "streak": streak,
        "total_votes": total,
        "boost_expires_at": expires or _expiry(),
        # The statement's own verdict on the delivery: true when it recognised
        # the same vote arriving twice and left every column alone.
        "replayed": replayed,
    }


# ---------------------------------------------------------------------------
# The payload boundary
# ---------------------------------------------------------------------------


async def test_a_test_vote_is_dropped_before_any_write(fake_pool):
    """The top.gg edit page's test button must never write a streak."""
    bot, leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(kind="test"))

    assert fake_pool.calls == []
    assert leveling.armed == []


@pytest.mark.parametrize("kind", [None, "", "vote.create", "downvote", "UPVOTE"])
async def test_only_an_upvote_is_recorded(fake_pool, kind, caplog):
    """ALLOWLIST, not denylist: this listener writes, so a payload whose meaning
    we do not know must be refused, not banked as a real vote. top.gg's v1
    webhooks send type "vote.create" - the day that lands, this must be a log
    line per vote and zero rows, never a silently wrong ledger."""
    bot, leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()

    payload = _vote()
    if kind is None:
        del payload["type"]
    else:
        payload["type"] = kind

    with caplog.at_level(logging.WARNING):
        await Votes(bot).on_dbl_vote(payload)

    assert fake_pool.calls == []
    assert leveling.armed == []
    # Loud, unlike the routine test vote: an unknown type means top.gg changed.
    assert any(record.levelno >= logging.WARNING for record in caplog.records)


async def test_a_real_vote_records_the_string_user_id_as_an_int(fake_pool):
    bot, _leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    (method, query, args), = fake_pool.calls
    assert method == "fetchrow"
    assert "INSERT INTO topgg_votes" in query
    assert args[0] == 4242 and isinstance(args[0], int)


@pytest.mark.parametrize("bad", [None, "", "not-a-number", [], {"id": 1}])
async def test_a_payload_without_a_usable_user_id_writes_nothing(fake_pool, bad):
    bot, leveling = _make_bot(fake_pool)

    await Votes(bot).on_dbl_vote(_vote(user=bad))

    assert fake_pool.calls == []
    assert leveling.armed == []


@pytest.mark.parametrize("bad", ["0", "-7", str(1 << 64)])
async def test_a_user_id_that_is_not_a_snowflake_writes_nothing(fake_pool, bad):
    """int() accepts it, BIGINT would not: refused here as one log line rather
    than as an asyncpg range error raised out of a public webhook's listener."""
    bot, leveling = _make_bot(fake_pool)

    await Votes(bot).on_dbl_vote(_vote(user=bad))

    assert fake_pool.calls == []
    assert leveling.armed == []


async def test_a_weekday_vote_asks_for_the_twelve_hour_boost(fake_pool):
    bot, _leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(is_weekend=False))

    (_method, _query, args), = fake_pool.calls
    assert args[1] == votes.BOOST_HOURS == 12
    assert args[2] == votes.STREAK_WINDOW_HOURS == 24
    assert args[3] == votes.REPLAY_WINDOW_HOURS == 1


async def test_a_weekend_vote_doubles_the_boost_window(fake_pool):
    """top.gg counts a weekend vote double, so the boost lasts double - the
    SAME statement with a different duration, never a second reward."""
    bot, _leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(is_weekend=True))

    (_method, query, args), = fake_pool.calls
    assert args[1] == votes.WEEKEND_BOOST_HOURS == 24
    assert query == votes.RECORD_VOTE  # not a different query, just a different arg


async def test_a_payload_with_no_weekend_flag_falls_back_to_the_short_boost(
    fake_pool,
):
    """The flag is read with .get: a payload that omits it (a hand-made one, a
    future top.gg shape) must degrade to a normal boost, never raise."""
    bot, _leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote())  # no is_weekend key at all

    (_method, _query, args), = fake_pool.calls
    assert args[1] == votes.BOOST_HOURS


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


# The exact replay predicate, spelled once so every test below reads the same
# text the statement runs. Both halves matter: the time floor, and "$5 OR NOT
# caught_up", which is what stops the floor from swallowing a genuine webhook
# vote that lands on a row the /vote catch-up poll stamped (see below).
_REPLAY_GUARD = (
    "WHEN (now() - topgg_votes.last_vote_at\n"
    "              BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour')\n"
    "             AND ($5 OR NOT topgg_votes.caught_up)"
)


def test_the_vote_is_one_atomic_statement_on_its_own_table():
    """No read-modify-write: the streak is decided from the row's own previous
    timestamp INSIDE the upsert, so two deliveries serialise on the row lock
    instead of both computing the same next streak."""
    query = votes.RECORD_VOTE
    assert query.strip().startswith("INSERT INTO topgg_votes")
    assert query.count(";") == 0  # one command (asyncpg prepares exactly one)
    assert "ON CONFLICT (user_id) DO UPDATE" in query
    assert "topgg_votes.streak + 1" in query
    assert "ELSE 1" in query
    # Every column the caller needs comes back from the same round trip,
    # including the statement's own verdict on the delivery.
    assert query.rstrip().endswith("last_vote_at < now() AS replayed")
    for column in ("streak", "total_votes", "boost_expires_at"):
        assert column in query.rsplit("RETURNING", 1)[1]
    # Only ever this user's row.
    assert "WHERE" not in query.upper().split("ON CONFLICT")[0]


def test_a_redelivered_vote_changes_nothing_at_all():
    """The v0 payload carries no vote id, so the row's own timestamp is the only
    evidence a duplicate leaves. Every SET keeps the existing value inside the
    replay window - including last_vote_at, without which repeated deliveries
    would slide the window forward and keep a dead streak alive."""
    query = votes.RECORD_VOTE
    # One per SET column: nothing may move on a replay.
    assert query.count(_REPLAY_GUARD) == 5
    for kept in (
        "THEN topgg_votes.last_vote_at",
        "THEN topgg_votes.streak\n",
        "THEN topgg_votes.total_votes\n",
        "THEN topgg_votes.boost_expires_at\n",
        "THEN topgg_votes.caught_up\n",
    ):
        assert kept in query
    # Bounded BELOW by zero: a last_vote_at in the FUTURE (a clock stepping
    # back) is not a replay, so it falls through and gets re-stamped instead of
    # freezing the ledger until that timestamp is reached.
    assert "INTERVAL '0 hours'" in query
    # And a vote never shortens a deadline its owner was already promised.
    assert "GREATEST(" in query


def test_the_replay_floor_sits_far_under_the_vote_floor():
    """top.gg lets a user vote every 12h, so the window may never grow near it:
    a genuine vote must never be mistaken for a redelivery."""
    assert votes.REPLAY_WINDOW_HOURS == 1
    assert votes.REPLAY_WINDOW_HOURS * 12 <= votes.BOOST_HOURS * 12
    assert votes.REPLAY_WINDOW_HOURS < votes.BOOST_HOURS
    assert votes.REPLAY_WINDOW_HOURS < votes.STREAK_WINDOW_HOURS


def test_the_vote_never_touches_a_table_but_its_own():
    referenced = set(
        re.findall(
            r"(\w+)\."
            r"(?:user_id|last_vote_at|streak|total_votes|boost_expires_at|caught_up)",
            votes.RECORD_VOTE,
        )
    )
    assert referenced == {"topgg_votes"}
    for table in ("levels", "xp_period", "user_profiles"):
        assert table not in votes.RECORD_VOTE


# ---------------------------------------------------------------------------
# The hand-off to the leveling boost map
# ---------------------------------------------------------------------------


async def test_a_recorded_vote_arms_the_boost_with_the_stored_deadline(fake_pool):
    """The deadline handed to memory is the one Postgres computed, not one
    recomputed here - a second clock would drift from the stored row."""
    expires = _expiry(24)
    bot, leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row(streak=3, total=9, expires=expires)

    await Votes(bot).on_dbl_vote(_vote(user="7"))

    assert leveling.armed == [(7, expires)]


async def test_a_redelivered_vote_still_arms_the_stored_deadline(fake_pool, caplog):
    """The statement left the row alone, so the deadline coming back is the one
    the FIRST delivery stored: re-arming it costs nothing and rescues the case
    where that first delivery could not reach the leveling cog. The duplicate
    itself is a WARNING, because a public endpoint delivering twice is worth
    seeing."""
    expires = _expiry(24)
    bot, leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row(streak=4, total=9, expires=expires, replayed=True)

    with caplog.at_level(logging.WARNING):
        await Votes(bot).on_dbl_vote(_vote(user="7"))

    assert leveling.armed == [(7, expires)]
    assert any("redelivered" in record.getMessage() for record in caplog.records)


async def test_a_missing_leveling_cog_still_banks_the_vote(fake_pool):
    """The vote is already in Postgres when the hand-off runs, so a leveling
    cog that failed to load costs the boost for this process only - and even
    that is recovered by reload_vote_boosts on the next restart."""
    bot = types.SimpleNamespace(db_pool=fake_pool, get_cog=lambda name: None)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote())

    assert len(fake_pool.calls) == 1  # the write happened


async def test_a_leveling_cog_that_raises_never_breaks_the_listener(fake_pool):
    bot, leveling = _make_bot(fake_pool, leveling=_FakeLeveling(raises=True))
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote())  # must not raise

    assert len(fake_pool.calls) == 1
    assert leveling.armed == []


async def test_a_failing_database_costs_a_log_line_and_nothing_else(fake_pool):
    async def boom(query, *args):
        raise RuntimeError("db down")

    fake_pool.fetchrow = boom
    bot, leveling = _make_bot(fake_pool)

    await Votes(bot).on_dbl_vote(_vote())  # must not raise

    assert leveling.armed == []


async def test_an_upsert_that_returns_nothing_arms_nothing(fake_pool):
    """Defensive: the upsert always returns its row, but a None must not be
    indexed into (that would be a TypeError inside the event loop)."""
    bot, leveling = _make_bot(fake_pool)
    fake_pool.fetchrow_return = None

    await Votes(bot).on_dbl_vote(_vote())

    assert leveling.armed == []


# ---------------------------------------------------------------------------
# The erasure seam (the twin of presence.forget_collected_presence)
# ---------------------------------------------------------------------------


def test_forget_drops_the_boost_through_the_leveling_cog(fake_pool):
    bot, leveling = _make_bot(fake_pool)

    assert votes.forget_vote_boost(bot, 4242) is True
    assert leveling.forgotten == [4242]


def test_forget_is_false_and_silent_without_a_leveling_cog():
    bot = types.SimpleNamespace(get_cog=lambda name: None)
    assert votes.forget_vote_boost(bot, 1) is False


def test_forget_is_false_and_silent_on_a_bot_with_no_get_cog():
    assert votes.forget_vote_boost(types.SimpleNamespace(), 1) is False


def test_forget_never_turns_a_completed_erasure_into_an_error(fake_pool):
    """Best effort by contract: the rows are already gone when this runs, so a
    raise here would report a failure for an erasure that SUCCEEDED."""
    bot, _leveling = _make_bot(fake_pool, leveling=_FakeLeveling(raises=True))

    assert votes.forget_vote_boost(bot, 1) is False


# ---------------------------------------------------------------------------
# Structural: the two dbl_vote listeners coexist
# ---------------------------------------------------------------------------


def test_the_vote_listener_is_registered_and_does_not_replace_the_logger():
    """discord.py delivers dbl_vote to EVERY listener: this cog's recorder and
    the webstats cog's log line are siblings, not a replacement."""
    from cogs.system.webstats import Webstats

    assert Votes.on_dbl_vote.__cog_listener__ is True
    assert Votes.on_dbl_vote.__cog_listener_names__ == ["on_dbl_vote"]
    assert Webstats.on_dbl_vote.__cog_listener__ is True


def test_the_vote_command_is_registered_hybrid():
    assert Votes.vote.name == "vote"
    # A hybrid command exposes both a text and an app command surface.
    assert Votes.vote.app_command is not None


def test_the_reminder_key_matches_the_preference_panel():
    """usersettings.PREFS reads/writes this literal key; a drift here would
    mean the panel toggles a preference this module never reads."""
    from cogs.community import usersettings

    keys = {pref.key for pref in usersettings.PREFS}
    assert votes.VOTE_REMINDER_PREF_KEY in keys
    pref = next(p for p in usersettings.PREFS if p.key == votes.VOTE_REMINDER_PREF_KEY)
    assert pref.default is False  # additive rule: opt-in, OFF by default


# ---------------------------------------------------------------------------
# V2: the thank-you DM
# ---------------------------------------------------------------------------


async def test_a_real_vote_gets_a_thank_you_dm(fake_pool):
    user = _FakeUser(4242)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), user=user)
    fake_pool.fetchrow_return = _row(streak=3)

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    assert len(user.sent) == 1
    assert "3" in user.sent[0]  # the streak is named in the message


async def test_a_replayed_vote_gets_no_second_thank_you(fake_pool):
    user = _FakeUser(7)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), user=user)
    fake_pool.fetchrow_return = _row(replayed=True)

    await Votes(bot).on_dbl_vote(_vote(user="7"))

    assert user.sent == []


async def test_a_test_vote_gets_no_thank_you(fake_pool):
    user = _FakeUser(4242)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), user=user)

    await Votes(bot).on_dbl_vote(_vote(kind="test"))

    assert user.sent == []


async def test_closed_dms_are_silent(fake_pool, caplog):
    """Forbidden is the one failure a member chose (closed DMs) - it must cost
    nothing beyond the DM itself, never the vote that was already banked."""
    user = _FakeUser(4242, raise_on_send=_forbidden())
    leveling = _FakeLeveling()
    bot = _make_rich_bot(fake_pool, leveling=leveling, user=user)
    fake_pool.fetchrow_return = _row()

    with caplog.at_level(logging.WARNING):
        await Votes(bot).on_dbl_vote(_vote(user="4242"))  # must not raise

    assert leveling.armed  # the boost still armed despite the closed DM
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_a_bot_account_is_never_dmed(fake_pool):
    """A payload names an ID, not a person. Anything that resolves to a bot
    never opted into this and never gets a DM."""
    robot = _FakeUser(4242)
    robot.bot = True
    leveling = _FakeLeveling()
    bot = _make_rich_bot(fake_pool, leveling=leveling, user=robot)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    assert robot.sent == []
    assert leveling.armed  # the ledger and the boost are untouched by the skip


async def test_an_unresolvable_user_costs_only_a_log_line(fake_pool):
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), fetch_error=LookupError())
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))  # must not raise


async def test_the_thank_you_is_localised_from_the_voters_saved_preference(
    fake_pool, monkeypatch
):
    """Same seam as dashboard_user_actions._export_note: resolve_locale, then
    render inside i18n.locale(loc)."""
    from tools import i18n

    seen = {}

    async def fake_resolve(bot, *, user_id, guild_id=None, interaction=None):
        seen["user_id"] = user_id
        return "fr"

    entered = []

    class _Spy:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(i18n, "resolve_locale", fake_resolve)
    monkeypatch.setattr(i18n, "locale", lambda loc: _Spy())

    user = _FakeUser(4242)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), user=user)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    assert seen["user_id"] == 4242
    assert entered == [True]


# ---------------------------------------------------------------------------
# V2: the opt-in "vote again" reminder - scheduling
# ---------------------------------------------------------------------------


async def test_a_real_vote_schedules_no_reminder_by_default(fake_pool):
    """The additive rule: opted OUT (the default) means zero DMs, ever."""
    reminder = _FakeReminder()
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), reminder=reminder)
    fake_pool.fetchval_return = {}  # no vote_reminder key -> default False
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    assert reminder.created == []


async def test_a_real_vote_schedules_the_reminder_when_opted_in(fake_pool):
    reminder = _FakeReminder()
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), reminder=reminder)
    fake_pool.fetchval_return = {"vote_reminder": True}
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    assert len(reminder.created) == 1
    when, event, extra = reminder.created[0]
    assert event == votes.VOTE_REMINDER_EVENT
    assert extra["user_id"] == 4242
    assert "voted_at" in extra
    now = datetime.datetime.now(UTC)
    assert now + datetime.timedelta(hours=11, minutes=55) < when
    assert when < now + datetime.timedelta(hours=12, minutes=5)


async def test_scheduling_cancels_any_earlier_pending_reminder_first(fake_pool):
    """Cancel-then-reschedule: the scoped DELETE runs before the INSERT, keyed
    on (event, user_id) - the same shape reminders.cancel_reminder uses for
    its own event, just a different key."""
    reminder = _FakeReminder()
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), reminder=reminder)
    fake_pool.fetchval_return = {"vote_reminder": True}
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    deletes = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(deletes) == 1
    query, args = deletes[0][1], deletes[0][2]
    assert "DELETE FROM timers" in query
    assert votes.VOTE_REMINDER_EVENT in args
    assert "4242" in args
    assert "claimed_at IS NULL" in query


async def test_a_replayed_vote_never_touches_the_reminder(fake_pool):
    reminder = _FakeReminder()
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), reminder=reminder)
    fake_pool.fetchval_return = {"vote_reminder": True}
    fake_pool.fetchrow_return = _row(replayed=True)

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    assert reminder.created == []


async def test_no_reminder_cog_costs_nothing_but_a_skipped_schedule(fake_pool):
    """Additive: a process without the Reminder cog loaded must not error, and
    must not even read the preference (nothing to act on it with)."""
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), reminder=None)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote(user="4242"))

    assert not any(c[0] == "fetchval" for c in fake_pool.calls)


async def test_a_failing_preference_lookup_never_breaks_the_vote(fake_pool):
    async def boom(*args, **kwargs):
        raise RuntimeError("settings down")

    reminder = _FakeReminder()
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), reminder=reminder)
    fake_pool.fetchrow_return = _row()
    fake_pool.fetchval = boom

    await Votes(bot).on_dbl_vote(_vote(user="4242"))  # must not raise

    assert reminder.created == []


# ---------------------------------------------------------------------------
# V2: the opt-in reminder - firing (on_vote_reminder_timer_complete)
# ---------------------------------------------------------------------------


async def test_the_reminder_dms_when_still_opted_in_and_not_revoted(fake_pool):
    user = _FakeUser(4242)
    bot = _make_rich_bot(fake_pool, user=user)
    fake_pool.fetchval_return = {"vote_reminder": True}
    voted_at = datetime.datetime.now(UTC) - datetime.timedelta(hours=12)
    fake_pool.fetchrow_return = {"last_vote_at": voted_at}

    await Votes(bot).on_vote_reminder_timer_complete(
        {"user_id": 4242, "voted_at": voted_at.isoformat()}
    )

    assert len(user.sent) == 1


async def test_the_reminder_is_silent_when_the_preference_is_now_off(fake_pool):
    user = _FakeUser(4242)
    bot = _make_rich_bot(fake_pool, user=user)
    fake_pool.fetchval_return = {"vote_reminder": False}

    await Votes(bot).on_vote_reminder_timer_complete(
        {"user_id": 4242, "voted_at": datetime.datetime.now(UTC).isoformat()}
    )

    assert user.sent == []


async def test_the_reminder_is_silent_when_the_user_already_revoted(fake_pool):
    """A newer vote landed since this reminder was scheduled - it already
    (re)scheduled its own, so this firing would be a duplicate nag."""
    user = _FakeUser(4242)
    bot = _make_rich_bot(fake_pool, user=user)
    fake_pool.fetchval_return = {"vote_reminder": True}
    scheduled_for = datetime.datetime.now(UTC) - datetime.timedelta(hours=13)
    newer_vote = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    fake_pool.fetchrow_return = {"last_vote_at": newer_vote}

    await Votes(bot).on_vote_reminder_timer_complete(
        {"user_id": 4242, "voted_at": scheduled_for.isoformat()}
    )

    assert user.sent == []


async def test_the_reminder_is_silent_after_the_user_erased_their_data(fake_pool):
    """`?mydata deleteprofile` deletes the topgg_votes row but not a timer that
    was already pending. Erasure wins: a member who just asked to be forgotten
    must not then be DMed about voting again."""
    user = _FakeUser(4242)
    bot = _make_rich_bot(fake_pool, user=user)
    fake_pool.fetchval_return = {"vote_reminder": True}
    fake_pool.fetchrow_return = None  # the ledger row is gone

    await Votes(bot).on_vote_reminder_timer_complete(
        {"user_id": 4242, "voted_at": datetime.datetime.now(UTC).isoformat()}
    )

    assert user.sent == []


async def test_the_reminder_ignores_a_payload_with_no_user_id(fake_pool):
    bot = _make_rich_bot(fake_pool)

    await Votes(bot).on_vote_reminder_timer_complete({})  # must not raise

    assert fake_pool.calls == []


async def test_a_failing_precondition_check_never_raises(fake_pool):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    bot = _make_rich_bot(fake_pool, user=_FakeUser(4242))
    fake_pool.fetchval = boom

    await Votes(bot).on_vote_reminder_timer_complete(
        {"user_id": 4242, "voted_at": datetime.datetime.now(UTC).isoformat()}
    )  # must not raise


# ---------------------------------------------------------------------------
# V2: the lazy top.gg catch-up poll (/vote open)
# ---------------------------------------------------------------------------


async def test_catch_up_is_skipped_when_the_ledger_is_fresh(fake_pool):
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, webstats=_FakeWebstats(client))
    fresh_row = _row(last_vote_at=datetime.datetime.now(UTC) - datetime.timedelta(hours=1))

    result = await Votes(bot)._maybe_catch_up(4242, fresh_row)

    assert result is fresh_row
    assert client.calls == []


async def test_catch_up_polls_top_gg_when_there_is_no_row_at_all(fake_pool):
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), webstats=_FakeWebstats(client))
    fake_pool.fetchrow_return = _row()

    result = await Votes(bot)._maybe_catch_up(4242, None)

    assert client.calls == [4242]
    assert result is not None


async def test_catch_up_polls_top_gg_when_the_row_is_stale(fake_pool):
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), webstats=_FakeWebstats(client))
    stale_row = _row(
        last_vote_at=datetime.datetime.now(UTC) - datetime.timedelta(hours=13)
    )
    fake_pool.fetchrow_return = _row()

    result = await Votes(bot)._maybe_catch_up(4242, stale_row)

    assert client.calls == [4242]
    assert result is not stale_row


async def test_catch_up_records_nothing_when_top_gg_says_no(fake_pool):
    client = _FakeDBLClient(voted=False)
    bot = _make_rich_bot(fake_pool, webstats=_FakeWebstats(client))

    result = await Votes(bot)._maybe_catch_up(4242, None)

    assert client.calls == [4242]
    assert result is None
    assert fake_pool.calls == []  # top.gg said no, so RECORD_VOTE never ran


async def test_catch_up_uses_the_default_boost_not_the_weekend_one(fake_pool):
    """is_weekend is unknown from this path - the catch-up must never guess
    weekend-double, only ever the normal 12h boost."""
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), webstats=_FakeWebstats(client))
    fake_pool.fetchrow_return = _row()

    await Votes(bot)._maybe_catch_up(4242, None)

    (method, query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert query == votes.RECORD_VOTE
    assert args[1] == votes.BOOST_HOURS


async def test_catch_up_records_under_the_wider_replay_floor(fake_pool):
    """The one argument that differs from the webhook's call. top.gg's /check
    only says "voted in the last 12h", which may well be the vote our ledger
    already holds, so the statement is handed the SAME 12h as the staleness
    check that got us here - the DB clock then re-decides what this method
    decided on the app clock, and no drift between the two can bank one vote
    twice. The webhook's own one-hour floor is untouched (see the V1 tests)."""
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), webstats=_FakeWebstats(client))
    fake_pool.fetchrow_return = _row()

    await Votes(bot)._maybe_catch_up(4242, None)

    (method, query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert args[3] == votes.CATCHUP_STALE_HOURS
    assert args[3] > votes.REPLAY_WINDOW_HOURS


async def test_a_catch_up_the_ledger_already_had_sends_nothing(fake_pool):
    """The DB refused it as a replay (see the floor above): no thank-you DM, no
    reminder, no second streak step - only the boost re-armed from the deadline
    the row already carried."""
    client = _FakeDBLClient(voted=True)
    user = _FakeUser(4242)
    reminder = _FakeReminder()
    leveling = _FakeLeveling()
    bot = _make_rich_bot(
        fake_pool,
        leveling=leveling,
        reminder=reminder,
        webstats=_FakeWebstats(client),
        user=user,
    )
    fake_pool.fetchval_return = {"vote_reminder": True}
    fake_pool.fetchrow_return = _row(replayed=True)

    await Votes(bot)._maybe_catch_up(4242, None)

    assert user.sent == []
    assert reminder.created == []
    assert leveling.armed


async def test_catch_up_is_bounded_to_one_poll_per_cooldown_window(fake_pool):
    client = _FakeDBLClient(voted=False)
    bot = _make_rich_bot(fake_pool, webstats=_FakeWebstats(client))
    cog = Votes(bot)

    await cog._maybe_catch_up(4242, None)
    await cog._maybe_catch_up(4242, None)

    assert client.calls == [4242]  # the second call never reached top.gg


async def test_catch_up_is_bounded_process_wide_too(fake_pool):
    """The per-user cooldown is only the fairness half. Every first-ever /vote
    open by a member who never voted reads as stale, so a spike of DISTINCT
    members would each pass their own cooldown - the process-wide ceiling is
    what keeps that off the top.gg token the autopost shares."""
    client = _FakeDBLClient(voted=False)
    bot = _make_rich_bot(fake_pool, webstats=_FakeWebstats(client))
    cog = Votes(bot)

    for user_id in range(votes.CATCHUP_GLOBAL_LIMIT + 10):
        await cog._maybe_catch_up(user_id, None)

    assert len(client.calls) == votes.CATCHUP_GLOBAL_LIMIT


async def test_a_member_refused_by_the_ceiling_keeps_their_own_slot(fake_pool):
    """A rejection consumes nothing on the axis that rejected it: the member
    the ceiling turned away has not burnt their per-user cooldown, so their
    next open still gets a poll once the window frees."""
    client = _FakeDBLClient(voted=False)
    bot = _make_rich_bot(fake_pool, webstats=_FakeWebstats(client))
    cog = Votes(bot)
    for user_id in range(votes.CATCHUP_GLOBAL_LIMIT):
        await cog._maybe_catch_up(user_id, None)

    await cog._maybe_catch_up(4242, None)  # refused by the ceiling
    assert 4242 not in [c for c in client.calls]
    assert not cog._catchup_cooldown.is_active(4242)


async def test_catch_up_skips_silently_without_a_webstats_cog(fake_pool):
    bot = _make_rich_bot(fake_pool)  # no Webstats

    result = await Votes(bot)._maybe_catch_up(4242, None)

    assert result is None
    assert fake_pool.calls == []


async def test_catch_up_skips_silently_when_topgg_is_not_configured(fake_pool):
    bot = _make_rich_bot(fake_pool, webstats=_FakeWebstats(client=None))

    result = await Votes(bot)._maybe_catch_up(4242, None)

    assert result is None


async def test_a_failing_topgg_poll_costs_only_a_log_line(fake_pool):
    client = _FakeDBLClient(raises=True)
    bot = _make_rich_bot(fake_pool, webstats=_FakeWebstats(client))

    result = await Votes(bot)._maybe_catch_up(4242, None)  # must not raise

    assert result is None
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# V2: is_recent_supporter / get_last_vote_at (the profile badge)
# ---------------------------------------------------------------------------


def test_a_never_voted_user_is_not_a_supporter():
    assert votes.is_recent_supporter(None, datetime.datetime.now(UTC)) is False


def test_a_vote_inside_the_window_is_a_supporter():
    now = datetime.datetime.now(UTC)
    assert votes.is_recent_supporter(now - datetime.timedelta(days=6), now) is True


def test_a_vote_outside_the_window_is_not_a_supporter():
    now = datetime.datetime.now(UTC)
    assert votes.is_recent_supporter(now - datetime.timedelta(days=8), now) is False


def test_the_supporter_window_is_keyed_on_last_vote_not_the_boost():
    """Documented divergence: boost_expires_at is gone within 12-24h, but the
    badge window is a full week - see SUPPORTER_WINDOW_DAYS's docstring."""
    assert votes.SUPPORTER_WINDOW_DAYS == 7
    assert votes.SUPPORTER_WINDOW_DAYS * 24 > votes.WEEKEND_BOOST_HOURS


async def test_get_last_vote_at_reads_the_one_indexed_column(fake_pool):
    stamp = datetime.datetime.now(UTC)
    fake_pool.fetchval_return = stamp

    result = await votes.get_last_vote_at(fake_pool, 4242)

    assert result is stamp
    (method, query, args), = fake_pool.calls
    assert method == "fetchval"
    assert "topgg_votes" in query
    assert args == (4242,)


async def test_get_last_vote_at_is_none_for_a_never_voted_user(fake_pool):
    fake_pool.fetchval_return = None
    assert await votes.get_last_vote_at(fake_pool, 4242) is None


# ---------------------------------------------------------------------------
# V2: the /vote card - status lines and the command itself
# ---------------------------------------------------------------------------


def _walk(node):
    """Every nested Components V2 payload dict inside a to_components() tree."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (list, dict)):
                yield from _walk(value)


def _texts(view):
    """Every TextDisplay's content in a rendered LayoutView, joined together."""
    return "\n".join(
        node["content"]
        for node in _walk(view.to_components())
        if node.get("type") == 10
    )


def test_a_never_voted_member_sees_the_invitation_shape_not_an_error():
    view = votes.VoteStatusView(999, None)
    text = _texts(view)
    assert "none yet" in text
    assert "0" in text  # streak and lifetime votes both render as 0


def test_an_active_booster_sees_their_boost_deadline():
    row = _row(streak=5, total=20)
    view = votes.VoteStatusView(999, row)
    text = _texts(view)
    assert "5" in text
    assert "20" in text


def test_the_card_always_links_to_this_bots_own_vote_page():
    view = votes.VoteStatusView(999, None)
    urls = [node.get("url") for node in _walk(view.to_components()) if "url" in node]
    assert urls == [votes.vote_url(999)] == ["https://top.gg/bot/999/vote"]


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _VoteCtx:
    def __init__(self, author_id):
        self.author = types.SimpleNamespace(id=author_id)
        self.sends = []
        self.typings = []

    def typing(self, **kwargs):
        # discord.py's real Context.typing takes ephemeral as a keyword-only
        # argument and, on a slash invocation, IS the defer - so what is passed
        # here decides whether the deferred response is public.
        self.typings.append(kwargs)
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


async def test_the_vote_command_sends_an_ephemeral_card(fake_pool):
    bot = _make_rich_bot(fake_pool)  # no Webstats -> catch-up skips silently
    cog = Votes(bot)
    ctx = _VoteCtx(4242)

    await Votes.vote.callback(cog, ctx)

    assert len(ctx.sends) == 1
    _args, kwargs = ctx.sends[0]
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["view"], votes.VoteStatusView)


async def test_the_vote_command_defers_ephemerally_too(fake_pool):
    """The card is private, so the DEFER has to be private as well: a plain
    ctx.typing() on a slash invocation defers publicly and leaves a visible
    "thinking" placeholder that an ephemeral followup never resolves."""
    bot = _make_rich_bot(fake_pool)
    ctx = _VoteCtx(4242)

    await Votes.vote.callback(Votes(bot), ctx)

    assert ctx.typings == [{"ephemeral": True}]


async def test_the_vote_command_shows_a_row_the_ledger_already_has(fake_pool):
    bot = _make_rich_bot(fake_pool)
    fake_pool.fetchrow_return = {
        "last_vote_at": datetime.datetime.now(UTC),
        "streak": 9,
        "total_votes": 40,
        "boost_expires_at": _expiry(),
    }
    cog = Votes(bot)
    ctx = _VoteCtx(4242)

    await Votes.vote.callback(cog, ctx)

    _args, kwargs = ctx.sends[0]
    text = _texts(kwargs["view"])
    assert "9" in text
    assert "40" in text


async def test_the_vote_command_runs_the_catch_up_when_the_ledger_is_stale(fake_pool):
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), webstats=_FakeWebstats(client))
    fake_pool.fetchrow_return = None  # no row at all -> stale -> catch-up polls
    cog = Votes(bot)
    ctx = _VoteCtx(4242)

    await Votes.vote.callback(cog, ctx)

    assert client.calls == [4242]


# ---------------------------------------------------------------------------
# WAVE-B-B1: a catch-up row must not make the NEXT real vote look like a replay
# ---------------------------------------------------------------------------
#
# THE BUG: the catch-up poll banks a vote on the evidence "top.gg says this
# member voted some time in the last 12h" and can only stamp last_vote_at =
# now() for it. top.gg then lets that member vote again as soon as their TRUE
# 12h is up - which can be minutes later - and that genuine webhook delivery
# landed inside the one-hour replay floor and was thrown away: no streak step,
# no lifetime count, no thank-you DM.
#
# THE FIX: the row remembers WHICH kind of write stamped it (topgg_votes
# .caught_up), and the replay predicate yields to it for a webhook delivery.
#
# The SQL's own semantics are proven against a real Postgres (the lot's probe
# transcript: DDL applied twice, then both orders), not re-implemented here:
#   catch-up, then a webhook 5 min later -> streak 1 -> 2, total 1 -> 2,
#                                           caught_up t -> f, replayed false
#   that webhook, then another 5 min later -> streak stays 2, replayed TRUE
#   webhook, then a webhook 5 min later    -> streak stays 1, replayed TRUE
#   catch-up, then a catch-up 5 min later  -> streak stays 1, caught_up stays t
# What these tests hold is the shape of the statement and the arguments each
# caller hands it - the halves that live in this repo.


def test_the_replay_predicate_yields_to_a_catch_up_stamped_row():
    """The predicate's second half. Without it the one-hour floor swallows the
    first REAL vote after any catch-up."""
    query = votes.RECORD_VOTE
    assert "AND ($5 OR NOT topgg_votes.caught_up)" in query
    # In EVERY branch, not just the one that decides last_vote_at: a streak or
    # a lifetime count that moved while the timestamp did not would be worse
    # than either behaviour on its own.
    assert query.count(_REPLAY_GUARD) == 5


def test_the_row_remembers_which_kind_of_write_stamped_it():
    """caught_up is set from the SAME parameter the predicate reads, on both
    the insert and the update - so the first webhook after a catch-up counts
    the vote AND clears the flag, and the one after that is a replay again."""
    query = votes.RECORD_VOTE
    columns = query.split("VALUES", 1)[0]
    assert "caught_up" in columns
    values_line = query.split("VALUES", 1)[1].splitlines()[0]
    assert values_line.rstrip().endswith("$5)")  # seeded on the FIRST vote too
    assert "caught_up = CASE" in query
    assert "THEN topgg_votes.caught_up\n        ELSE $5" in query


def test_the_two_flag_values_say_what_they_mean():
    assert votes.FROM_WEBHOOK is False
    assert votes.FROM_CATCHUP is True


async def test_a_webhook_vote_is_recorded_as_a_webhook_vote(fake_pool):
    bot, _ = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()

    await Votes(bot).on_dbl_vote(_vote())

    (method, query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert args[3] == votes.REPLAY_WINDOW_HOURS  # the narrow floor, unchanged
    assert args[4] is votes.FROM_WEBHOOK


async def test_a_catch_up_marks_the_row_it_stamps(fake_pool):
    """The write that stamps now() for a vote it only knows happened "some time
    in the last 12h" is exactly the one that must be marked as soft."""
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), webstats=_FakeWebstats(client))
    fake_pool.fetchrow_return = _row()

    await Votes(bot)._maybe_catch_up(4242, None)

    (method, query, args), = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert args[3] == votes.CATCHUP_STALE_HOURS
    assert args[4] is votes.FROM_CATCHUP


async def test_a_catch_up_then_a_real_webhook_vote_both_count(fake_pool):
    """The order the bug was about. Both writes go to the same statement, and
    the two arguments that differ are what let the DB tell them apart: the
    catch-up marks the row, the webhook that follows overrides the floor,
    counts, and clears the mark (streak 1 -> 2 in the probe)."""
    client = _FakeDBLClient(voted=True)
    bot = _make_rich_bot(fake_pool, leveling=_FakeLeveling(), webstats=_FakeWebstats(client))
    fake_pool.fetchrow_return = _row()
    cog = Votes(bot)

    await cog._maybe_catch_up(4242, None)
    await cog.on_dbl_vote(_vote())

    writes = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert [c[1] for c in writes] == [votes.RECORD_VOTE, votes.RECORD_VOTE]
    catch_up, webhook = (c[2] for c in writes)
    assert (catch_up[3], catch_up[4]) == (votes.CATCHUP_STALE_HOURS, True)
    assert (webhook[3], webhook[4]) == (votes.REPLAY_WINDOW_HOURS, False)


async def test_two_webhook_deliveries_are_still_one_vote(fake_pool):
    """The redelivery the floor exists for is untouched: both deliveries hand
    the statement the SAME narrow floor and the SAME flag, so the second one
    finds a row that is neither old enough nor marked, and changes nothing."""
    bot, _ = _make_bot(fake_pool)
    fake_pool.fetchrow_return = _row()
    cog = Votes(bot)

    await cog.on_dbl_vote(_vote())
    await cog.on_dbl_vote(_vote())

    writes = [c[2] for c in fake_pool.calls if c[0] == "fetchrow"]
    assert len(writes) == 2
    assert writes[0][3:] == writes[1][3:] == (votes.REPLAY_WINDOW_HOURS, False)
