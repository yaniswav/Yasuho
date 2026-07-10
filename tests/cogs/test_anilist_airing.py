"""Unit tests for the pure helpers in ``cogs/anilist/airing.py``.

Everything exercised here is pure and side-effect-free (no network, DB, Discord or
Lavalink): the tuned poller constants the design leans on, and - the piece the
guild-channel fan-out adds - :func:`plan_airing_channel_posts`, which decides which
feed channels get a post for an aired episode. Its two contracts under test are the
ones the fan-out invariants rest on: it is membership-only (NOT progress-gated,
unlike the DM planner) and it emits at most ONE post per (feed channel, aired row)
in a stable, deterministic order. Importing the cog module is enough; nothing in it
runs at import time.
"""

from cogs.anilist import airing as ai
from cogs.anilist.airing import (
    LIST_TTL,
    MAX_LIST_REFRESHES_PER_TICK,
    MAX_SCHEDULE_PAGES,
    PER_PAGE,
    POLL_SECONDS,
    plan_airing_channel_posts,
)

# ---------------------------------------------------------------------------
# documented constants (guard the tuned values the design leans on)
# ---------------------------------------------------------------------------


def test_documented_constants():
    assert POLL_SECONDS == 600
    assert PER_PAGE == 50
    assert MAX_SCHEDULE_PAGES == 5
    assert MAX_LIST_REFRESHES_PER_TICK == 5
    assert LIST_TTL == 1800.0
    assert ai.LIST_SWEEP_AT == 500


# ---------------------------------------------------------------------------
# plan_airing_channel_posts - which feed channels get a post for an airing
# ---------------------------------------------------------------------------


def test_channel_posts_single_channel_membership():
    aired = [{"media_id": 10, "episode": 3}]
    channel_media = {(1, 111): {10, 20}, (2, 222): {30}}
    posts = plan_airing_channel_posts(aired, channel_media)
    assert posts == [(1, 111, 10, 3)]  # only the channel whose union has media 10


def test_channel_posts_sorted_by_feed_key():
    aired = [{"media_id": 5, "episode": 7}]
    channel_media = {(2, 20): {5}, (1, 10): {5}, (1, 5): {5}}
    posts = plan_airing_channel_posts(aired, channel_media)
    # every channel follows media 5 -> one post each, feed key ascending.
    assert posts == [(1, 5, 5, 7), (1, 10, 5, 7), (2, 20, 5, 7)]


def test_channel_posts_not_progress_gated():
    # channel_media carries no progress at all - it is a media-id set - so every
    # aired episode of a followed title posts, unlike the progress-gated DM path.
    aired = [{"media_id": 10, "episode": 1}, {"media_id": 10, "episode": 2}]
    channel_media = {(1, 111): {10}}
    posts = plan_airing_channel_posts(aired, channel_media)
    assert posts == [(1, 111, 10, 1), (1, 111, 10, 2)]


def test_channel_posts_preserve_aired_row_order():
    # Outer order is aired-row order (the poll's TIME-ascending scan), not media id.
    aired = [{"media_id": 30, "episode": 1}, {"media_id": 10, "episode": 9}]
    channel_media = {(1, 111): {10, 30}}
    posts = plan_airing_channel_posts(aired, channel_media)
    assert posts == [(1, 111, 30, 1), (1, 111, 10, 9)]


def test_channel_posts_one_post_per_channel_per_row():
    # Two feeds in the same guild both follow the media -> exactly one post each,
    # never a duplicate for the same (guild, channel, media, episode).
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
    # An airing whose media no feed follows -> nothing.
    assert (
        plan_airing_channel_posts([{"media_id": 99, "episode": 1}], {(1, 1): {10}})
        == []
    )
