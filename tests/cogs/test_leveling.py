"""Unit tests for ``Leveling.level_for_xp`` (cogs/community/leveling.py).

``level_for_xp`` is the single source of truth for turning an XP total into a
level, and the ``rank`` command derives its progress bar from the inverse
``level**2 * 100`` threshold math. These tests pin down three properties:

* the origin case ``xp=0`` maps to level ``0``;
* the function is monotonic non-decreasing across a wide XP range (more XP can
  never lower your level);
* it agrees exactly with the ``cur_threshold``/``next_threshold`` arithmetic the
  ``rank`` command uses, so the card's "into level" / "span" figures stay sane.

The method is a ``@staticmethod`` with no dependencies, so it is called directly
on the class - no cog instance, bot, pool, or event loop required.
"""

from cogs.community.leveling import Leveling


def test_zero_xp_is_level_zero():
    assert Leveling.level_for_xp(0) == 0


def test_monotonic_non_decreasing():
    """More XP must never yield a lower level across a broad range."""
    prev = Leveling.level_for_xp(0)
    for xp in range(0, 100_001):
        level = Leveling.level_for_xp(xp)
        assert level >= prev, f"level dropped at xp={xp}: {level} < {prev}"
        prev = level


def test_matches_rank_threshold_math():
    """Agrees with the ``level**2 * 100`` thresholds used by the rank command.

    For every level, entering XP (``cur_threshold``) and the last XP before the
    next level must both resolve to that level, while ``next_threshold`` rolls
    over to level+1 - exactly what the rank card relies on.
    """
    for level in range(0, 100):
        cur_threshold = level**2 * 100
        next_threshold = (level + 1) ** 2 * 100

        # The threshold math is the inverse of level_for_xp: the entry XP for a
        # level maps back to that same level.
        assert Leveling.level_for_xp(cur_threshold) == level

        # Anywhere inside the band [cur, next) stays on the current level...
        assert Leveling.level_for_xp(next_threshold - 1) == level
        # ...and crossing next_threshold advances exactly one level.
        assert Leveling.level_for_xp(next_threshold) == level + 1


def test_level_boundaries_are_exact():
    """Spot-check the first few hand-computed thresholds and their edges."""
    # (xp, expected level) - boundaries and one below each.
    cases = [
        (0, 0),
        (99, 0),
        (100, 1),
        (399, 1),
        (400, 2),
        (899, 2),
        (900, 3),
        (1599, 3),
        (1600, 4),
    ]
    for xp, expected in cases:
        assert Leveling.level_for_xp(xp) == expected, f"xp={xp}"


def test_is_staticmethod_callable_without_instance():
    """Guard the call contract the rank/levels/on_message paths depend on."""
    assert isinstance(
        Leveling.__dict__["level_for_xp"], staticmethod
    )
    # Callable straight off the class, returning a plain int.
    result = Leveling.level_for_xp(2500)
    assert result == 5
    assert isinstance(result, int)
