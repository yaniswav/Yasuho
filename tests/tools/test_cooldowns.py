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
