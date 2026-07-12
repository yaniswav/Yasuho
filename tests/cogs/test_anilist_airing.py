"""Unit tests for the pure helpers in ``cogs/anilist/airing.py``.

Everything exercised here is pure and side-effect-free (no network, DB, Discord or
Lavalink): the tuned poller constants the design leans on, and - the piece the
guild-channel fan-out adds - :func:`plan_airing_channel_posts`, which decides which
feed channels get a post for an aired episode. Its ``channel_media`` map is now
built from a feed's EXPLICIT title subscriptions (``anilist_channel_subs``), not
from its followed users' lists, but the planner's shape is unchanged. Its two
contracts under test are the ones the fan-out invariants rest on: it is
membership-only (NOT progress-gated, unlike the DM planner) and it emits at most ONE
post per (feed channel, aired row) in a stable, deterministic order. Importing the
cog module is enough; nothing in it runs at import time.
"""

from math import ceil

from cogs.anilist import airing as ai
from cogs.anilist.airing import (
    LIST_FAIL_THRESHOLD,
    LIST_FETCH_BUDGET,
    LIST_TTL,
    MAX_SCHEDULE_PAGES,
    PER_PAGE,
    POLL_SECONDS,
    plan_airing_channel_posts,
    warmup_status,
)
from tools import round_robin as rr

# ---------------------------------------------------------------------------
# documented constants (guard the tuned values the design leans on)
# ---------------------------------------------------------------------------


def test_documented_constants():
    assert POLL_SECONDS == 600
    assert PER_PAGE == 50
    assert MAX_SCHEDULE_PAGES == 5
    assert LIST_FETCH_BUDGET == 10
    assert LIST_FAIL_THRESHOLD == 3
    assert LIST_TTL == 1800.0
    assert ai.LIST_SWEEP_AT == 500


# ---------------------------------------------------------------------------
# plan_airing_channel_posts - which feed channels get a post for an airing
# ---------------------------------------------------------------------------


def test_channel_posts_single_channel_membership():
    aired = [{"media_id": 10, "episode": 3}]
    channel_media = {(1, 111): {10, 20}, (2, 222): {30}}
    posts = plan_airing_channel_posts(aired, channel_media)
    # only the channel subscribed to media 10 gets a post.
    assert posts == [(1, 111, 10, 3)]


def test_channel_posts_sorted_by_feed_key():
    aired = [{"media_id": 5, "episode": 7}]
    channel_media = {(2, 20): {5}, (1, 10): {5}, (1, 5): {5}}
    posts = plan_airing_channel_posts(aired, channel_media)
    # every channel subscribes to media 5 -> one post each, feed key ascending.
    assert posts == [(1, 5, 5, 7), (1, 10, 5, 7), (2, 20, 5, 7)]


def test_channel_posts_not_progress_gated():
    # channel_media carries no progress at all - it is a media-id set - so every
    # aired episode of a subscribed title posts, unlike the progress-gated DM path.
    aired = [{"media_id": 10, "episode": 1}, {"media_id": 10, "episode": 2}]
    channel_media = {(1, 111): {10}}
    posts = plan_airing_channel_posts(aired, channel_media)
    # every aired episode of a subscribed title posts.
    assert posts == [(1, 111, 10, 1), (1, 111, 10, 2)]


def test_channel_posts_preserve_aired_row_order():
    # Outer order is aired-row order (the poll's TIME-ascending scan), not media id.
    aired = [{"media_id": 30, "episode": 1}, {"media_id": 10, "episode": 9}]
    channel_media = {(1, 111): {10, 30}}
    posts = plan_airing_channel_posts(aired, channel_media)
    assert posts == [(1, 111, 30, 1), (1, 111, 10, 9)]


def test_channel_posts_one_post_per_channel_per_row():
    # Two feeds in the same guild both subscribe to the media -> exactly one post
    # each, never a duplicate for the same (guild, channel, media, episode).
    aired = [{"media_id": 42, "episode": 4}]
    channel_media = {(7, 100): {42}, (7, 200): {42, 43}}
    posts = plan_airing_channel_posts(aired, channel_media)
    assert posts == [(7, 100, 42, 4), (7, 200, 42, 4)]
    assert len(posts) == len(set(posts))


def test_channel_posts_skip_incomplete_rows():
    aired = [
        {"media_id": None, "episode": 3},
        {"media_id": 10, "episode": None},
        {"media_id": 10, "episode": 4},
    ]
    channel_media = {(1, 111): {10}}
    posts = plan_airing_channel_posts(aired, channel_media)
    assert posts == [(1, 111, 10, 4)]


def test_channel_posts_empty_cases():
    # No feed channels opted in -> nothing, even with airings.
    assert plan_airing_channel_posts([{"media_id": 10, "episode": 1}], {}) == []
    # No airings this tick -> nothing, even with opted-in feeds.
    assert plan_airing_channel_posts([], {(1, 1): {10}}) == []
    # An airing whose media no feed subscribes to -> nothing.
    assert (
        plan_airing_channel_posts([{"media_id": 99, "episode": 1}], {(1, 1): {10}})
        == []
    )


# ---------------------------------------------------------------------------
# warmup_status - the GLOBAL-cursor hold gate (the C4 truth table)
#
# The airing cursor is global, so it must NOT advance while any tracked user's
# watch-list is still missing from the tracked-media union. warmup_status is the
# pure decision the poller consults: holding is True iff at least one tracked user
# is not yet cached.
# ---------------------------------------------------------------------------


def test_warmup_holds_while_any_list_missing():
    # missing > 0 -> holding (no cursor advance); loaded/total reported for the log.
    loaded, total, holding = warmup_status({1, 2, 3}, cached_user_ids={1})
    assert (loaded, total, holding) == (1, 3, True)


def test_warmup_releases_only_when_all_loaded():
    # Every tracked user cached -> not holding, the cursor may advance.
    loaded, total, holding = warmup_status({1, 2, 3}, cached_user_ids={1, 2, 3})
    assert (loaded, total, holding) == (3, 3, False)


def test_warmup_truth_table_edges():
    # No tracked users at all (channel-subs-only tick): nothing to warm, never hold.
    assert warmup_status(set(), cached_user_ids={}) == (0, 0, False)
    # Extra cached users that are NOT tracked do not count towards loaded/total.
    assert warmup_status({5}, cached_user_ids={5, 6, 7}) == (1, 1, False)
    # A single missing user still holds the whole global cursor.
    assert warmup_status({5, 6}, cached_user_ids={6}) == (1, 2, True)


def test_warmup_cold_start_300_users_drains_at_budget_no_episode_loss():
    """Simulated cold start with 300 opt-in users, C4 preserved throughout.

    Models the poller's warmup exactly: an empty in-memory cache, the round-robin
    refresh wheel spending LIST_FETCH_BUDGET fetches per tick over the still-missing
    set, and the global cursor HELD (never advanced) on any tick where a list is
    still missing. Asserts: the cursor is held on every tick until the last, it is
    released on exactly the tick the final list loads, the warmup takes
    ceil(300/budget) ticks, and no tracked user is ever skipped (zero episode loss).
    """

    tracked = set(range(300))
    cache: set = set()
    missing_wheel = None
    budget = LIST_FETCH_BUDGET

    advanced_on_tick = []  # ticks on which the cursor WOULD have advanced
    ticks = 0
    while True:
        # Refresh phase: the missing slice served through the fair wheel this tick.
        missing = [u for u in tracked if u not in cache]
        if missing:
            batch, missing_wheel = rr.next_batch(missing, missing_wheel, budget)
            cache.update(batch)  # every fetch succeeds in this simulation
            assert len(batch) == min(budget, len(missing))  # constant budget

        # Gate phase: decide hold using the SAME pure helper the poller uses.
        loaded, total, holding = warmup_status(tracked, cache)
        advanced_on_tick.append(not holding)
        ticks += 1
        if not holding:
            break
        assert ticks < 1000  # guard against a non-terminating wheel

    # Warmup took exactly ceil(N / budget) ticks...
    assert ticks == ceil(300 / budget)
    # ...the cursor was HELD on every tick but the last (C4: no advance while any
    # list is missing), and released on exactly the final tick.
    assert advanced_on_tick[-1] is True
    assert all(a is False for a in advanced_on_tick[:-1])
    # Zero episode loss: by release, every tracked user's list is loaded, so the
    # union is complete before the cursor is ever allowed to move.
    assert cache >= tracked


def test_warmup_wheel_rotates_past_a_stuck_user_so_the_rest_load():
    """The fair wheel must not starve the majority behind one failing id.

    This pins the WHEEL property in isolation: with one user whose fetch never
    succeeds, the round-robin rotation still serves and loads all the others. The
    gate then still holds for the one stuck user - which at this pure level is
    correct and is EXACTLY why _refresh_lists caps consecutive failures and caches a
    dead account EMPTY to release the global cursor (see the escape-hatch tests
    below); without that escape this hold would be permanent.
    """

    tracked = set(range(300))
    stuck = 7  # this user's fetch perpetually fails, so it stays missing here
    cache: set = set()
    missing_wheel = None
    budget = LIST_FETCH_BUDGET

    for _ in range(200):  # far more than a clean warmup needs
        missing = [u for u in tracked if u not in cache]
        if not missing:
            break
        batch, missing_wheel = rr.next_batch(missing, missing_wheel, budget)
        for u in batch:
            if u != stuck:
                cache.add(u)  # everyone but the stuck user loads

    # Every other user loaded despite the stuck one (no starvation); the gate still
    # holds for it alone until the escape hatch (tested next) caches it empty.
    assert cache == tracked - {stuck}
    _loaded, _total, holding = warmup_status(tracked, cache)
    assert holding is True


# ---------------------------------------------------------------------------
# The failed-N-times escape hatch (the CRITICAL fix): a single tracked user whose
# list fetch ALWAYS raises must NOT freeze the GLOBAL airing cursor for everyone.
# After LIST_FAIL_THRESHOLD consecutive failures _refresh_lists caches an EMPTY list
# for that user so warmup_status counts it loaded and the cursor releases; a later
# success re-caches its real list and clears the counter (never silently dropping a
# healthy user). These drive the REAL cog method with a stubbed fetch.
# ---------------------------------------------------------------------------


def _airing_cog_no_loop():
    """A bare AniListAiring with only the _refresh_lists state, no task loop/bot."""

    cog = object.__new__(ai.AniListAiring)
    cog._list_cache = {}
    cog._list_fail_counts = {}
    cog._missing_wheel_after = None
    cog._stale_wheel_after = None
    cog._spaced = False
    cog._req_count = 0

    async def _no_space():
        cog._req_count += 1

    cog._space = _no_space  # skip the REQUEST_SPACING sleep in tests
    return cog


async def test_refresh_lists_caches_empty_after_threshold_releasing_the_cursor():
    cog = _airing_cog_no_loop()

    async def _always_fail(_aid):
        raise ai._FetchError("account gone")

    cog._fetch_public_list = _always_fail
    tracked = {7}

    # Below the threshold the user stays MISSING, so the global cursor is HELD.
    for attempt in range(1, LIST_FAIL_THRESHOLD):
        cog._spaced = False
        await cog._refresh_lists(tracked, now=1000.0)
        assert cog._list_fail_counts.get(7) == attempt
        _l, _t, holding = warmup_status(tracked, cog._list_cache)
        assert holding is True

    # The threshold-th failure caches the user EMPTY and releases the hold, so the
    # poller stops blacking out airing for everyone else.
    cog._spaced = False
    await cog._refresh_lists(tracked, now=1000.0)
    assert cog._list_cache_get(7) == {}
    assert 7 not in cog._list_fail_counts  # counter cleared once resolved
    _l, _t, holding = warmup_status(tracked, cog._list_cache)
    assert holding is False


async def test_refresh_lists_success_before_threshold_clears_the_counter():
    # A healthy user hit by a couple of transient blips is never dropped: the next
    # success re-caches its real list and resets the failure counter to zero.
    cog = _airing_cog_no_loop()
    state = {"fail": True}

    async def _fetch(_aid):
        if state["fail"]:
            raise ai._FetchError("transient blip")
        return {101: 5}

    cog._fetch_public_list = _fetch
    tracked = {7}

    for _ in range(LIST_FAIL_THRESHOLD - 1):
        cog._spaced = False
        await cog._refresh_lists(tracked, now=1000.0)
    assert cog._list_fail_counts.get(7) == LIST_FAIL_THRESHOLD - 1
    assert cog._list_cache_get(7) is None  # still missing, no empty cached yet

    state["fail"] = False  # recovers before ever hitting the threshold
    cog._spaced = False
    await cog._refresh_lists(tracked, now=1000.0)
    assert cog._list_cache_get(7) == {101: 5}  # real list, not an empty stub
    assert 7 not in cog._list_fail_counts


async def test_refresh_lists_one_dead_user_does_not_stall_the_healthy_majority():
    # End-to-end of the CRITICAL: with one permanently-dead account among healthy
    # users, the warmup completes (all healthy loaded, the dead one cached empty) so
    # the global cursor is released instead of frozen forever.
    cog = _airing_cog_no_loop()
    dead = 7

    async def _fetch(aid):
        if aid == dead:
            raise ai._FetchError("deactivated")
        return {1000 + aid: 0}

    cog._fetch_public_list = _fetch
    tracked = set(range(30))

    holding = True
    for _ in range(200):  # bounded warmup, must terminate
        cog._spaced = False
        await cog._refresh_lists(tracked, now=1000.0)
        _l, _t, holding = warmup_status(tracked, cog._list_cache)
        if not holding:
            break
    assert holding is False  # cursor released despite the dead account
    assert cog._list_cache_get(dead) == {}  # dead account cached empty
    assert all(cog._list_cache_get(a) for a in tracked if a != dead)
