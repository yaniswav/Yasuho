"""Unit tests for tools.quotas (pure, clock-injected, no bot needed).

Every time-aware assertion pins ``now`` explicitly so the tests never touch a
real clock, and the sweep tests drive the size cap deterministically.
"""

import pytest

from tools.quotas import (
    EFFECTS_GUILD_LIMIT,
    FILTERED_PLAYERS_CAP,
    LYRICS_GUILD_LIMIT,
    LYRICS_USER_LIMIT,
    SYNCED_LYRICS_CAP,
    GlobalCeiling,
    QuotaRegistry,
    SlidingWindowQuota,
)

# ---------------------------------------------------------------------------
# SlidingWindowQuota: boundaries
# ---------------------------------------------------------------------------


def test_limit_th_hit_ok_and_over_the_limit_rejected():
    q = SlidingWindowQuota(3, 60.0)
    assert q.hit("k", now=0.0) is True
    assert q.hit("k", now=1.0) is True
    assert q.hit("k", now=2.0) is True  # 3rd hit fills the window
    assert q.hit("k", now=3.0) is False  # 4th is over the limit


def test_check_does_not_consume_a_slot():
    q = SlidingWindowQuota(1, 60.0)
    assert q.check("k", now=0.0) is True
    assert q.check("k", now=0.0) is True  # still free - check never consumed
    assert q.hit("k", now=0.0) is True
    assert q.check("k", now=0.0) is False  # now full


def test_remaining_counts_down_and_refills_on_expiry():
    q = SlidingWindowQuota(2, 100.0)
    assert q.remaining("k", now=0.0) == 2
    q.hit("k", now=0.0)
    assert q.remaining("k", now=0.0) == 1
    q.hit("k", now=10.0)
    assert q.remaining("k", now=10.0) == 0
    # The first hit ages out at t=100; at t=100 exactly it is expired (<= cutoff).
    assert q.remaining("k", now=100.0) == 1
    # The second hit (t=10) ages out at t=110.
    assert q.remaining("k", now=110.0) == 2


def test_window_is_rolling_not_fixed_bucket():
    q = SlidingWindowQuota(2, 10.0)
    q.hit("k", now=0.0)
    q.hit("k", now=5.0)
    assert q.hit("k", now=9.0) is False  # both still live
    # First hit expires at t=10; a slot frees, so a hit at t=10 fits.
    assert q.hit("k", now=10.0) is True
    # Now live hits are t=5 and t=10; next slot frees at t=15.
    assert q.hit("k", now=14.0) is False
    assert q.hit("k", now=15.0) is True


# ---------------------------------------------------------------------------
# SlidingWindowQuota: expiry driven by the injected clock
# ---------------------------------------------------------------------------


def test_expiry_via_injected_clock_callable():
    fake = {"t": 0.0}
    q = SlidingWindowQuota(1, 50.0, clock=lambda: fake["t"])
    assert q.hit("k") is True  # reads clock -> t=0
    fake["t"] = 49.9
    assert q.hit("k") is False  # still inside the window
    fake["t"] = 50.0
    assert q.hit("k") is True  # first hit aged out exactly at the boundary


def test_boundary_at_exactly_window_expires():
    q = SlidingWindowQuota(1, 30.0)
    q.hit("k", now=100.0)
    assert q.check("k", now=129.999) is False
    assert q.check("k", now=130.0) is True  # <= cutoff drops the old hit


# ---------------------------------------------------------------------------
# SlidingWindowQuota: retry_after exactness
# ---------------------------------------------------------------------------


def test_retry_after_zero_when_slot_free():
    q = SlidingWindowQuota(2, 60.0)
    assert q.retry_after("k", now=0.0) == 0.0
    q.hit("k", now=0.0)
    assert q.retry_after("k", now=0.0) == 0.0  # one slot still free


def test_retry_after_exact_seconds_until_a_slot_frees():
    q = SlidingWindowQuota(1, 60.0)
    q.hit("k", now=10.0)
    # Oldest (only) hit at t=10 frees at t=70.
    assert q.retry_after("k", now=10.0) == pytest.approx(60.0)
    assert q.retry_after("k", now=40.0) == pytest.approx(30.0)
    assert q.retry_after("k", now=69.5) == pytest.approx(0.5)
    # At/after the free instant it reads 0 (never negative).
    assert q.retry_after("k", now=70.0) == 0.0
    assert q.retry_after("k", now=80.0) == 0.0


def test_retry_after_tracks_the_oldest_of_several_hits():
    q = SlidingWindowQuota(3, 100.0)
    q.hit("k", now=0.0)
    q.hit("k", now=20.0)
    q.hit("k", now=40.0)
    # Full: the next slot frees when the oldest (t=0) ages out at t=100.
    assert q.retry_after("k", now=50.0) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# SlidingWindowQuota: key isolation and tuple keys
# ---------------------------------------------------------------------------


def test_keys_are_isolated():
    q = SlidingWindowQuota(1, 60.0)
    assert q.hit("a", now=0.0) is True
    assert q.hit("b", now=0.0) is True  # different key, own budget
    assert q.hit("a", now=0.0) is False
    assert q.remaining("b", now=0.0) == 0
    assert q.remaining("c", now=0.0) == 1  # untouched key is full


def test_tuple_keys_work_as_guild_user_pairs():
    q = SlidingWindowQuota(1, 60.0)
    assert q.hit((1, 10), now=0.0) is True
    assert q.hit((1, 11), now=0.0) is True  # same guild, other user
    assert q.hit((2, 10), now=0.0) is True  # other guild, same user
    assert q.hit((1, 10), now=0.0) is False  # the exact pair is now full
    assert q.tracked_keys() == 3


# ---------------------------------------------------------------------------
# SlidingWindowQuota: sweep bounds + post-sweep over-allowance
# ---------------------------------------------------------------------------


def test_idle_key_leaves_no_trace_in_the_map():
    q = SlidingWindowQuota(1, 10.0)
    q.hit("k", now=0.0)
    assert q.tracked_keys() == 1
    # A later read past the window prunes the dead deque out of the map.
    assert q.remaining("k", now=100.0) == 1
    assert q.tracked_keys() == 0


def test_sweep_reclaims_expired_keys_past_the_cap():
    q = SlidingWindowQuota(1, 10.0, max_keys=3)
    q.hit("a", now=0.0)
    q.hit("b", now=0.0)
    q.hit("c", now=0.0)
    assert q.tracked_keys() == 3  # at the cap, no sweep yet
    # A 4th key well past the window trips the sweep; the stale trio is dropped.
    q.hit("d", now=1000.0)
    assert q.tracked_keys() == 1
    assert q.check("a", now=1000.0) is True  # 'a' was reclaimed -> fresh budget


def test_sweep_hard_evicts_when_all_keys_are_live():
    # Every key is live within the window, so the free reclamation frees nothing;
    # the hard-eviction pass must still bound the map at max_keys. limit=1 makes
    # the eviction observable: the oldest key ('a') is dropped and restarts fresh,
    # while a survivor ('b') stays at its limit.
    q = SlidingWindowQuota(1, 1000.0, max_keys=3)
    q.hit("a", now=1.0)
    q.hit("b", now=2.0)
    q.hit("c", now=3.0)
    q.hit("d", now=4.0)  # 4th live key trips the sweep -> evict oldest ('a')
    assert q.tracked_keys() == 3
    assert q.check("a", now=5.0) is True  # 'a' was evicted -> fresh again
    assert q.check("b", now=5.0) is False  # 'b' survived -> still at its limit


def test_post_sweep_over_allowance_is_bounded_and_documented():
    # 'a' hits its limit, then is hard-evicted by a live-key burst. On its next
    # use it restarts fresh (brief over-allowance) - the accepted trade-off.
    q = SlidingWindowQuota(1, 1000.0, max_keys=2)
    assert q.hit("a", now=1.0) is True
    assert q.hit("a", now=1.0) is False  # 'a' is at its limit
    q.hit("b", now=2.0)
    q.hit("c", now=3.0)  # trips sweep; 'a' (oldest) is evicted despite being live
    # 'a' restarts fresh - it is allowed again inside what was its window.
    assert q.hit("a", now=4.0) is True


# ---------------------------------------------------------------------------
# SlidingWindowQuota: stats and validation
# ---------------------------------------------------------------------------


def test_stats_counts_hits_rejections_and_tracked_keys():
    q = SlidingWindowQuota(1, 60.0)
    q.hit("a", now=0.0)
    q.hit("b", now=0.0)
    q.hit("a", now=0.0)  # rejected (over limit)
    q.hit("a", now=0.0)  # rejected again
    s = q.stats()
    assert s == {"hits": 2, "rejections": 2, "tracked_keys": 2}


def test_zero_limit_rejects_everything():
    q = SlidingWindowQuota(0, 60.0)
    assert q.check("k", now=0.0) is False
    assert q.hit("k", now=0.0) is False
    assert q.stats()["rejections"] == 1


def test_construction_validates_limit_and_window():
    with pytest.raises(ValueError):
        SlidingWindowQuota(-1, 60.0)
    with pytest.raises(ValueError):
        SlidingWindowQuota(1, 0.0)
    with pytest.raises(ValueError):
        SlidingWindowQuota(1, -5.0)


# ---------------------------------------------------------------------------
# GlobalCeiling
# ---------------------------------------------------------------------------


def test_ceiling_acquire_until_full_then_reject():
    c = GlobalCeiling(2)
    assert c.acquire("p1") is True
    assert c.acquire("p2") is True
    assert c.acquire("p3") is False  # full
    assert c.count() == 2


def test_ceiling_acquire_is_idempotent():
    c = GlobalCeiling(1)
    assert c.acquire("p1") is True
    assert c.acquire("p1") is True  # same id -> still True, no extra slot
    assert c.count() == 1
    assert c.acquire("p2") is False  # the one slot is taken by p1


def test_ceiling_release_frees_a_slot_and_is_idempotent():
    c = GlobalCeiling(1)
    c.acquire("p1")
    c.release("p1")
    assert c.count() == 0
    c.release("p1")  # releasing again is a harmless no-op
    c.release("never-held")  # unknown id is a no-op too
    assert c.acquire("p2") is True  # slot is free again


def test_ceiling_membership_and_holders_snapshot():
    c = GlobalCeiling(3)
    c.acquire("a")
    c.acquire("b")
    assert "a" in c
    assert "z" not in c
    snap = c.holders()
    assert snap == frozenset({"a", "b"})
    c.acquire("c")  # snapshot is a copy - not mutated by later acquires
    assert snap == frozenset({"a", "b"})


def test_ceiling_stats():
    c = GlobalCeiling(1)
    c.acquire("a")
    c.acquire("a")  # idempotent - not a new acquire
    c.acquire("b")  # rejected (full)
    assert c.stats() == {"acquires": 1, "rejections": 1, "holders": 1}


def test_ceiling_validates_capacity():
    with pytest.raises(ValueError):
        GlobalCeiling(-1)


def test_zero_capacity_ceiling_rejects_all():
    c = GlobalCeiling(0)
    assert c.acquire("a") is False
    assert c.count() == 0


# ---------------------------------------------------------------------------
# QuotaRegistry
# ---------------------------------------------------------------------------


def test_registry_wires_the_tuned_constants():
    reg = QuotaRegistry()
    assert reg.effects_guild.limit == EFFECTS_GUILD_LIMIT
    assert reg.lyrics_user.limit == LYRICS_USER_LIMIT
    assert reg.lyrics_guild.limit == LYRICS_GUILD_LIMIT
    assert reg.filtered_players.capacity == FILTERED_PLAYERS_CAP
    assert reg.synced_lyrics.capacity == SYNCED_LYRICS_CAP


def test_registry_threads_one_clock_into_every_windowed_quota():
    fake = {"t": 0.0}
    reg = QuotaRegistry(clock=lambda: fake["t"])
    for _ in range(EFFECTS_GUILD_LIMIT):
        assert reg.effects_guild.hit(1) is True
    assert reg.effects_guild.hit(1) is False  # guild 1 exhausted its effects budget
    fake["t"] = 601.0  # past the 600s effects window
    assert reg.effects_guild.hit(1) is True  # window rolled over via the shared clock


def test_registry_stats_folds_every_member():
    reg = QuotaRegistry()
    reg.lyrics_user.hit(7, now=0.0)
    reg.filtered_players.acquire("g1")
    s = reg.stats()
    assert set(s) == {
        "effects_guild",
        "lyrics_user",
        "lyrics_guild",
        "filtered_players",
        "synced_lyrics",
    }
    assert s["lyrics_user"]["hits"] == 1
    assert s["filtered_players"]["holders"] == 1
