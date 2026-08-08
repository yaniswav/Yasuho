"""Unit tests for cogs.community.votes (top.gg vote rewards, V1).

The cog is silent infrastructure: one listener, one upsert, one cross-cog hand
-off. These drive it against fakes for the four things it owns:

* the payload boundary - a test vote is dropped, a string user id is int()ed,
  a missing/junk one is refused, and ``is_weekend`` picks the boost window;
* the write itself - ONE statement, on the right table, with the right
  parameters (the SQL's own semantics are proven against a real Postgres, see
  the lot's probe transcript, not re-implemented here);
* the hand-off to the Leveling cog's in-memory boost map, and its refusal to
  turn any failure of that into a failure of the vote;
* the erasure seam forget_vote_boost, the twin of
  presence.forget_collected_presence.
"""

import datetime
import logging
import re
import types

import pytest

from cogs.community import votes
from cogs.community.votes import Votes

UTC = datetime.timezone.utc


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
    """A bot exposing exactly the two seams the cog uses: db_pool and get_cog."""
    cog = leveling() if isinstance(leveling, type) else leveling
    return types.SimpleNamespace(
        db_pool=fake_pool,
        get_cog=lambda name: cog if name == "Leveling" else None,
    ), cog


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


def _row(streak=1, total=1, expires=None, replayed=False):
    return {
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
    guard = (
        "WHEN now() - topgg_votes.last_vote_at\n"
        "             BETWEEN INTERVAL '0 hours' AND $4 * INTERVAL '1 hour'"
    )
    # One per SET column: nothing may move on a replay.
    assert query.count(guard) == 4
    for kept in (
        "THEN topgg_votes.last_vote_at",
        "THEN topgg_votes.streak\n",
        "THEN topgg_votes.total_votes\n",
        "THEN topgg_votes.boost_expires_at\n",
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
            r"(\w+)\.(?:user_id|last_vote_at|streak|total_votes|boost_expires_at)",
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
