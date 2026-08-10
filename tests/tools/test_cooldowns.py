"""Unit tests for tools.cooldowns.Cooldowns (pure, no bot needed)."""

from tools.cooldowns import Cooldowns


def test_unknown_key_is_not_active():
    cd = Cooldowns(60)
    assert cd.is_active("missing", now=1.0) is False


def test_active_within_window_then_expires():
    cd = Cooldowns(60)
    cd.touch("k", now=1000.0)
    assert cd.is_active("k", now=1030.0) is True
    assert cd.is_active("k", now=1059.9) is True
    assert cd.is_active("k", now=1060.0) is False


def test_touch_refreshes_the_window():
    cd = Cooldowns(10)
    cd.touch("k", now=0.0)
    cd.touch("k", now=100.0)
    assert cd.is_active("k", now=105.0) is True


def test_seconds_override_wins_over_the_instance_window():
    """A per-check ``seconds`` override lets one map serve per-caller windows."""
    cd = Cooldowns(60)  # instance default
    cd.touch("k", now=1000.0)
    # A shorter override expires the key sooner than the instance's 60s.
    assert cd.is_active("k", now=1030.0, seconds=10) is False
    # A longer override keeps it active past the instance's 60s.
    assert cd.is_active("k", now=1090.0, seconds=120) is True
    # A zero override means "never cooling": any elapsed time reads inactive.
    assert cd.is_active("k", now=1000.0, seconds=0) is False


def test_sweep_bounds_the_map():
    cd = Cooldowns(10, sweep_at=3)
    cd.touch("a", now=0.0)
    cd.touch("b", now=0.0)
    cd.touch("c", now=0.0)
    assert len(cd) == 3  # still at the cap, no sweep yet

    # A fourth key well past the window trips the sweep, dropping the stale ones.
    cd.touch("d", now=1000.0)
    assert len(cd) == 1
    assert cd.is_active("d", now=1000.0) is True
    assert cd.is_active("a", now=1000.0) is False


# ---------------------------------------------------------------------------
# WAVE-B-B1: the sweep judges each entry by ITS OWN window, and is amortised
# ---------------------------------------------------------------------------


def _counting_sweep(cd):
    """Wrap ``cd._sweep`` and return a list of the map size at each call.

    The list length is the number of full rebuilds, and its sum is the total
    number of entries copied - the two things the amortisation claim is about.
    """
    sizes = []
    original = cd._sweep

    def counted(now):
        sizes.append(len(cd))
        original(now)

    cd._sweep = counted
    return sizes


def test_touch_stores_a_per_entry_window():
    """A per-caller window handed to touch governs that entry, not the default."""
    cd = Cooldowns(10)
    cd.touch("slow", now=0.0, seconds=300)
    cd.touch("fast", now=0.0)

    assert cd.is_active("slow", now=100.0) is True  # its own 300s, not the 10s
    assert cd.is_active("fast", now=100.0) is False


def test_the_sweep_keeps_a_key_whose_own_window_is_still_running():
    """THE BUG: one module-wide window swept every key, so an entry touched
    under a longer per-caller window was evicted while still cooling - which
    reads as "cooldown over" and lets the debounced event straight through."""
    cd = Cooldowns(10, sweep_at=2)
    cd.touch("slow", now=0.0, seconds=300)
    cd.touch("stale", now=0.0)
    cd.touch("trip", now=60.0)  # trips the sweep, 60s in

    assert cd.is_active("slow", now=60.0) is True
    assert "slow" in cd._seen  # not merely reported active: still in the map
    assert cd.is_active("stale", now=60.0) is False
    assert len(cd) == 2  # the stale one, and only that one, is gone


def test_a_longer_check_window_widens_the_entry_it_checks():
    """Belt and braces for a caller that names its window on the check but not
    on the touch: the check widens the entry, so the sweep still cannot evict a
    key that caller considers cooling. Every in-tree caller now names it on
    BOTH (see test_leveling / test_profile_connectors), so this is the safety
    net for the next one, not the mechanism anything relies on."""
    cd = Cooldowns(10, sweep_at=2)
    cd.touch("k", now=0.0)  # stored under the 10s default
    assert cd.is_active("k", now=5.0, seconds=300) is True  # widens to 300s

    cd.touch("other", now=60.0)
    cd.touch("third", now=60.0)  # trips the sweep well past the 10s default

    assert "k" in cd._seen
    assert cd.is_active("k", now=60.0, seconds=300) is True


def test_a_shorter_check_window_never_narrows_the_entry():
    """A one-off short check must not shrink what the map is keeping for the
    key: an over-kept entry is harmless, an under-kept one is a missed
    cooldown."""
    cd = Cooldowns(100)
    cd.touch("k", now=0.0)
    assert cd.is_active("k", now=50.0, seconds=10) is False

    assert cd.is_active("k", now=50.0) is True
    assert cd._seen["k"][1] == 100


def test_the_sweep_is_amortised_not_once_per_touch():
    """THE PERF BUG: past the cap, EVERY touch rebuilt the whole dict. With
    2000 live keys and a hot listener that is an O(n) copy per event, forever.
    The threshold now doubles after each sweep, so the rebuilds are
    logarithmic in the number of keys and the copying is O(1) per touch."""
    cd = Cooldowns(1000, sweep_at=4)
    sizes = _counting_sweep(cd)

    for i in range(1000):  # all fresh: nothing is ever evictable
        cd.touch(i, now=1.0)

    assert len(cd) == 1000  # nothing lost
    assert len(sizes) <= 20, sizes  # the old code swept ~996 times
    # Total entries copied across every rebuild stays a small multiple of the
    # keys touched, instead of the ~500k the per-touch rebuild cost.
    assert sum(sizes) < 4 * 1000


def test_the_map_stays_bounded_when_keys_keep_aging_out():
    """The amortised threshold must not turn the cap into unbounded growth: a
    churn of distinct one-shot keys still collapses back to the live set."""
    cd = Cooldowns(10, sweep_at=4)
    for i in range(500):
        cd.touch(i, now=float(i))  # each key is stale 10 ticks later

    assert len(cd) <= 40  # bounded by ~2x the genuinely-active keys


def test_the_sweep_threshold_never_drops_below_sweep_at():
    """After a sweep that leaves almost nothing, the next one is armed at the
    floor rather than at 2x0 - otherwise every touch would sweep again."""
    cd = Cooldowns(10, sweep_at=3)
    cd.touch("a", now=0.0)
    cd.touch("b", now=0.0)
    cd.touch("c", now=0.0)
    cd.touch("d", now=1000.0)  # sweeps down to one survivor

    sizes = _counting_sweep(cd)
    cd.touch("e", now=1000.0)
    cd.touch("f", now=1000.0)

    assert len(cd) == 3
    assert sizes == []  # still under the floor: no rebuild
