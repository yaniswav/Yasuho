"""Per-channel MUTED-USER list for the AniList activity feed (LOT A1).

Muting hides a FOLLOWED AniList user in ONE feed channel. The follow itself is
untouched: the poller keeps fetching that user, the global cursor keeps seeing
their activity, and a second feed following them keeps receiving it. These tests
pin exactly that separation, because getting it wrong is silent and expensive -
a mute that reached the cursor would make one channel's preference skip an
activity for every channel, for ever (the marks only advance).

What is pinned here:

* the skip is DELIVERY-ONLY: a muted user's activity still advances the tick's
  high-water marks (``_save_state``) and still lands in a second channel that
  follows them unmuted,
* the FETCH SCOPE is untouched: the muted user is still in the user id list sent
  to AniList,
* the mute rows are loaded once per tick, only for enabled feeds, and only on
  the delivery path (nothing above the cursor maths reads them),
* the per-feed cap is MAX_FOLLOWS_PER_FEED and it only blocks a genuinely new
  mute,
* a mute cannot outlive the follow it qualifies (unfollow) nor the feed it
  belongs to (feed delete).

Everything is offline: the cog is built with ``__new__`` and fed hand-rolled
fakes, exactly like tests/cogs/test_anilist_feed_pacing.py.
"""

import types

import pytest

from cogs.anilist import feed as feed_mod
from cogs.anilist import feed_policy as af
from cogs.anilist.feed import AniListFeed

GUILD = 1
MUTED_USER = 7
OTHER_USER = 9


# --- Fakes ------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class _FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        # resolve_guild_locale returns the default locale for a None guild, so
        # delivery needs no locale DB access here.
        self.guild = None
        self.sends = []
        self._next_message_id = 5000

    def is_nsfw(self):
        return False

    async def send(self, **kwargs):
        self._next_message_id += 1
        self.sends.append(kwargs)
        return _FakeMessage(self._next_message_id)

    def get_partial_message(self, message_id):  # pragma: no cover - unused here
        raise AssertionError("text activities never coalesce")


class _FakePool:
    """Records every statement; answers the few reads these tests trigger."""

    def __init__(self, *, rows=None, values=None, returning=1):
        self.executes = []
        self.fetches = []
        self.fetchvals = []
        self.fetchrows = []
        self._rows = rows or {}
        self._values = values or {}
        # What an in-transaction ``DELETE ... RETURNING`` answers: 1 = a row was
        # there, None = it had already gone (the race the caller must not
        # confirm). Kept apart from ``values`` because None is that dict's
        # "no match" answer and could not express an empty delete.
        self.returning = returning

    def _match(self, table, mapping):
        for key, value in mapping.items():
            if key in table:
                return value
        return None

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "DELETE 1"

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return self._match(sql, self._rows) or []

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        value = self._match(sql, self._values)
        return 0 if value is None else value

    async def fetchrow(self, sql, *args):
        self.fetchrows.append((sql, args))
        return self._match(sql, self._rows)

    def acquire(self):
        return _Acquire(self)


class _Acquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return _Connection(self._pool)

    async def __aexit__(self, *exc):
        return False


class _Connection:
    """Every statement lands in ``pool.executes``, whatever verb ran it.

    ``fetchval``/``fetchrow`` record there too so the ordering assertions can
    read one statement log: inside a transaction a ``DELETE ... RETURNING`` is
    just another statement, and splitting it into a second list would let a
    reordering slip past unnoticed.
    """

    def __init__(self, pool):
        self._pool = pool

    def transaction(self):
        return _Transaction()

    async def execute(self, sql, *args):
        self._pool.executes.append((sql, args))
        return "DELETE 1"

    async def fetchval(self, sql, *args):
        self._pool.executes.append((sql, args))
        return self._pool.returning

    async def fetchrow(self, sql, *args):
        self._pool.executes.append((sql, args))
        return self._pool._match(sql, self._pool._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeBot:
    def __init__(self, pool, channels):
        self.db_pool = pool
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def wait_until_ready(self):
        return None


def _cog(channels=None, pool=None):
    cog = AniListFeed.__new__(AniListFeed)
    cog.bot = _FakeBot(pool or _FakePool(), channels or {})
    return cog


def _feed_row(channel_id, guild_id=GUILD):
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "types": ["TEXT"],
        "fail_count": 0,
    }


def _text_activity(activity_id, user_id=MUTED_USER):
    """A TEXT activity: never coalescible, so delivery is one plain send."""

    return {
        "id": activity_id,
        "kind": "TextActivity",
        "type": "TEXT",
        "user_id": user_id,
        "user": {"id": user_id, "name": "reader"},
        "created_at": 1_700_000_000 + activity_id,
        "site_url": "https://anilist.co/activity/%s" % activity_id,
        "is_adult": False,
        "text": "hello",
    }


def _act(activity_id, user_id, type="TEXT"):
    """The minimal shape route_activities reads."""

    return {"id": activity_id, "type": type, "user_id": user_id, "is_adult": False}


# ---------------------------------------------------------------------------
# route_activities: the mute is one skip at DELIVERY, per channel
# ---------------------------------------------------------------------------


def test_route_hides_a_muted_user_from_that_channel_only():
    """THE feature: one activity, two feeds following the same user, one of them
    muting them. The muting channel renders nothing; the other is untouched."""

    activities = [_act(1, MUTED_USER)]
    feeds = [
        {
            "channel_id": 10,
            "types": {"TEXT"},
            "followed_ids": {MUTED_USER},
            "muted_ids": {MUTED_USER},
            "allow_adult": False,
        },
        {
            "channel_id": 20,
            "types": {"TEXT"},
            "followed_ids": {MUTED_USER},
            "muted_ids": set(),
            "allow_adult": False,
        },
    ]

    routed = af.route_activities(activities, feeds)

    assert 10 not in routed  # muted here...
    assert [a["id"] for a in routed[20]] == [1]  # ...delivered there


def test_a_mute_only_hides_the_muted_user_not_the_whole_feed():
    activities = [_act(1, MUTED_USER), _act(2, OTHER_USER)]
    feeds = [
        {
            "channel_id": 10,
            "types": {"TEXT"},
            "followed_ids": {MUTED_USER, OTHER_USER},
            "muted_ids": {MUTED_USER},
            "allow_adult": False,
        }
    ]

    routed = af.route_activities(activities, feeds)

    assert [a["id"] for a in routed[10]] == [2]


def test_muting_someone_the_feed_does_not_follow_changes_nothing():
    # A stale mute (e.g. left by a hand-edited row) must not shadow anyone else.
    activities = [_act(1, OTHER_USER)]
    feeds = [
        {
            "channel_id": 10,
            "types": {"TEXT"},
            "followed_ids": {OTHER_USER},
            "muted_ids": {MUTED_USER},
            "allow_adult": False,
        }
    ]

    assert [a["id"] for a in af.route_activities(activities, feeds)[10]] == [1]


def test_a_feed_dict_without_muted_ids_routes_exactly_as_before():
    """``muted_ids`` is optional: every pre-existing caller keeps its behaviour."""

    activities = [_act(1, MUTED_USER)]
    feed = {
        "channel_id": 10,
        "types": {"TEXT"},
        "followed_ids": {MUTED_USER},
        "allow_adult": False,
    }

    assert [a["id"] for a in af.route_activities(activities, [feed])[10]] == [1]
    assert af.route_activities(activities, [{**feed, "muted_ids": None}])[10]


# ---------------------------------------------------------------------------
# _dispatch threads the mutes in, and only there
# ---------------------------------------------------------------------------


async def test_dispatch_delivers_to_the_unmuted_channel_and_not_the_muted_one():
    """The same proof one level up, through the real delivery path."""

    channels = {100: _FakeChannel(100), 200: _FakeChannel(200)}
    cog = _cog(channels)
    items = [_text_activity(1)]

    await cog._dispatch(
        [_feed_row(100), _feed_row(200)],
        {(GUILD, 100): {MUTED_USER}, (GUILD, 200): {MUTED_USER}},
        items,
        mutes_by_channel={(GUILD, 100): {MUTED_USER}},
    )

    assert channels[100].sends == []
    assert len(channels[200].sends) == 1


async def test_dispatch_without_mutes_is_unchanged():
    channels = {100: _FakeChannel(100)}
    cog = _cog(channels)

    await cog._dispatch(
        [_feed_row(100)], {(GUILD, 100): {MUTED_USER}}, [_text_activity(1)]
    )

    assert len(channels[100].sends) == 1


async def test_a_mute_is_scoped_to_its_own_guild_channel_pair():
    """Two guilds can use the same channel id in their own row space, and the
    mute map is keyed by the (guild, channel) pair the follows already use."""

    channels = {100: _FakeChannel(100)}
    cog = _cog(channels)

    await cog._dispatch(
        [_feed_row(100, guild_id=GUILD)],
        {(GUILD, 100): {MUTED_USER}},
        [_text_activity(1)],
        # A mute belonging to a DIFFERENT guild's feed on the same channel id.
        mutes_by_channel={(GUILD + 1, 100): {MUTED_USER}},
    )

    assert len(channels[100].sends) == 1


# ---------------------------------------------------------------------------
# THE invariant: the cursor and the fetch scope never see a mute
# ---------------------------------------------------------------------------


def _tick_harness(cog, monkeypatch, *, feeds, follows, mutes, activities):
    """Stub _tick's I/O, keeping the cursor maths and _dispatch REAL.

    Returns a dict recording what the tick fetched (``fetched_ids``), what it
    saved to the cursor (``saved``) and whether the mute rows were read at all
    (``loaded_mutes``).
    """

    seen = {"fetched_ids": None, "saved": None, "loaded_mutes": 0}

    async def _noop(*args, **kwargs):
        return None

    async def _load_feeds():
        return feeds

    async def _load_follows():
        return follows

    async def _load_mutes():
        seen["loaded_mutes"] += 1
        # Loaded only after every cursor decision is made: if this ever ran
        # before the marks were computed the assertion below would still hold,
        # but the ORDER is the point, so record it.
        assert seen["saved"] is None
        return mutes

    async def _load_state():
        return 0, 1_000

    async def _fetch(user_ids, last_created):
        seen["fetched_ids"] = list(user_ids)
        return list(activities), None

    async def _save_state(last_id, last_created):
        seen["saved"] = (last_id, last_created)

    monkeypatch.setattr(feed_mod, "_monotonic", lambda: 0.0)
    monkeypatch.setattr(feed_mod, "_normalize", lambda raw: raw)
    cog._embargo_until = 0
    cog._prune_coalesce_posts = _noop
    cog._load_feeds = _load_feeds
    cog._load_follows = _load_follows
    cog._load_mutes = _load_mutes
    cog._load_state = _load_state
    cog._fetch_activities = _fetch
    cog._save_state = _save_state
    return seen


def _mute_row(channel_id, user_id=MUTED_USER, guild_id=GUILD):
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "anilist_user_id": user_id,
    }


def _follow_row(channel_id, user_id=MUTED_USER, guild_id=GUILD):
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "anilist_user_id": user_id,
    }


async def test_a_muted_activity_still_advances_the_global_cursor(monkeypatch):
    """THE invariant. The only feed in the fleet mutes the only followed user,
    so nothing is rendered anywhere - and the tick STILL advances both high-water
    marks past that activity. Were the mute applied any earlier, a single
    channel's preference would hold (or skip) the shared cursor for everybody.
    """

    channel = _FakeChannel(100)
    cog = _cog({100: channel})
    activity = _text_activity(42)
    seen = _tick_harness(
        cog,
        monkeypatch,
        feeds=[_feed_row(100)],
        follows=[_follow_row(100)],
        mutes=[_mute_row(100)],
        activities=[activity],
    )

    await cog._tick()

    assert channel.sends == []  # rendered nowhere...
    # ...yet the marks moved past it exactly as if it had been delivered.
    assert seen["saved"] == (activity["id"], activity["created_at"])


async def test_a_muted_user_is_still_fetched_from_anilist(monkeypatch):
    """The follow keeps being polled globally: muting is not an unfollow, so the
    user id stays in the batch sent to AniList (which another feed may need)."""

    cog = _cog({100: _FakeChannel(100)})
    seen = _tick_harness(
        cog,
        monkeypatch,
        feeds=[_feed_row(100)],
        follows=[_follow_row(100)],
        mutes=[_mute_row(100)],
        activities=[_text_activity(42)],
    )

    await cog._tick()

    assert seen["fetched_ids"] == [MUTED_USER]


async def test_one_channels_mute_never_hides_the_activity_from_another(monkeypatch):
    """Same tick, same activity: muted in channel 100, delivered in channel 200,
    and the cursor moves once for both."""

    channels = {100: _FakeChannel(100), 200: _FakeChannel(200)}
    cog = _cog(channels)
    activity = _text_activity(42)
    seen = _tick_harness(
        cog,
        monkeypatch,
        feeds=[_feed_row(100), _feed_row(200)],
        follows=[_follow_row(100), _follow_row(200)],
        mutes=[_mute_row(100)],
        activities=[activity],
    )

    await cog._tick()

    assert channels[100].sends == []
    assert len(channels[200].sends) == 1
    assert seen["saved"] == (activity["id"], activity["created_at"])


async def test_the_mute_rows_are_read_once_per_tick_and_only_when_delivering(
    monkeypatch,
):
    """Scale: one extra query per tick at most, and none at all on a tick with
    nothing fresh to deliver (the common case at 1000+ guilds)."""

    cog = _cog({100: _FakeChannel(100)})
    seen = _tick_harness(
        cog,
        monkeypatch,
        feeds=[_feed_row(100)],
        follows=[_follow_row(100)],
        mutes=[_mute_row(100)],
        activities=[_text_activity(42)],
    )
    await cog._tick()
    assert seen["loaded_mutes"] == 1

    quiet = _cog({100: _FakeChannel(100)})
    seen = _tick_harness(
        quiet,
        monkeypatch,
        feeds=[_feed_row(100)],
        follows=[_follow_row(100)],
        mutes=[_mute_row(100)],
        activities=[],  # nothing fetched -> nothing fresh
    )
    await quiet._tick()
    assert seen["loaded_mutes"] == 0


async def test_load_mutes_reads_only_enabled_feeds_and_touches_no_cursor():
    """The loader mirrors _load_follows: one statement, joined on enabled feeds,
    and it names neither anilist_feed_state nor anilist_follows."""

    pool = _FakePool()
    cog = _cog(pool=pool)

    await cog._load_mutes()

    (sql, args), = pool.fetches
    assert args == ()
    assert "FROM anilist_feed_mutes" in sql
    assert "fe.enabled = TRUE" in sql
    assert "anilist_feed_state" not in sql
    assert "anilist_follows" not in sql


# ---------------------------------------------------------------------------
# The per-feed cap
# ---------------------------------------------------------------------------


async def test_a_new_mute_is_blocked_at_the_follow_cap():
    # The cap is the follow cap: a feed can at most mute everyone it follows.
    pool = _FakePool(
        values={
            "SELECT 1 FROM anilist_feed_mutes": None,  # not muted yet
            "COUNT(*) FROM anilist_feed_mutes": af.MAX_FOLLOWS_PER_FEED,
        }
    )
    cog = _cog(pool=pool)

    error = await cog._add_mute(GUILD, 100, MUTED_USER, "reader")

    assert error is not None
    assert str(af.MAX_FOLLOWS_PER_FEED) in error
    assert pool.executes == []  # nothing stored past the cap


async def test_a_new_mute_is_accepted_just_under_the_cap():
    pool = _FakePool(
        values={
            "SELECT 1 FROM anilist_feed_mutes": None,
            "COUNT(*) FROM anilist_feed_mutes": af.MAX_FOLLOWS_PER_FEED - 1,
        }
    )
    cog = _cog(pool=pool)

    assert await cog._add_mute(GUILD, 100, MUTED_USER, "reader") is None
    (sql, args), = pool.executes
    assert "INSERT INTO anilist_feed_mutes" in sql
    assert args == (GUILD, 100, MUTED_USER, "reader")


async def test_re_muting_an_already_muted_user_is_never_blocked():
    """Re-muting adds no row (it only refreshes the cached name), so it must not
    be rejected at the cap - and it must not even cost the COUNT query."""

    pool = _FakePool(
        values={
            "SELECT 1 FROM anilist_feed_mutes": 1,  # already muted
            "COUNT(*) FROM anilist_feed_mutes": af.MAX_FOLLOWS_PER_FEED,
        }
    )
    cog = _cog(pool=pool)

    assert await cog._add_mute(GUILD, 100, MUTED_USER, "reader") is None
    assert len(pool.fetchvals) == 1  # the existence probe only, no COUNT
    assert "ON CONFLICT" in pool.executes[0][0]


# ---------------------------------------------------------------------------
# A mute cannot outlive what it qualifies
# ---------------------------------------------------------------------------


async def test_unfollowing_drops_the_mute_row_too():
    """Otherwise a later re-follow would arrive silently muted."""

    pool = _FakePool()
    cog = _cog(pool=pool)

    await cog._remove_follow(GUILD, 100, MUTED_USER)

    statements = [sql for sql, _args in pool.executes]
    assert any("DELETE FROM anilist_follows" in sql for sql in statements)
    assert any("DELETE FROM anilist_feed_mutes" in sql for sql in statements)
    for _sql, args in pool.executes:
        assert args == (GUILD, 100, MUTED_USER)


async def test_unfollowing_reports_whether_a_follow_row_was_actually_deleted():
    """The confirmation must not claim an unfollow the DELETE never made."""

    hit = _cog(pool=_FakePool(returning=1))
    assert await hit._remove_follow(GUILD, 100, MUTED_USER) is True

    miss = _cog(pool=_FakePool(returning=None))
    assert await miss._remove_follow(GUILD, 100, MUTED_USER) is False


async def test_moving_a_feed_carries_its_mutes_to_the_new_channel():
    """Otherwise the ON DELETE CASCADE below erases them.

    A mute left pointing at the old channel would be destroyed by the old feed
    row's delete, so a member who muted someone would silently start being
    posted at again after an admin moved the feed.
    """

    pool = _FakePool(
        rows={
            "SELECT types": {
                "types": ["TEXT"],
                "self_add": True,
                "enabled": True,
                "fail_count": 0,
            }
        }
    )
    cog = _cog(pool=pool)

    assert await cog._move_feed(GUILD, 100, 200) is None

    statements = [sql for sql, _args in pool.executes]
    moved = [sql for sql in statements if "SET channel_id" in sql]
    assert any("anilist_follows" in sql for sql in moved)
    assert any("anilist_channel_subs" in sql for sql in moved)
    assert any("anilist_feed_mutes" in sql for sql in moved)
    for sql, args in pool.executes:
        if "SET channel_id" in sql:
            assert args == (GUILD, 100, 200)


async def test_moving_a_feed_inserts_before_it_moves_and_deletes_last():
    """The FK to anilist_feeds is enforced on UPDATE and cascades on DELETE.

    Moving a child before the new feed row exists is a foreign key violation;
    deleting the old feed row before the children have moved cascades them away
    instead of moving them. Only insert -> move -> delete satisfies both.
    """

    pool = _FakePool(
        rows={
            "SELECT types": {
                "types": ["TEXT"],
                "self_add": True,
                "enabled": True,
                "fail_count": 0,
            }
        }
    )
    cog = _cog(pool=pool)

    await cog._move_feed(GUILD, 100, 200)

    statements = [sql for sql, _args in pool.executes]
    insert_at = next(
        i for i, sql in enumerate(statements) if "INSERT INTO anilist_feeds" in sql
    )
    delete_at = next(
        i for i, sql in enumerate(statements) if "DELETE FROM anilist_feeds" in sql
    )
    moves = [i for i, sql in enumerate(statements) if "SET channel_id" in sql]
    assert moves, "the children must be moved, not left behind"
    assert insert_at < min(moves)
    assert max(moves) < delete_at


async def test_deleting_a_feed_drops_its_mutes():
    pool = _FakePool()
    cog = _cog(pool=pool)

    await cog._delete_feed_rows(GUILD, 100)

    statements = [sql for sql, _args in pool.executes]
    assert any("DELETE FROM anilist_feed_mutes" in sql for sql in statements)
    # The mute rows go BEFORE the feed row they hang off, like the follows.
    mutes_at = next(
        i for i, sql in enumerate(statements) if "anilist_feed_mutes" in sql
    )
    feed_at = next(
        i for i, sql in enumerate(statements) if "DELETE FROM anilist_feeds" in sql
    )
    assert mutes_at < feed_at


async def test_removing_a_mute_reports_whether_a_row_was_there():
    pool = _FakePool()
    cog = _cog(pool=pool)

    assert await cog._remove_mute(GUILD, 100, MUTED_USER) is True
    (sql, args), = pool.executes
    assert "DELETE FROM anilist_feed_mutes" in sql
    assert args == (GUILD, 100, MUTED_USER)


# ---------------------------------------------------------------------------
# Surfaces: mute only makes sense for a followed user
# ---------------------------------------------------------------------------


async def test_a_username_is_resolved_against_this_feeds_follows_only():
    """No AniList call and no way to mute a stranger: the name is matched, case
    insensitively, against what THIS feed already follows."""

    pool = _FakePool(
        rows={
            "FROM anilist_follows": {
                "anilist_user_id": MUTED_USER,
                "anilist_username": "Reader",
            }
        }
    )
    cog = _cog(pool=pool)

    row = await cog._followed_user_by_name(GUILD, 100, "reader")

    assert row["anilist_user_id"] == MUTED_USER
    (sql, args), = pool.fetchrows
    assert "FROM anilist_follows" in sql
    assert "lower(anilist_username) = lower($3)" in sql
    assert args == (GUILD, 100, "reader")


@pytest.mark.parametrize("cap", [af.MAX_FOLLOWS_PER_FEED])
def test_the_mute_cap_is_the_follow_cap(cap):
    assert cap == 25


# ---------------------------------------------------------------------------
# A mute is MODERATOR state: a member cannot drop it by leaving and re-joining
# ---------------------------------------------------------------------------


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _MeCtx:
    """The minimum ``/anilistfeed me`` reads: a guild, an author, sends."""

    def __init__(self, guild_id=GUILD, author_id=4242):
        self.guild = types.SimpleNamespace(id=guild_id)
        self.author = types.SimpleNamespace(id=author_id, display_name="member")
        self.sent = []

    def typing(self):
        return _Typing()

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return content


class _FakeAniList:
    """The AniList cog ``/anilistfeed me`` borrows to resolve the viewer."""

    def __init__(self, viewer_id=MUTED_USER, name="reader"):
        self._viewer = {"id": viewer_id, "name": name}

    async def _token_status(self, user_id):
        return "ok", "token"

    async def _graphql(self, query, variables, token=None):
        return {"data": {"Viewer": dict(self._viewer)}}


async def _run_me(cog, ctx):
    """Drive the real ``/anilistfeed me`` callback against the fakes."""

    cog._resolve_target = lambda _ctx: _resolved(100)
    cog.bot.get_cog = lambda name: _FakeAniList() if name == "AniList" else None
    return await AniListFeed.anilistfeed_me.callback(cog, ctx)


async def _resolved(channel_id):
    return channel_id, None


async def test_a_member_leaving_a_feed_does_not_clear_their_mute():
    """THE fix: a mute is a moderator decision about a member, and the member
    must not be able to undo it by toggling ``/anilistfeed me`` twice.

    Leaving deletes the follow only. Were the mute deleted with it (what the
    moderator-driven unfollow rightly does), re-joining would arrive unmuted
    and a muted member would have talked their way back into the channel.
    """

    pool = _FakePool(
        rows={"SELECT self_add": {"self_add": True}},
        values={"SELECT 1 FROM anilist_follows": 1},  # they are followed -> leave
    )
    cog = _cog(pool=pool)
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    statements = [sql for sql, _args in pool.executes]
    assert any("DELETE FROM anilist_follows" in sql for sql in statements)
    assert not any("DELETE FROM anilist_feed_mutes" in sql for sql in statements)
    assert "left the AniList feed" in ctx.sent[-1]


async def test_a_moderator_unfollow_still_clears_the_mute():
    """The documented intent, unchanged: an ADMIN unfollow drops the mute so a
    later re-follow is not silently muted. Only the member's own leave differs.
    """

    pool = _FakePool()
    cog = _cog(pool=pool)

    await cog._remove_follow(GUILD, 100, MUTED_USER)

    statements = [sql for sql, _args in pool.executes]
    assert any("DELETE FROM anilist_feed_mutes" in sql for sql in statements)


async def test_clear_mute_false_deletes_the_follow_and_nothing_else():
    """The helper's two modes, at the statement level."""

    pool = _FakePool()
    cog = _cog(pool=pool)

    assert await cog._remove_follow(GUILD, 100, MUTED_USER, clear_mute=False) is True

    statements = [sql for sql, _args in pool.executes]
    assert [("DELETE FROM anilist_follows" in sql) for sql in statements] == [True]


async def test_re_joining_while_muted_says_so_instead_of_going_quiet():
    """The mute survives the leave, so a re-join can land on a hidden member.

    Confirming the join and then posting nothing forever would look broken;
    the surviving mute is stated instead, in its own msgid appended to the
    unchanged join line.
    """

    pool = _FakePool(
        rows={"SELECT self_add": {"self_add": True}},
        values={"SELECT 1 FROM anilist_feed_mutes": 1},  # not followed, still muted
    )
    cog = _cog(pool=pool)
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    statements = [sql for sql, _args in pool.executes]
    assert any("INSERT INTO anilist_follows" in sql for sql in statements)
    assert "joined the AniList feed" in ctx.sent[-1]
    assert "hidden your activity" in ctx.sent[-1]


async def test_re_joining_unmuted_says_only_that_it_joined():
    pool = _FakePool(rows={"SELECT self_add": {"self_add": True}})
    cog = _cog(pool=pool)
    ctx = _MeCtx()

    await _run_me(cog, ctx)

    assert "joined the AniList feed" in ctx.sent[-1]
    assert "hidden your activity" not in ctx.sent[-1]


def test_a_surviving_mute_hides_nobody_until_the_follow_returns():
    """Why keeping the row is safe: routing reads mutes only for FOLLOWED users,
    so the leftover row is inert while they are gone - and hides them again the
    moment they come back."""

    gone = {"channel_id": 100, "types": {"TEXT"}, "followed_ids": set(),
            "muted_ids": {MUTED_USER}}
    back = dict(gone, followed_ids={MUTED_USER})
    activities = [_act(1, MUTED_USER)]

    assert af.route_activities(activities, [gone]) == {}
    assert af.route_activities(activities, [back]) == {}
