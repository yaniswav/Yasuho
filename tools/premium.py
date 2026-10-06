"""Premium offer catalog, entitlement projection and the capability resolver.

M3a of the monetisation plan (.claude/plans/monetisation/4-plan-retenu.md):
foundations only. No cog behaviour changes here, no gateway events, no
/premium command, nothing wired onto the bot yet - that is M3b/M3c. This
module provides the pieces those lots will assemble:

* the commercial offer catalog (:class:`GuildLimits`, :class:`UserLimits`,
  :data:`GUILD_FREE`/:data:`GUILD_PREMIUM`/:data:`USER_FREE`/
  :data:`USER_PREMIUM`), clamped to separate safety ceilings;
* :func:`is_active`, the ONE place the "is this entitlement currently
  granting its benefit" rule is written;
* the store helpers (:func:`upsert_entitlement`, :func:`mark_deleted`,
  :func:`load_active`) that keep ``premium_entitlements`` (schema.sql) in
  sync with Discord's own ledger;
* :class:`EntitlementCache` and the module-level :data:`premium_limits`
  instance, a synchronous O(1) resolver hot paths call directly:
  ``premium_limits.for_guild(guild_id)`` / ``.for_user(user_id)``.

DISCORD IS THE AUTHORITY. ``premium_entitlements`` is a PROJECTION,
reconstructible at any time by a resync against Discord's REST/gateway
entitlement surface (M3b). It holds no payment data: no card, no amount, no
invoice - only which SKU is granted to which guild or user, from when, until
when, and when that was last confirmed. That reconstructibility is exactly
why tools/retention.py purges a departed guild's rows and tools/privacy.py
exports/erases a user's rows: a resync restores whatever is still genuinely
granted, so nothing is lost by deleting the local copy.

NO SKU CONFIGURED = NOBODY IS PREMIUM. If ``[Premium] yasuho_plus_sku`` /
``comfort_pack_sku`` are absent (or invalid) in bot.ini,
:func:`EntitlementCache.is_guild_premium` / ``.has_comfort_pack`` always
return False and every resolver call returns the FREE tier - today's
behaviour, preserved as the default on every fresh checkout.

WHY THE FREE CONSTANTS ARE RESTATED HERE RATHER THAN IMPORTED. Every FREE
value below except ``FREE_MAX_HUBS`` mirrors a constant that lives in a cog
module (MAX_GUILD_PLAYLISTS in cogs/music/playlists_shared.py, and so on).
tools/ must not import cogs - cogs import tools, and importing the other way
would be the cycle this repo forbids (see tools/retention.py's own
PRESENCE_AGGREGATE_MAX_AGE_DAYS restatement for the established precedent).
So each one is retyped as a plain integer here, and
tests/tools/test_premium.py imports the OWNING cog module and asserts
equality against it - the restatement cannot silently drift, because the
test is the thing holding the two numbers together. ``FREE_MAX_HUBS`` is the
one exception: it already lives in tools/autoroom.py (tools importing
tools, no cycle), so it is imported directly instead of retyped.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from tools.autoroom import MAX_HUBS as FREE_MAX_HUBS
from tools.config_loader import ConfigLoader, config_loader
from tools.db import affected_rows

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# [Premium] SKU configuration (bot.ini)
# ---------------------------------------------------------------------------
# Optional on purpose: a fresh checkout has no [Premium] section, and that
# must never crash the bot at import (same posture as TOP_GG_TOKEN in
# cogs/system/webstats.py). An absent key reads as None; a present-but-
# non-integer value is a config typo, not a crash - it is rejected with one
# WARNING and treated as absent, which is the fail-closed direction (nobody
# becomes premium by accident, worst case is a real purchase not yet wired).


def _read_sku_id(option, *, loader=None):
    """Read one ``[Premium]`` SKU id. ``loader`` defaults to the real bot.ini
    loader; tests pass their own :class:`ConfigLoader` built from a temp
    file, the same substitution tests/tools/test_config_loader.py uses.
    """
    loader = loader or config_loader
    raw = loader.get("Premium", option, fallback=None)
    if raw is None:
        return None
    value = ConfigLoader._unquote(raw)
    try:
        return int(value)
    except ValueError:
        log.warning(
            "invalid [Premium] %s %r in bot.ini; treating as absent", option, value
        )
        return None


# Guild subscription SKU ("Yasuho+"). See GUILD_PREMIUM below for what it grants.
YASUHO_PLUS_SKU = _read_sku_id("yasuho_plus_sku")
# User durable-purchase SKU ("Pack Confort"). See USER_PREMIUM below.
COMFORT_PACK_SKU = _read_sku_id("comfort_pack_sku")


# ---------------------------------------------------------------------------
# FREE values - restated from their owning cog constants (see module docstring)
# ---------------------------------------------------------------------------

FREE_MAX_GUILD_PLAYLISTS = 25  # cogs/music/playlists_shared.MAX_GUILD_PLAYLISTS
FREE_MAX_PLAYLIST_TRACKS = 200  # cogs/music/playlists_shared.MAX_PLAYLIST_TRACKS
FREE_HISTORY_MAX_ITEMS = 100  # cogs/music/player.HISTORY_MAX_ITEMS
FREE_MAX_FEEDS_PER_GUILD = 2  # cogs/anilist/feed_policy.MAX_FEEDS_PER_GUILD
FREE_MAX_FOLLOWS_PER_FEED = 25  # cogs/anilist/feed_policy.MAX_FOLLOWS_PER_FEED
FREE_MAX_SUBS_PER_FEED = 50  # cogs/anilist/feed_policy.MAX_SUBS_PER_FEED
FREE_MAX_MENUS_PER_GUILD = 25  # cogs/config/rolemenus.MAX_MENUS_PER_GUILD
# FREE_MAX_HUBS: imported above, not restated (tools/autoroom.MAX_HUBS).
# The ticket cap is the admin-configurable HARD CEILING, not the bot default:
# today an admin can set anywhere from 1 to this many open tickets per member
# (cogs/config/tickets/guild_config.MAX_OPEN_PER_USER); Yasuho+ raises the
# ceiling itself, which is why it belongs in the commercial catalog at all.
FREE_MAX_TICKETS_OPEN_PER_USER = 5
FREE_SERVERSTATS_RETENTION_DAYS = 90  # cogs/community/serverstats/cog.RETENTION_DAYS
# Brand-new benefits (M4a/M4b): there is no FREE constant to mirror because
# today NO guild has either, whatever SKUs exist - these two are the
# definition of "off" rather than a copy of one.
FREE_MUSIC_247 = False
FREE_PREMIUM_BADGE = False

FREE_MAX_FAVOURITES = 100  # cogs/music/music.MAX_FAVOURITES
FREE_MAX_PENDING_REMINDERS = 25  # cogs/community/reminders.MAX_PENDING_REMINDERS
FREE_MAX_RECURRING_REMINDERS = 5  # cogs/community/reminders.MAX_RECURRING_REMINDERS


# ---------------------------------------------------------------------------
# Safety ceilings - SEPARATE from the commercial catalog, never sold past
# ---------------------------------------------------------------------------
# Absolute maxima per field, independent of any price tier. These exist to
# protect shared budgets (Postgres row/blob counts, Lavalink queue memory,
# Discord API call volume) from a pricing mistake or a future tier someone
# adds without re-reading this file: the resolver (GuildLimits/UserLimits
# __post_init__ below) clamps every commercial value - FREE and premium
# alike - to its ceiling, so a catalog typo can be wrong but never unsafe.
#
# Each one is set comfortably above today's Yasuho+/Pack Confort values
# (room for a future tier or a measured increase - see the plan's AniList
# note: "relever plus apres mesure de charge") while still being a real
# bound, not a decoration:
#   * serverstats_retention_days is pinned EXACTLY to the premium value
#     (365): the plan ties that number to a storage/privacy promise ("chaque
#     agregat est supprime a 365 j"), so there is deliberately no headroom
#     past it - raising retention is a policy decision, not a pricing one.
#   * every other ceiling is a round number comfortably above the premium
#     column of the plan's table, e.g. the planned 1000+ guild memory story
#     in the module docstring already shows this stays cheap even near the
#     ceiling.
GUILD_CEILINGS = {
    "max_guild_playlists": 150,
    "max_playlist_tracks": 1000,
    "history_max_items": 500,
    "max_feeds_per_guild": 12,
    "max_follows_per_feed": 100,
    "max_subs_per_feed": 200,
    "max_menus_per_guild": 100,
    "max_hubs": 20,
    "max_tickets_open_per_user": 20,
    "serverstats_retention_days": 365,
}

USER_CEILINGS = {
    "max_favourites": 500,
    "max_pending_reminders": 100,
    "max_recurring_reminders": 25,
}


@dataclasses.dataclass(frozen=True)
class GuildLimits:
    """Effective per-guild commercial limits. Clamped to GUILD_CEILINGS."""

    max_guild_playlists: int
    max_playlist_tracks: int
    history_max_items: int
    max_feeds_per_guild: int
    max_follows_per_feed: int
    max_subs_per_feed: int
    max_menus_per_guild: int
    max_hubs: int
    max_tickets_open_per_user: int
    serverstats_retention_days: int
    music_247: bool
    premium_badge: bool

    def __post_init__(self):
        for name, ceiling in GUILD_CEILINGS.items():
            value = getattr(self, name)
            if value > ceiling:
                object.__setattr__(self, name, ceiling)


@dataclasses.dataclass(frozen=True)
class UserLimits:
    """Effective per-user commercial limits. Clamped to USER_CEILINGS."""

    max_favourites: int
    max_pending_reminders: int
    max_recurring_reminders: int

    def __post_init__(self):
        for name, ceiling in USER_CEILINGS.items():
            value = getattr(self, name)
            if value > ceiling:
                object.__setattr__(self, name, ceiling)


# The two guild tiers. GUILD_FREE must equal today's behaviour exactly -
# tests/tools/test_premium.py asserts every field against its FREE_* constant.
GUILD_FREE = GuildLimits(
    max_guild_playlists=FREE_MAX_GUILD_PLAYLISTS,
    max_playlist_tracks=FREE_MAX_PLAYLIST_TRACKS,
    history_max_items=FREE_HISTORY_MAX_ITEMS,
    max_feeds_per_guild=FREE_MAX_FEEDS_PER_GUILD,
    max_follows_per_feed=FREE_MAX_FOLLOWS_PER_FEED,
    max_subs_per_feed=FREE_MAX_SUBS_PER_FEED,
    max_menus_per_guild=FREE_MAX_MENUS_PER_GUILD,
    max_hubs=FREE_MAX_HUBS,
    max_tickets_open_per_user=FREE_MAX_TICKETS_OPEN_PER_USER,
    serverstats_retention_days=FREE_SERVERSTATS_RETENTION_DAYS,
    music_247=FREE_MUSIC_247,
    premium_badge=FREE_PREMIUM_BADGE,
)

# Yasuho+ (guild subscription). Values from the retained plan's table
# (.claude/plans/monetisation/4-plan-retenu.md).
GUILD_PREMIUM = GuildLimits(
    max_guild_playlists=75,
    max_playlist_tracks=500,
    history_max_items=200,
    max_feeds_per_guild=6,
    max_follows_per_feed=50,
    max_subs_per_feed=100,
    max_menus_per_guild=50,
    max_hubs=10,
    max_tickets_open_per_user=10,
    serverstats_retention_days=365,
    music_247=True,
    premium_badge=True,
)

USER_FREE = UserLimits(
    max_favourites=FREE_MAX_FAVOURITES,
    max_pending_reminders=FREE_MAX_PENDING_REMINDERS,
    max_recurring_reminders=FREE_MAX_RECURRING_REMINDERS,
)

# Pack Confort (user durable purchase). No avatar-history perk - the plan is
# explicit that one waits on the free/premium avatar-history windows being
# defined first.
USER_PREMIUM = UserLimits(
    max_favourites=300,
    max_pending_reminders=60,
    max_recurring_reminders=15,
)


# ---------------------------------------------------------------------------
# The ACTIVE rule
# ---------------------------------------------------------------------------
# A short technical grace, and ONLY that: it exists so a missed renewal
# webhook (a gateway hiccup, a restart that landed in the wrong window) never
# downgrades a real subscriber the instant their period ends, while a
# CONFIRMED end (we have re-synced since ends_at and Discord still says it
# ended) takes effect immediately - a grace is for "we have not checked",
# never for "we checked and it is over".
GRACE = datetime.timedelta(hours=48)


def _get(entitlement, name, default=None):
    """Read one field from a discord.Entitlement, a DB row, or a test double.

    Tries mapping-style access first (asyncpg.Record and dict rows), then
    falls back to attribute access (discord.Entitlement, SimpleNamespace test
    doubles). Never raises: a missing field reads as ``default``.
    """
    try:
        return entitlement[name]
    except (TypeError, KeyError, IndexError):
        pass
    return getattr(entitlement, name, default)


def is_active(entitlement, *, now=None):
    """Whether ``entitlement`` currently grants its benefit.

    ``entitlement`` is anything :func:`_get` can read ``deleted``, ``ends_at``
    and ``last_synced_at`` off: a stored ``premium_entitlements`` row, a
    discord.Entitlement (``last_synced_at`` then reads as absent, so the
    grace branch below never fires for one that has not been persisted yet -
    correct, since there is nothing to compare it against), or a test double.

    The rule, in order:
      1. ``deleted`` is True -> never active, whatever the dates say.
      2. ``ends_at`` is None -> active (a test-mode entitlement: active until
         deleted, exactly like discord.Entitlement.is_expired() always
         returning False for one).
      3. ``now < ends_at`` -> active (the ordinary case, still inside the
         paid period).
      4. ``now`` is within :data:`GRACE` of ``ends_at`` AND
         ``last_synced_at < ends_at`` (we have NOT re-confirmed the end since
         it happened) -> active (the short technical grace).
      5. Otherwise -> inactive, including a confirmed end
         (``last_synced_at >= ends_at``) still inside the grace window, and
         anything past the grace window regardless of when it was last synced.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if _get(entitlement, "deleted", False):
        return False
    ends_at = _get(entitlement, "ends_at")
    if ends_at is None:
        return True
    if now < ends_at:
        return True
    last_synced_at = _get(entitlement, "last_synced_at")
    if (
        last_synced_at is not None
        and now < ends_at + GRACE
        and last_synced_at < ends_at
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Store helpers (asyncpg pool)
# ---------------------------------------------------------------------------


def _coerce_entitlement(entitlement):
    """Normalise a discord.Entitlement (or test double) into DB column values.

    guild_id and user_id are independently Optional on discord.Entitlement
    (confirmed by reading discord/sku.py directly - both ``__slots__`` entries
    have no relation enforced on each other at the wrapper level), but our own
    catalog never sells both a guild subscription and a user purchase on the
    same SKU type at once (Discord itself forbids offering user and guild
    subscriptions simultaneously), so in practice at most one is ever set.
    guild_id wins if Discord ever sends both anyway: it is the defensive
    choice, since picking it never silently drops a guild-wide benefit that
    every member of that guild is relying on.
    """
    entitlement_id = int(_get(entitlement, "id"))
    sku_id = int(_get(entitlement, "sku_id"))
    raw_guild_id = _get(entitlement, "guild_id")
    raw_user_id = _get(entitlement, "user_id")
    guild_id = int(raw_guild_id) if raw_guild_id is not None else None
    user_id = int(raw_user_id) if raw_user_id is not None else None
    if guild_id is not None:
        scope_type = "guild"
        user_id = None
    elif user_id is not None:
        scope_type = "user"
    else:
        raise ValueError(
            f"entitlement {entitlement_id} names neither a guild nor a user"
        )
    raw_type = _get(entitlement, "type")
    entitlement_type = int(raw_type) if raw_type is not None else None
    return {
        "entitlement_id": entitlement_id,
        "sku_id": sku_id,
        "scope_type": scope_type,
        "guild_id": guild_id,
        "user_id": user_id,
        "entitlement_type": entitlement_type,
        "deleted": bool(_get(entitlement, "deleted", False)),
        "consumed": bool(_get(entitlement, "consumed", False)),
        "starts_at": _get(entitlement, "starts_at"),
        "ends_at": _get(entitlement, "ends_at"),
    }


_UPSERT_ENTITLEMENT = """
INSERT INTO premium_entitlements
    (entitlement_id, sku_id, scope_type, guild_id, user_id,
     entitlement_type, deleted, consumed, starts_at, ends_at, last_synced_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
ON CONFLICT (entitlement_id) DO UPDATE SET
    sku_id = EXCLUDED.sku_id,
    scope_type = EXCLUDED.scope_type,
    guild_id = EXCLUDED.guild_id,
    user_id = EXCLUDED.user_id,
    entitlement_type = EXCLUDED.entitlement_type,
    deleted = EXCLUDED.deleted,
    consumed = EXCLUDED.consumed,
    starts_at = EXCLUDED.starts_at,
    ends_at = EXCLUDED.ends_at,
    last_synced_at = now()
"""


async def upsert_entitlement(pool, entitlement):
    """Idempotently write one entitlement into the projection.

    Accepts a discord.Entitlement or any test double :func:`_coerce_entitlement`
    can read. ``last_synced_at`` is always stamped to now() by the query
    itself (never taken from the caller), since it means "the last time we
    confirmed this with Discord", not "the last time this row was touched".
    """
    row = _coerce_entitlement(entitlement)
    await pool.execute(
        _UPSERT_ENTITLEMENT,
        row["entitlement_id"],
        row["sku_id"],
        row["scope_type"],
        row["guild_id"],
        row["user_id"],
        row["entitlement_type"],
        row["deleted"],
        row["consumed"],
        row["starts_at"],
        row["ends_at"],
    )
    return row


async def mark_deleted(pool, entitlement_id):
    """Mark one entitlement deleted (ENTITLEMENT_DELETE), re-stamping the sync time."""
    status = await pool.execute(
        "UPDATE premium_entitlements SET deleted = TRUE, last_synced_at = now() "
        "WHERE entitlement_id = $1",
        int(entitlement_id),
    )
    return affected_rows(status) > 0


async def load_active(pool):
    """Every non-deleted entitlement row, for :meth:`EntitlementCache.load`.

    "Non-deleted" only - the time-based half of the ACTIVE rule (:func:`is_active`)
    is applied in Python at cache-build time, not in this query, so the one
    rule in the module docstring stays the only place that decides it.
    """
    return await pool.fetch(
        "SELECT entitlement_id, sku_id, scope_type, guild_id, user_id, "
        "entitlement_type, deleted, consumed, starts_at, ends_at, last_synced_at "
        "FROM premium_entitlements WHERE deleted = FALSE"
    )


# ---------------------------------------------------------------------------
# In-memory cache + resolver
# ---------------------------------------------------------------------------


class EntitlementCache:
    """O(1) guild_id/user_id -> active SKU set, and the limits resolver on top.

    M3a scope: this class provides ``load()`` and the read API only. WIRING an
    instance onto the bot (core.py setup_hook, the boot resync, the periodic
    reconciliation, the ENTITLEMENT_* gateway handlers) is M3b - the same split
    the module docstring states. :data:`premium_limits` below is a ready
    module-level instance so callers (and M3b's wiring) have one to reach for
    without constructing their own.

    SCALE STORY (1000+ guilds). Each active guild/user entry is one dict key
    (an int) mapping to a small set of ints (today at most one SKU per scope,
    since the catalog sells exactly one guild subscription and one user
    purchase) - a few dozen bytes per premium scope, not per guild: a free
    guild or user occupies no entry at all (``.get(id, ())`` on a miss is the
    whole cost). At 1000+ guilds with every one of them premium that is still
    only on the order of tens of kilobytes, and the realistic case (a minority
    paying) is smaller still. ``for_guild``/``for_user`` do a bare dict lookup
    and a set membership test - no await, no lock, no query - so the hot paths
    named in the M3a brief (playlist/favourite/reminder/ticket/menu/hub caps)
    pay nothing beyond what they already pay to read today's module-level
    constant.
    """

    def __init__(self):
        self._guild_skus = {}
        self._user_skus = {}

    def load_rows(self, rows, *, now=None):
        """Rebuild both maps from DB rows (or test doubles), applying is_active."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        guild_skus = {}
        user_skus = {}
        for row in rows:
            if not is_active(row, now=now):
                continue
            sku_id = _get(row, "sku_id")
            if sku_id is None:
                continue
            sku_id = int(sku_id)
            scope_type = _get(row, "scope_type")
            if scope_type == "guild":
                guild_id = _get(row, "guild_id")
                if guild_id is None:
                    continue
                guild_skus.setdefault(int(guild_id), set()).add(sku_id)
            elif scope_type == "user":
                user_id = _get(row, "user_id")
                if user_id is None:
                    continue
                user_skus.setdefault(int(user_id), set()).add(sku_id)
        self._guild_skus = guild_skus
        self._user_skus = user_skus

    async def load(self, pool):
        """Reload both maps from the database (boot; M3b also calls this on resync)."""
        rows = await load_active(pool)
        self.load_rows(rows)

    def is_guild_premium(self, guild_id):
        if YASUHO_PLUS_SKU is None:
            return False
        return YASUHO_PLUS_SKU in self._guild_skus.get(int(guild_id), ())

    def has_comfort_pack(self, user_id):
        if COMFORT_PACK_SKU is None:
            return False
        return COMFORT_PACK_SKU in self._user_skus.get(int(user_id), ())

    def for_guild(self, guild_id):
        """The effective GuildLimits for this guild: GUILD_PREMIUM or GUILD_FREE."""
        return GUILD_PREMIUM if self.is_guild_premium(guild_id) else GUILD_FREE

    def for_user(self, user_id):
        """The effective UserLimits for this user: USER_PREMIUM or USER_FREE."""
        return USER_PREMIUM if self.has_comfort_pack(user_id) else USER_FREE


# Ready-to-use default instance. M3b decides final ownership/wiring (likely
# ``bot.premium`` built from this same class, parallel to ``bot.prefixes``);
# until then this is the one callers reach for.
premium_limits = EntitlementCache()
