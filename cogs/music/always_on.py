"""24/7 music (Yasuho+ M4b): the stored per-guild setting and its in-memory cache.

The feature is simple to STATE and easy to get wrong in the DETAILS, so this
module owns every piece that is not Discord-event plumbing (that lives in
music.py, which is the only importer):

* the stored intent - one voice channel per guild, kept in its own table
  (``music_247``) rather than the ``guild_settings`` JSONB blob every other
  music config key rides (see ``cogs/music/guild_config.py``). Those keys are
  dashboard-only and read one-at-a-time through a size-bounded LRU; this
  feature needs the OPPOSITE access pattern - "every enabled guild, in a
  stable order" (restart rejoin, the reconnect-after-node-drop pass, the
  global ceiling) - which a JSONB blob has no efficient query for. A tiny
  dedicated table with a bulk "load everything once" read is the simpler fit,
  and it is the ONE place this lot needed a schema change.

* the in-memory cache every hot path reads. "entitlement checks via the
  synchronous cache only" (the task's own hard rule) applies here exactly as
  it does to ``tools.premium.EntitlementCache``: the idle sweeper and the
  empty-channel auto-leave run every tick / every voice event for every guild,
  so a dict lookup is the only acceptable cost. ``AlwaysOnStore`` loads once
  (``ensure_loaded``) and is kept in step by every write going through it.

* SUSPENSION, which is deliberately NOT persisted. "suspend until the next
  restart or the next explicit /play or re-enable" describes an in-process
  flag perfectly: a restart naturally clears it (the cache reloads from a
  table that never recorded it), and a human action lifts it explicitly
  (music.py's ``_init_session`` seam, or the enable command). Writing it to
  the database would need its own migration-safe lifecycle for a fact that is
  only ever true "for the rest of this process".

* classifying a sonolink ``player_disconnect`` event into what it means for
  24/7 - see :func:`classify_player_disconnect`'s docstring for the mechanism
  (sonolink's own ``DisconnectTriggerType``, already set correctly by every
  call site in the installed library, needs no flag of our own).

No Discord objects cross this module's functions except where unavoidable
(:func:`count_active_sessions` walks ``bot.voice_clients`` to count a live
resource); everything else is plain data, so the cache and the classifier are
unit-testable with no gateway and no database.
"""

from __future__ import annotations

import asyncio
import logging
import time
import typing

import sonolink

log = logging.getLogger(__name__)


# Global ceiling on simultaneous 24/7 CONNECTIONS (not merely "enabled" rows -
# a lapsed Yasuho+ guild's row stays in the table forever, see the module
# docstring's "archived" note, and must never count against a live ceiling it
# is not currently using).
#
# The number: 300 simultaneous always-on voice connections is a deliberately
# small slice of the "1000+ guilds" scale target, not a per-shard or per-node
# capacity measurement (today's single Lavalink node has not been load-tested
# at that concurrency). It is a safety valve to reach for WHILE the feature is
# new and unmeasured, raised later once real occupancy is observed - exactly
# the posture the plan's AniList limits already took ("relever plus apres
# mesure de charge"). Every idle 24/7 connection costs Discord one voice
# gateway session and Lavalink one silent, non-streaming player (see
# music.py's module docstring for why an idle 24/7 player sends no audio) -
# cheap per guild, but not EVERY guild having one tested together yet.
MAX_247_SESSIONS = 300


async def _load_rows(pool: typing.Any) -> typing.List[typing.Any]:
    """Every persisted 24/7 row, oldest ``enabled_at`` first. Empty on error."""
    try:
        return await pool.fetch(
            "SELECT guild_id, channel_id, enabled_at FROM music_247 "
            "ORDER BY enabled_at, guild_id"
        )
    except Exception:
        log.exception("Failed to load 24/7 settings; starting with none cached")
        return []


async def _upsert_row(pool: typing.Any, guild_id: int, channel_id: int) -> None:
    await pool.execute(
        """
        INSERT INTO music_247 (guild_id, channel_id, enabled_at)
        VALUES ($1, $2, now())
        ON CONFLICT (guild_id) DO UPDATE SET
            channel_id = EXCLUDED.channel_id
        """,
        guild_id,
        channel_id,
    )


async def _delete_row(pool: typing.Any, guild_id: int) -> None:
    await pool.execute("DELETE FROM music_247 WHERE guild_id = $1", guild_id)


class AlwaysOnStore:
    """The in-memory cache of who has 24/7 configured, plus live suspension.

    One instance lives on the ``Music`` cog for the process lifetime. Every
    read used on a hot path (:meth:`is_enabled`, :meth:`channel_id`,
    :meth:`is_suspended`) is a plain dict/set lookup - no ``await``, so no
    query and no event-loop yield, callable from the idle sweeper and the
    voice-state listener exactly as cheaply as ``tools.premium`` is.
    """

    __slots__ = ("_channels", "_order", "_suspended", "_loaded", "_load_lock")

    def __init__(self) -> None:
        self._channels: typing.Dict[int, int] = {}
        # Insertion order, oldest first - the global ceiling's tie-break (an
        # existing session is kept over admitting a new one past the cap,
        # mirroring the plan's general "oldest stays, admin can still delete"
        # posture for every other commercial limit in this codebase).
        self._order: typing.Dict[int, float] = {}
        self._suspended: typing.Set[int] = set()
        self._loaded = False
        self._load_lock = asyncio.Lock()

    async def ensure_loaded(self, pool: typing.Any) -> None:
        """Load every row once. Idempotent and safe to call from many places
        (the idle loop's ``before_loop``, the startup restore, every command) -
        exactly one of them will actually hit the database."""
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            rows = await _load_rows(pool)
            for row in rows:
                guild_id = int(row["guild_id"])
                self._channels[guild_id] = int(row["channel_id"])
                enabled_at = row["enabled_at"]
                self._order[guild_id] = (
                    enabled_at.timestamp() if enabled_at is not None else 0.0
                )
            self._loaded = True

    def is_enabled(self, guild_id: typing.Optional[int]) -> bool:
        return guild_id is not None and int(guild_id) in self._channels

    def channel_id(self, guild_id: typing.Optional[int]) -> typing.Optional[int]:
        if guild_id is None:
            return None
        return self._channels.get(int(guild_id))

    def is_suspended(self, guild_id: typing.Optional[int]) -> bool:
        return guild_id is not None and int(guild_id) in self._suspended

    def suspend(self, guild_id: int) -> bool:
        """Mark ``guild_id`` suspended; returns whether it had 24/7 configured
        at all (so a caller only mentions the pause when it is relevant)."""
        guild_id = int(guild_id)
        if guild_id not in self._channels:
            return False
        self._suspended.add(guild_id)
        return True

    def lift_suspend(self, guild_id: typing.Optional[int]) -> None:
        """Clear a suspension - an explicit human action (``/play``, a fresh
        ``/playlist play``, re-enabling). A no-op for a guild that was never
        suspended, so every fresh-connect entry point can call this
        unconditionally."""
        if guild_id is not None:
            self._suspended.discard(int(guild_id))

    def ordered_guild_ids(self) -> typing.List[int]:
        """Every configured guild id, oldest-enabled first."""
        return sorted(self._channels, key=lambda gid: self._order.get(gid, 0.0))

    async def enable(self, pool: typing.Any, guild_id: int, channel_id: int) -> None:
        guild_id, channel_id = int(guild_id), int(channel_id)
        await _upsert_row(pool, guild_id, channel_id)
        self._channels[guild_id] = channel_id
        self._order.setdefault(guild_id, time.time())
        self._suspended.discard(guild_id)

    async def disable(self, pool: typing.Any, guild_id: int) -> None:
        guild_id = int(guild_id)
        await _delete_row(pool, guild_id)
        self.evict(guild_id)

    def evict(self, guild_id: int) -> None:
        """Drop a guild from the cache with no database write - the bot was
        removed from it (the row dies with the rest of its data on the usual
        30-day grace purge; see tools/retention.py), so there is nothing left
        to persist."""
        guild_id = int(guild_id)
        self._channels.pop(guild_id, None)
        self._order.pop(guild_id, None)
        self._suspended.discard(guild_id)


def count_active_sessions(bot: typing.Any, store: AlwaysOnStore) -> int:
    """How many guilds currently hold a voice connection kept by 24/7.

    Counts a guild once if it is both configured (``store.is_enabled``) and
    not suspended AND currently connected - whether or not it happens to be
    entitled RIGHT NOW. A guild that just lost Yasuho+ but has not been swept
    yet still physically occupies the connection the ceiling is bounding;
    excluding it would let the fleet quietly exceed ``MAX_247_SESSIONS`` for
    as long as the lapsed guild stays connected. Entitlement is what gates
    ADMITTING new sessions (see music.py's restore/enable call sites), not
    what this count measures.

    Walks ``bot.voice_clients`` - at most one pass over the whole fleet's live
    players, done only at enable time and at the start of a bounded
    restore/reconnect batch, never per-guild per-tick.
    """
    from cogs.music.player import Player  # local import: avoids a cycle at module load

    count = 0
    for voice_client in list(getattr(bot, "voice_clients", ()) or ()):
        if not isinstance(voice_client, Player):
            continue
        guild = getattr(getattr(voice_client, "channel", None), "guild", None)
        if guild is None:
            guild = getattr(getattr(voice_client, "home", None), "guild", None)
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            continue
        if store.is_enabled(guild_id) and not store.is_suspended(guild_id):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Classifying a sonolink ``player_disconnect`` event
# ---------------------------------------------------------------------------

# The four outcomes classify_player_disconnect can return.
OWN = "own"
EXTERNAL = "external"
NODE = "node"
UNKNOWN = "unknown"


def classify_player_disconnect(event: typing.Any) -> str:
    """Classify a sonolink ``on_sonolink_player_disconnect`` event for 24/7.

    THE MECHANISM (no flag of ours needed). sonolink's public
    ``Player.disconnect()`` - the ONLY call site in the installed library that
    passes ``trigger=DisconnectTriggerType.MANUAL`` (verified by reading
    ``gateway/player/_base.py``: every other disconnect the library triggers
    itself uses ``INACTIVITY`` or ``ERROR``) - is also the ONLY disconnect
    path this codebase ever calls (``/music disconnect``, ``/stop``'s sibling
    paths, the idle teardown, the empty-channel auto-leave). So ``MANUAL`` on
    this event means, with no exception anywhere in the codebase, "our own
    code decided to disconnect this player" - :data:`OWN`. (``INACTIVITY`` is
    sonolink's OWN built-in idle timer; core.py disarms it for every player
    via ``InactivitySettings(timeout=None)`` precisely so it can never race
    this feature's own idle sweeper, so it should never arrive here, but is
    folded into :data:`UNKNOWN` defensively rather than asserted against.)

    ``ERROR`` covers two DIFFERENT real situations that sonolink's trigger
    alone does not distinguish, but its ``extra_data`` does:

    * the Discord VOICE WEBSOCKET closed with code 4014 or 4022 ("call
      terminated remotely") - ``extra_data`` is the raw
      ``WebSocketClosedEventPayload``, which carries a ``code`` attribute.
      This is Discord itself ending the call: a moderator disconnecting or
      kicking the bot, or the channel the bot was in getting deleted. Nothing
      about Lavalink failed - the voice SESSION is just gone. Returns
      :data:`EXTERNAL`; the caller still has to tell "channel deleted" apart
      from "kicked while the channel still exists" by resolving the
      configured channel afterwards (see music.py).
    * a Lavalink/REST failure (a session 404 that a reconnect-and-retry could
      not recover, or a connect() that timed out) - ``extra_data`` is an
      ``Exception`` instance (``HTTPException``, ``TimeoutError``, ...),
      which has no ``code`` attribute. The voice call itself was never
      touched; the NODE side is what broke. Returns :data:`NODE` - recovery
      is the reconnect-rejoin pass triggered by ``on_sonolink_node_ready``,
      not a suspension (suspending would refuse to auto-rejoin a guild that
      did nothing wrong and whose human never touched anything).

    Anything else (a future sonolink trigger this module does not know about)
    returns :data:`UNKNOWN`, treated identically to :data:`NODE` by every
    caller: no suspension, no destructive action, on the same "a premium
    lookup must never crash, or silently misbehave" posture as the rest of
    this codebase's defensive reads.
    """
    trigger = getattr(event, "trigger", None)
    if trigger is sonolink.DisconnectTriggerType.MANUAL:
        return OWN
    if trigger is sonolink.DisconnectTriggerType.ERROR:
        extra = getattr(event, "extra_data", None)
        if hasattr(extra, "code"):
            return EXTERNAL
        return NODE
    return UNKNOWN
