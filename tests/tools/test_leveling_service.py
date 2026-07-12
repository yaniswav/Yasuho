"""Unit tests for tools.leveling (the pure leveling service - no bot/DB needed).

These pin the four contracts the cog leans on:

* the XP curve is byte-for-byte identical to the OLD inline formula (a property
  test over a wide range asserts ZERO drift, since the user decreed no curve
  change);
* level_up_between reports a level-up exactly when a threshold is crossed;
* grant_amount draws from an injectable rng within the given band;
* resolve_config / LevelConfig.from_row encode the JSONB -> level_config
  read-through precedence and default-filling.
"""

from tools import leveling

# ---------------------------------------------------------------------------
# Curve identity: zero drift from the original inline formula.
# ---------------------------------------------------------------------------


def test_level_for_xp_is_the_old_formula_verbatim():
    """level_for_xp must equal int((xp / 100) ** 0.5) at every point in range."""
    for xp in range(0, 200_001):
        assert leveling.level_for_xp(xp) == int((xp / 100) ** 0.5)


def test_xp_for_level_is_the_old_threshold_formula_verbatim():
    """xp_for_level must equal level ** 2 * 100 at every level in range."""
    for level in range(0, 1001):
        assert leveling.xp_for_level(level) == level**2 * 100


def test_curve_round_trips_at_thresholds():
    """The entry XP for a level maps back to that level (the two are inverses)."""
    for level in range(0, 500):
        assert leveling.level_for_xp(leveling.xp_for_level(level)) == level


# ---------------------------------------------------------------------------
# level_up_between truth table.
# ---------------------------------------------------------------------------


def test_level_up_between_truth_table():
    cases = [
        # (old_xp, new_xp, expected)
        (0, 10, None),      # still level 0
        (0, 99, None),      # still level 0 (just under the first threshold)
        (90, 100, 1),       # crossed 100 -> level 1
        (100, 110, None),   # both already level 1
        (399, 400, 2),      # crossed into level 2
        (100, 100, None),   # no movement
        (0, 400, 2),        # multi-level jump reports the FINAL level
        (110, 90, None),    # xp went down (defensive): never a level up
    ]
    for old_xp, new_xp, expected in cases:
        assert leveling.level_up_between(old_xp, new_xp) == expected, (
            old_xp,
            new_xp,
        )


def test_level_up_between_matches_the_cog_gate_over_a_range():
    """Agrees with the old ``new_level > old_level`` gate across many grants."""
    for old_xp in range(0, 5000, 7):
        for gain in (15, 20, 25):
            new_xp = old_xp + gain
            old_level = int((old_xp / 100) ** 0.5)
            new_level = int((new_xp / 100) ** 0.5)
            expected = new_level if new_level > old_level else None
            assert leveling.level_up_between(old_xp, new_xp) == expected


# ---------------------------------------------------------------------------
# grant_amount: injectable rng, correct band.
# ---------------------------------------------------------------------------


class _FakeRng:
    """Records each randint(a, b) call and returns a fixed value."""

    def __init__(self, value):
        self._value = value
        self.calls = []

    def randint(self, a, b):
        self.calls.append((a, b))
        return self._value


def test_grant_amount_uses_the_injected_rng_and_band():
    rng = _FakeRng(21)
    assert leveling.grant_amount(15, 25, rng=rng) == 21
    assert rng.calls == [(15, 25)]


def test_grant_amount_passes_a_custom_band_through():
    rng = _FakeRng(3)
    assert leveling.grant_amount(3, 3, rng=rng) == 3
    assert rng.calls == [(3, 3)]


def test_grant_amount_default_band_matches_the_original():
    """With no args it draws 15-25 inclusive, like the original random.randint."""
    seen = set()
    for _ in range(2000):
        gain = leveling.grant_amount()
        assert 15 <= gain <= 25
        seen.add(gain)
    assert min(seen) == 15 and max(seen) == 25  # both bounds are reachable


# ---------------------------------------------------------------------------
# LevelConfig.from_row + resolve_config: the read-through migration decision.
# ---------------------------------------------------------------------------


def _row(**overrides):
    row = {
        "enabled": True,
        "cooldown_seconds": 60,
        "xp_min": 15,
        "xp_max": 25,
        "announce_mode": "channel",
        "announce_channel_id": None,
        "announce_template": None,
    }
    row.update(overrides)
    return row


def test_from_row_reads_every_column():
    cfg = leveling.LevelConfig.from_row(
        _row(
            enabled=True,
            cooldown_seconds=30,
            xp_min=5,
            xp_max=9,
            announce_mode="dm",
            announce_channel_id=42,
            announce_template="gg {user}",
        )
    )
    assert cfg == leveling.LevelConfig(
        enabled=True,
        cooldown_seconds=30,
        xp_min=5,
        xp_max=9,
        announce_mode="dm",
        announce_channel_id=42,
        announce_template="gg {user}",
    )


def test_from_row_fills_defaults_for_missing_keys():
    """A partial mapping (only enabled) yields the all-default config."""
    cfg = leveling.LevelConfig.from_row({"enabled": True})
    assert cfg == leveling.LevelConfig(enabled=True)


def test_from_row_treats_null_columns_as_defaults():
    """A SQL NULL in a non-nullable-with-default column falls back to the default."""
    cfg = leveling.LevelConfig.from_row(
        _row(cooldown_seconds=None, xp_min=None, xp_max=None, announce_mode=None)
    )
    assert cfg.cooldown_seconds == leveling.DEFAULT_COOLDOWN_SECONDS
    assert cfg.xp_min == leveling.DEFAULT_XP_MIN
    assert cfg.xp_max == leveling.DEFAULT_XP_MAX
    assert cfg.announce_mode == leveling.DEFAULT_ANNOUNCE_MODE


def test_resolve_config_row_present_and_enabled_wins():
    cfg = leveling.resolve_config(_row(cooldown_seconds=45), legacy_enabled=False)
    assert cfg is not None
    assert cfg.enabled and cfg.cooldown_seconds == 45


def test_resolve_config_disabled_row_beats_legacy_true():
    """A present row is the source of truth: enabled=False wins over JSONB true."""
    assert leveling.resolve_config(_row(enabled=False), legacy_enabled=True) is None


def test_resolve_config_falls_back_to_legacy_when_no_row():
    assert (
        leveling.resolve_config(None, legacy_enabled=True)
        == leveling.LevelConfig(enabled=True)
    )
    assert leveling.resolve_config(None, legacy_enabled=False) is None
