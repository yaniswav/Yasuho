"""Unit tests for :mod:`tools.premium_usage` (L4: measuring real usage of
every premium-raisable limit from the database).

No network, no live Postgres: the ``_RecordingPool`` double below records
every ``fetchrow`` call's query text and arguments and returns a
configurable dict - this tests the WIRING (one query per resource, the right
threshold parameters, the right dict keys) and the PURE rendering/coverage
logic, never SQL semantics. The actual queries are probed by hand against a
throwaway PG11 container (see the lot's own report) - a real database is not
part of this test suite's job.
"""

from __future__ import annotations

import dataclasses

import pytest

from tools import premium, premium_usage

# ---------------------------------------------------------------------------
# Pure threshold logic
# ---------------------------------------------------------------------------


def test_thresholds_round_80_percent_up():
    # 80% of 25 is exactly 20 - no rounding needed.
    assert premium_usage.thresholds(25) == (20, 25)
    # 80% of 5 is 4.0 exactly too.
    assert premium_usage.thresholds(5) == (4, 5)
    # 80% of 6 is 4.8 - ceil to 5, never truncated down to 4.
    assert premium_usage.thresholds(6) == (5, 6)


def test_thresholds_collapse_to_zero_for_a_zero_free_limit():
    """music_247's FREE value (False/0): no division, both thresholds 0."""
    assert premium_usage.thresholds(0) == (0, 0)


def test_thresholds_never_negative_for_a_negative_input():
    # Defensive only - no real FREE value is negative, but this must not
    # explode or go through the ceil-multiply branch.
    assert premium_usage.thresholds(-1) == (0, 0)


# ---------------------------------------------------------------------------
# Coverage: every GuildLimits/UserLimits field is mapped or excluded
# ---------------------------------------------------------------------------


def test_missing_resource_mappings_flags_an_unmapped_unexcluded_field():
    """NEGATIVE CONTROL for the coverage check itself: a synthetic field that
    is neither mapped by any spec nor named in the exclusion dict must be
    flagged - proving the detector is not vacuously green."""
    fields = ["max_guild_playlists", "a_brand_new_field_nobody_mapped_yet"]
    specs = [s for s in premium_usage.RESOURCE_SPECS if s.scope == "guild"]
    missing = premium_usage.missing_resource_mappings(
        fields, specs, premium_usage.EXCLUDED_GUILD_FIELDS
    )
    assert missing == ["a_brand_new_field_nobody_mapped_yet"]


def test_missing_resource_mappings_empty_when_every_field_is_accounted_for():
    fields = ["max_guild_playlists", "history_max_items"]
    specs = [s for s in premium_usage.RESOURCE_SPECS if s.scope == "guild"]
    missing = premium_usage.missing_resource_mappings(
        fields, specs, premium_usage.EXCLUDED_GUILD_FIELDS
    )
    assert missing == []


def test_coverage_gaps_is_empty_against_the_real_dataclasses():
    """The real assertion: every field GuildLimits/UserLimits carries TODAY
    is either mapped by a RESOURCE_SPECS entry or documented as excluded."""
    assert premium_usage.coverage_gaps() == []


def test_negative_control_breaking_a_real_mapping_is_caught():
    """Removes ONE real spec's field mapping (in memory - the module itself
    is untouched) and proves coverage_gaps-style detection fails loudly,
    mirroring the manual "break tools/premium_usage.py, see the test fail,
    restore it" check this lot's report also performs by hand."""
    guild_fields = [f.name for f in dataclasses.fields(premium.GuildLimits)]
    broken_specs = [
        dataclasses.replace(s, field=None) if s.key == "guild_playlists" else s
        for s in premium_usage.RESOURCE_SPECS
        if s.scope == "guild"
    ]
    missing = premium_usage.missing_resource_mappings(
        guild_fields, broken_specs, premium_usage.EXCLUDED_GUILD_FIELDS
    )
    assert missing == ["max_guild_playlists"]


def test_every_resource_spec_key_is_unique():
    keys = [s.key for s in premium_usage.RESOURCE_SPECS]
    assert len(keys) == len(set(keys))


def test_context_only_resource_has_no_field_and_is_never_flagged():
    """tickets_open_per_guild claims no GuildLimits field on purpose (see the
    module docstring) - it must never be counted as "mapped" for some OTHER
    field by accident."""
    spec = next(
        s for s in premium_usage.RESOURCE_SPECS if s.key == "tickets_open_per_guild"
    )
    assert spec.field is None


# ---------------------------------------------------------------------------
# SQL text sanity - each resource's query names the right table/columns
# ---------------------------------------------------------------------------

# (resource key, substrings that MUST appear in its query text)
_EXPECTED_SQL_SUBSTRINGS = {
    "guild_playlists": ["guild_playlists", "GROUP BY guild_id"],
    "playlist_tracks": ["guild_playlists", "track_count"],
    "anilist_feeds": ["anilist_feeds", "GROUP BY guild_id"],
    "anilist_follows": ["anilist_follows", "channel_id"],
    "anilist_subs": ["anilist_channel_subs", "channel_id"],
    "role_menus": ["role_menus", "GROUP BY guild_id"],
    "voice_hubs": ["guild_settings", "autorooms"],
    "tickets_open_per_member": ["tickets", "status = 'open'", "opener_id"],
    "tickets_open_per_guild": ["tickets", "status = 'open'"],
    "music_247": ["music_247"],
    "favourites": ["music_favorites", "GROUP BY user_id"],
    "reminders_pending": ["timers", "event = 'reminder'", "author_id"],
    "reminders_recurring": ["timers", "repeat_seconds", "author_id"],
}


def test_every_resource_has_expected_sql_substrings_documented():
    """Fails loudly if a RESOURCE_SPECS key is added/renamed without updating
    this test's own table - the negative control for the loop test below."""
    spec_keys = {s.key for s in premium_usage.RESOURCE_SPECS}
    assert spec_keys == set(_EXPECTED_SQL_SUBSTRINGS)


@pytest.mark.parametrize("key", sorted(_EXPECTED_SQL_SUBSTRINGS))
def test_resource_query_references_right_table_and_columns(key):
    spec = next(s for s in premium_usage.RESOURCE_SPECS if s.key == key)
    for substring in _EXPECTED_SQL_SUBSTRINGS[key]:
        assert substring in spec.query, f"{key}: missing {substring!r} in SQL"


def test_every_query_uses_the_shared_percentile_shape():
    """Every resource is ONE bounded query: GROUP BY scope (done by the
    caller-provided inner SELECT), then percentile_cont/FILTER in SQL - never
    a per-row Python loop over the underlying table."""
    for spec in premium_usage.RESOURCE_SPECS:
        assert "percentile_cont(0.5)" in spec.query
        assert "percentile_cont(0.9)" in spec.query
        assert "percentile_cont(0.99)" in spec.query
        assert "FILTER (WHERE cnt >= $1)" in spec.query
        assert "FILTER (WHERE cnt >= $2)" in spec.query


# ---------------------------------------------------------------------------
# free_value / premium_value
# ---------------------------------------------------------------------------


def test_free_and_premium_values_match_the_catalog():
    guild_playlists = next(
        s for s in premium_usage.RESOURCE_SPECS if s.key == "guild_playlists"
    )
    assert premium_usage.free_value(guild_playlists) == premium.GUILD_FREE.max_guild_playlists
    assert (
        premium_usage.premium_value(guild_playlists)
        == premium.GUILD_PREMIUM.max_guild_playlists
    )


def test_context_only_resource_has_no_free_or_premium_value():
    spec = next(
        s for s in premium_usage.RESOURCE_SPECS if s.key == "tickets_open_per_guild"
    )
    assert premium_usage.free_value(spec) is None
    assert premium_usage.premium_value(spec) is None


# ---------------------------------------------------------------------------
# Rendering - pure, no DB
# ---------------------------------------------------------------------------


def test_format_resource_line_handles_a_full_row():
    spec = next(
        s for s in premium_usage.RESOURCE_SPECS if s.key == "guild_playlists"
    )
    row = {"scopes": 42, "median": 3.0, "p90": 12.5, "p99": 22.0, "max": 25,
           "at_80": 5, "at_100": 2}
    line = premium_usage.format_resource_line(spec, row)
    assert "42" in line
    assert "3" in line
    assert "12.5" in line
    assert "25" in line  # FREE value for this resource


def test_format_resource_line_handles_missing_values_as_dash():
    spec = next(
        s for s in premium_usage.RESOURCE_SPECS if s.key == "guild_playlists"
    )
    line = premium_usage.format_resource_line(spec, {})
    assert "-" in line


def test_render_report_includes_every_resource_and_stays_under_budget():
    rows_by_key = {
        spec.key: {"scopes": 1, "median": 1.0, "p90": 1.0, "p99": 1.0,
                   "max": 1, "at_80": 0, "at_100": 0}
        for spec in premium_usage.RESOURCE_SPECS
    }
    chunks = premium_usage.render_report(rows_by_key)
    assert chunks
    joined = "\n".join(chunks)
    for spec in premium_usage.RESOURCE_SPECS:
        assert spec.label in joined
    for chunk in chunks:
        assert len(chunk) <= premium_usage._MESSAGE_BUDGET + 200  # fence slack


def test_render_report_never_crashes_on_a_fully_empty_dataset():
    chunks = premium_usage.render_report({})
    assert chunks
    assert "resource" in chunks[0]  # the header line


# ---------------------------------------------------------------------------
# fetch_one / fetch_all wiring (bounded-query shape, right parameters)
# ---------------------------------------------------------------------------


class _RecordingPool:
    """Records every fetchrow call's (query, args); returns a fixed row."""

    def __init__(self, row=None):
        self.calls = []
        self._row = row if row is not None else {
            "scopes": 1, "median": 1.0, "p90": 1.0, "p99": 1.0,
            "max": 1, "at_80": 0, "at_100": 0,
        }

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self._row


async def test_fetch_one_binds_the_80_and_100_percent_thresholds():
    pool = _RecordingPool()
    spec = next(
        s for s in premium_usage.RESOURCE_SPECS if s.key == "guild_playlists"
    )
    await premium_usage.fetch_one(pool, spec)
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert query == spec.query
    expected = premium_usage.thresholds(premium.GUILD_FREE.max_guild_playlists)
    assert args == expected


async def test_fetch_one_returns_zeroed_defaults_on_no_row():
    class _EmptyPool:
        async def fetchrow(self, query, *args):
            return None

    spec = next(
        s for s in premium_usage.RESOURCE_SPECS if s.key == "guild_playlists"
    )
    row = await premium_usage.fetch_one(_EmptyPool(), spec)
    assert row["scopes"] == 0
    assert row["median"] is None


async def test_fetch_all_runs_exactly_one_query_per_resource_keyed_by_key():
    pool = _RecordingPool()
    rows = await premium_usage.fetch_all(pool)
    assert len(pool.calls) == len(premium_usage.RESOURCE_SPECS)
    assert set(rows) == {spec.key for spec in premium_usage.RESOURCE_SPECS}


async def test_fetch_one_for_music_247_uses_zero_thresholds():
    """music_247's FREE value is False/0 - confirms no division happened and
    the thresholds really do collapse to (0, 0) for this one resource."""
    pool = _RecordingPool()
    spec = next(s for s in premium_usage.RESOURCE_SPECS if s.key == "music_247")
    await premium_usage.fetch_one(pool, spec)
    _query, args = pool.calls[0]
    assert args == (0, 0)
