"""Unit tests for the audio-effect presets (``cogs/music/effects.py``).

The live filter PATCH cannot run under pytest (it needs a connected node and a
server-side player), so these cover the deterministic parts:

* the pure catalog - keys unique, presentation fields present, and each
  ``build()`` returning a fresh spec with the exact Lavalink / LavaDSPX field
  names verified against the plugin jar;
* the pure decision helpers - ``resolve_preset``, ``is_effect_exempt`` and the
  quota-heartbeat folders;
* ``_filters_from_spec`` and ``apply_preset`` driven against a fake player and a
  real ``GlobalCeiling``, exercising the acquire/release bookkeeping (including
  the restore-with-a-full-ceiling "skip the effect" path) and the best-effort
  "never raises" contract.

The catalog and helpers are sonolink-free and run everywhere; the two tests that
build a real ``Filters`` are skipped when only the stubbed sonolink is present
(the dev box), and run on the real-sonolink box where the gates execute.
"""

import types

import pytest

from cogs.music import effects
from tools.quotas import GlobalCeiling

try:
    from sonolink.models import Filters as _RealFilters  # noqa: F401

    _HAS_SONOLINK = True
except Exception:
    _HAS_SONOLINK = False

sonolink_required = pytest.mark.skipif(
    not _HAS_SONOLINK, reason="needs the real sonolink Filters models"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class _FakePlayer:
    """Records set_filters calls (or raises) and carries an effect_preset attr."""

    def __init__(self, guild_id=1, raise_on_set=False):
        self.guild = _FakeGuild(guild_id) if guild_id is not None else None
        self.effect_preset = None
        self.set_filters_calls = []
        self._raise = raise_on_set

    async def set_filters(self, filters, **kwargs):
        self.set_filters_calls.append(filters)
        if self._raise:
            raise RuntimeError("node down")


def _fake_quotas(capacity):
    """A minimal registry stand-in exposing just the filtered_players ceiling."""
    return types.SimpleNamespace(filtered_players=GlobalCeiling(capacity))


# ---------------------------------------------------------------------------
# Catalog invariants
# ---------------------------------------------------------------------------


def test_catalog_keys_unique():
    keys = [p.key for p in effects.PRESET_CATALOG]
    assert len(keys) == len(set(keys))


def test_catalog_has_expected_presets():
    keys = {p.key for p in effects.PRESET_CATALOG}
    assert keys == {
        "off",
        "nightcore",
        "slowed",
        "bassboost",
        "8d",
        "karaoke",
        "vaporwave",
        "echo",
        "soft",
    }


def test_off_is_first_and_is_the_off_key():
    assert effects.PRESET_CATALOG[0].key == effects.OFF_KEY == "off"


def test_presets_by_key_covers_catalog():
    assert set(effects.PRESETS_BY_KEY) == {p.key for p in effects.PRESET_CATALOG}
    for key, preset in effects.PRESETS_BY_KEY.items():
        assert preset.key == key


def test_every_preset_has_presentation_fields():
    for preset in effects.PRESET_CATALOG:
        assert preset.emoji and isinstance(preset.emoji, str)
        assert preset.label and isinstance(preset.label, str)
        assert preset.description and isinstance(preset.description, str)
        assert callable(preset.builder)


def test_build_returns_dict():
    for preset in effects.PRESET_CATALOG:
        assert isinstance(preset.build(), dict)


def test_off_builds_empty_spec():
    assert effects.PRESETS_BY_KEY["off"].build() == {}


def test_build_returns_fresh_copies():
    # Mutating one build() result must never leak into the next or the catalog.
    preset = effects.PRESETS_BY_KEY["bassboost"]
    first = preset.build()
    first["equalizer"].append((14, 0.0))
    second = preset.build()
    assert (14, 0.0) not in second["equalizer"]


# ---------------------------------------------------------------------------
# Payload shapes (pure) - native filters
# ---------------------------------------------------------------------------


def test_nightcore_speeds_up_and_pitches_up():
    spec = effects.PRESETS_BY_KEY["nightcore"].build()
    assert spec["timescale"]["speed"] > 1.0
    assert spec["timescale"]["pitch"] > 1.0


def test_slowed_slows_tempo():
    spec = effects.PRESETS_BY_KEY["slowed"].build()
    assert spec["timescale"]["speed"] < 1.0


def test_bass_boost_lifts_only_low_bands_within_range():
    spec = effects.PRESETS_BY_KEY["bassboost"].build()
    bands = spec["equalizer"]
    assert bands, "bass boost must set some bands"
    for band, gain in bands:
        assert 0 <= band <= 4, "only the low bands should move"
        assert -0.25 <= gain <= 1.0, "gain must stay in Lavalink's range"
        assert gain > 0, "bass boost lifts, never cuts"


def test_eight_d_uses_slow_rotation():
    spec = effects.PRESETS_BY_KEY["8d"].build()
    assert spec["rotation"]["rotation_hz"] > 0


def test_karaoke_sets_vocal_cut_band():
    spec = effects.PRESETS_BY_KEY["karaoke"].build()
    kar = spec["karaoke"]
    assert set(kar) == {"level", "mono_level", "filter_band", "filter_width"}
    assert 0.0 <= kar["level"] <= 1.0


def test_vaporwave_is_slowed_pitched_down_with_tremolo():
    spec = effects.PRESETS_BY_KEY["vaporwave"].build()
    assert spec["timescale"]["speed"] < 1.0
    assert spec["timescale"]["pitch"] < 1.0
    assert 0.0 < spec["tremolo"]["depth"] <= 1.0
    assert spec["tremolo"]["frequency"] > 0.0


# ---------------------------------------------------------------------------
# Payload shapes (pure) - LavaDSPX plugin filters (field names from the jar)
# ---------------------------------------------------------------------------


def test_echo_plugin_filter_exact_fields():
    spec = effects.PRESETS_BY_KEY["echo"].build()
    echo = spec["plugin_filters"]["echo"]
    assert set(echo) == {"echoLength", "decay"}
    # Both must be > 0 or the LavaDSPX plugin leaves echo disabled.
    assert echo["echoLength"] > 0 and echo["decay"] > 0


def test_soft_plugin_filter_exact_fields():
    spec = effects.PRESETS_BY_KEY["soft"].build()
    pf = spec["plugin_filters"]
    assert set(pf) == {"high-pass", "normalization"}
    assert set(pf["high-pass"]) == {"cutoffFrequency", "boostFactor"}
    assert isinstance(pf["high-pass"]["cutoffFrequency"], int)
    assert pf["high-pass"]["cutoffFrequency"] > 0
    assert set(pf["normalization"]) == {"maxAmplitude", "adaptive"}
    assert 0.0 < pf["normalization"]["maxAmplitude"] <= 1.0
    assert isinstance(pf["normalization"]["adaptive"], bool)


def test_only_plugin_presets_carry_plugin_filters():
    plugin_keys = {
        p.key for p in effects.PRESET_CATALOG if "plugin_filters" in p.build()
    }
    assert plugin_keys == {"echo", "soft"}


# ---------------------------------------------------------------------------
# resolve_preset
# ---------------------------------------------------------------------------


def test_resolve_by_key():
    assert effects.resolve_preset("nightcore").key == "nightcore"


def test_resolve_is_case_insensitive_on_key_and_label():
    assert effects.resolve_preset("NIGHTCORE").key == "nightcore"
    assert effects.resolve_preset("Bass Boost").key == "bassboost"
    assert effects.resolve_preset("8D").key == "8d"


def test_resolve_unknown_and_empty_drop_to_none():
    assert effects.resolve_preset("does-not-exist") is None
    assert effects.resolve_preset("") is None
    assert effects.resolve_preset(None) is None


# ---------------------------------------------------------------------------
# is_effect_exempt (pure decision helper)
# ---------------------------------------------------------------------------


def test_exempt_dj_is_exempt():
    assert effects.is_effect_exempt(dj_id=5, actor_id=5, has_manage_guild=False)


def test_exempt_manage_guild_is_exempt_even_without_dj():
    assert effects.is_effect_exempt(dj_id=None, actor_id=9, has_manage_guild=True)


def test_non_dj_non_manager_is_not_exempt():
    assert not effects.is_effect_exempt(dj_id=5, actor_id=9, has_manage_guild=False)


def test_no_dj_and_no_manage_is_not_exempt():
    assert not effects.is_effect_exempt(dj_id=None, actor_id=9, has_manage_guild=False)


# ---------------------------------------------------------------------------
# _filters_from_spec (real sonolink)
# ---------------------------------------------------------------------------


@sonolink_required
def test_filters_from_empty_spec_has_no_active_subfilters():
    f = effects._filters_from_spec({})
    assert f.timescale is None
    assert f.rotation is None
    assert f.karaoke is None
    assert not f.equalizer
    assert not f.plugin_filters


@sonolink_required
def test_filters_from_spec_builds_each_subfilter():
    ts = effects._filters_from_spec(effects.PRESETS_BY_KEY["nightcore"].build())
    assert ts.timescale is not None and ts.timescale.speed == 1.15
    eq = effects._filters_from_spec(effects.PRESETS_BY_KEY["bassboost"].build())
    assert len(eq.equalizer) == 5 and eq.equalizer[0].band == 0
    echo = effects._filters_from_spec(effects.PRESETS_BY_KEY["echo"].build())
    assert echo.plugin_filters == {"echo": {"echoLength": 0.4, "decay": 0.5}}


# ---------------------------------------------------------------------------
# apply_preset - ceiling / attr bookkeeping
# ---------------------------------------------------------------------------


@sonolink_required
async def test_apply_acquires_slot_and_sets_attr():
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7)
    assert await effects.apply_preset(p, "nightcore", quotas=q) == effects.RESULT_OK
    assert p.effect_preset == "nightcore"
    assert q.filtered_players.count() == 1
    assert len(p.set_filters_calls) == 1


@sonolink_required
async def test_switch_preset_keeps_single_slot():
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7)
    await effects.apply_preset(p, "nightcore", quotas=q)
    assert await effects.apply_preset(p, "slowed", quotas=q) == effects.RESULT_OK
    assert p.effect_preset == "slowed"
    assert q.filtered_players.count() == 1  # idempotent acquire, still one slot


@sonolink_required
async def test_off_releases_slot_and_clears_attr():
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7)
    await effects.apply_preset(p, "nightcore", quotas=q)
    assert await effects.apply_preset(p, "off", quotas=q) == effects.RESULT_CLEARED
    assert p.effect_preset is None
    assert q.filtered_players.count() == 0
    assert len(p.set_filters_calls) == 2  # applied, then cleared


@sonolink_required
async def test_off_never_blocked_by_full_ceiling():
    # A full ceiling must never stop a user clearing their own effect.
    q = _fake_quotas(capacity=1)
    p = _FakePlayer(guild_id=7)
    await effects.apply_preset(p, "nightcore", quotas=q)  # takes the only slot
    assert await effects.apply_preset(p, "off", quotas=q) == effects.RESULT_CLEARED
    assert q.filtered_players.count() == 0


async def test_off_when_nothing_held_is_a_safe_noop_release():
    # Reaches set_filters, so guard-free only if sonolink is present; the release
    # itself is idempotent regardless.
    if not _HAS_SONOLINK:
        pytest.skip("needs the real sonolink Filters models")
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7)
    assert await effects.apply_preset(p, "off", quotas=q) == effects.RESULT_CLEARED
    assert p.effect_preset is None
    assert q.filtered_players.count() == 0


async def test_full_ceiling_refuses_without_applying():
    # The restore-with-full-ceiling core: refuse cleanly, apply nothing, keep the
    # sound untouched. Returns BEFORE building any Filters, so no sonolink needed.
    q = _fake_quotas(capacity=1)
    q.filtered_players.acquire(999)  # another guild holds the only slot
    p = _FakePlayer(guild_id=7)
    result = await effects.apply_preset(p, "nightcore", quotas=q)
    assert result == effects.RESULT_CEILING_FULL
    assert p.effect_preset is None
    assert p.set_filters_calls == []
    assert q.filtered_players.count() == 1  # unchanged; the other guild keeps it


async def test_unknown_key_returns_unknown_and_touches_nothing():
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7)
    result = await effects.apply_preset(p, "garbage", quotas=q)
    assert result == effects.RESULT_UNKNOWN
    assert q.filtered_players.count() == 0
    assert p.set_filters_calls == []


@sonolink_required
async def test_apply_error_releases_newly_taken_slot():
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7, raise_on_set=True)
    result = await effects.apply_preset(p, "nightcore", quotas=q)
    assert result == effects.RESULT_ERROR
    assert p.effect_preset is None
    assert q.filtered_players.count() == 0  # released the slot this call took


@sonolink_required
async def test_apply_error_on_switch_keeps_existing_slot():
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7)
    await effects.apply_preset(p, "nightcore", quotas=q)  # holds a slot
    p._raise = True
    result = await effects.apply_preset(p, "slowed", quotas=q)
    assert result == effects.RESULT_ERROR
    assert p.effect_preset == "nightcore"  # previous effect unchanged
    assert q.filtered_players.count() == 1  # slot kept: the old effect still runs


@sonolink_required
async def test_off_clears_even_when_node_rejects_the_reset():
    q = _fake_quotas(capacity=40)
    p = _FakePlayer(guild_id=7)
    await effects.apply_preset(p, "nightcore", quotas=q)
    p._raise = True
    # Best-effort: the node PATCH fails but our tracking still clears / releases.
    assert await effects.apply_preset(p, "off", quotas=q) == effects.RESULT_CLEARED
    assert p.effect_preset is None
    assert q.filtered_players.count() == 0


# ---------------------------------------------------------------------------
# Quota-heartbeat helpers
# ---------------------------------------------------------------------------


def test_stats_are_nonzero_false_when_all_zero():
    assert not effects.stats_are_nonzero(
        {"a": {"x": 0, "y": 0}, "b": {"z": 0}}
    )


def test_stats_are_nonzero_true_when_any_nonzero():
    assert effects.stats_are_nonzero({"a": {"x": 0}, "b": {"z": 3}})


def test_format_quota_stats_folds_members():
    line = effects.format_quota_stats(
        {"effects_guild": {"hits": 3, "rejections": 1}, "filtered_players": {"holders": 2}}
    )
    assert "effects_guild(hits=3 rejections=1)" in line
    assert "filtered_players(holders=2)" in line
