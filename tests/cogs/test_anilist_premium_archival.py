"""AniList feed premium limits + lazy archival (M4a-2,
.claude/plans/monetisation/4-plan-retenu.md).

What is pinned here:

* :func:`cogs.anilist.helpers.resolve_guild_limits` degrades to FREE on a
  missing or raising ``bot.premium`` and never crashes - the resolver
  contract every poller/command cap in cogs/anilist reads through;
* ``_create_feed``/``_add_follow``/``_add_mute``/``_add_channel_sub`` refuse
  at the guild's CURRENT EFFECTIVE cap (free or premium) and the refusal
  message carries that effective number, not the FREE constant;
* ``_feed_archival``/``_feed_follow_archival``/``_feed_sub_archival`` order
  kept-first/oldest-first and cut at the limit, and re-classifying the SAME
  rows against a higher limit (a renewal, a freed slot) flips the verdict
  with zero bookkeeping - the "reactivation" story;
* the poller (``_load_feeds``/``_load_follows``/``_tick``) drops archived
  feeds and follows before they ever reach a fetch or a delivery;
* mutes stay fully EFFECTIVE however far over the follow cap a feed's follow
  count climbs - only adding a NEW one is refused at the cap;
* delete is never gated by any of this.

Everything is offline: the cog is built with ``__new__`` and fed hand-rolled
fakes, exactly like tests/cogs/test_anilist_feed_mutes.py.
"""

import datetime
import types

from cogs.anilist import feed as feed_mod
from cogs.anilist import feed_policy as af
from cogs.anilist import helpers as anilist_helpers
from cogs.anilist.feed import AniListFeed
from tools import premium

GUILD = 1


def _dt(seconds):
    return datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + (
        datetime.timedelta(seconds=seconds)
    )


# --- Fakes -------------------------------------------------------------------


class _FakeResolver:
    """A ``bot.premium``-shaped double: answers fixed limits, or raises."""

    def __init__(self, limits=None, raises=False):
        self._limits = limits
        self._raises = raises

    def for_guild(self, guild_id, *, now=None):
        if self._raises:
            raise RuntimeError("boom")
        return self._limits


_NO_PREMIUM = object()


class _FakePool:
    """Answers ``fetch``/``fetchval`` by the first matching SQL substring.

    ``fetch_results``/``fetchval_results`` are ``[(substring, value), ...]``
    checked in order - the first substring found in the statement wins.
    Unmatched statements get the empty/zero default. ``executes`` records
    every ``execute`` call verbatim, for assertions on what was (or was not)
    written.
    """

    def __init__(self, fetch_results=None, fetchval_results=None):
        self._fetch_results = fetch_results or []
        self._fetchval_results = fetchval_results or []
        self.executes = []

    async def fetch(self, sql, *args):
        for substring, rows in self._fetch_results:
            if substring in sql:
                return rows
        return []

    async def fetchval(self, sql, *args):
        for substring, value in self._fetchval_results:
            if substring in sql:
                return value
        return 0

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

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
    def __init__(self, pool):
        self._pool = pool

    def transaction(self):
        return _Transaction()

    async def execute(self, sql, *args):
        return await self._pool.execute(sql, *args)

    async def fetchval(self, sql, *args):
        return await self._pool.fetchval(sql, *args)

    async def fetchrow(self, sql, *args):
        return await self._pool.fetchrow(sql, *args)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeBot:
    def __init__(self, pool, premium_resolver=_NO_PREMIUM):
        self.db_pool = pool
        if premium_resolver is not _NO_PREMIUM:
            self.premium = premium_resolver

    async def wait_until_ready(self):
        return None


def _cog(pool=None, premium_resolver=_NO_PREMIUM):
    cog = AniListFeed.__new__(AniListFeed)
    cog.bot = _FakeBot(pool or _FakePool(), premium_resolver)
    return cog


# ---------------------------------------------------------------------------
# resolve_guild_limits: the resolver contract itself
# ---------------------------------------------------------------------------


def test_resolve_guild_limits_is_free_with_no_premium_attribute():
    bot = types.SimpleNamespace()  # no .premium at all - every test bot here
    assert anilist_helpers.resolve_guild_limits(bot, GUILD) is premium.GUILD_FREE


def test_resolve_guild_limits_is_free_when_the_resolver_raises():
    bot = types.SimpleNamespace(premium=_FakeResolver(raises=True))
    assert anilist_helpers.resolve_guild_limits(bot, GUILD) is premium.GUILD_FREE


def test_resolve_guild_limits_returns_what_the_resolver_says():
    bot = types.SimpleNamespace(premium=_FakeResolver(premium.GUILD_PREMIUM))
    assert anilist_helpers.resolve_guild_limits(bot, GUILD) is premium.GUILD_PREMIUM


def test_cog_guild_limits_wraps_the_same_resolver():
    cog = _cog(premium_resolver=_FakeResolver(premium.GUILD_PREMIUM))
    assert cog._guild_limits(GUILD) is premium.GUILD_PREMIUM
    cog_free = _cog()
    assert cog_free._guild_limits(GUILD) is premium.GUILD_FREE


# ---------------------------------------------------------------------------
# Creation refusals show the EFFECTIVE max, free or premium
# ---------------------------------------------------------------------------


async def test_create_feed_refused_at_the_free_cap_by_default():
    pool = _FakePool(
        fetchval_results=[
            ("SELECT 1 FROM anilist_feeds", None),
            ("SELECT COUNT(*) FROM anilist_feeds", af.MAX_FEEDS_PER_GUILD),
        ]
    )
    cog = _cog(pool)  # no bot.premium -> FREE

    error = await cog._create_feed(GUILD, 100)

    assert error is not None
    assert str(af.MAX_FEEDS_PER_GUILD) in error
    assert pool.executes == []  # nothing stored past the cap


async def test_create_feed_refused_at_the_premium_cap_shows_the_premium_number():
    assert premium.GUILD_PREMIUM.max_feeds_per_guild != af.MAX_FEEDS_PER_GUILD
    pool = _FakePool(
        fetchval_results=[
            ("SELECT 1 FROM anilist_feeds", None),
            (
                "SELECT COUNT(*) FROM anilist_feeds",
                premium.GUILD_PREMIUM.max_feeds_per_guild,
            ),
        ]
    )
    cog = _cog(pool, premium_resolver=_FakeResolver(premium.GUILD_PREMIUM))

    error = await cog._create_feed(GUILD, 100)

    assert error is not None
    assert str(premium.GUILD_PREMIUM.max_feeds_per_guild) in error
    assert str(af.MAX_FEEDS_PER_GUILD) not in error


async def test_create_feed_accepted_just_under_the_premium_cap():
    pool = _FakePool(
        fetchval_results=[
            ("SELECT 1 FROM anilist_feeds", None),
            (
                "SELECT COUNT(*) FROM anilist_feeds",
                premium.GUILD_PREMIUM.max_feeds_per_guild - 1,
            ),
        ]
    )
    cog = _cog(pool, premium_resolver=_FakeResolver(premium.GUILD_PREMIUM))

    assert await cog._create_feed(GUILD, 100) is None
    assert any("INSERT INTO anilist_feeds" in sql for sql, _args in pool.executes)


async def test_add_follow_refused_shows_the_premium_number():
    pool = _FakePool(
        fetchval_results=[
            ("SELECT 1 FROM anilist_follows", None),
            (
                "SELECT COUNT(*) FROM anilist_follows",
                premium.GUILD_PREMIUM.max_follows_per_feed,
            ),
        ]
    )
    cog = _cog(pool, premium_resolver=_FakeResolver(premium.GUILD_PREMIUM))

    error = await cog._add_follow(GUILD, 100, 7, "reader", 9)

    assert error is not None
    assert str(premium.GUILD_PREMIUM.max_follows_per_feed) in error


async def test_add_mute_refused_shows_the_premium_number():
    pool = _FakePool(
        fetchval_results=[
            ("SELECT 1 FROM anilist_feed_mutes", None),
            (
                "COUNT(*) FROM anilist_feed_mutes",
                premium.GUILD_PREMIUM.max_follows_per_feed,
            ),
        ]
    )
    cog = _cog(pool, premium_resolver=_FakeResolver(premium.GUILD_PREMIUM))

    error = await cog._add_mute(GUILD, 100, 7, "reader")

    assert error is not None
    assert str(premium.GUILD_PREMIUM.max_follows_per_feed) in error


async def test_add_channel_sub_refused_shows_the_premium_number():
    pool = _FakePool(
        fetchval_results=[
            ("SELECT 1 FROM anilist_channel_subs", None),
            (
                "COUNT(*) FROM anilist_channel_subs",
                premium.GUILD_PREMIUM.max_subs_per_feed,
            ),
        ]
    )
    cog = _cog(pool, premium_resolver=_FakeResolver(premium.GUILD_PREMIUM))

    error = await cog._add_channel_sub(
        GUILD, 100, 55, "ANIME", "Frieren", 9
    )

    assert error is not None
    assert str(premium.GUILD_PREMIUM.max_subs_per_feed) in error


async def test_add_follow_never_touches_the_global_cursor():
    """A reactivated follow is just an insert, like any other: nothing here
    ever writes anilist_feed_state, which is what makes it indistinguishable
    from a brand new follow (see the module docstring's reactivation story) -
    the global createdAt cursor already advanced past old activity on its
    own, independently of this row's history."""

    pool = _FakePool(fetchval_results=[("SELECT 1 FROM anilist_follows", None)])
    cog = _cog(pool)

    await cog._add_follow(GUILD, 100, 7, "reader", 9)

    assert pool.executes  # the insert did happen
    assert all("anilist_feed_state" not in sql for sql, _args in pool.executes)


# ---------------------------------------------------------------------------
# Archival classification: ordering + reactivation
# ---------------------------------------------------------------------------


async def test_feed_archival_keeps_the_oldest_and_archives_the_newest():
    cog = _cog()
    feeds = [
        {"channel_id": 300, "created_at": _dt(300)},
        {"channel_id": 100, "created_at": _dt(100)},
        {"channel_id": 200, "created_at": _dt(200)},
    ]

    archival = await cog._feed_archival(GUILD, feeds=feeds, max_feeds=2)

    assert archival.active_ids == {100, 200}
    assert archival.archived_ids == {300}


async def test_feed_follow_archival_keeps_the_oldest_and_archives_the_newest():
    cog = _cog()
    follows = [
        {"anilist_user_id": 3, "added_at": _dt(300)},
        {"anilist_user_id": 1, "added_at": _dt(100)},
        {"anilist_user_id": 2, "added_at": _dt(200)},
    ]

    archival = await cog._feed_follow_archival(
        GUILD, 100, follows=follows, max_follows=2
    )

    assert archival.active_ids == {1, 2}
    assert archival.archived_ids == {3}


async def test_feed_sub_archival_keeps_the_oldest_and_archives_the_newest():
    cog = _cog()
    subs = [
        {"media_id": 30, "created_at": _dt(300)},
        {"media_id": 10, "created_at": _dt(100)},
        {"media_id": 20, "created_at": _dt(200)},
    ]

    archival = await cog._feed_sub_archival(GUILD, 100, subs=subs, max_subs=2)

    assert archival.active_ids == {10, 20}
    assert archival.archived_ids == {30}


async def test_a_downgraded_feed_set_archives_only_the_newest_excess():
    """Three feeds, a guild that falls back to FREE (max_feeds_per_guild=2):
    the two OLDEST stay active, the newest (however it got created) is the
    one archived - never the other way around."""

    cog = _cog()
    feeds = [
        {"channel_id": 1, "created_at": _dt(10)},
        {"channel_id": 2, "created_at": _dt(20)},
        {"channel_id": 3, "created_at": _dt(30)},
    ]

    archival = await cog._feed_archival(GUILD, feeds=feeds, max_feeds=2)
    assert archival.is_active(1)
    assert archival.is_active(2)
    assert not archival.is_active(3)


async def test_reactivation_is_the_same_rows_reclassified_with_no_bookkeeping():
    """The plan's own promise: nothing is written down when a resource is
    archived, so a cap that rises (Yasuho+ renews, a sibling is deleted) makes
    the SAME untouched row active again on the very next read."""

    cog = _cog()
    follows = [
        {"anilist_user_id": 1, "added_at": _dt(100)},
        {"anilist_user_id": 2, "added_at": _dt(200)},
        {"anilist_user_id": 3, "added_at": _dt(300)},
    ]

    archived_now = await cog._feed_follow_archival(
        GUILD, 100, follows=follows, max_follows=2
    )
    assert archived_now.is_archived(3)

    # Nothing about `follows` changed - only the limit did.
    reactivated = await cog._feed_follow_archival(
        GUILD, 100, follows=follows, max_follows=3
    )
    assert reactivated.is_active(3)
    assert reactivated.active_ids == {1, 2, 3}


# ---------------------------------------------------------------------------
# The poller drops archived feeds/follows before fetch or delivery
# ---------------------------------------------------------------------------


async def test_load_feeds_drops_a_feed_archived_over_the_guild_cap():
    rows = [
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "types": ["TEXT"],
            "fail_count": 0,
            "enabled": True,
            "created_at": _dt(100),
        },
        {
            "guild_id": GUILD,
            "channel_id": 200,
            "types": ["TEXT"],
            "fail_count": 0,
            "enabled": True,
            "created_at": _dt(200),
        },
        {
            "guild_id": GUILD,
            "channel_id": 300,
            "types": ["TEXT"],
            "fail_count": 0,
            "enabled": True,
            "created_at": _dt(300),
        },
    ]
    pool = _FakePool(fetch_results=[("FROM anilist_feeds", rows)])
    cog = _cog(
        pool, premium_resolver=_FakeResolver(types.SimpleNamespace(max_feeds_per_guild=2))
    )

    kept = await cog._load_feeds()

    assert {row["channel_id"] for row in kept} == {100, 200}


async def test_load_feeds_drops_a_disabled_feed_even_if_it_would_be_active():
    """enabled stays its own gate - archival only narrows an already-enabled set."""

    rows = [
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "types": ["TEXT"],
            "fail_count": 0,
            "enabled": False,
            "created_at": _dt(100),
        },
    ]
    pool = _FakePool(fetch_results=[("FROM anilist_feeds", rows)])
    cog = _cog(
        pool,
        premium_resolver=_FakeResolver(types.SimpleNamespace(max_feeds_per_guild=12)),
    )

    assert await cog._load_feeds() == []


async def test_load_follows_drops_a_follow_archived_over_the_feed_cap():
    rows = [
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "anilist_user_id": 1,
            "added_at": _dt(100),
        },
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "anilist_user_id": 2,
            "added_at": _dt(200),
        },
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "anilist_user_id": 3,
            "added_at": _dt(300),
        },
    ]
    pool = _FakePool(fetch_results=[("FROM anilist_follows f", rows)])
    cog = _cog(
        pool,
        premium_resolver=_FakeResolver(
            types.SimpleNamespace(max_follows_per_feed=2)
        ),
    )

    kept = await cog._load_follows()

    assert {row["anilist_user_id"] for row in kept} == {1, 2}


async def _noop(*args, **kwargs):
    return None


def _tick_harness(cog, monkeypatch, *, feeds, follows, activities=()):
    """Stub _tick's I/O except the new active_feed_keys intersection, which is
    plain Python inside _tick itself and therefore stays real."""

    seen = {"fetched_ids": None}

    async def _load_feeds():
        return feeds

    async def _load_follows():
        return follows

    async def _load_mutes():
        return []

    async def _load_state():
        return 0, 1_000

    async def _fetch(user_ids, last_created):
        seen["fetched_ids"] = set(user_ids)
        return list(activities), None

    async def _save_state(last_id, last_created):
        return None

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


async def test_tick_excludes_follows_whose_feed_is_not_in_the_active_set(
    monkeypatch,
):
    """A follow row for a channel _load_feeds did NOT return (i.e. its feed
    was archived) must never reach the AniList fetch - this is the new
    intersection _tick itself performs, on top of whatever _load_follows
    already filtered."""

    cog = _cog()
    # Only channel 100's feed is "active" (what the real _load_feeds would
    # have returned after dropping an archived feed on channel 999).
    active_feeds = [{"guild_id": GUILD, "channel_id": 100}]
    follow_rows = [
        {"guild_id": GUILD, "channel_id": 100, "anilist_user_id": 1},
        # Orphaned: belongs to a feed that is NOT in the active set.
        {"guild_id": GUILD, "channel_id": 999, "anilist_user_id": 2},
    ]
    seen = _tick_harness(cog, monkeypatch, feeds=active_feeds, follows=follow_rows)

    await cog._tick()

    assert seen["fetched_ids"] == {1}


# ---------------------------------------------------------------------------
# Mutes stay EFFECTIVE however far over the cap - only adding is refused
# ---------------------------------------------------------------------------


async def test_load_mutes_is_untouched_by_archival_even_over_the_follow_cap():
    """_load_mutes is not part of this lot at all: whatever is stored comes
    back, however many rows that is relative to any follow cap."""

    rows = [
        {"guild_id": GUILD, "channel_id": 100, "anilist_user_id": uid}
        for uid in range(10)
    ]
    pool = _FakePool(fetch_results=[("FROM anilist_feed_mutes m", rows)])
    cog = _cog(
        pool,
        premium_resolver=_FakeResolver(
            types.SimpleNamespace(max_follows_per_feed=2)
        ),
    )

    kept = await cog._load_mutes()

    assert len(kept) == 10


async def test_add_mute_accepted_just_under_the_premium_cap():
    pool = _FakePool(
        fetchval_results=[
            ("SELECT 1 FROM anilist_feed_mutes", None),
            (
                "COUNT(*) FROM anilist_feed_mutes",
                premium.GUILD_PREMIUM.max_follows_per_feed - 1,
            ),
        ]
    )
    cog = _cog(pool, premium_resolver=_FakeResolver(premium.GUILD_PREMIUM))

    assert await cog._add_mute(GUILD, 100, 7, "reader") is None
    assert any("INSERT INTO anilist_feed_mutes" in sql for sql, _a in pool.executes)


# ---------------------------------------------------------------------------
# Delete is never gated by any of this
# ---------------------------------------------------------------------------


async def test_delete_feed_rows_works_regardless_of_premium_or_archival():
    pool = _FakePool()
    cog = _cog(pool, premium_resolver=_FakeResolver(raises=True))

    await cog._delete_feed_rows(GUILD, 100)

    statements = [sql for sql, _args in pool.executes]
    assert any("DELETE FROM anilist_feeds" in sql for sql in statements)


async def test_remove_follow_works_regardless_of_premium_or_archival():
    # The follow DELETE rides a `... RETURNING 1` fetchval, not execute.
    pool = _FakePool(fetchval_results=[("DELETE FROM anilist_follows", 1)])
    cog = _cog(pool, premium_resolver=_FakeResolver(raises=True))

    removed = await cog._remove_follow(GUILD, 100, 7)

    assert removed is True


async def test_remove_channel_sub_works_regardless_of_premium_or_archival():
    pool = _FakePool()
    cog = _cog(pool, premium_resolver=_FakeResolver(raises=True))

    await cog._remove_channel_sub(GUILD, 100, 55)

    statements = [sql for sql, _args in pool.executes]
    assert any("DELETE FROM anilist_channel_subs" in sql for sql in statements)


# ---------------------------------------------------------------------------
# helpers.filter_active_channel_subs (airing.py / chapters.py)
# ---------------------------------------------------------------------------


def test_filter_active_channel_subs_drops_an_archived_feed_entirely():
    bot = types.SimpleNamespace(
        premium=_FakeResolver(
            types.SimpleNamespace(max_feeds_per_guild=1, max_subs_per_feed=10)
        )
    )
    rows = [
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "media_id": 5,
            "created_at": _dt(5),
            "feed_created_at": _dt(100),
        },
        {
            "guild_id": GUILD,
            "channel_id": 200,
            "media_id": 6,
            "created_at": _dt(6),
            "feed_created_at": _dt(200),
        },
    ]

    kept = anilist_helpers.filter_active_channel_subs(bot, rows)

    # Feed 200 is newer than feed 100 -> archived under max_feeds_per_guild=1.
    assert {row["channel_id"] for row in kept} == {100}


def test_filter_active_channel_subs_drops_an_archived_sub_within_an_active_feed():
    bot = types.SimpleNamespace(
        premium=_FakeResolver(
            types.SimpleNamespace(max_feeds_per_guild=5, max_subs_per_feed=1)
        )
    )
    rows = [
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "media_id": 5,
            "created_at": _dt(5),
            "feed_created_at": _dt(1),
        },
        {
            "guild_id": GUILD,
            "channel_id": 100,
            "media_id": 6,
            "created_at": _dt(6),
            "feed_created_at": _dt(1),
        },
    ]

    kept = anilist_helpers.filter_active_channel_subs(bot, rows)

    assert {row["media_id"] for row in kept} == {5}


def test_filter_active_channel_subs_empty_input_is_a_noop():
    bot = types.SimpleNamespace()
    assert anilist_helpers.filter_active_channel_subs(bot, []) == []
