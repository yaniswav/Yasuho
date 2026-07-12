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
