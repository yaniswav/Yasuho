"""Premium offer catalog, entitlement projection and the capability resolver.

M3a laid the foundations (.claude/plans/monetisation/4-plan-retenu.md):
catalog, the ACTIVE rule, the store helpers, the resolver. M3a+ wired
owner-gifted grants and the boot load (cogs/system/premium.py, core.py).
M3b (this lot) adds the two things that keep the projection honest without
ever needing a restart: the ENTITLEMENT_* gateway event handlers and a
periodic reconciliation against Discord's own complete listing - see
:func:`upsert_entitlement_event`, :func:`reconcile` and
:meth:`EntitlementCache.refresh_entitlement_scope` below, and
cogs/system/premium.py for where the three gateway listeners and the
reconciliation loop are wired onto the bot. The public ``/premium``
purchase surface (test purchases, the audit journal) is still M3c.

This module provides:

* the commercial offer catalog (:class:`GuildLimits`, :class:`UserLimits`,
  :data:`GUILD_FREE`/:data:`GUILD_PREMIUM`/:data:`USER_FREE`/
  :data:`USER_PREMIUM`), clamped to separate safety ceilings;
* :func:`is_active`, the ONE place the "is this entitlement currently
  granting its benefit" rule is written;
* the store helpers (:func:`upsert_entitlement`, :func:`mark_deleted`,
  :func:`load_active`) that keep ``premium_entitlements`` (schema.sql) in
  sync with Discord's own ledger, plus the ORDER-SAFE event-write path
  (:func:`upsert_entitlement_event`) and the fail-safe full resync
  (:func:`reconcile`) M3b adds on top of them;
* :class:`EntitlementCache` and the module-level :data:`premium_limits`
  instance, a synchronous O(1) resolver hot paths call directly:
  ``premium_limits.for_guild(guild_id)`` / ``.for_user(user_id)``.

DISCORD IS THE AUTHORITY. ``premium_entitlements`` is a PROJECTION,
reconstructible at any time by a resync against Discord's REST/gateway
entitlement surface (:func:`reconcile`, driven by cogs/system/premium.py's
periodic loop). It holds no payment data: no card, no amount, no invoice -
only which SKU is granted to which guild or user, from when, until when,
and when that was last confirmed. That reconstructibility is exactly why
tools/retention.py purges a departed guild's rows and tools/privacy.py
exports/erases a user's rows: a resync restores whatever is still genuinely
granted, so nothing is lost by deleting the local copy.

EVENT ORDERING (M3b). The three ENTITLEMENT_* gateway events can arrive
duplicated, or out of the order they logically happened in (a gateway
reconnect can replay or drop deliveries). Two rules keep every ordering
converging on the same state:

  1. every field OTHER than ``deleted`` is last-write-wins from whichever
     event arrives last - acceptable because the periodic reconciliation
     (:func:`reconcile`) is the backstop that corrects any transient
     staleness within one cycle, and no commercial decision reads a field
     other than ``deleted``/``ends_at`` (already covered by rule 2 and the
     GRACE window in :func:`is_active`);
  2. ``deleted`` can only ever go FALSE -> TRUE through a gateway event,
     never the other way: :func:`upsert_entitlement_event` ORs the
     incoming value with whatever is already stored
     (``premium_entitlements.deleted OR EXCLUDED.deleted``), so a late,
     stale create/update for an entitlement that a delete (or a refund
     reported as an update with ``deleted=True`` - "a refund follows the
     expiry path") already marked deleted can never clear it back. The
     ONLY thing allowed to clear ``deleted`` back to ``FALSE`` is
     :func:`reconcile`'s COMPLETE listing explicitly showing the row alive
     again - a full resync is trusted to correct a wrongly-ordered or
     buggy event; a single event never is.

NO SKU CONFIGURED = NOBODY IS PREMIUM, UNLESS THE OWNER GIFTED IT. If
``[Premium] yasuho_plus_sku`` / ``comfort_pack_sku`` are absent (or invalid)
in bot.ini, :func:`EntitlementCache.is_guild_premium` / ``.has_comfort_pack``
never see a Discord-sourced grant - but an OWNER GRANT (``premium_grants``,
below) still counts. That is deliberate: the owner can gift a friend's
server or a user before any SKU exists, which is exactly the lot M3a+ adds.

OWNER GRANTS (M3a+). ``premium_grants`` is a second, INDEPENDENT source of
the same benefit, written only by the bot owner's ``?premium`` commands
(cogs/system/premium.py) - never by Discord, never by a resync. It is our
own audit trail (who granted what, to whom, when, until when, who revoked
it), not a projection of anything: Discord's test-entitlement API
(``create_entitlement``) is for development only and must not be used to
gift a real benefit, so a gift lives here instead. :func:`is_grant_active`
is the ONE place that rule is written (no grace: it is ours to end, not a
missed webhook to forgive). :class:`EntitlementCache` merges both sources -
``is_guild_premium``/``has_comfort_pack`` are true if EITHER an active
Discord entitlement (for the configured SKU) OR an active grant says so.

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

import asyncio
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
# Owner grant products - the two products ?premium can gift, independent of
# whether their SKU is configured. The scope each one commercially belongs to
# is fixed by the catalog above (Yasuho+ is a guild subscription, Pack Confort
# a user purchase) and is enforced by PRODUCT_SCOPE / validate_grant_scope
# below, not left to the caller to get right.
# ---------------------------------------------------------------------------
PRODUCT_YASUHO_PLUS = "yasuho_plus"
PRODUCT_COMFORT_PACK = "comfort_pack"
PRODUCTS = (PRODUCT_YASUHO_PLUS, PRODUCT_COMFORT_PACK)
PRODUCT_SCOPE = {
    PRODUCT_YASUHO_PLUS: "guild",
    PRODUCT_COMFORT_PACK: "user",
}


def validate_grant_scope(product, scope_type):
    """Raise ``ValueError`` unless ``scope_type`` is the one ``product`` sells as.

    The same rule schema.sql's ``premium_grants_product_scope_valid`` CHECK
    enforces at the database level - this is the Python-side half, so a bad
    call fails with a readable message before it ever reaches a query, and a
    unit test can exercise the rule without a pool.
    """
    if product not in PRODUCT_SCOPE:
        raise ValueError(f"unknown premium grant product: {product!r}")
    expected = PRODUCT_SCOPE[product]
    if scope_type != expected:
        raise ValueError(
            f"{product!r} is a {expected}-scoped product, not {scope_type!r}"
        )


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

    THE RECONCILIATION VARIANT (M3b). ``deleted`` here is assigned
    unconditionally from ``entitlement`` - unlike :func:`upsert_entitlement_event`
    below, this one can clear a previously-stored ``deleted = TRUE`` back to
    ``FALSE``. That is deliberate and safe ONLY because its one caller,
    :func:`reconcile`, only ever calls this for a row a COMPLETE Discord
    listing just returned as alive (``exclude_deleted=True``), so ``deleted``
    arrives as ``False`` in practice - a full resync is the one thing trusted
    to correct a wrongly-ordered or buggy event (see the module docstring's
    "EVENT ORDERING" section). A single gateway event must never call this
    function directly; it calls :func:`upsert_entitlement_event` instead.
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


_UPSERT_ENTITLEMENT_EVENT = """
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
    deleted = premium_entitlements.deleted OR EXCLUDED.deleted,
    consumed = EXCLUDED.consumed,
    starts_at = EXCLUDED.starts_at,
    ends_at = EXCLUDED.ends_at,
    last_synced_at = now()
"""


async def upsert_entitlement_event(pool, entitlement, *, force_deleted=False):
    """Idempotent, ORDER-SAFE write for a single ENTITLEMENT_* gateway event.

    Same row shape as :func:`upsert_entitlement`, and the same one-statement
    INSERT ... ON CONFLICT shape, but ``deleted`` is OR-ed with whatever is
    already stored (``premium_entitlements.deleted OR EXCLUDED.deleted``)
    rather than assigned outright. That one difference is what makes the
    three gateway events converge to the same state regardless of delivery
    order or duplication:

      * two duplicate events (same id, same payload) are a no-op either way;
      * an UPDATE delivered before its own CREATE for the same id still
        converges once the CREATE lands (whichever arrives first INSERTs
        the row, the second just updates the other fields);
      * once ANY event has set ``deleted = TRUE`` on a row (including a
        refund reported as an UPDATE with ``deleted: true`` - "a refund
        follows the expiry path"), a LATER-arriving but OLDER/stale
        CREATE or UPDATE for that same id - one that still carries
        ``deleted: false`` - can never clear it back: TRUE OR anything is
        TRUE. Only :func:`reconcile` is trusted to clear it.

    ``force_deleted=True`` is the ``on_entitlement_delete`` handler's own
    case (cogs/system/premium.py), and is why this is NOT simply
    :func:`mark_deleted` called from the listener: ``mark_deleted`` is
    UPDATE-only and does nothing when the row does not exist yet, which
    would LOSE a delete that the gateway happened to deliver before the
    matching create (out-of-order delivery is exactly what this function
    exists to survive). ``force_deleted=True`` ignores whatever the
    payload's own ``deleted`` field says (not trusted here - receiving the
    DELETE event at all is the signal) and sets ``deleted = True`` on the
    row this call writes, INSERTing it already-deleted if it does not exist
    yet; the OR in the SQL above then still protects that TRUE from a later
    stale create/update for the same id, exactly like every other case.
    """
    row = _coerce_entitlement(entitlement)
    if force_deleted:
        row["deleted"] = True
    await pool.execute(
        _UPSERT_ENTITLEMENT_EVENT,
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


async def load_active_for_guild(pool, guild_id):
    """Active entitlement rows for ONE guild, for
    :meth:`EntitlementCache.refresh_entitlement_scope`. Same column set and
    same "non-deleted only" split as :func:`load_active`, narrowed to a
    single scope so a gateway event never pays for a whole-table reload."""
    return await pool.fetch(
        "SELECT entitlement_id, sku_id, ends_at, last_synced_at "
        "FROM premium_entitlements WHERE guild_id = $1 AND deleted = FALSE",
        int(guild_id),
    )


async def load_active_for_user(pool, user_id):
    """The user-scoped twin of :func:`load_active_for_guild`."""
    return await pool.fetch(
        "SELECT entitlement_id, sku_id, ends_at, last_synced_at "
        "FROM premium_entitlements WHERE user_id = $1 AND deleted = FALSE",
        int(user_id),
    )


async def load_active_entitlement_ids(pool):
    """Every non-deleted ``entitlement_id`` on record, as a ``set[int]``.

    The diff base :func:`reconcile` subtracts a complete Discord listing
    from: whatever id is in this set but was NOT seen in that listing is an
    entitlement a complete snapshot no longer reports, and is marked deleted.
    """
    rows = await pool.fetch(
        "SELECT entitlement_id FROM premium_entitlements WHERE deleted = FALSE"
    )
    return {int(_get(row, "entitlement_id")) for row in rows}


async def reconcile(pool, entitlements, *, application_id):
    """Resync ``premium_entitlements`` against a COMPLETE Discord listing.

    ``entitlements`` is an async iterable of discord.Entitlement-like
    objects (or any test double :func:`upsert_entitlement` already accepts) -
    in production, ``bot.entitlements(skus=..., exclude_deleted=True,
    limit=None)`` (cogs/system/premium.py), which discord.py itself paginates
    page-by-page under the hood. This function stays discord.py-free (duck-
    typed, the same posture as the rest of this module - see :func:`_get`),
    so it is unit-testable with a plain async generator and no real Client.

    THE ALGORITHM:
      1. consume ``entitlements`` fully, upserting (:func:`upsert_entitlement`
         - the reconciliation/clobbering variant, see its own docstring)
         every row whose ``application_id`` matches ours, and remembering
         every id seen;
      2. once the iterable is FULLY consumed with no error, read every
         currently-stored non-deleted id (:func:`load_active_entitlement_ids`)
         and mark deleted (:func:`mark_deleted`) whichever of THOSE ids was
         not in the seen set - an entitlement a complete listing no longer
         reports is, by definition, gone.

    FAIL-SAFE BY CONSTRUCTION. Step 2 - the only DESTRUCTIVE part of a
    reconciliation pass - runs ONLY if step 1 finished without raising. If
    ``entitlements`` raises partway through (a Discord outage, a timeout, a
    rate-limit error, a dropped connection mid-page), this function logs the
    failure and returns ``None`` immediately: NOTHING is marked deleted, and
    the rows already upserted during the partial pass are kept as they stand
    (harmless - an upsert only ever makes a row MORE current, never less, so
    a partial pass can improve the local projection but this function never
    lets one DOWNGRADE it). A Discord outage must never read as "everyone's
    subscription ended" - see the plan's own "panne Discord" rule.

    Rows naming a different ``application_id`` are skipped entirely - not
    upserted, not counted as seen. Discord's own REST/gateway filtering
    already scopes a listing to our application, so this is defence in
    depth, not the primary guard; it matters because a row like that must
    never make this function mark one of OUR OWN entitlements deleted on
    its account either.

    Returns ``{"seen": ..., "upserted": ..., "missing": ...}`` on a complete
    pass, or ``None`` when the pass was aborted - so a caller
    (cogs/system/premium.py's periodic loop) can tell the two outcomes apart
    without parsing logs, and in particular knows NOT to reload
    :class:`EntitlementCache` from the database after an aborted pass (that
    reload is a plain re-read of whatever is in Postgres right now, which is
    exactly why it must only happen after a pass that left Postgres alone).
    """
    seen_ids = set()
    upserted = 0
    try:
        async for entitlement in entitlements:
            entitlement_application_id = _get(entitlement, "application_id")
            if (
                entitlement_application_id is not None
                and int(entitlement_application_id) != int(application_id)
            ):
                continue
            row = await upsert_entitlement(pool, entitlement)
            seen_ids.add(row["entitlement_id"])
            upserted += 1
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(
            "Premium reconciliation: listing failed or was interrupted; "
            "leaving premium_entitlements untouched (marking nothing deleted)"
        )
        return None

    stored_ids = await load_active_entitlement_ids(pool)
    missing_ids = stored_ids - seen_ids
    for entitlement_id in missing_ids:
        await mark_deleted(pool, entitlement_id)

    return {
        "seen": len(seen_ids),
        "upserted": upserted,
        "missing": len(missing_ids),
    }


# ---------------------------------------------------------------------------
# Owner grants - ``premium_grants`` (schema.sql). OUR OWN audit trail, never
# Discord's: no entitlement_id, no sku_id, no resync. See the module
# docstring's "OWNER GRANTS" paragraph for why this exists alongside the
# projection above rather than inside it.
# ---------------------------------------------------------------------------


def is_grant_active(grant, *, now=None):
    """Whether ``grant`` currently grants its benefit.

    Deliberately NOT :func:`is_active`'s rule: a grant is ours to end, so
    there is no 48h technical grace for "we have not re-synced yet" - that
    grace exists only because Discord's webhook delivery can be missed. A
    grant is active exactly when it has not been revoked and (it has no
    expiry, or that expiry has not passed yet).
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if _get(grant, "revoked_at") is not None:
        return False
    expires_at = _get(grant, "expires_at")
    if expires_at is None:
        return True
    return now < expires_at


_CREATE_GRANT = """
INSERT INTO premium_grants
    (product, scope_type, guild_id, user_id, reason, granted_by, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING id
"""


async def create_grant(
    pool,
    *,
    product,
    scope_type,
    granted_by,
    guild_id=None,
    user_id=None,
    reason=None,
    expires_at=None,
):
    """Insert one owner grant and return its new id.

    Raises ``ValueError`` (via :func:`validate_grant_scope`) before touching
    the database if ``product``/``scope_type`` disagree with the catalog, or
    if the scope's own id is missing - the same shape schema.sql's
    ``premium_grants_scope_matches_ids`` CHECK enforces, surfaced early with a
    readable message instead of a constraint-violation traceback.
    """
    validate_grant_scope(product, scope_type)
    if scope_type == "guild":
        if guild_id is None:
            raise ValueError("a guild-scoped grant needs a guild_id")
        user_id = None
    else:
        if user_id is None:
            raise ValueError("a user-scoped grant needs a user_id")
        guild_id = None
    row = await pool.fetchrow(
        _CREATE_GRANT,
        product,
        scope_type,
        guild_id,
        user_id,
        reason,
        int(granted_by),
        expires_at,
    )
    return _get(row, "id")


async def revoke_grant(pool, grant_id, *, revoked_by):
    """Mark one grant revoked. Never deletes the row - it is the audit trail.

    A no-op (returns ``False``) on an already-revoked or non-existent id: the
    ``revoked_at IS NULL`` guard means a second revoke cannot overwrite who
    revoked it, or when, with a later call's values.
    """
    status = await pool.execute(
        "UPDATE premium_grants SET revoked_at = now(), revoked_by = $2 "
        "WHERE id = $1 AND revoked_at IS NULL",
        int(grant_id),
        int(revoked_by),
    )
    return affected_rows(status) > 0


def _grant_filter(*, scope_type=None, guild_id=None, user_id=None, active_only=True):
    """Build the WHERE clause + args shared by :func:`list_grants` callers."""
    clauses = []
    args = []
    if scope_type is not None:
        args.append(scope_type)
        clauses.append(f"scope_type = ${len(args)}")
    if guild_id is not None:
        args.append(int(guild_id))
        clauses.append(f"guild_id = ${len(args)}")
    if user_id is not None:
        args.append(int(user_id))
        clauses.append(f"user_id = ${len(args)}")
    if active_only:
        clauses.append(
            "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"
        )
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, args


async def list_grants(
    pool, *, scope_type=None, guild_id=None, user_id=None, active_only=True
):
    """List grants, active-only by default, newest first.

    Filters are AND-combined: pass ``guild_id``/``user_id`` to see one scope's
    grants, or neither for every grant on record (``?premium list`` with no
    argument). ``active_only=False`` is the audit view (``?premium list ...``
    is always active-only in M3a+; a future admin surface can widen it).
    """
    where, args = _grant_filter(
        scope_type=scope_type,
        guild_id=guild_id,
        user_id=user_id,
        active_only=active_only,
    )
    return await pool.fetch(
        "SELECT id, product, scope_type, guild_id, user_id, reason, "
        "granted_by, granted_at, expires_at, revoked_at, revoked_by "
        f"FROM premium_grants{where} ORDER BY granted_at DESC",
        *args,
    )


async def load_active_grants(pool):
    """Every non-revoked grant row, for :meth:`EntitlementCache.load`.

    "Non-revoked" only, same split as :func:`load_active`: the expiry half of
    :func:`is_grant_active` is applied in Python at cache-build time.
    """
    return await pool.fetch(
        "SELECT id, product, scope_type, guild_id, user_id, reason, "
        "granted_by, granted_at, expires_at, revoked_at, revoked_by "
        "FROM premium_grants WHERE revoked_at IS NULL"
    )


# ---------------------------------------------------------------------------
# In-memory cache + resolver
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _EntitlementSnapshot:
    """The minimal ``premium_entitlements`` fields :func:`is_active` needs.

    Kept as data, NOT pre-collapsed into a boolean, so the ACTIVE rule is
    evaluated at LOOKUP time against the current clock rather than once at
    load time - see :class:`EntitlementCache`'s docstring for why that
    distinction is the whole point. ``deleted`` is never carried here: both
    :func:`load_active` (the query) and :meth:`EntitlementCache.load_rows`
    (defensively, for a caller that hands rows straight in) already drop a
    deleted row before it reaches this snapshot, so it is always ``False``
    and :func:`_get`'s default for the missing attribute is exactly that.
    """

    entitlement_id: int
    sku_id: int
    ends_at: datetime.datetime | None
    last_synced_at: datetime.datetime | None


@dataclasses.dataclass(frozen=True)
class _GrantSnapshot:
    """The minimal ``premium_grants`` fields :func:`is_grant_active` needs.

    Same reasoning as :class:`_EntitlementSnapshot`: ``expires_at`` is kept
    as data so a grant's own expiry is re-checked at lookup time. ``revoked_at``
    is never carried here for the same reason ``deleted`` is absent above -
    every row that reaches this snapshot (:func:`load_active_grants`'s query,
    or :meth:`EntitlementCache.load_grant_rows`'s own defensive filter) is
    already known not-revoked.
    """

    grant_id: int
    product: str
    expires_at: datetime.datetime | None


class EntitlementCache:
    """O(1)-per-scope guild_id/user_id -> active benefit, and the limits
    resolver on top.

    Merges TWO independent sources, per the module docstring's "OWNER GRANTS"
    paragraph: Discord entitlements (``_guild_skus``/``_user_skus``, keyed by
    guild/user id, require a configured SKU) and owner grants
    (``_guild_grants``/``_user_grants``, keyed the same way, work with no SKU
    configured at all). ``is_guild_premium``/``has_comfort_pack`` are true if
    EITHER source says so.

    LIVE EXPIRY, NOT A SNAPSHOT TAKEN AT LOAD TIME. Each map holds a short
    list of :class:`_EntitlementSnapshot`/:class:`_GrantSnapshot` per id - the
    row's own ``ends_at``/``expires_at`` and (for entitlements) the
    ``last_synced_at`` the 48h grace needs - rather than a precomputed
    boolean or a bare sku/product set. ``is_guild_premium``/``has_comfort_pack``
    re-run :func:`is_active`/:func:`is_grant_active` against ``now`` on every
    call, so a benefit that was active at the last :meth:`load` correctly
    stops resolving premium the instant its own ``ends_at``/``expires_at``
    (plus grace, for an entitlement) passes - WITHOUT waiting for the next
    reload. An earlier version of this cache filtered by :func:`is_active` at
    LOAD time and kept only the bare id, which silently froze that one-time
    verdict until the next boot: a 30-day gift still read as premium on day
    45 if the bot had not restarted since. See
    tests/tools/test_premium.py's "live expiry, no reload" section for the
    regression test, and its negative control.

    M3a wired ``load()``/the read API only; M3a+ attached an instance to the
    bot (core.py ``setup_hook``) and added the grants half. M3b (this lot)
    adds :meth:`refresh_entitlement_scope` - the ENTITLEMENT_* gateway
    handlers' counterpart to :meth:`refresh_grant_scope` - and, with it, this
    cache's own ``_lock``: M3a+ had no concurrent writer worth serialising
    against, but M3b's gateway handlers, its periodic reconciliation loop
    (:func:`reconcile`) and the owner's ``?premium`` commands can now all
    write here at once. ``_lock`` is held across each writer's fetch-then-
    rebind (:meth:`load`) or fetch-then-mutate-one-entry
    (:meth:`refresh_entitlement_scope`/:meth:`refresh_grant_scope`), the same
    "fetch and rebind never interleave with another writer's" shape
    core.py's ``eager_cache_lock`` documents for the bot's other hot caches -
    this cache just carries its own lock rather than sharing that one, since
    it is a single self-contained object rather than one of the four bare
    dicts hanging directly off the bot. :data:`premium_limits` below is a
    ready module-level instance for any caller that does not want to
    construct its own.

    SCALE STORY (1000+ guilds). Each active guild/user entry is one dict key
    (an int) mapping to a short list (today at most one SKU/product per
    scope, since the catalog sells exactly one guild subscription and one
    user purchase, so one small frozen dataclass instance) - a few dozen
    bytes per premium scope, not per guild: a free guild or user occupies no
    entry in ANY of the four maps (every lookup below is a plain
    ``.get(id, ())`` on a miss). At 1000+ guilds with every one of them
    premium that is still only on the order of tens of kilobytes, doubled at
    most by adding the grants maps, and the realistic case (a minority
    paying, and owner grants smaller still) is far lighter.
    ``for_guild``/``for_user`` do a handful of dict lookups, a short list scan
    and a handful of datetime comparisons - no await, no lock, no query - so
    the hot paths named in the M3a brief (playlist/favourite/reminder/ticket/
    menu/hub caps) pay nothing beyond what they already pay to read today's
    module-level constant.
    """

    def __init__(self):
        self._guild_skus = {}
        self._user_skus = {}
        self._guild_grants = {}
        self._user_grants = {}
        # Serialises every WRITER below (load/refresh_entitlement_scope/
        # refresh_grant_scope) against each other - see the class docstring's
        # "M3b" paragraph. The read side (is_guild_premium/has_comfort_pack/
        # for_guild/for_user) deliberately takes no lock: they are plain
        # dict reads with no await, exactly the hot-path cost the SCALE
        # STORY above promises, and a reader racing a writer's in-place
        # rebind only ever sees the old map or the new one (Python dict/list
        # assignment is already atomic from one coroutine's point of view
        # with no awaits in between), never a half-built one.
        self._lock = asyncio.Lock()

    def load_rows(self, rows):
        """Rebuild the two ENTITLEMENT maps from DB rows.

        Every non-deleted row is kept as a :class:`_EntitlementSnapshot` -
        NOT filtered down to "active right now", on purpose: filtering here
        would bake today's verdict into the map exactly like the bug this
        class's docstring describes, just with the filter moved one line up.
        A row's own ``deleted`` IS still checked (defensively - the
        :func:`load_active` query this normally runs behind already excludes
        it), because a deleted entitlement must never resolve active at any
        future ``now``, however it got here.
        """
        guild_skus = {}
        user_skus = {}
        for row in rows:
            if _get(row, "deleted", False):
                continue
            sku_id = _get(row, "sku_id")
            if sku_id is None:
                continue
            snapshot = _EntitlementSnapshot(
                entitlement_id=_get(row, "entitlement_id"),
                sku_id=int(sku_id),
                ends_at=_get(row, "ends_at"),
                last_synced_at=_get(row, "last_synced_at"),
            )
            scope_type = _get(row, "scope_type")
            if scope_type == "guild":
                guild_id = _get(row, "guild_id")
                if guild_id is None:
                    continue
                guild_skus.setdefault(int(guild_id), []).append(snapshot)
            elif scope_type == "user":
                user_id = _get(row, "user_id")
                if user_id is None:
                    continue
                user_skus.setdefault(int(user_id), []).append(snapshot)
        self._guild_skus = guild_skus
        self._user_skus = user_skus

    def load_grant_rows(self, rows):
        """Rebuild the two GRANT maps from DB rows.

        Same "keep the data, not a verdict" posture as :meth:`load_rows`:
        every non-revoked row becomes a :class:`_GrantSnapshot`, and its own
        ``expires_at`` is re-checked at lookup time rather than once here.
        """
        guild_grants = {}
        user_grants = {}
        for row in rows:
            if _get(row, "revoked_at") is not None:
                continue
            product = _get(row, "product")
            if product is None:
                continue
            snapshot = _GrantSnapshot(
                grant_id=_get(row, "id"),
                product=product,
                expires_at=_get(row, "expires_at"),
            )
            scope_type = _get(row, "scope_type")
            if scope_type == "guild":
                guild_id = _get(row, "guild_id")
                if guild_id is None:
                    continue
                guild_grants.setdefault(int(guild_id), []).append(snapshot)
            elif scope_type == "user":
                user_id = _get(row, "user_id")
                if user_id is None:
                    continue
                user_grants.setdefault(int(user_id), []).append(snapshot)
        self._guild_grants = guild_grants
        self._user_grants = user_grants

    async def load(self, pool):
        """Reload all four maps from the database (boot, and the periodic
        reconciliation loop in cogs/system/premium.py after a complete,
        successful listing). A caller that wants FREE-on-failure wraps this
        itself (see core.py setup_hook) - this method raises straight
        through, so a partial reload can never be mistaken for a successful
        empty one.

        Fetch AND rebind happen under ``self._lock`` (see __init__), so a
        concurrent :meth:`refresh_entitlement_scope`/:meth:`refresh_grant_scope`
        either completes entirely before this reload's fetch starts, or
        lands entirely after this reload's rebind - never interleaved, which
        is what stops a per-scope write from landing in the dict this reload
        is about to discard.
        """
        async with self._lock:
            entitlement_rows = await load_active(pool)
            grant_rows = await load_active_grants(pool)
            self.load_rows(entitlement_rows)
            self.load_grant_rows(grant_rows)

    def is_guild_premium(self, guild_id, *, now=None):
        """Whether guild ``guild_id`` currently has Yasuho+, evaluated against
        ``now`` (defaults to the real current time, like :func:`is_active`'s
        own default) - a fresh verdict on every call, never a cached one."""
        guild_id = int(guild_id)
        if YASUHO_PLUS_SKU is not None:
            for snapshot in self._guild_skus.get(guild_id, ()):
                if snapshot.sku_id == YASUHO_PLUS_SKU and is_active(
                    snapshot, now=now
                ):
                    return True
        for snapshot in self._guild_grants.get(guild_id, ()):
            if snapshot.product == PRODUCT_YASUHO_PLUS and is_grant_active(
                snapshot, now=now
            ):
                return True
        return False

    def has_comfort_pack(self, user_id, *, now=None):
        """Whether user ``user_id`` currently has the Pack Confort - same
        fresh-verdict-per-call posture as :meth:`is_guild_premium`."""
        user_id = int(user_id)
        if COMFORT_PACK_SKU is not None:
            for snapshot in self._user_skus.get(user_id, ()):
                if snapshot.sku_id == COMFORT_PACK_SKU and is_active(
                    snapshot, now=now
                ):
                    return True
        for snapshot in self._user_grants.get(user_id, ()):
            if snapshot.product == PRODUCT_COMFORT_PACK and is_grant_active(
                snapshot, now=now
            ):
                return True
        return False

    def for_guild(self, guild_id, *, now=None):
        """The effective GuildLimits for this guild: GUILD_PREMIUM or GUILD_FREE."""
        return (
            GUILD_PREMIUM if self.is_guild_premium(guild_id, now=now) else GUILD_FREE
        )

    def for_user(self, user_id, *, now=None):
        """The effective UserLimits for this user: USER_PREMIUM or USER_FREE."""
        return USER_PREMIUM if self.has_comfort_pack(user_id, now=now) else USER_FREE

    async def refresh_grant_scope(self, pool, scope_type, *, guild_id=None, user_id=None):
        """Re-read ONE scope's active grants from the database and update the
        matching map in place.

        Used by ``?premium grant``/``?premium revoke`` (cogs/system/premium.py)
        AFTER their write has already committed, never before - a re-read
        rather than an in-place add/discard, so a grant/revoke and a
        concurrent full :meth:`load` can never disagree about what is in the
        database for this one scope, and a double-grant or an already-revoked
        grant resolves itself from the same source of truth instead of having
        its own special case here. An empty result pops the scope entirely
        (same "absence is the free answer" rule :meth:`load_grant_rows`
        already follows), rather than leaving a stale non-empty entry behind.
        ``list_grants(..., active_only=True)`` filters by expiry AT QUERY
        TIME, same as every other read here - the snapshots this stores are
        still re-checked against ``now`` on every future lookup, so a grant
        that expires later with no further write still turns itself off.

        Fetch AND mutate happen under ``self._lock`` (see __init__ and
        :meth:`load`'s docstring for why).
        """
        if scope_type not in ("guild", "user"):
            raise ValueError(f"unknown scope_type: {scope_type!r}")
        async with self._lock:
            if scope_type == "guild":
                rows = await list_grants(
                    pool, scope_type="guild", guild_id=guild_id, active_only=True
                )
                snapshots = [
                    _GrantSnapshot(
                        grant_id=_get(row, "id"),
                        product=_get(row, "product"),
                        expires_at=_get(row, "expires_at"),
                    )
                    for row in rows
                ]
                if snapshots:
                    self._guild_grants[int(guild_id)] = snapshots
                else:
                    self._guild_grants.pop(int(guild_id), None)
            else:
                rows = await list_grants(
                    pool, scope_type="user", user_id=user_id, active_only=True
                )
                snapshots = [
                    _GrantSnapshot(
                        grant_id=_get(row, "id"),
                        product=_get(row, "product"),
                        expires_at=_get(row, "expires_at"),
                    )
                    for row in rows
                ]
                if snapshots:
                    self._user_grants[int(user_id)] = snapshots
                else:
                    self._user_grants.pop(int(user_id), None)

    async def refresh_entitlement_scope(
        self, pool, scope_type, *, guild_id=None, user_id=None
    ):
        """Re-read ONE scope's active Discord entitlements from the database
        and update the matching map in place - the ENTITLEMENT_* gateway
        handlers' (cogs/system/premium.py) counterpart to
        :meth:`refresh_grant_scope`, called AFTER
        :func:`upsert_entitlement_event` has already committed, never
        before, for the exact same "cache only ever follows a successful
        write" reason. An empty result pops the scope entirely, same
        "absence is the free answer" rule as every loader in this module.
        ``now``-based expiry (the 48h GRACE window included) is still
        re-checked at every future lookup, not baked in here - see the
        class docstring's "LIVE EXPIRY" paragraph.

        Fetch AND mutate happen under ``self._lock`` (see __init__ and
        :meth:`load`'s docstring for why).
        """
        if scope_type not in ("guild", "user"):
            raise ValueError(f"unknown scope_type: {scope_type!r}")
        async with self._lock:
            if scope_type == "guild":
                rows = await load_active_for_guild(pool, guild_id)
                snapshots = [
                    _EntitlementSnapshot(
                        entitlement_id=_get(row, "entitlement_id"),
                        sku_id=int(_get(row, "sku_id")),
                        ends_at=_get(row, "ends_at"),
                        last_synced_at=_get(row, "last_synced_at"),
                    )
                    for row in rows
                ]
                if snapshots:
                    self._guild_skus[int(guild_id)] = snapshots
                else:
                    self._guild_skus.pop(int(guild_id), None)
            else:
                rows = await load_active_for_user(pool, user_id)
                snapshots = [
                    _EntitlementSnapshot(
                        entitlement_id=_get(row, "entitlement_id"),
                        sku_id=int(_get(row, "sku_id")),
                        ends_at=_get(row, "ends_at"),
                        last_synced_at=_get(row, "last_synced_at"),
                    )
                    for row in rows
                ]
                if snapshots:
                    self._user_skus[int(user_id)] = snapshots
                else:
                    self._user_skus.pop(int(user_id), None)


# Ready-to-use default instance for a caller with no bot handy (a script, a
# test). Production code reaches for ``bot.premium`` instead (set in
# core.py's ``Yasuho.__init__``) - the gateway handlers and the periodic
# reconciliation loop added in M3b (cogs/system/premium.py) read and write
# that bot-owned instance, never this module-level one.
premium_limits = EntitlementCache()
