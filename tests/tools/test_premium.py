"""Unit tests for :mod:`tools.premium` (M3a: catalog, projection, resolver).

No network, DB, Discord, or Lavalink is touched: the store helpers are
exercised against the repo's ``fake_pool`` fixture and the cache against
plain Python rows (dicts / SimpleNamespace), exactly like the entitlement-like
objects :func:`tools.premium._get` is written to accept.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import types

import pytest

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
# premium_skus sync (dashboard bridge)
# ---------------------------------------------------------------------------


async def test_sync_premium_skus_upserts_both_products_from_the_module_constants(
    fake_pool, monkeypatch
):
    """ONE source of truth: the two writes must carry exactly the module's
    OWN already-parsed constants, never a fresh config read."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)

    await premium.sync_premium_skus(fake_pool)

    assert len(fake_pool.calls) == 2
    (method_a, query_a, args_a), (method_b, query_b, args_b) = fake_pool.calls
    assert method_a == method_b == "execute"
    assert "INSERT INTO premium_skus" in query_a
    assert "ON CONFLICT (product) DO UPDATE" in query_a
    assert args_a == (premium.PRODUCT_YASUHO_PLUS, 111)
    assert args_b == (premium.PRODUCT_COMFORT_PACK, 222)


async def test_sync_premium_skus_writes_null_for_an_unconfigured_sku(
    fake_pool, monkeypatch
):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)

    await premium.sync_premium_skus(fake_pool)

    args = [call[2] for call in fake_pool.calls]
    assert (premium.PRODUCT_YASUHO_PLUS, None) in args
    assert (premium.PRODUCT_COMFORT_PACK, None) in args


async def test_sync_premium_skus_writes_exactly_the_two_catalog_products(
    fake_pool, monkeypatch
):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)

    await premium.sync_premium_skus(fake_pool)

    products_written = {call[2][0] for call in fake_pool.calls}
    assert products_written == set(premium.PRODUCTS)


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
    from cogs.config.tickets import storage as tickets_storage
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
    # The internal, never-advertised per-SERVER ticket cap (anti-abuse, not a
    # sales lever - see cogs/config/tickets/open.py). Mirrors
    # cogs/config/tickets/storage.MAX_OPEN_PER_GUILD, the module that
    # actually enforces it in the guarded INSERT.
    assert (
        premium.FREE_MAX_TICKETS_OPEN_PER_GUILD == tickets_storage.MAX_OPEN_PER_GUILD
    )
    assert premium.FREE_SERVERSTATS_RETENTION_DAYS == serverstats_cog.RETENTION_DAYS
    # M4d: the Yasuho+ ceiling (cog.MAX_RETENTION_DAYS, the daily purge's
    # hard "nothing survives past here" cutoff for every tier) must equal
    # the catalog's own Yasuho+ retention value - see rollups.
    # PREMIUM_MAX_WINDOW_DAYS for the read-layer's own copy of this tie.
    assert (
        serverstats_cog.MAX_RETENTION_DAYS
        == premium.GUILD_PREMIUM.serverstats_retention_days
    )
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
        max_tickets_open_per_guild=premium.FREE_MAX_TICKETS_OPEN_PER_GUILD,
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
        max_tickets_open_per_guild=1,
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
# L1 premium adjustments (2026-10-07): history raised to 500, new per-server
# ticket cap.
# ---------------------------------------------------------------------------


def test_guild_premium_history_cap_is_500_and_within_its_ceiling():
    """The Previous-history raise (L1a): 200 -> 500, pinned exactly at the
    ceiling (tests/cogs/test_music_history_cap.py exercises the resolution
    path; this is the catalog literal)."""
    assert premium.GUILD_PREMIUM.history_max_items == 500
    assert premium.GUILD_CEILINGS["history_max_items"] == 500


def test_guild_ticket_cap_per_guild_values():
    """L1b: the internal, never-advertised per-SERVER ticket cap - FREE 50,
    Yasuho+ 200, ceiling 500 (far above either, room to raise after
    measuring load like every other ceiling in this module)."""
    assert premium.GUILD_FREE.max_tickets_open_per_guild == 50
    assert premium.GUILD_PREMIUM.max_tickets_open_per_guild == 200
    assert premium.GUILD_CEILINGS["max_tickets_open_per_guild"] == 500


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
    )
    assert cache.for_guild(42, now=NOW) == premium.GUILD_PREMIUM
    assert cache.for_guild(43, now=NOW) == premium.GUILD_FREE
    assert cache.is_guild_premium(42, now=NOW) is True


def test_cache_resolves_premium_for_an_active_user_purchase(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=222, scope_type="user", guild_id=None, user_id=7)],
    )
    assert cache.for_user(7, now=NOW) == premium.USER_PREMIUM
    assert cache.for_user(8, now=NOW) == premium.USER_FREE
    assert cache.has_comfort_pack(7, now=NOW) is True


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
    )
    assert cache.for_guild(42, now=NOW) == premium.GUILD_FREE
    # A deleted row is dropped entirely, not merely inactive - it leaves no
    # trace in the map at all (the merge below would otherwise still hold it).
    assert cache._guild_skus == {}


def test_cache_ignores_a_different_sku_than_the_configured_one(monkeypatch):
    """An entitlement for some OTHER SKU (e.g. a future theme pack) must not
    grant Yasuho+ just because it is active and guild-scoped."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=999, scope_type="guild", guild_id=42, user_id=None)],
    )
    assert cache.for_guild(42, now=NOW) == premium.GUILD_FREE


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
    assert cache.for_guild(42, now=NOW) == premium.GUILD_PREMIUM
    assert "WHERE deleted = FALSE" in fake_pool.calls[0][1]


# ---------------------------------------------------------------------------
# Live expiry, no reload - the M3a+ regression this lot fixes
# ---------------------------------------------------------------------------
#
# The bug: an earlier version of EntitlementCache.load_rows/load_grant_rows
# called is_active()/is_grant_active() ONCE at load time and kept only a bare
# sku-id/product SET - no ends_at, no expires_at. is_guild_premium/
# has_comfort_pack then did nothing but a set-membership test, so an entry
# that was active at the moment of the last load() stayed "premium" forever,
# regardless of how far into the future `now` moved, until the next reload
# (boot, or M3b's periodic reconciliation). A 30-day gift outlived its own
# expiry for as long as the bot kept running. These tests load a row that IS
# active at load time and then move `now` PAST its end with no second load()
# call at all - the fix is this clock injection resolving FREE without any
# reload, not a different row.


def test_an_entitlement_active_at_load_time_expires_with_no_reload(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    ends_at = NOW + datetime.timedelta(days=30)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [
            _row(
                sku_id=111,
                scope_type="guild",
                guild_id=42,
                user_id=None,
                ends_at=ends_at,
                last_synced_at=NOW,
            )
        ],
    )
    # Still well inside the paid period: active.
    assert cache.is_guild_premium(42, now=NOW) is True
    assert cache.for_guild(42, now=NOW) == premium.GUILD_PREMIUM
    # Long past ends_at AND past the 48h grace, same cache object, no load()
    # call in between: must resolve FREE.
    later = ends_at + premium.GRACE + datetime.timedelta(days=1)
    assert cache.is_guild_premium(42, now=later) is False
    assert cache.for_guild(42, now=later) == premium.GUILD_FREE


def test_an_entitlement_still_grants_the_48h_grace_with_no_reload(monkeypatch):
    """The same live-clock re-evaluation must still honour the grace, not
    just the hard cutoff - a missed renewal webhook must not downgrade a
    subscriber the instant ends_at passes."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    ends_at = NOW + datetime.timedelta(days=30)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [
            _row(
                sku_id=111,
                scope_type="guild",
                guild_id=42,
                user_id=None,
                ends_at=ends_at,
                # last_synced_at predates ends_at: nothing has reconfirmed
                # the end, so the grace applies.
                last_synced_at=NOW,
            )
        ],
    )
    just_after_end = ends_at + datetime.timedelta(hours=1)
    assert cache.is_guild_premium(42, now=just_after_end) is True
    well_past_grace = ends_at + premium.GRACE + datetime.timedelta(minutes=1)
    assert cache.is_guild_premium(42, now=well_past_grace) is False


def test_a_user_purchase_active_at_load_time_expires_with_no_reload(monkeypatch):
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    ends_at = NOW + datetime.timedelta(days=7)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [
            _row(
                sku_id=222,
                scope_type="user",
                guild_id=None,
                user_id=7,
                ends_at=ends_at,
                last_synced_at=NOW,
            )
        ],
    )
    assert cache.has_comfort_pack(7, now=NOW) is True
    later = ends_at + premium.GRACE + datetime.timedelta(days=1)
    assert cache.has_comfort_pack(7, now=later) is False
    assert cache.for_user(7, now=later) == premium.USER_FREE


def test_a_grant_active_at_load_time_expires_with_no_reload(monkeypatch):
    """Same bug, same fix, for the owner-grant half of the merge: a grant's
    own expires_at must still end it with no further write or reload."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    expires_at = GRANT_NOW + datetime.timedelta(days=30)
    cache = premium.EntitlementCache()
    cache.load_grant_rows(
        [
            _grant(
                product="yasuho_plus",
                scope_type="guild",
                guild_id=111,
                expires_at=expires_at,
            )
        ],
    )
    assert cache.is_guild_premium(111, now=GRANT_NOW) is True
    # A grant has NO grace (is_grant_active's own rule) - one minute past
    # expiry is already inactive, with the same cache object, no reload.
    assert (
        cache.is_guild_premium(111, now=expires_at + datetime.timedelta(minutes=1))
        is False
    )


# --- Negative control (mandatory, this lot): revert to load-time evaluation ---
#
# Verified by hand during this review: tools/premium.py was copied aside,
# EntitlementCache.load_rows/load_grant_rows were edited back to their
# original shape (call is_active()/is_grant_active() once inside the loop and
# keep only a bare sku-id/product set, dropping ends_at/expires_at entirely),
# and is_guild_premium/has_comfort_pack were reverted to a plain set-
# membership test with no `now` parameter at all. Both
# test_an_entitlement_active_at_load_time_expires_with_no_reload and
# test_a_grant_active_at_load_time_expires_with_no_reload then failed
# (AssertionError on the post-expiry assertion: the reverted cache still
# answered True), which is exactly the historical bug this lot fixes - a
# stale verdict frozen at load time, immune to the clock. The file was
# restored immediately after by copying the original back (never git
# stash/checkout/reset, per this review's rules), and the full suite was
# re-run green before continuing.


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
    # Fix during review (P0): ended_at is set via COALESCE, so a second call
    # on an already-ended row (should not happen in practice, but harmless
    # if it does) cannot push the frozen end time forward.
    assert "ended_at = COALESCE(ended_at, now())" in query
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


# ---------------------------------------------------------------------------
# Bulk "premium-ish" lookup (M4d) - cogs/community/serverstats/cog.py's daily
# purge is the only caller today. Scope here: the WRAPPER (cutoff arithmetic,
# ONE query, int-set parsing) - the WHERE clause's own correctness
# (entitlement ends_at/deleted/last_synced_at, grant expires_at/revoked_at)
# is reasoned out in the query's own comment in tools/premium.py and mirrors
# is_active/is_grant_active's established rules; this repo's tests are
# mock-based throughout (FakePool answers a canned row set regardless of the
# WHERE clause, see conftest.py), so it cannot exercise live SQL semantics -
# consistent with every other query test in this suite.
# ---------------------------------------------------------------------------


async def test_premium_ish_guild_ids_issues_exactly_one_query(fake_pool):
    fake_pool.fetch_return = []
    result = await premium.premium_ish_guild_ids(fake_pool, within_days=365)

    assert len(fake_pool.calls) == 1
    method, query, _args = fake_pool.calls[0]
    assert method == "fetch"
    assert "premium_entitlements" in query
    assert "premium_grants" in query
    assert result == set()


async def test_premium_ish_guild_ids_computes_the_cutoff_from_within_days(fake_pool):
    fake_pool.fetch_return = []
    now = datetime.datetime(2026, 10, 6, tzinfo=UTC)

    await premium.premium_ish_guild_ids(fake_pool, within_days=365, now=now)

    _method, _query, args = fake_pool.calls[0]
    assert args == (now - datetime.timedelta(days=365), premium.YASUHO_PLUS_SKU)


async def test_premium_ish_guild_ids_defaults_now_to_the_real_clock(fake_pool):
    fake_pool.fetch_return = []
    before = datetime.datetime.now(UTC)

    await premium.premium_ish_guild_ids(fake_pool, within_days=90)

    after = datetime.datetime.now(UTC)
    _method, _query, args = fake_pool.calls[0]
    cutoff, _sku_id = args
    # cutoff = "now" - 90 days, "now" taken somewhere between the two reads
    # above (never a frozen/stale value from import time).
    assert before - datetime.timedelta(days=90) <= cutoff
    assert cutoff <= after - datetime.timedelta(days=90)


async def test_premium_ish_guild_ids_returns_a_set_of_ints(fake_pool):
    fake_pool.fetch_return = [
        {"guild_id": 111}, {"guild_id": "222"}, {"guild_id": 111},
    ]
    result = await premium.premium_ish_guild_ids(fake_pool, within_days=365)

    assert result == {111, 222}
    assert all(isinstance(guild_id, int) for guild_id in result)


async def test_premium_ish_guild_ids_empty_rows_is_an_empty_set(fake_pool):
    fake_pool.fetch_return = []
    assert await premium.premium_ish_guild_ids(fake_pool, within_days=365) == set()


# ---------------------------------------------------------------------------
# Fix during review (P0): the "ended" branch must read a FROZEN timestamp,
# never last_synced_at - a gateway reconnect can replay/duplicate a delivery
# (this module's own "EVENT ORDERING" docstring), and every write path used
# to re-stamp last_synced_at = now() on an ALREADY-deleted row regardless, so
# one replayed delete/update could resurrect a long-ended guild's retention
# window forever. Proven against a real throwaway PostgreSQL 11 during this
# review (fixtures: a guild refunded 400 days ago, re-touched by a duplicate
# event today, was wrongly kept premium-ish by the old `last_synced_at > $1`
# clause and correctly excluded by `ended_at > $1`) - not re-run here since
# this suite is mock-based throughout (see this section's header comment),
# but guarded at the level this suite CAN reach: the exact query text never
# regresses back to the broken column, and the cutoff/sku args stay correct.
# ---------------------------------------------------------------------------


def test_premium_ish_guild_ids_deleted_branch_reads_ended_at_not_last_synced_at():
    assert "deleted = TRUE AND ended_at > $1" in premium._PREMIUM_ISH_GUILD_IDS
    assert "last_synced_at" not in premium._PREMIUM_ISH_GUILD_IDS


async def test_premium_ish_guild_ids_filters_entitlements_to_the_yasuho_plus_sku(
    fake_pool, monkeypatch
):
    """Fix during review: scope_type = 'guild' alone is not a product filter
    for premium_entitlements (unlike premium_grants, which schema.sql's own
    CHECK ties to 'yasuho_plus') - this must bind YASUHO_PLUS_SKU as the
    query's own sku filter, the same thing guild_status/is_guild_premium
    already do."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    fake_pool.fetch_return = []

    await premium.premium_ish_guild_ids(fake_pool, within_days=365)

    _method, query, args = fake_pool.calls[0]
    assert "sku_id = $2" in query
    assert args[1] == 111


async def test_premium_ish_guild_ids_sku_filter_is_none_when_unconfigured(
    fake_pool, monkeypatch
):
    """No Yasuho+ SKU configured yet (dev/test default) must bind NULL, not
    0 or some other sentinel that could accidentally match a real row -
    ``sku_id = NULL`` is never true in SQL, so this correctly contributes no
    guild through the entitlements half of the query."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    fake_pool.fetch_return = []

    await premium.premium_ish_guild_ids(fake_pool, within_days=365)

    _method, _query, args = fake_pool.calls[0]
    assert args[1] is None


# NOTE: a negative control against this function's own WHERE clause would
# need a real PostgreSQL (every test above only reaches the wrapper - see
# this section's header comment); the two negative controls for the M4d
# purge this function serves are instead exercised at the call site, against
# observable PYTHON behaviour - see tests/cogs/test_serverstats.py's
# "NEGATIVE CONTROL" comments (the entitled-keep, and the unconditional
# 365-day delete).


# ---------------------------------------------------------------------------
# Owner grants - product/scope coherence
# ---------------------------------------------------------------------------


def test_validate_grant_scope_accepts_the_matching_pair():
    premium.validate_grant_scope("yasuho_plus", "guild")
    premium.validate_grant_scope("comfort_pack", "user")


def test_validate_grant_scope_rejects_the_crossed_pair():
    with pytest.raises(ValueError):
        premium.validate_grant_scope("yasuho_plus", "user")
    with pytest.raises(ValueError):
        premium.validate_grant_scope("comfort_pack", "guild")


def test_validate_grant_scope_rejects_an_unknown_product():
    with pytest.raises(ValueError):
        premium.validate_grant_scope("theme_pack", "guild")


async def test_create_grant_refuses_a_crossed_pair_before_touching_the_pool(
    fake_pool,
):
    with pytest.raises(ValueError):
        await premium.create_grant(
            fake_pool,
            product="yasuho_plus",
            scope_type="user",
            user_id=1,
            granted_by=1,
        )
    assert fake_pool.calls == []  # the pool was never touched


async def test_create_grant_refuses_a_guild_grant_missing_its_guild_id(fake_pool):
    with pytest.raises(ValueError):
        await premium.create_grant(
            fake_pool, product="yasuho_plus", scope_type="guild", granted_by=1
        )


async def test_create_grant_inserts_with_returning_id(fake_pool):
    fake_pool.fetchrow_return = {"id": 42}
    grant_id = await premium.create_grant(
        fake_pool,
        product="yasuho_plus",
        scope_type="guild",
        guild_id=111,
        reason="friend's server",
        granted_by=1,
        expires_at=None,
    )
    assert grant_id == 42
    _method, query, args = fake_pool.calls[0]
    assert "INSERT INTO premium_grants" in query
    assert "RETURNING id" in query
    assert args == ("yasuho_plus", "guild", 111, None, "friend's server", 1, None)


# ---------------------------------------------------------------------------
# Owner grants - is_grant_active (no grace, unlike is_active)
# ---------------------------------------------------------------------------

GRANT_NOW = datetime.datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)


def _grant(**overrides):
    row = {"revoked_at": None, "expires_at": None}
    row.update(overrides)
    return row


def test_grant_with_no_expiry_or_revocation_is_active():
    assert premium.is_grant_active(_grant(), now=GRANT_NOW) is True


def test_revoked_grant_is_never_active_even_before_its_expiry():
    row = _grant(
        revoked_at=GRANT_NOW - datetime.timedelta(minutes=1),
        expires_at=GRANT_NOW + datetime.timedelta(days=30),
    )
    assert premium.is_grant_active(row, now=GRANT_NOW) is False


def test_future_expiry_is_active():
    row = _grant(expires_at=GRANT_NOW + datetime.timedelta(days=1))
    assert premium.is_grant_active(row, now=GRANT_NOW) is True


def test_past_expiry_is_inactive_with_no_grace():
    """Unlike is_active's 48h grace for a missed Discord webhook, a grant has
    none: it is ours to end, so a minute past expiry is already inactive."""
    row = _grant(expires_at=GRANT_NOW - datetime.timedelta(minutes=1))
    assert premium.is_grant_active(row, now=GRANT_NOW) is False


# --- Negative control: the revoked_at check must gate is_grant_active ------
#
# Verified by hand: editing is_grant_active() to drop the
# ``revoked_at is not None`` early return (checking only the expiry) turns
# test_revoked_grant_is_never_active_even_before_its_expiry red, because a
# revoked-but-not-yet-expired grant then reads as active. Restored
# immediately after with the file copied aside and back.


# ---------------------------------------------------------------------------
# EntitlementCache - the merge: entitlement OR grant, either is enough
# ---------------------------------------------------------------------------


def test_grant_alone_with_no_sku_configured_grants_premium(monkeypatch):
    """THE M3a+ headline requirement: the owner can gift before any SKU
    exists at all."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    cache = premium.EntitlementCache()
    cache.load_grant_rows(
        [_grant(product="yasuho_plus", scope_type="guild", guild_id=111)],
    )
    assert cache.is_guild_premium(111, now=GRANT_NOW) is True
    assert cache.for_guild(111, now=GRANT_NOW) == premium.GUILD_PREMIUM


def test_entitlement_alone_still_grants_premium_with_no_grant_on_record(
    monkeypatch,
):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=111, scope_type="guild", guild_id=42, user_id=None)],
    )
    assert cache.is_guild_premium(42, now=NOW) is True


def test_both_entitlement_and_grant_still_resolve_premium_once(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=111, scope_type="guild", guild_id=42, user_id=None)],
    )
    cache.load_grant_rows(
        [_grant(product="yasuho_plus", scope_type="guild", guild_id=42)],
    )
    assert cache.is_guild_premium(42, now=NOW) is True


def test_neither_entitlement_nor_grant_resolves_free(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    cache = premium.EntitlementCache()
    assert cache.is_guild_premium(42, now=NOW) is False
    assert cache.for_guild(42, now=NOW) == premium.GUILD_FREE


def test_comfort_pack_grant_alone_with_no_sku_configured(monkeypatch):
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", None)
    cache = premium.EntitlementCache()
    cache.load_grant_rows(
        [_grant(product="comfort_pack", scope_type="user", user_id=7)],
    )
    assert cache.has_comfort_pack(7, now=GRANT_NOW) is True
    assert cache.for_user(7, now=GRANT_NOW) == premium.USER_PREMIUM


def test_a_revoked_grant_does_not_grant_premium(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)
    cache = premium.EntitlementCache()
    cache.load_grant_rows(
        [
            _grant(
                product="yasuho_plus",
                scope_type="guild",
                guild_id=111,
                revoked_at=GRANT_NOW - datetime.timedelta(days=1),
            )
        ],
    )
    assert cache.is_guild_premium(111, now=GRANT_NOW) is False
    # Revoked rows are dropped entirely at load time, same as a deleted
    # entitlement - no trace left in the map.
    assert cache._guild_grants == {}


async def test_cache_load_reads_both_entitlements_and_grants(fake_pool, monkeypatch):
    """EntitlementCache.load() populates all four maps from the two tables."""
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", None)

    class _TwoTablePool:
        def __init__(self):
            self.calls = []

        async def fetch(self, query, *args):
            self.calls.append(query)
            if "FROM premium_entitlements" in query:
                return []
            if "FROM premium_grants" in query:
                return [
                    {
                        "id": 1,
                        "product": "yasuho_plus",
                        "scope_type": "guild",
                        "guild_id": 111,
                        "user_id": None,
                        "revoked_at": None,
                        "expires_at": None,
                    }
                ]
            raise AssertionError(f"unexpected query: {query}")

    pool = _TwoTablePool()
    cache = premium.EntitlementCache()
    await cache.load(pool)

    assert cache.is_guild_premium(111) is True
    assert any("FROM premium_entitlements" in q for q in pool.calls)
    assert any("FROM premium_grants" in q for q in pool.calls)


# ---------------------------------------------------------------------------
# EntitlementCache.refresh_grant_scope - the single-scope re-read
# ---------------------------------------------------------------------------


async def test_refresh_grant_scope_populates_an_active_guild_grant(fake_pool):
    fake_pool.fetch_return = [{"id": 9, "product": "yasuho_plus", "expires_at": None}]
    cache = premium.EntitlementCache()

    await cache.refresh_grant_scope(fake_pool, "guild", guild_id=111)

    assert cache._guild_grants == {
        111: [premium._GrantSnapshot(grant_id=9, product="yasuho_plus", expires_at=None)]
    }
    _method, query, args = fake_pool.calls[0]
    assert "guild_id = " in query
    assert 111 in args


async def test_refresh_grant_scope_pops_the_scope_when_nothing_is_active(fake_pool):
    fake_pool.fetch_return = []
    cache = premium.EntitlementCache()
    cache._guild_grants[111] = [
        premium._GrantSnapshot(grant_id=9, product="yasuho_plus", expires_at=None)
    ]

    await cache.refresh_grant_scope(fake_pool, "guild", guild_id=111)

    assert cache._guild_grants == {}


async def test_refresh_grant_scope_populates_an_active_user_grant(fake_pool):
    fake_pool.fetch_return = [{"id": 3, "product": "comfort_pack", "expires_at": None}]
    cache = premium.EntitlementCache()

    await cache.refresh_grant_scope(fake_pool, "user", user_id=7)

    assert cache._user_grants == {
        7: [premium._GrantSnapshot(grant_id=3, product="comfort_pack", expires_at=None)]
    }


async def test_refresh_grant_scope_carries_the_expiry_so_it_still_ends_itself(
    fake_pool,
):
    """refresh_grant_scope's snapshot is not exempt from the live-expiry fix:
    a grant refreshed into the cache with a future expires_at must still
    turn itself off once `now` passes it, with no further write."""
    expires_at = GRANT_NOW + datetime.timedelta(days=1)
    fake_pool.fetch_return = [
        {"id": 9, "product": "yasuho_plus", "expires_at": expires_at}
    ]
    cache = premium.EntitlementCache()
    await cache.refresh_grant_scope(fake_pool, "guild", guild_id=111)

    assert cache.is_guild_premium(111, now=GRANT_NOW) is True
    assert (
        cache.is_guild_premium(111, now=expires_at + datetime.timedelta(minutes=1))
        is False
    )


async def test_refresh_grant_scope_rejects_an_unknown_scope_type():
    cache = premium.EntitlementCache()
    with pytest.raises(ValueError):
        await cache.refresh_grant_scope(object(), "guild_or_user", guild_id=1)


# ---------------------------------------------------------------------------
# M3b: upsert_entitlement_event / reconcile / refresh_entitlement_scope
#
# A plain FakePool (query-string + args recording only) cannot prove an
# ORDERING claim - "a late create cannot undo a delete" is a statement about
# what ends up STORED after a SEQUENCE of calls, not about any one query's
# text. _FakeEntitlementTable below is a small, faithful in-memory
# simulation of the premium_entitlements table that actually APPLIES the
# same SQL semantics upsert_entitlement/upsert_entitlement_event/
# mark_deleted/the loaders rely on (including the ON CONFLICT ... OR
# EXCLUDED.deleted clause), so a test can assert on resulting STATE across a
# sequence of events - exactly the thing being claimed. It recognises only
# the handful of queries tools.premium actually issues; anything else raises
# instead of silently returning nothing (the "every result needs a positive
# control, every silence needs to be impossible by construction" rule).
# ---------------------------------------------------------------------------


class _FakeEntitlementTable:
    def __init__(self):
        self.rows = {}  # entitlement_id -> dict of columns
        self.calls = []
        # A monotonically advancing stand-in for the query's own now() - a
        # FIXED NOW (the original shape) cannot distinguish "this write
        # happened AFTER that one", which is exactly what the ended_at
        # freeze fix (fix during review, P0) needs a test to observe: that a
        # SECOND, later write to an already-deleted row does not move
        # ended_at forward even though last_synced_at does.
        self._tick = 0

    def _now(self):
        self._tick += 1
        return NOW + datetime.timedelta(seconds=self._tick)

    @staticmethod
    def _columns(args):
        (
            entitlement_id,
            sku_id,
            scope_type,
            guild_id,
            user_id,
            entitlement_type,
            deleted,
            consumed,
            starts_at,
            ends_at,
        ) = args
        return {
            "entitlement_id": entitlement_id,
            "sku_id": sku_id,
            "scope_type": scope_type,
            "guild_id": guild_id,
            "user_id": user_id,
            "entitlement_type": entitlement_type,
            "deleted": deleted,
            "consumed": consumed,
            "starts_at": starts_at,
            "ends_at": ends_at,
        }

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        now = self._now()
        if "ON CONFLICT (entitlement_id) DO UPDATE" in query:
            not_before = None
            if len(args) == 11:
                args, not_before = args[:10], args[10]
            row = self._columns(args)
            row["last_synced_at"] = now
            existing = self.rows.get(row["entitlement_id"])
            if "premium_entitlements.deleted OR EXCLUDED.deleted" in query:
                # upsert_entitlement_event's OR-preserve path.
                if existing is not None:
                    row["deleted"] = existing["deleted"] or row["deleted"]
                # ended_at mirrors _UPSERT_ENTITLEMENT_EVENT's own CASE:
                # frozen once the row is ALREADY deleted, stamped fresh the
                # moment it first transitions, NULL otherwise.
                if existing is not None and existing["deleted"]:
                    row["ended_at"] = existing.get("ended_at")
                elif row["deleted"]:
                    row["ended_at"] = now
                else:
                    row["ended_at"] = None
            else:
                # upsert_entitlement's plain/guarded (reconciliation) path.
                guarded = (
                    not_before is not None
                    and existing is not None
                    and existing["last_synced_at"] > not_before
                )
                if guarded:
                    row["deleted"] = existing["deleted"]
                    row["ended_at"] = existing.get("ended_at")
                elif not row["deleted"]:
                    row["ended_at"] = None
                elif existing is not None and existing["deleted"]:
                    row["ended_at"] = existing.get("ended_at")
                else:
                    row["ended_at"] = now
            self.rows[row["entitlement_id"]] = row
            return "INSERT 0 1"
        if "SET deleted = TRUE" in query:
            entitlement_id = args[0]
            if entitlement_id not in self.rows:
                return "UPDATE 0"
            self.rows[entitlement_id]["deleted"] = True
            self.rows[entitlement_id]["last_synced_at"] = now
            # mark_deleted's COALESCE(ended_at, now()): frozen if already set.
            self.rows[entitlement_id]["ended_at"] = (
                self.rows[entitlement_id].get("ended_at") or now
            )
            return "UPDATE 1"
        raise AssertionError(f"unexpected query: {query}")

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        # M5: load_active_entitlement_rows - full columns, checked FIRST so
        # its own "sku_id = ANY(...)" variant is not swallowed by the older,
        # ids-only branch of the same substring below.
        if "entitlement_id, sku_id, scope_type, guild_id, user_id, entitlement_type, ends_at" in query:
            if "sku_id = ANY($1::bigint[])" in query:
                (sku_ids,) = args
                sku_ids = {int(sku_id) for sku_id in sku_ids}
                return [
                    dict(row)
                    for row in self.rows.values()
                    if not row["deleted"] and row["sku_id"] in sku_ids
                ]
            return [dict(row) for row in self.rows.values() if not row["deleted"]]
        if "sku_id = ANY($1::bigint[])" in query:
            (sku_ids,) = args
            sku_ids = {int(sku_id) for sku_id in sku_ids}
            return [
                {"entitlement_id": rid}
                for rid, row in self.rows.items()
                if not row["deleted"] and row["sku_id"] in sku_ids
            ]
        # M5: load_all_entitlement_ids - EVERY id ever recorded, deleted or
        # not. Checked before the (longer, WHERE-qualified) ids-only branch
        # below so the two never collide: this one is the bare query with no
        # WHERE clause at all.
        if query == "SELECT entitlement_id FROM premium_entitlements":
            return [{"entitlement_id": rid} for rid in self.rows]
        if "SELECT entitlement_id FROM premium_entitlements WHERE deleted = FALSE" in query:
            return [
                {"entitlement_id": rid}
                for rid, row in self.rows.items()
                if not row["deleted"]
            ]
        if "WHERE guild_id = $1 AND deleted = FALSE" in query:
            (guild_id,) = args
            return [
                row
                for row in self.rows.values()
                if row["guild_id"] == guild_id and not row["deleted"]
            ]
        if "WHERE user_id = $1 AND deleted = FALSE" in query:
            (user_id,) = args
            return [
                row
                for row in self.rows.values()
                if row["user_id"] == user_id and not row["deleted"]
            ]
        if "FROM premium_entitlements WHERE deleted = FALSE" in query:
            return list(self.rows.values())
        raise AssertionError(f"unexpected query: {query}")


async def _stream(items, *, fail_after=None):
    """An async generator standing in for ``bot.entitlements(...)``: yields
    ``items`` in order, raising partway through when ``fail_after`` is set -
    simulating a Discord outage mid-listing."""
    for index, item in enumerate(items):
        if fail_after is not None and index == fail_after:
            raise RuntimeError("discord outage mid-listing")
        yield item


# ---------------------------------------------------------------------------
# upsert_entitlement_event: the ORDER-SAFE write path
# ---------------------------------------------------------------------------


async def test_event_upsert_sql_ors_deleted_against_the_stored_value():
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement_event(table, _entitlement())

    _method, query, args = table.calls[0]
    assert "INSERT INTO premium_entitlements" in query
    assert "premium_entitlements.deleted OR EXCLUDED.deleted" in query
    assert "last_synced_at = now()" in query
    assert args == (555, 111, "guild", 42, None, 2, False, False, NOW, NOW + datetime.timedelta(days=30))


async def test_event_upsert_duplicate_event_is_idempotent():
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement_event(table, _entitlement())
    await premium.upsert_entitlement_event(table, _entitlement())

    assert len(table.rows) == 1
    assert table.rows[555]["deleted"] is False


async def test_event_upsert_update_before_its_create_still_converges():
    """An UPDATE for an entitlement_id with no row yet (reordered delivery)
    inserts it; the CREATE that logically came first then lands on top -
    final state matches the fields either carried (both non-deleted), same
    as if they had arrived in the "right" order."""
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement_event(
        table, _entitlement(ends_at=NOW + datetime.timedelta(days=60))
    )  # "update" arrives first
    await premium.upsert_entitlement_event(
        table, _entitlement(ends_at=NOW + datetime.timedelta(days=30))
    )  # "create" arrives second

    assert table.rows[555]["deleted"] is False
    assert table.rows[555]["ends_at"] == NOW + datetime.timedelta(days=30)


async def test_event_upsert_delete_before_its_create_stays_deleted():
    """THE ordering guarantee: on_entitlement_delete (force_deleted=True)
    arriving BEFORE the entitlement's own create, followed by that late,
    stale create (deleted=False) - must converge to deleted, not revert."""
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement_event(
        table, _entitlement(), force_deleted=True
    )  # delete arrives first; no row existed yet
    assert table.rows[555]["deleted"] is True  # inserted already-deleted

    await premium.upsert_entitlement_event(
        table, _entitlement(deleted=False)
    )  # the late, stale create

    assert table.rows[555]["deleted"] is True  # NOT undone


async def test_event_upsert_duplicate_delete_is_a_no_op():
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement_event(table, _entitlement(), force_deleted=True)
    await premium.upsert_entitlement_event(table, _entitlement(), force_deleted=True)

    assert table.rows[555]["deleted"] is True


async def test_event_upsert_duplicate_delete_does_not_move_ended_at():
    """Fix during review (P0): a gateway reconnect can replay a delivery
    (this module's "EVENT ORDERING" docstring), so on_entitlement_delete can
    fire twice for the same id. Before the fix, EVERY write re-stamped
    last_synced_at = now() even on an already-deleted row, and
    tools.premium.premium_ish_guild_ids read exactly that column as "when
    this ended" - so a replayed delete kept resurrecting a long-ended
    guild's 365-day retention window forever. ended_at must freeze on the
    FIRST delete and stay there, no matter how many more duplicates land."""
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement_event(table, _entitlement(), force_deleted=True)
    first_ended_at = table.rows[555]["ended_at"]
    first_last_synced_at = table.rows[555]["last_synced_at"]
    assert first_ended_at is not None

    await premium.upsert_entitlement_event(table, _entitlement(), force_deleted=True)

    assert table.rows[555]["deleted"] is True
    # last_synced_at legitimately advances (we DID just re-confirm with
    # Discord)...
    assert table.rows[555]["last_synced_at"] > first_last_synced_at
    # ...but ended_at - the one tools.premium.premium_ish_guild_ids reads -
    # must not: this is the exact bug the fix closes.
    assert table.rows[555]["ended_at"] == first_ended_at


async def test_event_upsert_refund_reported_as_update_stays_deleted():
    """Plan rule: 'a refund follows the expiry path' - Discord reports it as
    an entitlement UPDATE with deleted=True, not a dedicated event. A later,
    stale create/update for the same id must not revive it."""
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement_event(table, _entitlement(deleted=False))
    assert table.rows[555]["deleted"] is False

    await premium.upsert_entitlement_event(table, _entitlement(deleted=True))  # the refund
    assert table.rows[555]["deleted"] is True

    await premium.upsert_entitlement_event(table, _entitlement(deleted=False))  # late, stale
    assert table.rows[555]["deleted"] is True


async def test_negative_control_b_the_clobbering_upsert_would_undo_a_delete():
    """NEGATIVE CONTROL for the two tests above: if the event path called
    :func:`premium.upsert_entitlement` (the reconciliation/clobbering
    variant) instead of :func:`premium.upsert_entitlement_event`, the exact
    same "delete, then a late stale create" sequence WOULD incorrectly
    revive the entitlement - proving this fixture actually detects the bug
    the ordering tests above guard against, and that the fix is the OR in
    upsert_entitlement_event's SQL, not something the fake table assumes for
    free."""
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement(table, _entitlement(deleted=True))
    assert table.rows[555]["deleted"] is True

    await premium.upsert_entitlement(table, _entitlement(deleted=False))  # late, stale
    assert table.rows[555]["deleted"] is False  # undone - the bug this fixture catches


# ---------------------------------------------------------------------------
# upsert_entitlement's not_before guard: reconciliation racing a live event
# ---------------------------------------------------------------------------


def _seed_row(table, *, deleted, last_synced_at):
    """Plant a row directly (bypassing a write) so its ``last_synced_at`` can
    be set to an arbitrary value - simulating "a gateway event already
    committed a write for this id, stamped at this moment," independent of
    whatever the fake's own ``now()`` stand-in would otherwise pick."""
    table.rows[1] = {
        "entitlement_id": 1,
        "sku_id": 111,
        "scope_type": "guild",
        "guild_id": 42,
        "user_id": None,
        "entitlement_type": 2,
        "deleted": deleted,
        "consumed": False,
        "starts_at": NOW,
        "ends_at": NOW + datetime.timedelta(days=30),
        "last_synced_at": last_synced_at,
    }


async def test_upsert_entitlement_guarded_preserves_a_newer_events_delete():
    """THE RACE this guard closes: a reconciliation pass's own page for this
    row says "alive" (fetched before a concurrent refund committed), but by
    the time this write runs, a gateway event has ALREADY stamped
    last_synced_at newer than the pass's own start time with deleted=True.
    The guarded write must not clobber that back to False."""
    table = _FakeEntitlementTable()
    pass_started_at = NOW - datetime.timedelta(seconds=5)
    _seed_row(table, deleted=True, last_synced_at=NOW)  # NOW > pass_started_at

    await premium.upsert_entitlement(
        table, _remote(id=1, deleted=False), not_before=pass_started_at
    )

    assert table.rows[1]["deleted"] is True  # preserved, not resurrected


async def test_negative_control_unguarded_upsert_would_resurrect_a_newer_delete():
    """NEGATIVE CONTROL for the test above: the exact same race, through the
    pre-fix call shape (no ``not_before``) - proves the scenario really was
    a bug, not an artefact of the fixture."""
    table = _FakeEntitlementTable()
    _seed_row(table, deleted=True, last_synced_at=NOW)

    await premium.upsert_entitlement(table, _remote(id=1, deleted=False))

    assert table.rows[1]["deleted"] is False  # resurrected - the bug this guard fixes


async def test_upsert_entitlement_guarded_still_clears_a_stale_deleted_row():
    """Regression check: the guard must not block the LEGITIMATE case the
    module docstring calls out - a row genuinely marked deleted by a PAST
    pass (last_synced_at predates this pass's own start, i.e. nothing raced
    it) that a fresh complete listing now reports alive again. That must
    still clear back to False exactly as before this guard existed."""
    table = _FakeEntitlementTable()
    pass_started_at = NOW
    stale_sync = NOW - datetime.timedelta(hours=6)  # older than the pass start
    _seed_row(table, deleted=True, last_synced_at=stale_sync)

    await premium.upsert_entitlement(
        table, _remote(id=1, deleted=False), not_before=pass_started_at
    )

    assert table.rows[1]["deleted"] is False  # revived, as a complete listing should


async def test_reconcile_threads_one_consistent_not_before_through_every_upsert():
    """``reconcile`` must pass the SAME pass-start timestamp to every row it
    upserts during one pass (not, say, a fresh ``now()`` per row, which
    would narrow the guard's protection to nothing for every row but the
    first)."""
    table = _FakeEntitlementTable()
    captured = []
    real_upsert = premium.upsert_entitlement

    async def _spy(pool, entitlement, *, not_before=None):
        captured.append(not_before)
        return await real_upsert(pool, entitlement, not_before=not_before)

    import tools.premium as premium_module

    original = premium_module.upsert_entitlement
    premium_module.upsert_entitlement = _spy
    try:
        items = [_remote(id=1), _remote(id=2, guild_id=43)]
        await premium.reconcile(table, _stream(items), application_id=APP_ID)
    finally:
        premium_module.upsert_entitlement = original

    assert len(captured) == 2
    assert captured[0] is not None
    assert captured[0] == captured[1]  # one timestamp for the whole pass


# ---------------------------------------------------------------------------
# load_active_for_guild / load_active_for_user / load_active_entitlement_ids
# ---------------------------------------------------------------------------


async def test_load_active_for_guild_filters_by_guild_and_excludes_deleted(fake_pool):
    fake_pool.fetch_return = []
    await premium.load_active_for_guild(fake_pool, 111)

    _method, query, args = fake_pool.calls[0]
    assert "guild_id = $1" in query
    assert "deleted = FALSE" in query
    assert args == (111,)


async def test_load_active_for_user_filters_by_user_and_excludes_deleted(fake_pool):
    fake_pool.fetch_return = []
    await premium.load_active_for_user(fake_pool, 7)

    _method, query, args = fake_pool.calls[0]
    assert "user_id = $1" in query
    assert "deleted = FALSE" in query
    assert args == (7,)


async def test_load_active_entitlement_ids_returns_a_set_of_ints(fake_pool):
    fake_pool.fetch_return = [{"entitlement_id": 1}, {"entitlement_id": 2}]
    ids = await premium.load_active_entitlement_ids(fake_pool)
    assert ids == {1, 2}


# ---------------------------------------------------------------------------
# reconcile: the fail-safe full resync
# ---------------------------------------------------------------------------

APP_ID = 999


def _remote(**overrides):
    row = dict(
        id=1,
        sku_id=111,
        guild_id=42,
        user_id=None,
        type=2,
        deleted=False,
        consumed=False,
        starts_at=NOW,
        ends_at=NOW + datetime.timedelta(days=30),
        application_id=APP_ID,
    )
    row.update(overrides)
    return types.SimpleNamespace(**row)


async def test_reconcile_upserts_every_row_the_listing_returns():
    table = _FakeEntitlementTable()
    items = [_remote(id=1), _remote(id=2, guild_id=43)]

    result = await premium.reconcile(table, _stream(items), application_id=APP_ID)

    assert result["seen"] == 2
    assert result["upserted"] == 2
    assert result["missing"] == 0
    # M5: both ids are brand new - never recorded before this pass.
    assert {row["entitlement_id"] for row in result["new_rows"]} == {1, 2}
    assert result["ended_rows"] == []
    assert set(table.rows) == {1, 2}


async def test_reconcile_marks_a_row_missing_from_a_complete_listing_as_deleted():
    table = _FakeEntitlementTable()
    # Pre-existing state: two active rows, as if a previous pass had seen them.
    await premium.upsert_entitlement(table, _remote(id=1))
    await premium.upsert_entitlement(table, _remote(id=2, guild_id=43))

    # This COMPLETE listing only reports id=1 - id=2 is gone from Discord.
    result = await premium.reconcile(table, _stream([_remote(id=1)]), application_id=APP_ID)

    assert result["seen"] == 1
    assert result["upserted"] == 1
    assert result["missing"] == 1
    # Both ids pre-existed (seeded above, before this pass) - id=1 is not
    # new, id=2 is the "ended" transition (missing from a complete listing).
    assert result["new_rows"] == []
    assert {row["entitlement_id"] for row in result["ended_rows"]} == {2}
    assert table.rows[1]["deleted"] is False
    assert table.rows[2]["deleted"] is True


async def test_reconcile_ignores_rows_of_a_different_application():
    table = _FakeEntitlementTable()
    items = [_remote(id=1, application_id=APP_ID), _remote(id=2, application_id=APP_ID + 1)]

    result = await premium.reconcile(table, _stream(items), application_id=APP_ID)

    assert result["seen"] == 1
    assert result["upserted"] == 1
    assert result["missing"] == 0
    assert {row["entitlement_id"] for row in result["new_rows"]} == {1}
    assert result["ended_rows"] == []
    assert set(table.rows) == {1}  # the foreign row was never written


async def test_reconcile_handles_a_large_multi_page_stream_without_truncation():
    """Stands in for REST pagination (discord.py's own async iterator pages
    internally): a stream far bigger than one page's worth of rows must be
    consumed to its end with no row lost or double counted."""
    table = _FakeEntitlementTable()
    items = [_remote(id=i, guild_id=1000 + i) for i in range(1, 251)]  # 250 "rows"

    result = await premium.reconcile(table, _stream(items), application_id=APP_ID)

    assert result["seen"] == 250
    assert result["upserted"] == 250
    assert result["missing"] == 0
    assert len(result["new_rows"]) == 250  # the storm-collapse is a cog-level decision
    assert result["ended_rows"] == []
    assert len(table.rows) == 250


async def test_reconcile_a_failed_listing_marks_nothing_deleted():
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement(table, _remote(id=1))
    await premium.upsert_entitlement(table, _remote(id=2, guild_id=43))

    items = [_remote(id=3, guild_id=44), _remote(id=4, guild_id=45)]
    result = await premium.reconcile(
        table, _stream(items, fail_after=1), application_id=APP_ID
    )

    assert result is None
    # Nothing marked deleted - not the pre-existing rows the failed listing
    # never got to re-confirm, and not even id=3 which WAS upserted during
    # the partial pass before the failure (an upsert only ever makes a row
    # more current, never less, so it is kept; only the DESTRUCTIVE
    # "mark missing" step is skipped).
    assert table.rows[1]["deleted"] is False
    assert table.rows[2]["deleted"] is False
    assert table.rows[3]["deleted"] is False
    assert 4 not in table.rows  # never reached


async def test_reconcile_with_sku_filter_leaves_a_different_skus_row_untouched():
    """A SKU id change (or any listing narrowed to a SKU subset, which
    production always passes - see tools.premium.reconcile's own "sku_ids"
    docstring paragraph): a row for a sku OUTSIDE that filter must be left
    exactly as it stood - neither upserted (it was never listed) nor marked
    deleted (it was never "missing" from a listing that never claimed to
    cover it) - even though a complete-for-THAT-SKU listing comes back
    reporting nothing for it at all."""
    table = _FakeEntitlementTable()
    # id=1 is the OLD sku (111); id=2 is the CURRENT one (222) and the
    # listing below reports it as still alive.
    await premium.upsert_entitlement(table, _remote(id=1, sku_id=111))
    await premium.upsert_entitlement(table, _remote(id=2, sku_id=222, guild_id=43))

    result = await premium.reconcile(
        table,
        _stream([_remote(id=2, sku_id=222, guild_id=43)]),
        application_id=APP_ID,
        sku_ids=[222],
    )

    assert result["seen"] == 1
    assert result["upserted"] == 1
    assert result["missing"] == 0
    # id=2 pre-existed - not new; id=1's OTHER sku is outside the filter so
    # it is neither upserted nor counted as "ended".
    assert result["new_rows"] == []
    assert result["ended_rows"] == []
    assert table.rows[1]["deleted"] is False  # untouched - outside the filter
    assert table.rows[2]["deleted"] is False


async def test_negative_control_reconcile_without_sku_filter_marks_the_other_skus_row_deleted():
    """NEGATIVE CONTROL for the test above: the exact same listing, WITHOUT
    ``sku_ids``, proves the scenario really would have been a bug - id=1's
    row (a sku the listing never mentions) is read as "a complete listing
    no longer reports this" and wrongly marked deleted, demonstrating the
    fix above is what prevents it, not an artefact of the fixture."""
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement(table, _remote(id=1, sku_id=111))
    await premium.upsert_entitlement(table, _remote(id=2, sku_id=222, guild_id=43))

    result = await premium.reconcile(
        table,
        _stream([_remote(id=2, sku_id=222, guild_id=43)]),
        application_id=APP_ID,
    )

    assert result["seen"] == 1
    assert result["upserted"] == 1
    assert result["missing"] == 1
    assert result["new_rows"] == []  # id=2 pre-existed
    assert {row["entitlement_id"] for row in result["ended_rows"]} == {1}
    assert table.rows[1]["deleted"] is True  # the bug this fix prevents


async def test_load_active_entitlement_ids_filters_by_sku_ids(fake_pool):
    fake_pool.fetch_return = [{"entitlement_id": 1}]
    ids = await premium.load_active_entitlement_ids(fake_pool, sku_ids=[111, 222])

    _method, query, args = fake_pool.calls[0]
    assert "deleted = FALSE" in query
    assert "sku_id = ANY($1::bigint[])" in query
    assert args == ([111, 222],)
    assert ids == {1}


async def test_negative_control_a_marking_missing_without_the_guard_would_downgrade_everyone():
    """NEGATIVE CONTROL for the outage test above: if reconcile ran its
    "mark every stored id the listing did not see as deleted" step WITHOUT
    first checking the listing completed, a Discord outage (an EMPTY/failed
    partial listing) would mark every pre-existing row deleted - exactly
    the "a Discord outage must never read as everyone's subscription ended"
    failure this fixture must be able to catch. This reproduces that
    unguarded step directly (not reconcile() - the real function is
    asserted NOT to do this, above) to prove the fixture is sensitive to it."""
    table = _FakeEntitlementTable()
    await premium.upsert_entitlement(table, _remote(id=1))
    await premium.upsert_entitlement(table, _remote(id=2, guild_id=43))

    # The unguarded shape: a listing that failed, but "mark missing" runs
    # anyway using whatever was seen before the failure (nothing).
    seen_ids = set()
    stored_ids = await premium.load_active_entitlement_ids(table)
    for entitlement_id in stored_ids - seen_ids:
        await premium.mark_deleted(table, entitlement_id)

    assert table.rows[1]["deleted"] is True  # wrongly downgraded
    assert table.rows[2]["deleted"] is True  # wrongly downgraded


# ---------------------------------------------------------------------------
# EntitlementCache.refresh_entitlement_scope
# ---------------------------------------------------------------------------


async def test_refresh_entitlement_scope_populates_an_active_guild_entitlement(fake_pool):
    fake_pool.fetch_return = [
        {"entitlement_id": 1, "sku_id": premium.YASUHO_PLUS_SKU or 111, "ends_at": None, "last_synced_at": NOW}
    ]
    cache = premium.EntitlementCache()

    await cache.refresh_entitlement_scope(fake_pool, "guild", guild_id=111)

    assert 111 in cache._guild_skus
    assert cache._guild_skus[111][0].entitlement_id == 1
    _method, query, args = fake_pool.calls[0]
    assert "guild_id = $1" in query
    assert args == (111,)


async def test_refresh_entitlement_scope_pops_the_scope_when_nothing_is_active(fake_pool):
    fake_pool.fetch_return = []
    cache = premium.EntitlementCache()
    cache._guild_skus[111] = [
        premium._EntitlementSnapshot(
            entitlement_id=1, sku_id=111, ends_at=None, last_synced_at=NOW
        )
    ]

    await cache.refresh_entitlement_scope(fake_pool, "guild", guild_id=111)

    assert cache._guild_skus == {}


async def test_refresh_entitlement_scope_populates_an_active_user_entitlement(fake_pool):
    fake_pool.fetch_return = [
        {"entitlement_id": 2, "sku_id": 222, "ends_at": None, "last_synced_at": NOW}
    ]
    cache = premium.EntitlementCache()

    await cache.refresh_entitlement_scope(fake_pool, "user", user_id=7)

    assert 7 in cache._user_skus
    assert cache._user_skus[7][0].sku_id == 222


async def test_refresh_entitlement_scope_rejects_an_unknown_scope_type():
    cache = premium.EntitlementCache()
    with pytest.raises(ValueError):
        await cache.refresh_entitlement_scope(object(), "guild_or_user", guild_id=1)


# ---------------------------------------------------------------------------
# EntitlementCache._lock: load()/refresh_entitlement_scope()/
# refresh_grant_scope() serialise against each other
# ---------------------------------------------------------------------------


async def test_load_holds_the_lock_across_its_fetch():
    """While load()'s fetch is in flight, the lock must be held - proving a
    concurrent refresh cannot interleave its own fetch-then-mutate between
    this fetch and this reload's rebind (see the class docstring)."""
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowPool:
        def __init__(self):
            self.calls = 0

        async def fetch(self, query, *args):
            self.calls += 1
            started.set()
            await release.wait()
            return []

    pool = _SlowPool()
    cache = premium.EntitlementCache()
    task = asyncio.ensure_future(cache.load(pool))

    await started.wait()
    assert cache._lock.locked() is True

    release.set()
    await task
    assert cache._lock.locked() is False


async def test_refresh_entitlement_scope_and_refresh_grant_scope_share_the_lock():
    """The two refresh methods take the SAME lock instance - a concurrent
    entitlement-scope refresh and grant-scope refresh on the same cache must
    not run their fetch-then-mutate halves interleaved either."""
    cache = premium.EntitlementCache()
    assert cache._lock is cache._lock  # sanity: one lock per cache instance

    gate = asyncio.Event()

    class _GatedPool:
        async def fetch(self, query, *args):
            await gate.wait()
            return []

    pool = _GatedPool()
    task = asyncio.ensure_future(
        cache.refresh_entitlement_scope(pool, "guild", guild_id=111)
    )
    await asyncio.sleep(0)
    assert cache._lock.locked() is True

    # A concurrent grant-scope refresh on an UNGATED pool must wait for the
    # lock rather than running its own fetch+mutate in between.
    fast_pool = types.SimpleNamespace()

    async def _fetch(query, *args):
        return []

    fast_pool.fetch = _fetch
    second = asyncio.ensure_future(
        cache.refresh_grant_scope(fast_pool, "guild", guild_id=222)
    )
    await asyncio.sleep(0)
    assert not second.done()  # blocked on the same lock

    gate.set()
    await task
    await second
    assert cache._lock.locked() is False


# ---------------------------------------------------------------------------
# resolve_guild_limits (M4a-3): the defensive resolver every hot-path caller
# (role-menu component callback, autoroom voice listener, ticket open button)
# shares, instead of each re-implementing the same getattr/try/except guard.
# ---------------------------------------------------------------------------


def test_resolve_guild_limits_with_no_premium_attribute_is_free():
    """A bot with no ``premium`` attribute (a test double, a script, or a cog
    running before setup_hook attaches one) must resolve FREE, never raise."""
    bot = types.SimpleNamespace()
    assert premium.resolve_guild_limits(bot, 42) is premium.GUILD_FREE


def test_resolve_guild_limits_with_none_guild_id_is_free():
    bot = types.SimpleNamespace(premium=premium.EntitlementCache())
    assert premium.resolve_guild_limits(bot, None) is premium.GUILD_FREE


def test_resolve_guild_limits_when_for_guild_raises_is_free(caplog):
    class _Boom:
        def for_guild(self, guild_id):
            raise RuntimeError("boom")

    bot = types.SimpleNamespace(premium=_Boom())
    with caplog.at_level(logging.ERROR, logger="tools.premium"):
        result = premium.resolve_guild_limits(bot, 42)
    assert result is premium.GUILD_FREE
    assert any("Failed to resolve" in r.message for r in caplog.records)


def test_resolve_guild_limits_resolves_premium_for_a_premium_guild(monkeypatch):
    monkeypatch.setattr(premium, "YASUHO_PLUS_SKU", 111)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=111, scope_type="guild", guild_id=42, user_id=None)],
    )
    bot = types.SimpleNamespace(premium=cache)
    assert premium.resolve_guild_limits(bot, 42) == premium.GUILD_PREMIUM


# --- Negative control: without the getattr guard, a missing attribute raises -
#
# Verified by hand during this lot: calling ``bot.premium.for_guild(guild_id)``
# directly (no ``getattr(bot, "premium", None)`` guard) against the plain
# ``types.SimpleNamespace()`` used in
# test_resolve_guild_limits_with_no_premium_attribute_is_free raises
# AttributeError instead of resolving FREE - proving the guard is load-bearing,
# not a decoration. Restored immediately after by editing the file back (no
# git stash/checkout/reset), and the full suite was re-run green.


# ---------------------------------------------------------------------------
# resolve_user_limits (M4c): the user-scoped twin of resolve_guild_limits -
# same guard, mirrored exactly - shared by the favourites add/list/play paths
# and the reminders creation/listing/dispatch paths.
# ---------------------------------------------------------------------------


def test_resolve_user_limits_with_no_premium_attribute_is_free():
    """A bot with no ``premium`` attribute (a test double, a script, or a cog
    running before setup_hook attaches one) must resolve FREE, never raise."""
    bot = types.SimpleNamespace()
    assert premium.resolve_user_limits(bot, 42) is premium.USER_FREE


def test_resolve_user_limits_with_none_user_id_is_free():
    bot = types.SimpleNamespace(premium=premium.EntitlementCache())
    assert premium.resolve_user_limits(bot, None) is premium.USER_FREE


def test_resolve_user_limits_when_for_user_raises_is_free(caplog):
    class _Boom:
        def for_user(self, user_id):
            raise RuntimeError("boom")

    bot = types.SimpleNamespace(premium=_Boom())
    with caplog.at_level(logging.ERROR, logger="tools.premium"):
        result = premium.resolve_user_limits(bot, 42)
    assert result is premium.USER_FREE
    assert any("Failed to resolve" in r.message for r in caplog.records)


def test_resolve_user_limits_resolves_premium_for_a_pack_confort_user(monkeypatch):
    monkeypatch.setattr(premium, "COMFORT_PACK_SKU", 222)
    cache = premium.EntitlementCache()
    cache.load_rows(
        [_row(sku_id=222, scope_type="user", guild_id=None, user_id=42)],
    )
    bot = types.SimpleNamespace(premium=cache)
    assert premium.resolve_user_limits(bot, 42) == premium.USER_PREMIUM


# ---------------------------------------------------------------------------
# M5: classify_entitlement_transition - the pure "new"/"ended"/None rule a
# before/after snapshot of ONE entitlement_id reduces to (see
# cogs/system/premium.py for the before/after reads that feed it, and that
# module's own docstring for the sourced verdict on why a renewal is never
# one of these three outcomes at all).
# ---------------------------------------------------------------------------


def test_classify_a_never_seen_active_row_is_new():
    assert (
        premium.classify_entitlement_transition(
            is_new=True, prev_deleted=False, now_deleted=False
        )
        == "new"
    )


def test_classify_a_never_seen_already_deleted_row_is_nothing():
    """The out-of-order DELETE-before-its-own-CREATE case: it was never
    actually granted, so there is nothing to announce."""
    assert (
        premium.classify_entitlement_transition(
            is_new=True, prev_deleted=False, now_deleted=True
        )
        is None
    )


def test_classify_an_existing_active_row_going_deleted_is_ended():
    assert (
        premium.classify_entitlement_transition(
            is_new=False, prev_deleted=False, now_deleted=True
        )
        == "ended"
    )


def test_classify_an_existing_row_already_deleted_staying_deleted_is_nothing():
    """The replayed-delete / reconciliation-re-reads-a-known-row case - the
    database transition already happened once; a second identical read
    must not announce it again."""
    assert (
        premium.classify_entitlement_transition(
            is_new=False, prev_deleted=True, now_deleted=True
        )
        is None
    )


def test_classify_a_plain_field_update_on_an_active_row_is_nothing():
    assert (
        premium.classify_entitlement_transition(
            is_new=False, prev_deleted=False, now_deleted=False
        )
        is None
    )


def test_classify_a_resurrection_is_nothing():
    """deleted True -> False only ever happens through reconcile's own
    "clobbering" upsert, never through this classifier's event path - and is
    not one of the two sale-relevant outcomes this lot DMs for either way."""
    assert (
        premium.classify_entitlement_transition(
            is_new=False, prev_deleted=True, now_deleted=False
        )
        is None
    )


# --- Negative control: without a correct is_new check, every event would
# read as "new" forever, breaking the "duplicate event sends only one DM"
# promise -----------------------------------------------------------------
#
# Actually run during this lot (not just asserted by comment): temporarily
# replacing classify_entitlement_transition's body with
# ``return None if now_deleted else "new"`` (dropping the ``if is_new:``
# branch entirely, i.e. "new" no matter what the prior state was) made
# tests/cogs/test_premium.py::test_the_same_create_event_processed_twice_sends_only_one_dm
# FAIL (``assert len(owner.sent) == 1`` saw 2) - proving that test actually
# depends on classify_entitlement_transition's is_new check, not on some
# incidental property of the fixture. The edit was then reverted by hand (no
# git stash/checkout/reset) and ``git diff`` confirmed tools/premium.py
# matched its pre-break state before the full suite was re-run green.


# --- Negative control: without the getattr guard, a missing attribute raises -
#
# Actually run during this lot (not just asserted by comment): temporarily
# replacing resolve_user_limits's body with a direct
# ``return bot.premium.for_user(user_id)`` (no ``getattr(bot, "premium", None)``
# guard) made test_resolve_user_limits_with_no_premium_attribute_is_free FAIL
# with ``AttributeError: 'types.SimpleNamespace' object has no attribute
# 'premium'`` - proving the guard is load-bearing, not a decoration. The edit
# was then reverted by hand (no git stash/checkout/reset) and ``git diff``
# confirmed tools/premium.py matched its pre-break state before the full
# suite was re-run green.


def test_a_real_discord_entitlement_type_enum_is_coerced_to_its_int():
    """discord.EntitlementType is discord.py's own enum: int() refuses it, so
    the first real (or test-mode) purchase crashed the write. The stored row
    must carry the plain int value."""
    import types as _types

    import discord

    entitlement = _types.SimpleNamespace(
        id=1,
        sku_id=2,
        guild_id=3,
        user_id=None,
        type=discord.EntitlementType.test_mode_purchase,
        deleted=False,
        consumed=False,
        starts_at=None,
        ends_at=None,
    )
    row = premium._coerce_entitlement(entitlement)
    assert row["entitlement_type"] == discord.EntitlementType.test_mode_purchase.value
    assert isinstance(row["entitlement_type"], int)
