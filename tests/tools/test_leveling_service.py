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

import datetime

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


# ---------------------------------------------------------------------------
# No-XP zones (L3): NoXpSnapshot.from_rows + is_no_xp_message.
# ---------------------------------------------------------------------------


def _no_xp_row(kind, target_id):
    return {"kind": kind, "target_id": target_id}


def test_from_rows_splits_by_kind():
    rows = [
        _no_xp_row("channel", 10),
        _no_xp_row("channel", 20),
        _no_xp_row("role", 30),
    ]
    snapshot = leveling.NoXpSnapshot.from_rows(rows)
    assert snapshot.channels == {10, 20}
    assert snapshot.roles == {30}


def test_from_rows_empty_yields_empty_snapshot():
    snapshot = leveling.NoXpSnapshot.from_rows([])
    assert snapshot.channels == frozenset()
    assert snapshot.roles == frozenset()


def test_is_no_xp_message_empty_snapshot_never_blocks():
    assert (
        leveling.is_no_xp_message(
            leveling.EMPTY_NO_XP_SNAPSHOT, 1, 2, [3, 4]
        )
        is False
    )


def test_is_no_xp_message_channel_hit():
    snapshot = leveling.NoXpSnapshot(channels=frozenset({10}))
    assert leveling.is_no_xp_message(snapshot, 10, None, []) is True
    assert leveling.is_no_xp_message(snapshot, 99, None, []) is False


def test_is_no_xp_message_category_hit():
    """A category id in `channels` mutes every channel inside it (no per-
    channel row needed) - see NoXpSnapshot's docstring for the design call."""
    snapshot = leveling.NoXpSnapshot(channels=frozenset({50}))
    assert leveling.is_no_xp_message(snapshot, 999, 50, []) is True
    # A channel not in the muted category, with no category at all, is fine.
    assert leveling.is_no_xp_message(snapshot, 999, None, []) is False
    assert leveling.is_no_xp_message(snapshot, 999, 51, []) is False


def test_is_no_xp_message_role_hit():
    snapshot = leveling.NoXpSnapshot(roles=frozenset({7}))
    assert leveling.is_no_xp_message(snapshot, 1, None, [7]) is True
    assert leveling.is_no_xp_message(snapshot, 1, None, [8, 9]) is False
    assert leveling.is_no_xp_message(snapshot, 1, None, []) is False


def test_is_no_xp_message_channel_and_role_both_configured():
    snapshot = leveling.NoXpSnapshot(channels=frozenset({10}), roles=frozenset({7}))
    # Either alone is enough to block.
    assert leveling.is_no_xp_message(snapshot, 10, None, []) is True
    assert leveling.is_no_xp_message(snapshot, 1, None, [7]) is True
    assert leveling.is_no_xp_message(snapshot, 1, None, [8]) is False


def test_can_add_no_xp_entry_below_and_at_cap():
    assert leveling.can_add_no_xp_entry(0) is True
    assert leveling.can_add_no_xp_entry(leveling.MAX_NO_XP_PER_GUILD - 1) is True
    assert leveling.can_add_no_xp_entry(leveling.MAX_NO_XP_PER_GUILD) is False
    assert leveling.can_add_no_xp_entry(leveling.MAX_NO_XP_PER_GUILD + 1) is False


# ---------------------------------------------------------------------------
# Announce template validation (validate_announce_template truth table).
# ---------------------------------------------------------------------------


def test_validate_template_accepts_every_allowed_placeholder():
    ok, reason = leveling.validate_announce_template(
        "{user} hit level {level} in {guild}!"
    )
    assert ok is True
    assert reason is None


def test_validate_template_accepts_a_subset_of_placeholders():
    ok, reason = leveling.validate_announce_template("gg {user}")
    assert ok is True
    assert reason is None


def test_validate_template_accepts_no_placeholders_at_all():
    ok, reason = leveling.validate_announce_template("Nice work, everyone!")
    assert (ok, reason) == (True, None)


def test_validate_template_rejects_none():
    assert leveling.validate_announce_template(None) == (False, "empty")


def test_validate_template_rejects_empty_and_whitespace():
    assert leveling.validate_announce_template("") == (False, "empty")
    assert leveling.validate_announce_template("   ") == (False, "empty")


def test_validate_template_rejects_too_long():
    too_long = "x" * (leveling.MAX_ANNOUNCE_TEMPLATE_LEN + 1)
    assert leveling.validate_announce_template(too_long) == (False, "too_long")


def test_validate_template_accepts_exactly_the_length_cap():
    exact = "x" * leveling.MAX_ANNOUNCE_TEMPLATE_LEN
    ok, reason = leveling.validate_announce_template(exact)
    assert (ok, reason) == (True, None)


def test_validate_template_rejects_unknown_placeholder():
    ok, reason = leveling.validate_announce_template("{user} says {secret}")
    assert (ok, reason) == (False, "unknown_placeholder")


def test_validate_template_rejects_attribute_access():
    """Only the bare {user}/{level}/{guild} names are allowed - no "{user.x}"
    attribute/index reach-through, a safety boundary."""
    ok, reason = leveling.validate_announce_template("{user.mention}")
    assert (ok, reason) == (False, "unknown_placeholder")


def test_validate_template_rejects_positional_fields():
    for template in ("{} says hi", "{0} says hi"):
        ok, reason = leveling.validate_announce_template(template)
        assert (ok, reason) == (False, "unknown_placeholder"), template


def test_validate_template_rejects_format_spec():
    """A format spec on an allowed name must be rejected at SET time: it parses
    as name='level'/spec='>9999999' and would render to a multi-megabyte string
    (a memory DoS) - the name-only allow-list would otherwise wave it through."""
    for template in ("{level:>9999999}", "{level:>10}", "{user:^80}", "{level:{user}}"):
        ok, reason = leveling.validate_announce_template(template)
        assert (ok, reason) == (False, "unknown_placeholder"), template


def test_validate_template_rejects_conversion():
    """A conversion ("{user!r}"/"{user!s}") is likewise reported separately from
    the name by parse(), so it too must be rejected - only bare placeholders."""
    for template in ("{user!r}", "{user!s}", "{level!a}"):
        ok, reason = leveling.validate_announce_template(template)
        assert (ok, reason) == (False, "unknown_placeholder"), template


def test_validate_template_rejects_index_access():
    """Index reach-through ("{user[0]}") is caught by the name allow-list."""
    ok, reason = leveling.validate_announce_template("{user[0]}")
    assert (ok, reason) == (False, "unknown_placeholder")


def test_validate_template_rejects_malformed_braces():
    ok, reason = leveling.validate_announce_template("gg {user")
    assert (ok, reason) == (False, "malformed")


def test_validate_template_accepts_escaped_literal_braces():
    ok, reason = leveling.validate_announce_template("{{not a placeholder}} {user}")
    assert (ok, reason) == (True, None)


# ---------------------------------------------------------------------------
# render_announce_template.
# ---------------------------------------------------------------------------


def test_render_template_fills_every_placeholder():
    text = leveling.render_announce_template(
        "{user} hit {level} in {guild}!",
        user_text="<@2>",
        level=5,
        guild_name="Test Guild",
    )
    assert text == "<@2> hit 5 in Test Guild!"


def test_render_template_none_falls_back_to_default():
    text = leveling.render_announce_template(
        None, user_text="<@2>", level=5, guild_name="G"
    )
    assert text == leveling.DEFAULT_ANNOUNCE_TEMPLATE.format(
        user="<@2>", level=5, guild="G"
    )


def test_render_template_survives_a_stale_malformed_template():
    """Defensive fallback: a template that somehow reaches render() malformed
    (should never happen post-validation) never raises, it degrades to the
    default instead."""
    text = leveling.render_announce_template(
        "gg {user", user_text="<@2>", level=5, guild_name="G"
    )
    assert text == leveling.DEFAULT_ANNOUNCE_TEMPLATE.format(
        user="<@2>", level=5, guild="G"
    )


def test_render_template_escaped_braces_stay_literal():
    text = leveling.render_announce_template(
        "{{hi}} {user}", user_text="<@2>", level=5, guild_name="G"
    )
    assert text == "{hi} <@2>"


def test_render_template_caps_an_abusive_format_spec():
    """Defensive: a STORED template with an abusive format spec (which
    format_map honours WITHOUT raising, so the except clause never fires) can
    never emit a giant string - render caps it and falls back to the default.
    Validation blocks such a template at SET time; this is the second line."""
    text = leveling.render_announce_template(
        "{level:>9999999}", user_text="<@2>", level=5, guild_name="G"
    )
    assert len(text) <= leveling.MAX_RENDERED_ANNOUNCE_LEN
    assert text == leveling.DEFAULT_ANNOUNCE_TEMPLATE.format(
        user="<@2>", level=5, guild="G"
    )


# ---------------------------------------------------------------------------
# resolve_announce_target routing decision.
# ---------------------------------------------------------------------------


def test_resolve_announce_target_off():
    assert leveling.resolve_announce_target("off", 111, 222) == ("off", None)


def test_resolve_announce_target_channel():
    assert leveling.resolve_announce_target("channel", 111, 222) == ("channel", 111)


def test_resolve_announce_target_dm():
    assert leveling.resolve_announce_target("dm", 111, 222) == ("dm", None)


def test_resolve_announce_target_fixed_with_channel_configured():
    assert leveling.resolve_announce_target("fixed", 111, 222) == ("fixed", 222)


def test_resolve_announce_target_fixed_without_channel_configured_falls_back():
    """A 'fixed' mode with no announce_channel_id set falls back to the
    source channel (the original, always-safe behaviour) rather than going
    silent."""
    assert leveling.resolve_announce_target("fixed", 111, None) == ("channel", 111)


def test_resolve_announce_target_unknown_mode_falls_back_to_channel():
    assert leveling.resolve_announce_target("not-a-real-mode", 111, 222) == (
        "channel",
        111,
    )


# ---------------------------------------------------------------------------
# Voice XP (L7): rate validation, eligibility truth table, credit maths,
# batch-payload building, and the LevelConfig voice fields.
# ---------------------------------------------------------------------------


def test_validate_voice_xp_rate_accepts_the_bounds_and_inside():
    assert leveling.validate_voice_xp_rate(
        leveling.MIN_VOICE_XP_PER_MINUTE
    ) == (True, None)
    assert leveling.validate_voice_xp_rate(
        leveling.MAX_VOICE_XP_PER_MINUTE
    ) == (True, None)
    assert leveling.validate_voice_xp_rate(5) == (True, None)


def test_validate_voice_xp_rate_rejects_out_of_range():
    for bad in (0, -1, leveling.MAX_VOICE_XP_PER_MINUTE + 1, 1000):
        assert leveling.validate_voice_xp_rate(bad) == (False, "out_of_range"), bad


def test_validate_voice_xp_rate_rejects_bool_and_non_int():
    # bool is an int subclass: True == 1 would slip through the range test, so
    # it is rejected explicitly (an admin can never set the rate to "True").
    for bad in (True, False, 5.0, "5", None):
        assert leveling.validate_voice_xp_rate(bad) == (False, "out_of_range"), bad


def _eligible_kwargs(**overrides):
    """The all-eligible base case; each test flips exactly one flag."""
    base = dict(
        enabled=True,
        in_voice=True,
        human_count=2,
        is_afk_channel=False,
        self_deaf=False,
        self_mute=False,
        is_no_xp=False,
    )
    base.update(overrides)
    return base


def test_is_voice_xp_eligible_all_conditions_met():
    assert leveling.is_voice_xp_eligible(**_eligible_kwargs()) is True


def test_is_voice_xp_eligible_truth_table_each_blocker():
    # (override, expected) - flipping any single blocker makes it ineligible.
    cases = [
        (dict(enabled=False), False),          # leveling/voice XP off
        (dict(in_voice=False), False),         # not in a voice channel
        (dict(human_count=1), False),          # alone (below VOICE_MIN_HUMANS)
        (dict(human_count=0), False),          # empty channel
        (dict(is_afk_channel=True), False),    # parked in the AFK channel
        (dict(self_deaf=True), False),         # self-deafened
        (dict(self_mute=True), False),         # self-muted
        (dict(is_no_xp=True), False),          # a no-XP zone / role (L3 reuse)
        (dict(human_count=3), True),           # more than two humans is fine
    ]
    for override, expected in cases:
        assert (
            leveling.is_voice_xp_eligible(**_eligible_kwargs(**override)) is expected
        ), override


def test_voice_min_humans_is_two():
    """A member alone earns nothing; a pair earns - the anti-farm floor."""
    assert leveling.VOICE_MIN_HUMANS == 2
    assert leveling.is_voice_xp_eligible(**_eligible_kwargs(human_count=1)) is False
    assert leveling.is_voice_xp_eligible(**_eligible_kwargs(human_count=2)) is True


def test_voice_credit_full_window_eligible():
    """A full sweep window credits interval/60 minutes at the given rate."""
    xp, consumed = leveling.voice_credit(300, 5, 300, eligible=True)
    assert (xp, consumed) == (25, 300)  # 5 minutes x 5 XP


def test_voice_credit_honours_the_rate():
    xp, consumed = leveling.voice_credit(300, 10, 300, eligible=True)
    assert (xp, consumed) == (50, 300)


def test_voice_credit_ineligible_credits_nothing_but_advances_marker():
    """Ineligible time is NOT banked: zero XP, yet the marker still advances by
    the whole minutes consumed (so the next sweep starts fresh)."""
    xp, consumed = leveling.voice_credit(300, 5, 300, eligible=False)
    assert (xp, consumed) == (0, 300)


def test_voice_credit_partial_minutes_floor_and_carry():
    """150s = 2 whole minutes credited; the 30s remainder carries (marker only
    advances by the 120s consumed, not the full 150s)."""
    xp, consumed = leveling.voice_credit(150, 5, 300, eligible=True)
    assert (xp, consumed) == (10, 120)


def test_voice_credit_under_a_minute_is_a_no_op():
    """Less than a whole minute credits nothing and leaves the marker put."""
    assert leveling.voice_credit(59, 5, 300, eligible=True) == (0, 0)
    assert leveling.voice_credit(0, 5, 300, eligible=True) == (0, 0)


def test_voice_credit_caps_credited_minutes_no_catch_up_banking():
    """A returning session (an hour of elapsed time after a missed sweep) is
    capped at interval/60 credited minutes, and the excess is CONSUMED (marker
    jumps forward the whole hour), never banked into future sweeps."""
    xp, consumed = leveling.voice_credit(3600, 5, 300, eligible=True)
    assert xp == 25  # capped at 5 minutes x 5 XP, not 60 minutes
    assert consumed == 3600  # all 60 whole minutes consumed -> marker to now


def test_voice_credit_marker_never_passes_now():
    """consumed_seconds (the marker advance) is always <= elapsed, so advancing
    the marker by it can never push it past the present."""
    for elapsed in (0, 59, 60, 125, 300, 301, 5000):
        _xp, consumed = leveling.voice_credit(elapsed, 5, 300, eligible=True)
        assert consumed <= elapsed


def test_build_voice_grant_payload_folds_triples_into_parallel_arrays():
    credits = [(1, 2, 10), (1, 3, 20), (5, 7, 5)]
    guild_ids, user_ids, gains = leveling.build_voice_grant_payload(credits)
    assert guild_ids == [1, 1, 5]
    assert user_ids == [2, 3, 7]
    assert gains == [10, 20, 5]


def test_build_voice_grant_payload_empty():
    assert leveling.build_voice_grant_payload([]) == ([], [], [])


def test_from_row_reads_voice_xp_columns():
    cfg = leveling.LevelConfig.from_row(
        _row(voice_xp_enabled=True, voice_xp_per_minute=12)
    )
    assert cfg.voice_xp_enabled is True
    assert cfg.voice_xp_per_minute == 12


def test_from_row_defaults_voice_xp_when_absent_or_null():
    # Absent (a row written before the columns existed) -> field defaults.
    cfg = leveling.LevelConfig.from_row({"enabled": True})
    assert cfg.voice_xp_enabled is False
    assert cfg.voice_xp_per_minute == leveling.DEFAULT_VOICE_XP_PER_MINUTE
    # SQL NULL -> the same defaults.
    cfg2 = leveling.LevelConfig.from_row(
        _row(voice_xp_enabled=None, voice_xp_per_minute=None)
    )
    assert cfg2.voice_xp_enabled is False
    assert cfg2.voice_xp_per_minute == leveling.DEFAULT_VOICE_XP_PER_MINUTE


# ---------------------------------------------------------------------------
# XP multipliers (L4): apply_multiplier, validation, duration parsing,
# MultiplierSnapshot.from_rows / is_trivial, and the compute_multiplier
# stacking truth table (the Lurkr rule).
# ---------------------------------------------------------------------------


def test_apply_multiplier_rounds_to_nearest_and_floors_at_zero():
    assert leveling.apply_multiplier(10, 1.0) == 10
    assert leveling.apply_multiplier(10, 2.0) == 20
    assert leveling.apply_multiplier(10, 0.5) == 5
    assert leveling.apply_multiplier(10, 0.04) == 0  # rounds down to zero
    assert leveling.apply_multiplier(10, 0.0) == 0  # explicit mute


def test_apply_multiplier_never_goes_negative():
    """A pathological negative multiplier (should never reach here past
    validation) still floors at 0 rather than going negative."""
    assert leveling.apply_multiplier(10, -1.0) == 0


def test_validate_multiplier_factor_accepts_the_bounds_and_inside():
    assert leveling.validate_multiplier_factor(leveling.MIN_MULTIPLIER_FACTOR) == (
        True,
        None,
    )
    assert leveling.validate_multiplier_factor(leveling.MAX_MULTIPLIER_FACTOR) == (
        True,
        None,
    )
    assert leveling.validate_multiplier_factor(1.0) == (True, None)
    assert leveling.validate_multiplier_factor(2) == (True, None)  # plain int


def test_validate_multiplier_factor_rejects_out_of_range():
    for bad in (-0.1, leveling.MAX_MULTIPLIER_FACTOR + 0.1, 100):
        assert leveling.validate_multiplier_factor(bad) == (False, "out_of_range")


def test_validate_multiplier_factor_rejects_bool_and_non_numeric():
    for bad in (True, False, "2", None, [2]):
        assert leveling.validate_multiplier_factor(bad) == (False, "invalid")


def test_can_add_multiplier_below_and_at_cap():
    assert leveling.can_add_multiplier(0) is True
    assert leveling.can_add_multiplier(leveling.MAX_MULTIPLIERS_PER_GUILD - 1) is True
    assert leveling.can_add_multiplier(leveling.MAX_MULTIPLIERS_PER_GUILD) is False
    assert (
        leveling.can_add_multiplier(leveling.MAX_MULTIPLIERS_PER_GUILD + 1) is False
    )


def test_validate_event_duration_accepts_the_bounds_and_inside():
    assert leveling.validate_event_duration(leveling.MIN_EVENT_DURATION_SECONDS) == (
        True,
        None,
    )
    assert leveling.validate_event_duration(leveling.MAX_EVENT_DURATION_SECONDS) == (
        True,
        None,
    )
    assert leveling.validate_event_duration(3600) == (True, None)


def test_validate_event_duration_rejects_out_of_range():
    assert leveling.validate_event_duration(
        leveling.MIN_EVENT_DURATION_SECONDS - 1
    ) == (False, "out_of_range")
    assert leveling.validate_event_duration(
        leveling.MAX_EVENT_DURATION_SECONDS + 1
    ) == (False, "out_of_range")


def test_validate_event_duration_rejects_bool_and_non_numeric():
    for bad in (True, False, "3600", None):
        assert leveling.validate_event_duration(bad) == (False, "invalid")


def test_parse_short_duration_reads_every_unit():
    assert leveling.parse_short_duration("2h") == 2 * 3600
    assert leveling.parse_short_duration("3d") == 3 * 86400
    assert leveling.parse_short_duration("1d12h") == 86400 + 12 * 3600
    assert leveling.parse_short_duration("30m") == 30 * 60
    assert leveling.parse_short_duration("45s") == 45


def test_parse_short_duration_rejects_empty_and_garbage():
    assert leveling.parse_short_duration("") is None
    assert leveling.parse_short_duration(None) is None
    assert leveling.parse_short_duration("not a duration") is None
    assert leveling.parse_short_duration("0h") is None  # matches, but zero total


# ---------------------------------------------------------------------------
# MultiplierSnapshot: from_rows + is_trivial.
# ---------------------------------------------------------------------------


def _mult_row(kind, target_id, factor):
    return {"kind": kind, "target_id": target_id, "factor": factor}


def test_multiplier_snapshot_from_rows_splits_by_kind():
    rows = [
        _mult_row(leveling.MULTIPLIER_GLOBAL, leveling.GLOBAL_MULTIPLIER_TARGET_ID, 2.0),
        _mult_row(leveling.MULTIPLIER_CHANNEL, 10, 1.5),
        _mult_row(leveling.MULTIPLIER_ROLE, 20, 3.0),
    ]
    snapshot = leveling.MultiplierSnapshot.from_rows(rows)
    assert snapshot.global_factor == 2.0
    assert snapshot.channels == {10: 1.5}
    assert snapshot.roles == {20: 3.0}
    assert snapshot.event_factor is None
    assert snapshot.event_ends_at is None


def test_multiplier_snapshot_from_rows_empty_yields_defaults():
    snapshot = leveling.MultiplierSnapshot.from_rows([])
    assert snapshot.global_factor == 1.0
    assert snapshot.channels == {}
    assert snapshot.roles == {}


def test_multiplier_snapshot_from_rows_carries_the_event_through():
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    snapshot = leveling.MultiplierSnapshot.from_rows([], event_factor=2.0, event_ends_at=now)
    assert snapshot.event_factor == 2.0
    assert snapshot.event_ends_at == now


def test_empty_multiplier_snapshot_is_trivial():
    assert leveling.EMPTY_MULTIPLIER_SNAPSHOT.is_trivial is True


def test_multiplier_snapshot_is_trivial_truth_table():
    assert leveling.MultiplierSnapshot().is_trivial is True
    assert leveling.MultiplierSnapshot(global_factor=2.0).is_trivial is False
    assert leveling.MultiplierSnapshot(channels={10: 1.5}).is_trivial is False
    assert leveling.MultiplierSnapshot(roles={20: 1.5}).is_trivial is False
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    assert (
        leveling.MultiplierSnapshot(event_factor=2.0, event_ends_at=now).is_trivial
        is False
    )


# ---------------------------------------------------------------------------
# compute_multiplier: the Lurkr stacking truth table.
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
_FUTURE = _NOW + datetime.timedelta(hours=1)
_PAST = _NOW - datetime.timedelta(hours=1)


def test_compute_multiplier_empty_snapshot_is_neutral():
    assert (
        leveling.compute_multiplier(
            leveling.EMPTY_MULTIPLIER_SNAPSHOT, 1, None, [], _NOW
        )
        == 1.0
    )


def test_compute_multiplier_global_alone():
    snapshot = leveling.MultiplierSnapshot(global_factor=2.0)
    assert leveling.compute_multiplier(snapshot, 1, None, [], _NOW) == 2.0


def test_compute_multiplier_channel_alone():
    snapshot = leveling.MultiplierSnapshot(channels={10: 3.0})
    assert leveling.compute_multiplier(snapshot, 10, None, [], _NOW) == 3.0
    # A different, unconfigured channel is unaffected.
    assert leveling.compute_multiplier(snapshot, 99, None, [], _NOW) == 1.0


def test_compute_multiplier_category_alone():
    """A category-level entry applies to every channel inside it, mirroring
    NoXpSnapshot's own channel-or-category lookup."""
    snapshot = leveling.MultiplierSnapshot(channels={50: 0.5})
    assert leveling.compute_multiplier(snapshot, 999, 50, [], _NOW) == 0.5
    assert leveling.compute_multiplier(snapshot, 999, None, [], _NOW) == 1.0
    assert leveling.compute_multiplier(snapshot, 999, 51, [], _NOW) == 1.0


def test_compute_multiplier_channel_wins_over_category():
    """A channel-specific entry always wins over its category's entry - the
    locked design's tie-break rule."""
    snapshot = leveling.MultiplierSnapshot(channels={10: 3.0, 50: 0.5})  # 10 in cat 50
    assert leveling.compute_multiplier(snapshot, 10, 50, [], _NOW) == 3.0


def test_compute_multiplier_role_alone():
    snapshot = leveling.MultiplierSnapshot(roles={20: 2.5})
    assert leveling.compute_multiplier(snapshot, 1, None, [20], _NOW) == 2.5
    assert leveling.compute_multiplier(snapshot, 1, None, [99], _NOW) == 1.0
    assert leveling.compute_multiplier(snapshot, 1, None, [], _NOW) == 1.0


def test_compute_multiplier_role_uses_the_highest_not_a_product():
    """A member holding TWO boosted roles gets the bigger boost, never a
    stacked/multiplied one - the locked design's highest-role rule."""
    snapshot = leveling.MultiplierSnapshot(roles={20: 2.0, 21: 3.0, 22: 1.5})
    assert leveling.compute_multiplier(snapshot, 1, None, [20, 21, 22], _NOW) == 3.0
    assert leveling.compute_multiplier(snapshot, 1, None, [20, 22], _NOW) == 2.0


def test_compute_multiplier_event_alone_while_active():
    snapshot = leveling.MultiplierSnapshot(event_factor=2.0, event_ends_at=_FUTURE)
    assert leveling.compute_multiplier(snapshot, 1, None, [], _NOW) == 2.0


def test_compute_multiplier_event_expiry_boundary():
    """The event is active strictly BEFORE its ends_at, and expired AT and
    after it - an inclusive-exclusive boundary."""
    snapshot = leveling.MultiplierSnapshot(event_factor=2.0, event_ends_at=_NOW)
    assert leveling.compute_multiplier(snapshot, 1, None, [], _NOW) == 1.0  # at == expired
    assert (
        leveling.compute_multiplier(snapshot, 1, None, [], _NOW - datetime.timedelta(seconds=1))
        == 2.0
    )  # one second before -> still active


def test_compute_multiplier_event_already_expired_is_ignored():
    snapshot = leveling.MultiplierSnapshot(event_factor=2.0, event_ends_at=_PAST)
    assert leveling.compute_multiplier(snapshot, 1, None, [], _NOW) == 1.0


def test_compute_multiplier_every_tier_stacks_multiplicatively():
    """The full Lurkr stacking rule: effective = global * channel * role *
    event, every tier multiplied together."""
    snapshot = leveling.MultiplierSnapshot(
        global_factor=2.0,
        channels={10: 1.5},
        roles={20: 2.0},
        event_factor=2.0,
        event_ends_at=_FUTURE,
    )
    effective = leveling.compute_multiplier(snapshot, 10, None, [20], _NOW)
    assert effective == 2.0 * 1.5 * 2.0 * 2.0


def test_compute_multiplier_zero_factor_mutes_everything():
    snapshot = leveling.MultiplierSnapshot(global_factor=0.0)
    assert leveling.compute_multiplier(snapshot, 1, None, [], _NOW) == 0.0
    assert leveling.apply_multiplier(20, 0.0) == 0


def test_compute_multiplier_role_ids_may_be_any_iterable():
    """A generator (the hot-path shape) works exactly like a list."""
    snapshot = leveling.MultiplierSnapshot(roles={20: 2.0})
    assert (
        leveling.compute_multiplier(snapshot, 1, None, (rid for rid in [20]), _NOW)
        == 2.0
    )


# ---------------------------------------------------------------------------
# Period leaderboards (L6): period-key maths, prune cutoffs, marker staleness.
# ---------------------------------------------------------------------------


def _dt(year, month, day):
    return datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)


def test_iso_week_period_key_format():
    # 2026-07-12 is a Sunday in ISO week 28 of 2026.
    assert leveling.iso_week_period_key(_dt(2026, 7, 12)) == "W2026-28"


def test_iso_week_period_key_crosses_forward_into_next_iso_year():
    """Dec 29-31 2025 already belong to ISO week 2026-W01 (the ISO year a
    late-December date falls in can run AHEAD of the calendar year)."""
    assert leveling.iso_week_period_key(_dt(2025, 12, 29)) == "W2026-01"
    assert leveling.iso_week_period_key(_dt(2025, 12, 31)) == "W2026-01"


def test_iso_week_period_key_start_of_year_can_belong_to_the_prior_iso_year():
    """Jan 1 2023 still belongs to ISO week 2022-W52 (the ISO year can run
    BEHIND the calendar year at the other side of the same boundary)."""
    assert leveling.iso_week_period_key(_dt(2023, 1, 1)) == "W2022-52"
    assert leveling.iso_week_period_key(_dt(2023, 1, 2)) == "W2023-01"  # the Monday after


def test_month_period_key_format():
    assert leveling.month_period_key(_dt(2026, 7, 12)) == "M2026-07"
    assert leveling.month_period_key(_dt(2026, 1, 1)) == "M2026-01"


def test_current_period_keys_pairs_both_from_the_same_instant():
    now = _dt(2026, 7, 12)
    assert leveling.current_period_keys(now) == ("W2026-28", "M2026-07")


def test_weekly_prune_cutoff_key_subtracts_whole_weeks():
    now = _dt(2026, 7, 12)  # W2026-28
    assert leveling.weekly_prune_cutoff_key(now) == "W2026-25"  # 3 weeks back


def test_weekly_prune_cutoff_key_crosses_a_year_boundary():
    now = _dt(2026, 1, 12)  # early January, ISO week 2026-W03
    assert leveling.weekly_prune_cutoff_key(now) == "W2025-52"  # rolled into the prior ISO year


def test_monthly_prune_cutoff_key_within_the_same_year():
    assert (
        leveling.monthly_prune_cutoff_key(_dt(2026, 7, 1), periods_back=3)
        == "M2026-04"
    )


def test_monthly_prune_cutoff_key_handles_year_rollover():
    assert (
        leveling.monthly_prune_cutoff_key(_dt(2026, 2, 1), periods_back=3)
        == "M2025-11"
    )
    assert (
        leveling.monthly_prune_cutoff_key(_dt(2026, 1, 1), periods_back=1)
        == "M2025-12"
    )


def test_prune_cutoff_keys_sort_lexically_before_the_current_period_key():
    """The cutoff must sort STRICTLY before the current period key of the
    same kind, so a ``period_key < cutoff`` DELETE never touches the
    current (or the still-kept, recent) periods."""
    now = _dt(2026, 7, 12)
    week_key, month_key = leveling.current_period_keys(now)
    assert leveling.weekly_prune_cutoff_key(now) < week_key
    assert leveling.monthly_prune_cutoff_key(now) < month_key


def test_period_marker_changed_none_previous_is_always_stale():
    assert leveling.period_marker_changed(None, ("W2026-28", "M2026-07")) is True


def test_period_marker_changed_same_pair_is_not_stale():
    current = ("W2026-28", "M2026-07")
    assert leveling.period_marker_changed(current, current) is False


def test_period_marker_changed_week_rollover_is_stale():
    assert (
        leveling.period_marker_changed(
            ("W2026-28", "M2026-07"), ("W2026-29", "M2026-07")
        )
        is True
    )


def test_period_marker_changed_month_rollover_is_stale():
    assert (
        leveling.period_marker_changed(
            ("W2026-31", "M2026-07"), ("W2026-31", "M2026-08")
        )
        is True
    )


# ---------------------------------------------------------------------------
# level_down_between truth table (leveling L5 - the admin XP tools' mirror of
# level_up_between; only an explicit admin action ever removes XP).
# ---------------------------------------------------------------------------


def test_level_down_between_truth_table():
    cases = [
        # (old_xp, new_xp, expected) - level_for_xp: 10000 -> 10, 9900 -> 9.
        (10000, 9900, 9),      # dropped exactly one level
        (10000, 0, 0),         # reset to zero -> level 0
        (2500, 100, 1),        # 25 -> ... 100 XP is level 1
        (10000, 10000, None),  # no change -> no drop
        (10000, 10500, None),  # went UP -> that is level_up_between's job
        (150, 120, None),      # both inside level 1 -> no threshold crossed
    ]
    for old_xp, new_xp, expected in cases:
        assert leveling.level_down_between(old_xp, new_xp) == expected, (
            f"old={old_xp} new={new_xp}"
        )


def test_level_down_between_is_the_inverse_gate_of_level_up_between():
    """For a strict move, at most one of up/down fires, never both."""
    for old_xp in range(0, 5000, 137):
        for new_xp in range(0, 5000, 211):
            up = leveling.level_up_between(old_xp, new_xp)
            down = leveling.level_down_between(old_xp, new_xp)
            assert not (up is not None and down is not None)


# ---------------------------------------------------------------------------
# leaderboard_page pager maths (leveling L5) - mirrors queue_page's clamping.
# ---------------------------------------------------------------------------


def test_leaderboard_page_empty_board_is_one_clamped_page():
    clamped, total_pages, start, end = leveling.leaderboard_page(0, 0)
    assert (clamped, total_pages, start, end) == (0, 1, 0, 0)


def test_leaderboard_page_single_full_page():
    # Exactly the page size -> one page, whole slice.
    clamped, total_pages, start, end = leveling.leaderboard_page(15, 0)
    assert (clamped, total_pages, start, end) == (0, 1, 0, 15)


def test_leaderboard_page_second_page_slice():
    # 50 members, page 1 (0-indexed) -> ranks 16..30.
    clamped, total_pages, start, end = leveling.leaderboard_page(50, 1)
    assert total_pages == 4  # 50 / 15 -> 4 pages
    assert (clamped, start, end) == (1, 15, 30)


def test_leaderboard_page_last_partial_page():
    clamped, total_pages, start, end = leveling.leaderboard_page(50, 3)
    assert (clamped, total_pages, start, end) == (3, 4, 45, 50)


def test_leaderboard_page_over_high_index_clamps_down():
    # A board that shrank under the viewer: asking for page 9 of a 20-member
    # board lands on the last real page, never a blank one.
    clamped, total_pages, start, end = leveling.leaderboard_page(20, 9)
    assert total_pages == 2
    assert clamped == 1
    assert (start, end) == (15, 20)


def test_leaderboard_page_negative_index_clamps_to_zero():
    clamped, _pages, start, end = leveling.leaderboard_page(30, -3)
    assert clamped == 0
    assert (start, end) == (0, 15)


def test_leaderboard_page_default_size_is_15():
    assert leveling.LEADERBOARD_PAGE_SIZE == 15
