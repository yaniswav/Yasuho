"""Unit tests for tools.round_robin (the pure constant-budget scheduler).

The wheel is the heart of the AniList/MangaDex scale fix: it serves a fixed
``budget`` slice of a tracked set each tick and rotates through the rest, so the
per-tick request count is CONSTANT no matter how large the set grows. These tests
pin the three properties the pollers lean on: full fairness (every item served
within ceil(n/budget) ticks, no starvation, no duplicate work), stability under
set mutation (add / remove between ticks, including removing the resume marker),
and the totality of the helpers on degenerate input. Everything is pure - no
clock, no bot, no io.
"""

from math import ceil

from tools.round_robin import next_batch, poll_interval_ticks

# ---------------------------------------------------------------------------
# next_batch - basic slicing and the returned marker
# ---------------------------------------------------------------------------


def test_first_batch_starts_at_the_top():
    batch, after = next_batch([3, 1, 2, 5, 4], None, 2)
    assert batch == [1, 2]  # sorted canonical order, from the top
    assert after == 2  # marker is the last served item


def test_next_batch_resumes_after_marker():
    items = [1, 2, 3, 4, 5]
    batch1, after = next_batch(items, None, 2)
    assert batch1 == [1, 2]
    batch2, after = next_batch(items, after, 2)
    assert batch2 == [3, 4]
    batch3, after = next_batch(items, after, 2)
    assert batch3 == [5, 1]  # wraps past the end exactly once
    assert after == 1


def test_budget_ge_n_serves_every_item_once_not_repeats():
    # A small set with a large budget yields each item exactly once (never the same
    # item padded out to fill the budget).
    batch, after = next_batch([10, 20, 30], None, 25)
    assert batch == [10, 20, 30]
    assert after == 30
    # And it keeps serving all of them every tick (interval 1).
    batch2, _ = next_batch([10, 20, 30], after, 25)
    assert sorted(batch2) == [10, 20, 30]


def test_no_duplicates_within_a_batch_even_when_wrapping():
    items = [1, 2, 3, 4]
    batch, _ = next_batch(items, 3, 4)  # start after 3 -> [4, 1, 2, 3]
    assert batch == [4, 1, 2, 3]
    assert len(set(batch)) == len(batch)


def test_duplicate_items_collapse_via_canonical_sort():
    batch, after = next_batch([1, 1, 2, 2, 3], None, 2)
    assert batch == [1, 2]
    assert after == 2


# ---------------------------------------------------------------------------
# Fairness: full coverage within ceil(n/budget) ticks, no starvation
# ---------------------------------------------------------------------------


def test_full_coverage_within_one_cycle_static_set():
    for n, budget in [(125, 25), (100, 10), (7, 3), (2000, 25), (1, 1), (10, 10)]:
        items = list(range(n))
        cycle = poll_interval_ticks(n, budget)
        assert cycle == ceil(n / budget)

        after = None
        counts = {}
        for _ in range(cycle):
            batch, after = next_batch(items, after, budget)
            assert len(batch) == min(budget, n)  # constant per-tick budget
            for it in batch:
                counts[it] = counts.get(it, 0) + 1
        # Every item served at least once within a single cycle: no starvation.
        assert set(counts) == set(items), (n, budget)
        assert min(counts.values()) >= 1


def test_marker_advances_by_budget_each_tick():
    # Over many ticks on a static set the wheel sweeps uniformly: after k full
    # cycles every item has been served exactly k times.
    items = list(range(50))
    budget = 10
    after = None
    counts = {i: 0 for i in items}
    cycles = 4
    for _ in range(cycles * poll_interval_ticks(len(items), budget)):
        batch, after = next_batch(items, after, budget)
        for it in batch:
            counts[it] += 1
    assert all(c == cycles for c in counts.values())


# ---------------------------------------------------------------------------
# Stability under set mutation between ticks
# ---------------------------------------------------------------------------


def test_added_item_is_picked_up_within_a_cycle():
    items = list(range(40))
    budget = 25
    after = None
    # Two ticks on the original set.
    _b, after = next_batch(items, after, budget)
    _b, after = next_batch(items, after, budget)
    # A new item (larger than any) is added; drive a full further cycle.
    items2 = items + [999]
    seen = set()
    for _ in range(poll_interval_ticks(len(items2), budget)):
        batch, after = next_batch(items2, after, budget)
        seen.update(batch)
    assert 999 in seen  # the newcomer is not starved


def test_added_item_smaller_than_marker_still_served():
    # An item inserted BELOW the current marker value is still served within a cycle
    # thanks to wraparound (it is not skipped just for sorting before the marker).
    items = [10, 20, 30, 40]
    _b, after = next_batch(items, None, 2)  # -> [10, 20], marker 20
    items2 = [5] + items  # 5 sorts before the marker
    seen = set()
    for _ in range(poll_interval_ticks(len(items2), 2)):
        batch, after = next_batch(items2, after, 2)
        seen.update(batch)
    assert 5 in seen


def test_removing_the_marker_resumes_cleanly():
    items = [1, 2, 3, 4, 5]
    batch1, after = next_batch(items, None, 2)
    assert batch1 == [1, 2] and after == 2
    # Marker value 2 is removed from the set before the next tick.
    batch2, after = next_batch([1, 3, 4, 5], after, 2)
    assert batch2 == [3, 4]  # first items strictly greater than the removed marker
    assert after == 4


def test_removing_items_does_not_starve_survivors():
    # Shrinking the set between ticks still covers every survivor within a cycle.
    after = None
    items = list(range(30))
    _b, after = next_batch(items, after, 10)
    items = list(range(5, 30))  # drop the lowest 5 (some already served)
    seen = set()
    for _ in range(poll_interval_ticks(len(items), 10)):
        batch, after = next_batch(items, after, 10)
        seen.update(batch)
    assert set(items) <= seen | set(range(5))  # every remaining item eventually served
    assert set(items) - seen == set() or set(items).issubset(seen)


def test_string_ids_rotate_like_ints():
    # The manga wheel keys on MangaDex UUID strings; the helper is type-agnostic as
    # long as items are mutually comparable.
    items = ["b", "a", "d", "c"]
    batch, after = next_batch(items, None, 2)
    assert batch == ["a", "b"]
    assert after == "b"
    batch2, after = next_batch(items, after, 2)
    assert batch2 == ["c", "d"]


# ---------------------------------------------------------------------------
# Totality on degenerate input
# ---------------------------------------------------------------------------


def test_empty_items_returns_empty_and_keeps_marker():
    batch, after = next_batch([], 5, 10)
    assert batch == []
    assert after == 5  # an empty tick never resets the wheel


def test_non_positive_budget_returns_empty_and_keeps_marker():
    for budget in (0, -1, -100):
        batch, after = next_batch([1, 2, 3], 2, budget)
        assert batch == []
        assert after == 2


# ---------------------------------------------------------------------------
# poll_interval_ticks
# ---------------------------------------------------------------------------


def test_poll_interval_ticks_is_ceil_div():
    assert poll_interval_ticks(125, 25) == 5
    assert poll_interval_ticks(2000, 25) == 80
    assert poll_interval_ticks(26, 25) == 2
    assert poll_interval_ticks(25, 25) == 1
    assert poll_interval_ticks(1, 25) == 1


def test_poll_interval_ticks_edges():
    assert poll_interval_ticks(0, 25) == 0  # nothing tracked
    assert poll_interval_ticks(-3, 25) == 0
    assert poll_interval_ticks(10, 0) == 10  # defensive: a dead budget never drains
