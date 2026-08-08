"""Real-time cache invalidation driven by the Remix dashboard.

The dashboard is a SEPARATE Node process that writes per-guild settings straight
into the SAME Postgres database, then emits::

    SELECT pg_notify('yasuho_dashboard', $1)

with a JSON payload ``{"kind": "...", "guildId": "..."}`` where ``kind`` is one of
``prefix | autorole | modlog | muterole | welcome | starboard | automod |
leveling | rank_card | warn_escalation | verify_role | locale | custom_commands |
twitch | autorooms | music_config``.

ONE kind is USER-scoped rather than guild-scoped and carries ``userId`` in place
of ``guildId``: ``user_settings``, emitted when the dashboard writes somebody's
personal preferences (the ``/preferences`` panel's keys).
See :data:`USER_KINDS`. The bot mirrors those settings in memory (``bot.prefixes`` /
``bot.autoroles`` / ``bot.muteroles``, the ModLog cog's ``_channels`` cache, the
``tools.settings`` LRU for the welcome + automod + modlog_events +
warn_escalation + verify_role + locale + twitch + autorooms + music_* JSONB
blobs, the Starboard cog's ``_config`` cache, the AutoMod cog's ``_settings``
cache for its boolean toggle table, the Leveling cog's three caches - ``_configs``
(level_config scalar knobs), ``_no_xp`` (level_no_xp snapshot) and
``_multipliers`` (xp_multipliers + level_config event columns) - plus its
``_rank_cards`` rank-card style cache, the
CustomCommands cog's ``_cache``/``_uses``/``_cd`` (per-guild command map, usage
counts and per-command cooldown clocks) and the TemporaryRooms cog's
``_hub_index`` (the join-to-create lookup the voice event consults)), so without
this cog it would keep serving the stale in-memory value until the next restart.

This cog LISTENs on the ``yasuho_dashboard`` channel over a DEDICATED asyncpg
connection (kept open for the cog's lifetime, separate from the shared pool) and,
per notification, RE-READS the authoritative value from Postgres and updates the
SAME in-memory structure the bot's own commands mutate - so a change made in the
dashboard takes effect on the very next event, no restart.

Design mirrors the existing house patterns:
* prefix/autorole/muterole updates mirror ``cogs/config/settings.py`` and
  ``cogs/moderation/moderation.py`` (``bot.prefixes[gid] = row`` / ``pop`` etc.);
* the modlog invalidation drops the ModLog cog's negative-cached ``_channels``
  entry, exactly as ``tools/retention.invalidate_guild_caches`` does, AND evicts
  the ``tools.settings`` blob (the dashboard also writes ``modlog_events`` there);
* the welcome and warn_escalation invalidations evict the guild's cached
  ``tools.settings`` blob (same helper retention uses) - both settings live in
  the same JSONB row, so evicting the blob is enough to pick up either key;
* the starboard invalidation re-reads the guild's ``(channel_id, threshold)`` row
  and writes it into the Starboard cog's ``_config`` cache exactly as that cog's
  own ``_apply_set`` / ``starboard_disable`` do (set the tuple, or ``None`` when
  the row is gone);
* the custom_commands invalidation mirrors ``CustomCommands.save_command`` /
  ``.delete_command`` (``cogs/config/customcommands.py``) exactly: pop the
  guild's entry from ``_cache`` (the ``{name: response}`` map) and ``_uses``
  (the usage-count map), and drop every per-command cooldown clock for that
  guild from ``_cd`` - the SAME three lines those two methods run on their own
  writes, so a dashboard change is indistinguishable from a bot-side one;
* the autorooms invalidation evicts the settings blob (the hub list lives in the
  same JSONB row) AND rebuilds the TemporaryRooms cog's derived ``_hub_index``,
  the exact pair of lines ``cogs/system/events.py`` runs when a guild rejoins;
* the supervised background task started in ``__init__`` via
  ``bot.loop.create_task`` with a done-callback mirrors
  ``cogs/system/webstats.py``.

EVERY successful connect - the first one included - triggers a FULL resync
(:func:`resync_all`) of every structure the invalidators above maintain. That is
not belt-and-braces: Postgres does not queue NOTIFY for an absent listener, it
DROPS it. So every dashboard write made while no LISTEN was registered is lost
forever - the bot would keep serving the pre-gap value from memory until an LRU
eviction or the next restart, with nothing anywhere to hint at it. The changed
ids are precisely what is unknown, so the resync is deliberately whole-cache
rather than per-guild.

The FIRST connection is covered too because boot has the same gap, not because
it might: the caches are primed inside setup_hook, which runs BEFORE the gateway
connects, while this LISTEN is only registered after wait_until_ready returns -
i.e. after IDENTIFY, the guild stream and member chunking. On a 1000+ guild bot
that is tens of seconds to minutes, on every start, and main is production with
continuous deploy. The extra cost is one duplicate pass over caches that are cold
anyway.

The resync is NOT uniform, because the caches are not:

* EAGERLY-primed maps read synchronously with no read-through - ``bot.prefixes``,
  ``bot.autoroles``, ``bot.muteroles``, ``bot.blacklist`` (``Yasuho.load_eager_caches``)
  and the Leveling cog's ``_configs`` (``reload_configs``) - are RELOADED by
  re-running the startup queries. Clearing them would not be a cache miss, it
  would be a WRONG ANSWER: an empty ``bot.prefixes`` silently resets every
  custom-prefix guild to the default, an empty ``_configs`` silently turns
  leveling off bot-wide.
* READ-THROUGH caches - the ``tools.settings`` LRU (both scopes), ModLog's
  ``_channels``, Starboard's ``_config``, AutoMod's ``_settings``, Leveling's
  ``_no_xp`` / ``_multipliers`` / ``_rank_cards``, CustomCommands' ``_cache`` /
  ``_uses`` - are simply emptied; the next access reloads from the DB.
* DERIVED indexes - TemporaryRooms' ``_hub_index`` - are REBUILT the way the cog
  builds them (``reload_hub_index``), since nothing would ever re-derive them.

Two caches are deliberately left alone: CustomCommands' ``_cd`` (per-command
cooldown CLOCKS, not configuration - dropping them would hand every member a free
re-use) and Leveling's ``_period_markers`` (season rollover bookkeeping, not
dashboard state, and cold-miss-safe by design).

The cog also writes the ``bot_heartbeat`` row every 30s over the MAIN pool, which
is what lets the dashboard tell "the bot is down" from "the bot is up but its
dashboard listener is down": the beat keeps landing while this dedicated listen
connection is dead, carrying ``listening = false`` for exactly that window.

Everything is defensive: a malformed / unknown payload is a no-op, a missing cog
or dict is a no-op, a resync step that fails is logged and the others still run,
and a dropped listen connection is re-established with backoff without ever
crashing the bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

import asyncpg
from discord.ext import commands, tasks

from tools import settings
from tools.config_loader import config_loader

log = logging.getLogger(__name__)

# The Postgres NOTIFY channel the dashboard publishes on (see module docstring).
CHANNEL = "yasuho_dashboard"

# The settings the dashboard can change; anything else is ignored.
VALID_KINDS = frozenset(
    {
        "prefix",
        "autorole",
        "modlog",
        "muterole",
        "welcome",
        "starboard",
        "automod",
        "leveling",
        "rank_card",
        "warn_escalation",
        "verify_role",
        "locale",
        "custom_commands",
        "twitch",
        "autorooms",
        "music_config",
        "user_settings",
    }
)

# The kinds whose payload names a USER, not a guild -- they carry ``userId``.
#
# ``user_settings`` exists because ``tools.settings`` treats its LRU as
# AUTHORITATIVE for this single-process bot: a dashboard write straight to
# ``user_settings`` is invisible here until that user's blob happens to be
# evicted (cap 8192). The user would flip a preference on the web, see it saved,
# and watch the bot keep the old behaviour. This kind is what closes that.
USER_KINDS = frozenset({"user_settings"})

# Reconnect backoff bounds for the listen connection supervisor.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0
# How often the bot_heartbeat row is refreshed, over the MAIN pool. The
# dashboard's contract is "older than 90s => the bot is offline", i.e. three
# missed beats, so a single slow write or a one-off blip never flips the badge.
HEARTBEAT_SECONDS = 30.0
HEARTBEAT_STALE_SECONDS = 90
# How long cog_unload gives the final beat. Without it that write is bounded only
# by pool.acquire (which waits on the pool queue with NO bound) plus the pool's
# command_timeout (core.main: 60s), so a wedged pool could hold a clean shutdown
# open over a single liveness beat. Same call, and same reasoning, as
# botstats.UNLOAD_FLUSH_TIMEOUT - only here losing the write costs nothing at
# all: the row simply ages past HEARTBEAT_STALE_SECONDS and the dashboard says
# "offline", which by then is true.
UNLOAD_BEAT_TIMEOUT = 5
# How often to actively probe the listen connection for liveness. A dropped TCP
# socket is not always reflected by ``is_closed()`` until a query is attempted,
# so a light ``SELECT 1`` on this cadence detects a dead connection promptly.
_KEEPALIVE_INTERVAL = 30.0


def _parse_payload(payload):
    """Parse a NOTIFY payload defensively into ``(kind, scope_id)`` or ``None``.

    Rejects anything that is not a JSON object carrying a known ``kind`` and the
    numeric id that kind's SCOPE requires -- ``userId`` for a kind in
    :data:`USER_KINDS`, ``guildId`` for every other. Both are accepted as int or
    numeric string, since JS serialises large ids as strings. Never raises.

    The id field is chosen by the kind and NOT by which key happens to be
    present, so a guild kind carrying only ``userId`` (or the reverse) is
    rejected rather than silently invalidating the wrong scope.
    """
    if not isinstance(payload, (str, bytes, bytearray)):
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    if kind not in VALID_KINDS:
        return None
    id_field = "userId" if kind in USER_KINDS else "guildId"
    try:
        scope_id = int(data.get(id_field))
    except (TypeError, ValueError):
        return None
    return kind, scope_id


# ---------------------------------------------------------------------------
# Invalidators: RE-READ the authoritative value and update the SAME in-memory
# structure the bot's own commands mutate. Each guards a missing dict / cog so a
# stray notification can never crash the loop.
# ---------------------------------------------------------------------------


def _eager_cache_lock(bot):
    """The bot's eager-cache lock, or a no-op for a bot that has none.

    The three invalidators below mutate the map OBJECT (``bot.prefixes[gid] =
    ...``) while :meth:`core.Yasuho.load_eager_caches` REBINDS the attribute to a
    freshly fetched map. Between that method's fetch and its rebind there is a
    window in which a write here lands on a dict about to be discarded, losing
    the dashboard's change until the next write to the same guild - and
    ``resync_all`` runs that reload from this very cog, right after a gap during
    which notifications piled up in the dashboard's database. Both sides take
    this lock, so the two orderings are the only ones left.

    ``getattr`` guard for the same reason ``_resync_eager_caches`` has one: a
    stand-in bot or an older core simply has no lock, and must not crash.
    """
    lock = getattr(bot, "eager_cache_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


async def _refresh_eager_entry(bot, attr, query, gid):
    """Re-read one guild's row and set/pop it in the eager map named ``attr``.

    The single body behind the three eager invalidators below, which differ only
    in map, table and column. Under the lock throughout, and the map is resolved
    from the bot AFTER the fetch (never from a local captured before it), so a
    whole-map reload that ran while this was waiting or fetching cannot leave the
    write on a dict nobody reads any more.
    """
    if getattr(bot, attr, None) is None:
        return
    async with _eager_cache_lock(bot):
        row = await bot.db_pool.fetchval(query, gid)
        cache = getattr(bot, attr, None)
        if cache is None:  # defensive: the attribute cannot vanish in practice
            return
        if row is not None:
            cache[gid] = row
        else:
            cache.pop(gid, None)


async def _invalidate_prefix(bot, gid):
    """Mirror ``cogs/config/settings.py``: set ``bot.prefixes[gid]`` or pop it."""
    await _refresh_eager_entry(
        bot, "prefixes", "SELECT prefix FROM prefixes WHERE guild_id = $1", gid
    )


async def _invalidate_autorole(bot, gid):
    """Mirror ``settings.py`` autorole set/remove: ``bot.autoroles[gid]`` / pop."""
    await _refresh_eager_entry(
        bot, "autoroles", "SELECT role_id FROM autorole WHERE guild_id = $1", gid
    )


async def _invalidate_muterole(bot, gid):
    """Mirror ``moderation.py`` mute-role handling: ``bot.muteroles[gid]`` / pop."""
    await _refresh_eager_entry(
        bot, "muteroles", "SELECT role_id FROM muterole WHERE guild_id = $1", gid
    )


async def _invalidate_modlog(bot, gid):
    """Refresh BOTH stores a dashboard mod-log write can touch, for one guild.

    The mod-log CHANNEL lives in the ``modlog`` table and is cached in the
    ModLog cog's negative-cached ``_channels`` dict (``None`` means "looked up,
    not configured"); popping the guild's entry forces ``get_log_channel`` to
    re-query on the next event - the same eviction
    ``tools/retention.invalidate_guild_caches`` performs. The dashboard's
    "Moderation" section ALSO writes the guild_settings JSONB key
    ``modlog_events`` (which events get logged), served from the
    ``tools.settings`` LRU via ``settings.get_guild``/``_get_events``
    (``cogs/moderation/modlog.py``) - so this invalidator evicts that blob too,
    unconditionally, so an events-only change takes effect immediately even
    without touching the channel. No ModLog cog loaded => the ``_channels`` pop
    is skipped, but the settings eviction still runs.
    """
    settings.invalidate_guild(gid)
    cog = bot.get_cog("ModLog")
    if cog is None:
        return
    channels = getattr(cog, "_channels", None)
    if isinstance(channels, dict):
        channels.pop(gid, None)


async def _invalidate_welcome(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    Welcome state lives under the ``guild_settings`` JSONB key ``'welcome'`` and is
    served from the ``tools.settings`` LRU. ``invalidate_guild`` drops the guild's
    cached blob (the same helper retention uses), so the next
    ``settings.get_guild(..., 'welcome', ...)`` re-reads the authoritative row.
    """
    settings.invalidate_guild(gid)


async def _invalidate_warn_escalation(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    Warn escalation policy lives under the ``guild_settings`` JSONB key
    ``'warn_escalation'`` (``tools/warn_escalation.py`` ``SETTINGS_KEY``) and is
    read via ``cogs.moderation.modactions.load_escalation_policy`` -> ``settings.get_guild``,
    served from the SAME ``tools.settings`` LRU as welcome/automod/modlog_events.
    ``invalidate_guild`` drops the guild's cached blob (the same helper retention
    uses), so the next read re-fetches the authoritative row instead of serving a
    stale policy until the next restart.
    """
    settings.invalidate_guild(gid)


async def _invalidate_twitch(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    The Twitch go-live alert config lives under the ``guild_settings`` JSONB key
    ``'twitch'`` (``cogs/config/twitch.py`` reads it via ``settings.get_guild(...,
    'twitch', ...)``), served from the SAME ``tools.settings`` LRU as
    welcome/automod/warn_escalation. ``invalidate_guild`` drops the guild's cached
    blob so the next go-live re-reads the authoritative config. NB: the watchlist
    (``twitch_alert`` table) is read straight from the DB on each go-live and is
    not cached, so the dashboard's watchlist writes need no invalidation.
    """
    settings.invalidate_guild(gid)


async def _invalidate_verify_role(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    The verification role lives under the ``guild_settings`` JSONB key
    ``'verify_role'`` and is read via ``settings.get_guild(..., 'verify_role',
    None)`` in ``cogs/config/verification.py`` (both the ``/verify setup``
    command and the persistent ``verify_button`` view re-read it on every
    click), served from the SAME ``tools.settings`` LRU as
    welcome/automod/modlog_events/warn_escalation. ``invalidate_guild`` drops
    the guild's cached blob (the same helper retention uses), so the very next
    click re-reads the authoritative row instead of granting a stale role
    until the next restart.
    """
    settings.invalidate_guild(gid)


async def _invalidate_locale(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    The server language lives under the ``guild_settings`` JSONB key
    ``'locale'`` and is read via ``settings.get_guild(..., 'locale', ...)``,
    served from the SAME ``tools.settings`` LRU as
    welcome/automod/modlog_events/warn_escalation/verify_role.
    ``invalidate_guild`` drops the guild's cached blob (the same helper
    retention uses), so the next read re-fetches the authoritative row instead
    of serving a stale language until the next restart.
    """
    settings.invalidate_guild(gid)


async def _invalidate_starboard(bot, gid):
    """Refresh the Starboard cog's ``_config`` entry from the authoritative row.

    The Starboard cog caches per-guild ``(channel_id, threshold)`` in ``_config``
    - a NEGATIVE cache where ``None`` means "looked up, not configured"
    (``cogs/config/starboard.py`` ``get_config``, l.152-163). It keeps that cache
    coherent on its own writes: ``_apply_set`` sets the tuple
    (``cogs/config/starboard.py:176``) and ``starboard_disable`` sets ``None``
    (``cogs/config/starboard.py:266``). Mirror that exactly here - re-read the row
    and store the tuple when configured, else ``None`` (a dashboard "disable"
    deletes the row). No cog loaded / no cache dict => safe no-op.
    """
    cog = bot.get_cog("Starboard")
    if cog is None:
        return
    cache = getattr(cog, "_config", None)
    if not isinstance(cache, dict):
        return
    row = await bot.db_pool.fetchrow(
        "SELECT channel_id, threshold FROM starboard WHERE guild_id = $1", gid
    )
    cache[gid] = (row["channel_id"], row["threshold"]) if row else None


async def _invalidate_automod(bot, gid):
    """Refresh BOTH AutoMod stores for a guild after a dashboard write.

    AutoMod config is split across two stores, so this invalidator touches both:

    * The ``guild_settings`` JSONB keys ``antiinvite`` / ``automod_action`` /
      ``automod_exempt_roles`` / ``automod_exempt_channels`` are served from the
      ``tools.settings`` LRU (``cogs/moderation/automod.py`` reads them via
      ``settings.get_guild`` - l.172-197 / l.285-297 / l.417). ``invalidate_guild``
      evicts the guild's cached blob (the SAME helper the welcome path uses), so
      the next ``settings.get_guild`` re-reads the authoritative row. This part is
      unconditional - the blob is cached whether or not the AutoMod cog object is
      currently loaded.
    * The ``automod`` TABLE booleans (``antilink`` / ``antispam``) ARE cached, in
      the AutoMod cog's ``_settings`` dict - a NEGATIVE cache holding the fetched
      Record, or ``None`` when the guild has no row
      (``cogs/moderation/automod.py`` ``get_settings``, l.136-143). Mirror that
      exactly: re-read the row and store it under ``gid`` (Record, or ``None``), so
      the next ``on_message`` sees the dashboard's new toggles. No cog loaded / no
      cache dict => safe no-op (the settings eviction above still ran).
    """
    settings.invalidate_guild(gid)
    cog = bot.get_cog("AutoMod")
    if cog is None:
        return
    cache = getattr(cog, "_settings", None)
    if not isinstance(cache, dict):
        return
    row = await bot.db_pool.fetchrow(
        "SELECT antilink, antispam FROM automod WHERE guild_id = $1", gid
    )
    cache[gid] = row


async def _invalidate_leveling(bot, gid):
    """Refresh EVERY leveling cache the Leveling cog keeps, for one guild.

    Leveling config is spread across THREE in-memory caches on the Leveling cog
    (``cogs/community/leveling/leveling.py``), so this invalidator refreshes each by
    calling the cog's OWN public refresh hook - the SAME method
    ``cogs/community/leveling/level_config_ui.py`` invokes after every leveling write, so a
    dashboard change takes effect on the very next message / voice sweep, no
    restart:

    * ``self._configs`` - a plain dict of ``cogs.community.leveling.engine.LevelConfig``, the
      on_message hot-path mirror of the level_config scalar knobs (enabled,
      cooldown, xp band, announce, voice_xp; leveling.py l.252) - is refreshed by
      ``refresh_guild_config`` (leveling.py l.347), which re-reads level_config
      and re-resolves the cached config (or drops the guild when disabled).
    * ``self._no_xp`` - a ``BoundedLRU`` of ``cogs.community.leveling.engine.NoXpSnapshot``
      (leveling.py l.268) - is refreshed by ``refresh_no_xp_snapshot``
      (leveling.py l.526), which re-reads the guild's ``level_no_xp`` rows.
    * ``self._multipliers`` - a ``BoundedLRU`` of
      ``cogs.community.leveling.engine.MultiplierSnapshot`` (leveling.py l.276) - is refreshed by
      ``refresh_multiplier_snapshot`` (leveling.py l.562), which re-reads the
      guild's ``xp_multipliers`` rows AND the ``level_config`` event columns.

    ``level_rewards`` rows and the ``level_config.rewards_mode`` column are NOT
    cached (``cogs/community/leveling/level_rewards.py`` reads both fresh on each level-up -
    l.19-22 / ``_fetch_mode``), so a dashboard rewards/mode change needs no
    invalidation here. Leveling config is served from these dict / LRU caches, NOT
    the ``tools.settings`` LRU (that LRU is only a legacy read-through fallback in
    ``refresh_guild_config``, which a level_config row - always written by the
    dashboard - makes moot), so no ``settings.invalidate_guild`` call is needed.
    No Leveling cog loaded, or a missing refresh method, is a safe no-op.
    """
    cog = bot.get_cog("Leveling")
    if cog is None:
        return
    for method_name in (
        "refresh_guild_config",
        "refresh_no_xp_snapshot",
        "refresh_multiplier_snapshot",
    ):
        refresh = getattr(cog, method_name, None)
        if callable(refresh):
            await refresh(gid)


async def _invalidate_rank_card(bot, gid):
    """Drop the Leveling cog's cached rank-card style for one guild.

    The dashboard writes the ``rank_cards`` row directly (background blob and/or
    accent) using the SAME statements ``cogs/community/leveling/rank_card.py`` exposes, then
    notifies with this kind. The Leveling cog serves that row from its
    ``_rank_cards`` BoundedLRU (``cogs/community/leveling/leveling.py``), so without this
    a guild that just uploaded a background would keep getting the stock card
    until the entry aged out under cache pressure - possibly never.

    A plain eviction through the cog's own ``invalidate_rank_card`` hook, NOT
    the eager re-read the leveling/starboard invalidators do: /rank is a rare,
    human-paced command, so the lookup is cheaper paid on the next actual render
    than on every notification - and a background-BYTES-only change does not
    move any cached metadata anyway, which only a re-read on render can see. No
    Leveling cog loaded, or a missing hook, is a safe no-op.
    """
    cog = bot.get_cog("Leveling")
    if cog is None:
        return
    invalidate = getattr(cog, "invalidate_rank_card", None)
    if callable(invalidate):
        invalidate(gid)


async def _invalidate_custom_commands(bot, gid):
    """Drop the CustomCommands cog's per-guild caches, mirroring its own writes.

    ``CustomCommands.save_command`` and ``.delete_command``
    (``cogs/config/customcommands.py``) both run these exact three lines after
    every write of their own:

        self._cache.pop(guild_id, None)
        self._uses.pop(guild_id, None)
        self._cd = {k: v for k, v in self._cd.items() if k[0] != guild_id}

    ``_cache`` is the lazily-loaded ``{name: response}`` map
    (``get_custom_commands`` re-fetches on the next miss), ``_uses`` is the
    parallel ``{name: uses}`` map for the panel's usage display, and ``_cd``
    holds ``(guild_id, name, user_id) -> expiry`` per-command cooldown clocks -
    stale entries there would otherwise let a member dodge (or get stuck on) a
    cooldown the dashboard just changed. Mirrored verbatim here so a dashboard
    add/edit/delete takes effect on the very next message, no restart. No
    CustomCommands cog loaded is a safe no-op.
    """
    cog = bot.get_cog("CustomCommands")
    if cog is None:
        return
    cache = getattr(cog, "_cache", None)
    if isinstance(cache, dict):
        cache.pop(gid, None)
    uses = getattr(cog, "_uses", None)
    if isinstance(uses, dict):
        uses.pop(gid, None)
    cd = getattr(cog, "_cd", None)
    if isinstance(cd, dict):
        cog._cd = {k: v for k, v in cd.items() if k[0] != gid}


async def _invalidate_autorooms(bot, gid):
    """Evict the settings blob AND rebuild the Rooms cog's derived hub index.

    Autoroom hubs live under the ``guild_settings`` JSONB key ``'autorooms'`` (a
    list of hub dicts) served by the ``tools.settings`` LRU, which
    ``TemporaryRooms._load_hubs`` reads through ``settings.get_guild``
    (``cogs/config/rooms.py``) - so the eviction is what makes the dashboard's
    write visible at all. It is NOT sufficient on its own: the cog also keeps a
    DERIVED in-memory ``_hub_index`` (``{guild_id: {hub_channel_id: hub}}``) that
    ``on_voice_state_update`` consults on EVERY voice event and that is never
    re-read from settings - it is only ever rewritten by ``_index_guild``. Left
    stale, a hub the dashboard just retargeted (or a template/limit it changed)
    would keep spinning up rooms from the OLD config until the next restart.

    Hence both steps, in this order: evict first so the re-read returns the
    authoritative row (rebuilding before the eviction would just re-index the
    stale blob), then ``_load_hubs`` + ``_index_guild`` - the exact pair
    ``cogs/system/events.py`` runs when a guild rejoins, and what the cog's own
    ``_save_hubs`` does after every bot-side write. No TemporaryRooms cog loaded
    is a safe no-op (the settings eviction above still ran).
    """
    settings.invalidate_guild(gid)
    cog = bot.get_cog("TemporaryRooms")
    if cog is None:
        return
    hubs = await cog._load_hubs(gid)
    cog._index_guild(gid, hubs)


async def _invalidate_music_config(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    The per-guild music configuration (``music_default_volume``,
    ``music_autoplay``, ``music_voteskip``, ``music_dj_role``,
    ``music_sponsorblock`` - see ``cogs/music/guild_config.py``) lives under those
    keys in the SAME ``guild_settings`` JSONB row as welcome / automod /
    modlog_events / warn_escalation, served from the SAME ``tools.settings`` LRU.
    Evicting the blob is therefore enough for all five keys at once.

    There is deliberately NO derived music cache to refresh alongside it: the cog
    reads through ``tools.settings`` at decision points that run at command /
    player-birth frequency (never per message), so the LRU is the only cache in
    the path and a second one could only ever drift from it.
    """
    settings.invalidate_guild(gid)


async def _invalidate_user_settings(bot, uid):
    """Drop ONE user's ``tools.settings`` blob after a dashboard write.

    Unlike every other invalidator here there is nothing to re-read: the LRU is
    read-through, so discarding the entry is enough and the next ``get_user``
    reloads the authoritative row. ``invalidate_user`` exists for exactly this
    -- its own docstring calls it "after an out-of-band transactional write".

    Deliberately does NOT re-read and re-seed: ``tools.privacy`` also writes one
    of these keys (``avatar_history_tracking``) under an advisory lock, so
    re-seeding from a read taken outside that lock could resurrect a value it
    had just changed. Dropping the entry cannot.
    """
    settings.invalidate_user(uid)


_INVALIDATORS = {
    "prefix": _invalidate_prefix,
    "autorole": _invalidate_autorole,
    "muterole": _invalidate_muterole,
    "modlog": _invalidate_modlog,
    "welcome": _invalidate_welcome,
    "starboard": _invalidate_starboard,
    "automod": _invalidate_automod,
    "leveling": _invalidate_leveling,
    "rank_card": _invalidate_rank_card,
    "warn_escalation": _invalidate_warn_escalation,
    "verify_role": _invalidate_verify_role,
    "locale": _invalidate_locale,
    "custom_commands": _invalidate_custom_commands,
    "twitch": _invalidate_twitch,
    "autorooms": _invalidate_autorooms,
    "music_config": _invalidate_music_config,
    "user_settings": _invalidate_user_settings,
}


async def dispatch(bot, payload):
    """Parse ``payload`` and run the matching invalidator. Returns the handled
    ``kind`` on success, else ``None``.

    Pure and side-effect-scoped to the caches: malformed JSON, an unknown kind,
    or a bad guild id are ignored, and an invalidator that raises is logged and
    swallowed so a single bad notification can never take down the listener.
    """
    parsed = _parse_payload(payload)
    if parsed is None:
        return None
    kind, scope_id = parsed
    invalidator = _INVALIDATORS.get(kind)
    if invalidator is None:  # defensive: VALID_KINDS and _INVALIDATORS agree
        return None
    try:
        await invalidator(bot, scope_id)
    except Exception:
        log.exception("dashboard_sync: invalidation failed for kind=%s", kind)
        return None
    scope = "user" if kind in USER_KINDS else "guild"
    log.debug("dashboard_sync: invalidated kind=%s %s=%s", kind, scope, scope_id)
    return kind


# ---------------------------------------------------------------------------
# Reconnect resync: what the invalidators cannot do, because the ids are lost.
#
# One step per OWNER of state (the bot itself, then one per cog), each grounded
# in how that owner's own cold start / write path maintains the structure. Every
# step is independent and individually guarded: a cog that is not loaded, or one
# whose reload hits a DB error, must not stop the others from being resynced.
# ---------------------------------------------------------------------------


async def _resync_eager_caches(bot):
    """RELOAD (never clear) the bot-level maps primed once in setup_hook.

    ``Yasuho.load_eager_caches`` re-runs the very queries setup_hook runs, for
    ``bot.prefixes`` / ``bot.blacklist`` / ``bot.autoroles`` / ``bot.muteroles``.
    These are read synchronously on hot paths with NO read-through, so an absent
    key is an ANSWER ("no custom prefix", "not blacklisted"), not a miss: they
    can only be rebuilt from the DB. Guarded by getattr so a bot object without
    the seam (a stand-in, an older core) is a clean skip rather than a crash.
    """
    loader = getattr(bot, "load_eager_caches", None)
    if callable(loader):
        await loader()


async def _resync_settings_cache(bot):
    """Empty the ``tools.settings`` LRU, both scopes.

    Read-through by construction, so clearing is always safe and is the only
    option here: the blobs behind welcome / automod / modlog_events /
    warn_escalation / verify_role / locale / twitch / autorooms / music_* and
    every per-user preference belong to ids we cannot enumerate after a dropped
    NOTIFY. The next ``get_guild`` / ``get_user`` re-reads its row.
    """
    settings.invalidate_all()


async def _resync_modlog(bot):
    """Empty ModLog's ``_channels`` negative cache (read-through in get_log_channel)."""
    cog = bot.get_cog("ModLog")
    if cog is None:
        return
    channels = getattr(cog, "_channels", None)
    if isinstance(channels, dict):
        channels.clear()


async def _resync_starboard(bot):
    """Empty Starboard's ``_config`` negative cache (read-through in get_config)."""
    cog = bot.get_cog("Starboard")
    if cog is None:
        return
    config = getattr(cog, "_config", None)
    if isinstance(config, dict):
        config.clear()


async def _resync_automod(bot):
    """Empty AutoMod's ``_settings`` negative cache (read-through in get_settings).

    The JSONB half of AutoMod's config rides the settings LRU, which
    :func:`_resync_settings_cache` has already emptied.
    """
    cog = bot.get_cog("AutoMod")
    if cog is None:
        return
    cache = getattr(cog, "_settings", None)
    if isinstance(cache, dict):
        cache.clear()


async def _resync_leveling(bot):
    """RELOAD ``_configs``; empty the three read-through leveling caches.

    ``_configs`` is the odd one out and the reason this step is not a loop of
    ``.clear()``: ``Leveling.get_config`` is a plain synchronous dict lookup
    where a missing guild MEANS "leveling is off" - clearing it would silently
    stop XP bot-wide, with no read-through to heal it. ``reload_configs`` is the
    same whole-map rebuild the cog runs at load. ``_no_xp`` / ``_multipliers`` /
    ``_rank_cards`` are genuine read-through BoundedLRUs (``ensure_*`` /
    ``refresh_*`` reload on a miss), so emptying them is enough.

    ``_period_markers`` is deliberately untouched: it is season-rollover
    bookkeeping, not dashboard state, and is cold-miss-safe by design.
    """
    cog = bot.get_cog("Leveling")
    if cog is None:
        return
    reload_configs = getattr(cog, "reload_configs", None)
    if callable(reload_configs):
        await reload_configs()
    for attr in ("_no_xp", "_multipliers", "_rank_cards"):
        cache = getattr(cog, attr, None)
        clear = getattr(cache, "clear", None)
        if callable(clear):
            clear()


async def _resync_custom_commands(bot):
    """Empty CustomCommands' ``_cache`` / ``_uses`` (read-through in get_custom_commands).

    ``_cd`` is deliberately NOT cleared: those are per-(guild, name, user)
    cooldown CLOCKS, not configuration. The per-guild invalidator drops a guild's
    clocks because that guild's commands (and their cooldown values) just
    changed; a reconnect says nothing of the sort, and wiping every clock would
    hand every member of every guild one free re-use of every command.
    """
    cog = bot.get_cog("CustomCommands")
    if cog is None:
        return
    for attr in ("_cache", "_uses"):
        cache = getattr(cog, attr, None)
        if isinstance(cache, dict):
            cache.clear()


async def _resync_rooms(bot):
    """REBUILD TemporaryRooms' derived ``_hub_index`` from the authoritative rows.

    ``reload_hub_index`` is the cog's own whole-index build (cog_load's, minus the
    one-shot legacy migration): a derived index is never re-read from settings,
    so it must be rebuilt rather than dropped, and rebuilding only the guilds
    already in it would miss a guild that got its FIRST hub during the gap.

    It also RAISES on a failed read instead of installing an empty index, which
    is why this step can be a plain await: an empty ``_hub_index`` is not a miss,
    it is the answer "this guild has no hubs" for every guild at once. A step
    that raises is reported as NOT done by :func:`resync_all` and the live index
    is left alone.

    Ordered after :func:`_resync_settings_cache` for consistency with
    ``_invalidate_autorooms``, but NOT for the same reason: that per-guild path
    goes through ``_load_hubs`` -> ``settings.get_guild`` and would genuinely
    re-index the stale blob, whereas this whole-index rebuild reads
    ``guild_settings`` straight off the pool and never touches the LRU. Stated
    plainly so a later refactor does not trust an ordering claim that is not
    true of this step.
    """
    cog = bot.get_cog("TemporaryRooms")
    if cog is None:
        return
    reload_index = getattr(cog, "reload_hub_index", None)
    if callable(reload_index):
        await reload_index()


# Ordered: the settings LRU is emptied early, before any step that could read
# through it (see _resync_rooms on what that ordering does and does not buy).
_RESYNC_STEPS = (
    ("eager_caches", _resync_eager_caches),
    ("settings", _resync_settings_cache),
    ("modlog", _resync_modlog),
    ("starboard", _resync_starboard),
    ("automod", _resync_automod),
    ("leveling", _resync_leveling),
    ("custom_commands", _resync_custom_commands),
    ("rooms", _resync_rooms),
)


async def resync_all(bot):
    """Run every resync step; return the names of the ones that succeeded.

    Never raises: a step that fails is logged and the rest still run, because
    each owns a different cache and there is no reason one cog's DB blip should
    leave the others stale. The caller reports the result on one INFO line.

    The returned list is a real success list, which is why the two RELOAD seams
    (``Leveling.reload_configs``, ``TemporaryRooms.reload_hub_index``) raise on a
    failed read rather than logging it themselves: a step that swallowed its own
    exception would be named on the "resynced" line even though its cache is
    untouched, and the log would assert the opposite of what happened.
    """
    done = []
    for name, step in _RESYNC_STEPS:
        try:
            await step(bot)
        except Exception:
            log.exception("dashboard_sync: resync step %r failed", name)
        else:
            done.append(name)
    return done


# ---------------------------------------------------------------------------
# Heartbeat: one row the dashboard polls to know the bot is alive AND listening.
# ---------------------------------------------------------------------------


def _resolve_ref(git_dir, ref):
    """Resolve a symbolic ref to its sha, loose file first then packed-refs."""
    loose = os.path.join(git_dir, *ref.split("/"))
    if os.path.exists(loose):
        with open(loose, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    packed = os.path.join(git_dir, "packed-refs")
    if not os.path.exists(packed):
        return None
    with open(packed, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith(("#", "^")):
                continue
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    return None


def _git_short_hash():
    """The running commit's short hash, or ``None``. Never raises.

    Read once at cog load, by READING ``.git`` rather than forking git. The house
    rule is that nothing blocks the event loop (every other git/pg_dump call in
    the tree goes through ``asyncio.create_subprocess_exec`` - tools/backup.py,
    cogs/system/admin.py), and this one runs in ``__init__``: harmless at boot,
    where setup_hook precedes the websocket, but ``?reload dashboard_sync`` runs
    it on a LIVE loop, where a slow fork would freeze every shard for up to the
    old 5s timeout. Two small file reads have no such ceiling and need no async
    seam at all.

    Handles a detached HEAD (a raw sha in the file), a packed ref, and a ``.git``
    that is a FILE pointing elsewhere (worktrees, submodules). Anything else - no
    ``.git``, an unreadable file, a value that is not a hex sha - yields ``None``:
    the column is nullable precisely because "unknown" is an acceptable answer.
    """
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    try:
        git_dir = os.path.join(repo_root, ".git")
        if os.path.isfile(git_dir):  # worktree / submodule: "gitdir: <path>"
            with open(git_dir, "r", encoding="utf-8") as handle:
                pointer = handle.read().strip()
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = pointer.partition(":")[2].strip()
            if not os.path.isabs(git_dir):
                git_dir = os.path.join(repo_root, git_dir)
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8") as handle:
            head = handle.read().strip()
        sha = _resolve_ref(git_dir, head[5:].strip()) if head.startswith("ref:") else head
    except Exception:
        return None
    if not sha or len(sha) < 7:
        return None
    try:
        int(sha, 16)
    except ValueError:
        return None
    return sha[:7]


async def write_heartbeat(pool, listening, version):
    """Upsert THE single ``bot_heartbeat`` row (id = 1).

    One statement (asyncpg is mono-statement), over the MAIN pool rather than the
    dedicated listen connection - that is the whole point: the beat must keep
    landing while the listen connection is down, which is exactly when
    ``listening`` is false and the dashboard most needs to say "the bot is up,
    its dashboard link is not".
    """
    await pool.execute(
        "INSERT INTO bot_heartbeat (id, updated_at, listening, version) "
        "VALUES (1, now(), $1, $2) "
        "ON CONFLICT (id) DO UPDATE "
        "SET updated_at = now(), listening = $1, version = $2",
        bool(listening),
        version,
    )


class DashboardSync(commands.Cog):
    """LISTENs on Postgres NOTIFY and invalidates the bot's in-memory caches."""

    def __init__(self, bot):
        self.bot = bot
        self._conn = None
        self._closing = False
        self._supervisor = None
        # Strong refs to per-notification handler tasks so the loop can't GC one
        # mid-run (the sponsorblock / core startup-backup pattern).
        self._handlers = set()
        # True once a LISTEN has been successfully registered at least once.
        # BOTH cases resync (see _schedule_resync); this only names which one it
        # was in the log, since the gaps have different shapes: setup_hook to
        # READY at boot, socket death to reconnect afterwards.
        self._connected_once = False
        # The in-flight resync, so a connect-then-die loop schedules one sweep
        # rather than one per cycle (see _schedule_resync).
        self._resync_task = None
        # Mirrored into bot_heartbeat.listening: true only while a registered
        # LISTEN is believed live.
        self._listening = False
        # The running commit, read ONCE here at cog load (never per beat).
        self._version = _git_short_hash()

        # The heartbeat runs over the MAIN pool and is deliberately started even
        # when the sync below is disabled: "the bot is alive but not listening"
        # is a true and useful thing for the dashboard to be told.
        self._heartbeat.start()

        # Resolve the DSN the same way core.py does. Missing config -> the cog
        # loads but stays idle (mirrors webstats' top.gg fallback guard).
        self._dsn = config_loader.get("Database", "PostgreSQL", fallback=None)
        if not self._dsn:
            log.info("dashboard_sync: no PostgreSQL DSN configured; sync disabled.")
            return

        self._supervisor = self.bot.loop.create_task(self._supervise())

        def _on_supervisor_done(task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.error("dashboard_sync: supervisor exited unexpectedly: %s", exc)

        self._supervisor.add_done_callback(_on_supervisor_done)

    # -- teardown -------------------------------------------------------
    async def cog_unload(self):
        self._closing = True
        self._heartbeat.cancel()
        if self._supervisor is not None:
            self._supervisor.cancel()
        for task in list(self._handlers):
            task.cancel()
        await self._teardown_connection()
        # One last beat, best-effort, AFTER the teardown has cleared _listening:
        # a reload/shutdown otherwise leaves the last row claiming listening =
        # true until it ages past the staleness threshold, which is the one
        # window where the dashboard would show a link that no longer exists.
        # Bounded (UNLOAD_BEAT_TIMEOUT): TimeoutError is an Exception, so the
        # handler below already covers it.
        try:
            await asyncio.wait_for(
                write_heartbeat(self.bot.db_pool, False, self._version),
                UNLOAD_BEAT_TIMEOUT,
            )
        except Exception:
            log.debug("dashboard_sync: final heartbeat write failed", exc_info=True)

    async def _teardown_connection(self):
        # The listener is gone the moment we start tearing down; say so before
        # awaiting anything, so a beat that lands mid-teardown is already honest.
        self._listening = False
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            await conn.remove_listener(CHANNEL, self._on_notify)
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass

    # -- listener callback ---------------------------------------------
    def _on_notify(self, connection, pid, channel, payload):
        """asyncpg listener callback: runs in the loop, so it must NOT await.

        Any awaited DB re-read is handed off to a tracked task (per the task
        brief). Never raises: a failure here would otherwise surface inside
        asyncpg's dispatch.
        """
        try:
            task = self.bot.loop.create_task(self._handle(payload))
        except Exception:
            log.exception("dashboard_sync: failed to schedule handler")
            return
        self._track(task)

    def _track(self, task):
        """Hold a strong ref to a background task until it finishes."""
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle(self, payload):
        try:
            await dispatch(self.bot, payload)
        except Exception:
            log.exception("dashboard_sync: handler crashed")

    # -- supervised listen connection ----------------------------------
    async def _supervise(self):
        """Keep a dedicated listen connection alive, reconnecting with backoff.

        Gated on ``wait_until_ready`` so the pool and the other cogs exist before
        we start reacting to notifications. Every failure path is caught so the
        bot is never brought down by a DB blip; logs carry no secrets.
        """
        try:
            await self.bot.wait_until_ready()
        except Exception:
            pass

        backoff = _BACKOFF_START
        while not self._closing:
            try:
                # Sampled BEFORE the connect: _connect_and_listen sets the flag.
                reconnect = self._connected_once
                await self._connect_and_listen()
                backoff = _BACKOFF_START  # healthy connect resets the backoff
                self._schedule_resync(reconnect)
                await self._watch_connection()
            except asyncio.CancelledError:
                break
            except Exception:
                # No secrets in the message (never log the DSN).
                log.warning(
                    "dashboard_sync: listen connection error; reconnecting in %.0fs",
                    backoff,
                )
            finally:
                await self._teardown_connection()

            if self._closing:
                break
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, _BACKOFF_MAX)

        log.info("dashboard_sync: listener supervisor stopped.")

    def _schedule_resync(self, reconnect):
        """Run the full resync as a tracked background task, on EVERY connect.

        Both cases are the same hole, and neither is theoretical:

        * RECONNECT - the socket died, and Postgres drops (never queues) NOTIFY
          for an absent listener, so every dashboard write during the gap is
          gone.
        * BOOT - the caches are primed inside ``setup_hook`` (load_eager_caches
          plus every cog's cog_load), which runs BEFORE the gateway connects.
          Only after IDENTIFY, the guild stream and member chunking does
          ``wait_until_ready`` return and this LISTEN get registered. Every
          dashboard write in between is dropped exactly the same way, and on a
          1000+ guild bot that window is tens of seconds to minutes. ``main`` is
          production with continuous deploy, so it opens on every push - far more
          often than a socket failure. If the DB is also flaky at boot it
          stretches to however long the first successful connect takes.

        The cost of covering boot is one extra pass of the same seven steps over
        caches that were primed moments ago (the read-through clears are free at
        that point). ``dashboard_actions`` has never had this hole precisely
        because its sweep always ran at boot too.

        Scheduled AFTER the new LISTEN is registered, so a notification arriving
        while it works is delivered rather than lost, and OFF the supervisor's
        own path so a slow resync cannot delay the keepalive watch.

        At most ONE resync is ever in flight: a server that accepts a connection
        and immediately kills it (pgbouncer refusing LISTEN, connection churn)
        cycles about once a second - the backoff resets on every SUCCESSFUL
        connect - and each cycle would otherwise pile on another full sweep
        (four eager queries, a level_config scan and two whole guild_settings
        scans) at the exact moment the database is least able to take it.
        Concurrent runs would be correct, every step being idempotent; they would
        just be a self-inflicted storm.
        """
        task = self._resync_task
        if task is not None and not task.done():
            log.debug("dashboard_sync: a resync is still running; not scheduling another")
            return
        reason = "reconnect" if reconnect else "boot"

        async def _run():
            done = await resync_all(self.bot)
            log.info(
                "dashboard_sync: listen connection established (%s); "
                "notifications emitted before it was registered were dropped by "
                "Postgres, so a full cache resync ran (%s).",
                reason,
                ", ".join(done) if done else "no step succeeded",
            )

        self._resync_task = self.bot.loop.create_task(_run())
        self._track(self._resync_task)

    async def _connect_and_listen(self):
        """Open the dedicated connection and register the LISTEN callback."""
        conn = await asyncpg.connect(self._dsn)
        self._conn = conn
        await conn.add_listener(CHANNEL, self._on_notify)
        # Only now is a notification actually deliverable to this process: both
        # the reconnect marker and the heartbeat's flag flip HERE, never earlier.
        self._connected_once = True
        self._listening = True
        log.info("dashboard_sync: listening on Postgres channel '%s'.", CHANNEL)

    async def _watch_connection(self):
        """Block while the connection is healthy; return to trigger a reconnect.

        Actively probes with ``SELECT 1`` on a fixed cadence because a dropped
        socket is not always reflected by ``is_closed()`` until a query runs.

        Every exit is a "this connection is dead" verdict, so the heartbeat flag
        drops HERE - before the backoff sleep, not after the reconnect - which is
        precisely the window the dashboard needs to see as not-listening.
        """
        while not self._closing:
            conn = self._conn
            if conn is None or conn.is_closed():
                self._listening = False
                return
            try:
                await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._listening = False
                log.warning("dashboard_sync: keepalive failed; reconnecting.")
                return
            await asyncio.sleep(_KEEPALIVE_INTERVAL)

    # -- heartbeat ------------------------------------------------------
    @tasks.loop(seconds=HEARTBEAT_SECONDS)
    async def _heartbeat(self):
        """Refresh the bot_heartbeat row over the MAIN pool.

        Deliberately not gated on wait_until_ready and not tied to the listen
        connection: this loop's whole value is that it keeps beating when that
        connection is down. A failed write is logged and the loop carries on -
        an unhandled exception would stop a tasks.Loop outright, which would turn
        one blip into a permanent "bot offline" on the dashboard.
        """
        try:
            await write_heartbeat(self.bot.db_pool, self._listening, self._version)
        except Exception:
            log.warning("dashboard_sync: heartbeat write failed", exc_info=True)


async def setup(bot):
    await bot.add_cog(DashboardSync(bot))
