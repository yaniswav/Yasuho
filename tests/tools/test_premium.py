"""Unit tests for :mod:`tools.premium` (M3a: catalog, projection, resolver).

No network, DB, Discord, or Lavalink is touched: the store helpers are
exercised against the repo's ``fake_pool`` fixture and the cache against
plain Python rows (dicts / SimpleNamespace), exactly like the entitlement-like
objects :func:`tools.premium._get` is written to accept.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import types

from tools import premium
from tools.config_loader import ConfigLoader

UTC = datetime.timezone.utc


def _loader(text):
    """An isolated ConfigLoader read from an in-memory .ini (no file I/O),
    the same substitution tests/tools/test_config_loader.py uses."""
    loader = ConfigLoader()
    loader.read_string(text)
    return loader


# ---------------------------------------------------------------------------
# [Premium] config parsing
# ---------------------------------------------------------------------------


def test_sku_id_absent_key_is_none():
    loader = _loader("[Premium]\nother_key = 1\n")
    assert premium._read_sku_id("yasuho_plus_sku", loader=loader) is None


def test_sku_id_absent_section_is_none():
    loader = _loader("[Other]\nfoo = 1\n")
    assert premium._read_sku_id("yasuho_plus_sku", loader=loader) is None


def test_sku_id_valid_integer():
    loader = _loader("[Premium]\nyasuho_plus_sku = 123456789012345678\n")
    assert premium._read_sku_id("yasuho_plus_sku", loader=loader) == 123456789012345678


def test_sku_id_valid_quoted_integer_is_unquoted():
    loader = _loader('[Premium]\nyasuho_plus_sku = "123456789012345678"\n')
    assert premium._read_sku_id("yasuho_plus_sku", loader=loader) == 123456789012345678


def test_sku_id_invalid_value_warns_and_is_treated_as_absent(caplog):
    loader = _loader("[Premium]\nyasuho_plus_sku = not-a-snowflake\n")
    with caplog.at_level(logging.WARNING, logger="tools.premium"):
        result = premium._read_sku_id("yasuho_plus_sku", loader=loader)
    assert result is None
    assert any("invalid [Premium]" in record.message for record in caplog.records)


def test_no_sku_configured_in_the_real_bot_ini_today():
    """Today's behaviour: a fresh checkout (no [Premium] section) must read
    both SKUs as absent, so :data:`premium.premium_limits` resolves FREE for
    everyone - the default this whole lot must never change."""
    assert premium.YASUHO_PLUS_SKU is None
    assert premium.COMFORT_PACK_SKU is None


# ---------------------------------------------------------------------------
# Catalog defaults: no SKU configured -> FREE everywhere
# ---------------------------------------------------------------------------


def test_fresh_cache_resolves_free_for_any_guild_or_user():
    cache = premium.EntitlementCache()
    assert cache.for_guild(1) is premium.GUILD_FREE
    assert cache.for_user(1) is premium.USER_FREE
    assert cache.is_guild_premium(1) is False
    assert cache.has_comfort_pack(1) is False


def test_active_rows_are_ignored_when_no_sku_is_configured(monkeypatch):
    """Even a loaded, ACTIVE row cannot grant premium while the SKU id this
    deployment would recognise is unset - matches YASUHO_PLUS_SKU/COMFORT_PACK_SKU
    both defaulting to None on a fresh checkout."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [
            {
                "sku_id": 999,
                "scope_type": "guild",
                "guild_id": 1,
                "deleted": False,
                "ends_at": None,
            },
        ]
    )
    assert cache.for_guild(1) == premium.GUILD_FREE


# ---------------------------------------------------------------------------
# FREE must equal today's behaviour - the drift guard
# ---------------------------------------------------------------------------
#
# tools/ must not import cogs (see tools/premium.py's module docstring), so
# every FREE_* constant except FREE_MAX_HUBS is RESTATED there rather than
# imported. These tests are the only place the restatement is checked against
# its real owner - break one on purpose (change a FREE_* value in
# tools/premium.py without touching the cog) and the matching assertion below
# must go red; that is the negative control the M3a brief asks for.


def test_free_guild_values_match_their_owning_cog_constants():
    from cogs.anilist import feed_policy
    from cogs.community.serverstats import cog as serverstats_cog
    from cogs.config import rolemenus
    from cogs.config.tickets import guild_config as tickets_guild_config
    from cogs.music import player as music_player
    from cogs.music import playlists_shared
    from tools.autoroom import MAX_HUBS

    assert premium.FREE_MAX_GUILD_PLAYLISTS == playlists_shared.MAX_GUILD_PLAYLISTS
    assert premium.FREE_MAX_PLAYLIST_TRACKS == playlists_shared.MAX_PLAYLIST_TRACKS
    assert premium.FREE_HISTORY_MAX_ITEMS == music_player.HISTORY_MAX_ITEMS
    assert premium.FREE_MAX_FEEDS_PER_GUILD == feed_policy.MAX_FEEDS_PER_GUILD
    assert premium.FREE_MAX_FOLLOWS_PER_FEED == feed_policy.MAX_FOLLOWS_PER_FEED
    assert premium.FREE_MAX_SUBS_PER_FEED == feed_policy.MAX_SUBS_PER_FEED
    assert premium.FREE_MAX_MENUS_PER_GUILD == rolemenus.MAX_MENUS_PER_GUILD
    assert premium.FREE_MAX_HUBS == MAX_HUBS
    assert (
        premium.FREE_MAX_TICKETS_OPEN_PER_USER
        == tickets_guild_config.MAX_OPEN_PER_USER
    )
    assert premium.FREE_SERVERSTATS_RETENTION_DAYS == serverstats_cog.RETENTION_DAYS
    # Brand-new benefits: no cog constant exists yet because no guild has
    # either today, whatever SKUs are configured.
    assert premium.FREE_MUSIC_247 is False
    assert premium.FREE_PREMIUM_BADGE is False


def test_free_user_values_match_their_owning_cog_constants():
    from cogs.community import reminders
    from cogs.music import music as music_cog

    assert premium.FREE_MAX_FAVOURITES == music_cog.MAX_FAVOURITES
    assert premium.FREE_MAX_PENDING_REMINDERS == reminders.MAX_PENDING_REMINDERS
    assert premium.FREE_MAX_RECURRING_REMINDERS == reminders.MAX_RECURRING_REMINDERS


def test_guild_free_dataclass_is_built_from_the_checked_constants():
    assert premium.GUILD_FREE == premium.GuildLimits(
        max_guild_playlists=premium.FREE_MAX_GUILD_PLAYLISTS,
        max_playlist_tracks=premium.FREE_MAX_PLAYLIST_TRACKS,
        history_max_items=premium.FREE_HISTORY_MAX_ITEMS,
        max_feeds_per_guild=premium.FREE_MAX_FEEDS_PER_GUILD,
        max_follows_per_feed=premium.FREE_MAX_FOLLOWS_PER_FEED,
        max_subs_per_feed=premium.FREE_MAX_SUBS_PER_FEED,
        max_menus_per_guild=premium.FREE_MAX_MENUS_PER_GUILD,
        max_hubs=premium.FREE_MAX_HUBS,
        max_tickets_open_per_user=premium.FREE_MAX_TICKETS_OPEN_PER_USER,
        serverstats_retention_days=premium.FREE_SERVERSTATS_RETENTION_DAYS,
        music_247=premium.FREE_MUSIC_247,
        premium_badge=premium.FREE_PREMIUM_BADGE,
    )


def test_user_free_dataclass_is_built_from_the_checked_constants():
    assert premium.USER_FREE == premium.UserLimits(
        max_favourites=premium.FREE_MAX_FAVOURITES,
        max_pending_reminders=premium.FREE_MAX_PENDING_REMINDERS,
        max_recurring_reminders=premium.FREE_MAX_RECURRING_REMINDERS,
    )


# ---------------------------------------------------------------------------
# Safety ceilings: every premium value <= its ceiling, every FREE value too
# ---------------------------------------------------------------------------


def test_every_premium_guild_value_is_at_most_its_ceiling():
    for name, ceiling in premium.GUILD_CEILINGS.items():
        assert getattr(premium.GUILD_PREMIUM, name) <= ceiling, name


def test_every_free_guild_value_is_at_most_its_ceiling():
    for name, ceiling in premium.GUILD_CEILINGS.items():
        assert getattr(premium.GUILD_FREE, name) <= ceiling, name


def test_every_premium_user_value_is_at_most_its_ceiling():
    for name, ceiling in premium.USER_CEILINGS.items():
        assert getattr(premium.USER_PREMIUM, name) <= ceiling, name


def test_every_free_user_value_is_at_most_its_ceiling():
    for name, ceiling in premium.USER_CEILINGS.items():
        assert getattr(premium.USER_FREE, name) <= ceiling, name


def test_guild_limits_clamps_a_value_over_its_ceiling():
    """The resolver clamps rather than trusts the literal: a catalog mistake
    (a premium value entered above its ceiling) can never leave this
    dataclass, however it was constructed."""
    limits = premium.GuildLimits(
        max_guild_playlists=10_000,  # far past GUILD_CEILINGS["max_guild_playlists"]
        max_playlist_tracks=1,
        history_max_items=1,
        max_feeds_per_guild=1,
        max_follows_per_feed=1,
        max_subs_per_feed=1,
        max_menus_per_guild=1,
        max_hubs=1,
        max_tickets_open_per_user=1,
        serverstats_retention_days=1,
        music_247=False,
        premium_badge=False,
    )
    assert limits.max_guild_playlists == premium.GUILD_CEILINGS["max_guild_playlists"]


def test_user_limits_clamps_a_value_over_its_ceiling():
    limits = premium.UserLimits(
        max_favourites=10_000,
        max_pending_reminders=1,
        max_recurring_reminders=1,
    )
    assert limits.max_favourites == premium.USER_CEILINGS["max_favourites"]


def test_limits_dataclasses_are_frozen():
    with_error = None
    try:
        premium.GUILD_FREE.max_hubs = 999
    except dataclasses.FrozenInstanceError as error:
        with_error = error
    assert with_error is not None


# ---------------------------------------------------------------------------
# The ACTIVE rule - truth table
# ---------------------------------------------------------------------------

NOW = datetime.datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)


def _row(**overrides):
    row = {
        "deleted": False,
        "ends_at": None,
        "last_synced_at": NOW,
    }
    row.update(overrides)
    return row


def test_deleted_is_never_active_whatever_the_dates():
    row = _row(deleted=True, ends_at=NOW + datetime.timedelta(days=30))
    assert premium.is_active(row, now=NOW) is False


def test_no_ends_at_is_active_until_deleted():
    """Test-mode entitlement: starts_at/ends_at are both None and it stays
    active until explicitly deleted - mirrors discord.Entitlement.is_expired()
    always returning False in that case."""
    row = _row(ends_at=None)
    assert premium.is_active(row, now=NOW) is True
    assert premium.is_active(row, now=NOW + datetime.timedelta(days=3650)) is True


def test_future_end_date_is_active():
    row = _row(ends_at=NOW + datetime.timedelta(days=1))
    assert premium.is_active(row, now=NOW) is True


def test_past_end_synced_after_the_end_is_inactive():
    """CONFIRMED end: we re-synced AFTER ends_at and Discord still reported
    this row, so the end is real - inactive immediately, even though `now`
    is still well inside the 48h grace window."""
    ends_at = NOW - datetime.timedelta(hours=1)
    row = _row(ends_at=ends_at, last_synced_at=ends_at + datetime.timedelta(minutes=1))
    assert premium.is_active(row, now=NOW) is False


def test_past_end_within_grace_and_not_synced_since_is_active():
    """The short technical grace: ended recently, within 48h, and nothing has
    reconfirmed the end since (last_synced_at predates ends_at) - a missed
    renewal event must not downgrade this subscriber yet."""
    ends_at = NOW - datetime.timedelta(hours=1)
    row = _row(ends_at=ends_at, last_synced_at=ends_at - datetime.timedelta(days=1))
    assert premium.is_active(row, now=NOW) is True


def test_past_end_beyond_grace_is_inactive_regardless_of_sync():
    ends_at = NOW - premium.GRACE - datetime.timedelta(minutes=1)
    row = _row(ends_at=ends_at, last_synced_at=ends_at - datetime.timedelta(days=30))
    assert premium.is_active(row, now=NOW) is False


def test_missing_last_synced_at_never_grants_the_grace():
    """A raw discord.Entitlement has no last_synced_at at all (it is a DB-only
    column); without it the grace branch must not fire, since there is
    nothing to compare against ends_at."""
    ends_at = NOW - datetime.timedelta(hours=1)
    entitlement = types.SimpleNamespace(deleted=False, ends_at=ends_at)
    assert premium.is_active(entitlement, now=NOW) is False


def test_is_active_accepts_a_discord_entitlement_shaped_object():
    """discord.Entitlement exposes attributes, not mapping access - _get must
    fall back to getattr for a real Entitlement instance shape."""
    entitlement = types.SimpleNamespace(
        deleted=False,
        ends_at=NOW + datetime.timedelta(days=10),
    )
    assert premium.is_active(entitlement, now=NOW) is True


# --- Negative control (a): the grace must require the sync check ----------
#
# tests/tools/test_premium.py::test_past_end_synced_after_the_end_is_inactive
# is the assertion this protects. Verified by hand during this lot: editing
# is_active() to drop the `last_synced_at < ends_at` half of the grace
# condition (granting grace whenever `now < ends_at + GRACE`, unconditionally)
# turns that test red, because the confirmed-end row then reads as active for
# the next 47 hours. Restored immediately after with the file copied aside
# and back (never git checkout/reset), per this lot's rules.


# ---------------------------------------------------------------------------
# EntitlementCache: load_rows, for_guild, for_user
# ---------------------------------------------------------------------------


def test_cache_resolves_premium_for_an_active_guild_subscription(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=111, scope_type="guild", guild_id=42, user_id=None)],
        now=NOW,
    )
    assert cache.for_guild(42) == premium.GUILD_PREMIUM
    assert cache.for_guild(43) == premium.GUILD_FREE
    assert cache.is_guild_premium(42) is True


def test_cache_resolves_premium_for_an_active_user_purchase(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=222, scope_type="user", guild_id=None, user_id=7)],
        now=NOW,
    )
    assert cache.for_user(7) == premium.USER_PREMIUM
    assert cache.for_user(8) == premium.USER_FREE
    assert cache.has_comfort_pack(7) is True


def test_cache_drops_an_inactive_row_at_load_time(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [
            _row(
                sku_id=111,
                scope_type="guild",
                guild_id=42,
                user_id=None,
                deleted=True,
            )
        ],
        now=NOW,
    )
    assert cache.for_guild(42) == premium.GUILD_FREE


def test_cache_ignores_a_different_sku_than_the_configured_one(monkeypatch):
    """An entitlement for some OTHER SKU (e.g. a future theme pack) must not
    grant Yasuho+ just because it is active and guild-scoped."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=999, scope_type="guild", guild_id=42, user_id=None)],
        now=NOW,
    )
    assert cache.for_guild(42) == premium.GUILD_FREE


async def test_cache_load_reads_only_non_deleted_rows_from_the_store(fake_pool, monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    fake_pool.fetch_return = [
        {
            "sku_id": 111,
            "scope_type": "guild",
            "guild_id": 42,
            "user_id": None,
            "deleted": False,
            "ends_at": None,
            "last_synced_at": NOW,
        }
    ]
    cache = premium.EntitlementCache()
    await cache.load(fake_pool)
    assert cache.for_guild(42) == premium.GUILD_PREMIUM
    assert "WHERE deleted = FALSE" in fake_pool.calls[0][1]


# ---------------------------------------------------------------------------
# Store helpers: upsert_entitlement, mark_deleted, load_active
# ---------------------------------------------------------------------------


def _entitlement(**overrides):
    row = dict(
        id=555,
        sku_id=111,
        guild_id=42,
        user_id=None,
        type=2,
        deleted=False,
        consumed=False,
        starts_at=NOW,
        ends_at=NOW + datetime.timedelta(days=30),
    )
    row.update(overrides)
    return types.SimpleNamespace(**row)


async def test_upsert_entitlement_is_one_statement_with_on_conflict(fake_pool):
    await premium.upsert_entitlement(fake_pool, _entitlement())

    assert len(fake_pool.calls) == 1
    method, query, args = fake_pool.calls[0]
    assert method == "execute"
    assert "INSERT INTO premium_entitlements" in query
    assert "ON CONFLICT (entitlement_id) DO UPDATE" in query
    assert "last_synced_at = now()" in query
    assert args == (555, 111, "guild", 42, None, 2, False, False, NOW, NOW + datetime.timedelta(days=30))


async def test_upsert_entitlement_resolves_a_user_scoped_row(fake_pool):
    row = await premium.upsert_entitlement(
        fake_pool, _entitlement(guild_id=None, user_id=7)
    )
    assert row["scope_type"] == "user"
    assert row["guild_id"] is None
    assert row["user_id"] == 7


def test_upsert_entitlement_guild_wins_if_both_ids_are_somehow_set():
    coerced = premium._coerce_entitlement(_entitlement(guild_id=42, user_id=7))
    assert coerced["scope_type"] == "guild"
    assert coerced["guild_id"] == 42
    assert coerced["user_id"] is None


def test_coerce_entitlement_refuses_a_scopeless_row():
    with_error = None
    try:
        premium._coerce_entitlement(_entitlement(guild_id=None, user_id=None))
    except ValueError as error:
        with_error = error
    assert with_error is not None


async def test_upsert_entitlement_accepts_a_plain_dict_test_double(fake_pool):
    row = await premium.upsert_entitlement(
        fake_pool,
        {
            "id": 1,
            "sku_id": 111,
            "guild_id": 42,
            "user_id": None,
            "type": 8,
            "deleted": False,
            "consumed": False,
            "starts_at": None,
            "ends_at": None,
        },
    )
    assert row["entitlement_id"] == 1
    assert row["scope_type"] == "guild"


async def test_mark_deleted_sets_the_flag_and_restamps_sync_time(fake_pool):
    fake_pool.execute_return = "UPDATE 1"
    result = await premium.mark_deleted(fake_pool, 555)

    assert result is True
    _method, query, args = fake_pool.calls[0]
    assert "SET deleted = TRUE" in query
    assert "last_synced_at = now()" in query
    assert args == (555,)


async def test_mark_deleted_reports_false_when_nothing_matched(fake_pool):
    fake_pool.execute_return = "UPDATE 0"
    assert await premium.mark_deleted(fake_pool, 999) is False


async def test_load_active_selects_only_non_deleted_rows(fake_pool):
    fake_pool.fetch_return = []
    await premium.load_active(fake_pool)

    _method, query, _args = fake_pool.calls[0]
    assert "FROM premium_entitlements" in query
    assert "WHERE deleted = FALSE" in query
