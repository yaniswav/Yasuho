"""Voice XP (leveling L7): earn XP for time spent together in voice.

Arcane's growth wedge, free here. A guild opts in
(``/levelconfig voicexp on``); its members then accrue XP for every minute they
share a voice channel with at least one other human, subject to the usual
anti-idle rules. This cog owns three things and nothing else:

* an IN-MEMORY session map ``{(guild_id, user_id): _VoiceSession}``, maintained
  by its OWN ``on_voice_state_update`` listener (discord.py dispatches the event
  to every cog that listens, so this lives alongside the music cog's three voice
  concerns without touching them - see that listener's guard style);
* a single periodic SWEEP (one ``tasks.loop``) that, every
  :data:`VOICE_SWEEP_INTERVAL` seconds, credits each eligible live session in ONE
  batched DB write and routes any level-ups through the Leveling cog's existing
  announce + role-reward seams;
* nothing user-facing: the ``/levelconfig voicexp`` admin commands live in
  cogs/community/level_config_ui.py, and every level-up message/role is emitted
  by the Leveling cog (credit_voice_levelup), so this module carries no prose.

The pure decisions (eligibility predicate, credit arithmetic, batch-payload
building) live in tools/leveling.py; this cog is only the I/O and the clock.

Restart drops every live session (in-memory only) - accepted and documented.
To soften it, on_ready seeds sessions from the CURRENT voice states once (see
_seed_sessions and its docstring for why on_ready, not cog_load).

Scale story: the listener's non-matching path (bots, guilds without voice XP) is
pure O(1) dict work with ZERO awaits and zero allocations; all real work is
deferred to the sweep, which does exactly ONE DB round-trip per tick regardless
of how many members earned XP. See :meth:`_run_sweep`.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import discord
from discord.ext import commands, tasks

from tools import leveling

log = logging.getLogger(__name__)

# How often the sweep credits live voice sessions. 300s (5 min) keeps the DB
# write rate to at most one round-trip every 5 minutes bot-wide (see the scale
# story) while still crediting time at a fair, minute-level granularity.
VOICE_SWEEP_INTERVAL = 300

# Hard ceiling on the live-session map. This is a BACKSTOP against a pathological
# leak of sessions whose leave event was missed (a gateway outage can drop the
# on_voice_state_update that would end a session) - the sweep is the real,
# continuous evictor (it drops every session whose member is no longer in voice).
# Sized comfortably above the design's peak of ~1000 guilds x 5 concurrent voice
# users (== 5000), so legitimate load never triggers eviction; only a genuine
# runaway leak ever reaches it, and then the oldest-inserted entry is dropped so
# the map can never grow without bound.
SESSION_CAP = 8192

# One batched upsert credits every member who earned XP this sweep: unnest turns
# the three parallel arrays (tools.leveling.build_voice_grant_payload) into rows,
# and RETURNING hands back each member's NEW total so the caller can detect
# level-ups (old = new - the gain it recorded). EXCLUDED.xp is the per-row
# proposed gain, added onto the stored total on conflict.
#
# L6: the SAME round trip also credits both period rollups (xp_period,
# weekly + monthly - $4/$5, scalar) for every credited member. period_key
# depends only on the wall-clock instant, not on the row, so - unlike
# guild_id/user_id/gain - it is the SAME for every row in one sweep tick and
# needs no third array. This is ONE parameterized SQL command (a WITH query
# whose CTEs are the three upserts), never three statements joined by ';':
# asyncpg's extended query protocol (used whenever arguments are passed)
# prepares exactly one command, so a multi-statement string would raise
# "cannot insert multiple commands into a prepared statement". PostgreSQL
# guarantees every data-modifying CTE in a WITH clause executes exactly once,
# in full, even when the primary SELECT never reads its output (see
# "Data-Modifying Statements in WITH" in the Postgres docs) - so `week`/
# `month` below run unconditionally even though only `xp_grant` is selected
# from. NOTE: the CTE is named `xp_grant`, not `grant` - GRANT is a reserved
# SQL keyword and Postgres rejects it unquoted as a CTE name ("syntax error
# at or near 'grant'"), confirmed live.
_BATCH_GRANT_QUERY = """
    WITH batch(guild_id, user_id, gain) AS (
        SELECT * FROM unnest($1::bigint[], $2::bigint[], $3::bigint[])
    ), xp_grant AS (
        INSERT INTO levels (guild_id, user_id, xp)
        SELECT guild_id, user_id, gain FROM batch
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET xp = levels.xp + EXCLUDED.xp
        RETURNING guild_id, user_id, xp
    ), week AS (
        INSERT INTO xp_period (guild_id, user_id, period_key, xp)
        SELECT guild_id, user_id, $4, gain FROM batch
        ON CONFLICT (guild_id, user_id, period_key)
        DO UPDATE SET xp = xp_period.xp + EXCLUDED.xp
    ), month AS (
        INSERT INTO xp_period (guild_id, user_id, period_key, xp)
        SELECT guild_id, user_id, $5, gain FROM batch
        ON CONFLICT (guild_id, user_id, period_key)
        DO UPDATE SET xp = xp_period.xp + EXCLUDED.xp
    )
    SELECT guild_id, user_id, xp FROM xp_grant;
    """


@dataclass
class _VoiceSession:
    """One member's live voice session: the room they are in and the marker.

    ``channel_id`` tracks the channel the listener last saw them in (updated on a
    move). ``last_credit`` is a ``time.monotonic()`` timestamp of the last sweep
    that consumed this session's minutes; the sweep advances it by the whole
    minutes it consumes (credited or not - see tools.leveling.voice_credit), so
    ineligible or capped time is never banked.
    """

    channel_id: int
    last_credit: float


class VoiceXP(commands.Cog):
    """Time-in-voice XP: the session map, the listener, and the sweep loop."""

    def __init__(self, bot):
        self.bot = bot
        # (guild_id, user_id) -> live session. Bounded by SESSION_CAP; entries are
        # created on join/move by the listener and dropped on leave (or evicted by
        # the sweep when a leave was missed).
        self._sessions: dict[tuple[int, int], _VoiceSession] = {}
        # on_ready fires on every reconnect; seed the session map exactly once.
        self._seeded = False
        # Cumulative instrumentation (scale story): swept / credited / evicted.
        self._stats = {"swept": 0, "credited": 0, "evicted": 0, "writes": 0}

    async def cog_load(self):
        self._voice_sweep.start()

    async def cog_unload(self):
        self._voice_sweep.cancel()

    # ------------------------------------------------------------------
    # Config read-through (the Leveling cog's cached, DB-free config)
    # ------------------------------------------------------------------
    def _config_for(self, guild_id):
        """This guild's cached LevelConfig, or None (leveling off / cog absent).

        O(1), zero awaits: a bot.get_cog dict lookup plus the Leveling cog's own
        in-memory ``_configs`` read. Never hits the DB, so it is safe on the hot
        voice-event path.
        """
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            return None
        return leveling_cog.get_config(guild_id)

    # ------------------------------------------------------------------
    # Session bookkeeping (in-memory, synchronous)
    # ------------------------------------------------------------------
    def _start_session(self, guild_id, user_id, channel_id, now):
        """Begin OR refresh a session. A join creates one; a move keeps the
        running ``last_credit`` (they keep accruing across rooms) and only
        repoints ``channel_id``. Enforces the SESSION_CAP backstop."""
        key = (guild_id, user_id)
        existing = self._sessions.get(key)
        if existing is not None:
            existing.channel_id = channel_id
            return
        if len(self._sessions) >= SESSION_CAP:
            # Backstop only: drop the oldest-inserted entry (O(1)) so the map is
            # hard-bounded even if a flood of missed-leave sessions ever leaks.
            oldest = next(iter(self._sessions), None)
            if oldest is not None:
                del self._sessions[oldest]
        self._sessions[key] = _VoiceSession(channel_id=channel_id, last_credit=now)

    def _apply_transition(self, member, before, after, now):
        """Map a voice-state change to a session start/move/end (enabled guilds
        only). Pure in-memory dict work; the sweep re-reads live deaf/mute at
        credit time, so a same-channel mute/deaf/stream toggle is a no-op here."""
        key = (member.guild.id, member.id)
        after_channel = after.channel
        if after_channel is None:
            # Left voice entirely -> end the session.
            self._sessions.pop(key, None)
            return
        before_channel = before.channel
        if before_channel is None or before_channel.id != after_channel.id:
            # Joined voice, or moved between channels -> start / refresh.
            self._start_session(
                member.guild.id, member.id, after_channel.id, now
            )
        # else: same channel, a non-structural state change -> ignore.

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """React to voice joins/moves/leaves for guilds that opted into voice XP.

        HOT GLOBAL event: discord.py dispatches every voice-state change here AND
        to the music cog's own listener. The non-matching path (a bot, or a guild
        without voice XP enabled) must cost ZERO awaits and zero allocations - it
        is a bot flag check, one bot.get_cog + dict lookup, and a return.
        """
        if member.bot:
            return
        config = self._config_for(member.guild.id)
        if config is None or not config.voice_xp_enabled:
            # Not a voice-XP guild (or leveling off). A guild that just toggled
            # voice XP OFF may still hold live sessions; the sweep evicts those,
            # so this path stays a pure early return.
            return
        self._apply_transition(member, before, after, time.monotonic())

    # ------------------------------------------------------------------
    # The sweep
    # ------------------------------------------------------------------
    @tasks.loop(seconds=VOICE_SWEEP_INTERVAL)
    async def _voice_sweep(self):
        try:
            await self._run_sweep()
        except Exception:
            log.exception("voice-xp sweep iteration failed")

    @_voice_sweep.before_loop
    async def _before_voice_sweep(self):
        await self.bot.wait_until_ready()

    @_voice_sweep.error
    async def _voice_sweep_error(self, error):
        log.exception("voice-xp sweep crashed; restarting", exc_info=error)
        self._voice_sweep.restart()

    async def _run_sweep(self, now=None):
        """Credit every eligible live session in ONE batched write, then route
        any level-ups through the Leveling cog. ``now`` is injectable for tests;
        it defaults to ``time.monotonic()`` (the same clock the sessions use)."""
        if not self._sessions:
            return
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            return
        if now is None:
            now = time.monotonic()

        snapshots: dict[int, object] = {}  # per-guild no-xp snapshot memo
        multiplier_snapshots: dict[int, object] = {}  # per-guild multiplier memo
        wall_now = discord.utils.utcnow()  # for the multiplier event check only
        credits: list[tuple[int, int, int]] = []  # (guild_id, user_id, gain)
        # (guild_id, user_id) -> (member, channel, config, gain) for level-up routing.
        pending: dict[tuple[int, int], tuple] = {}
        evicted = 0

        for key, session in list(self._sessions.items()):
            guild_id, user_id = key
            try:
                config = leveling_cog.get_config(guild_id)
                if config is None or not config.voice_xp_enabled:
                    del self._sessions[key]  # leveling / voice XP turned off
                    evicted += 1
                    continue
                guild = self.bot.get_guild(guild_id)
                member = guild.get_member(user_id) if guild is not None else None
                voice = member.voice if member is not None else None
                channel = voice.channel if voice is not None else None
                if channel is None:
                    # Member left the guild or left voice without an event
                    # (missed leave) - evict the dead session.
                    del self._sessions[key]
                    evicted += 1
                    continue

                if guild_id not in snapshots:
                    snapshots[guild_id] = await leveling_cog.ensure_no_xp_snapshot(
                        guild_id
                    )
                snapshot = snapshots[guild_id]
                is_no_xp = bool(snapshot.channels or snapshot.roles) and (
                    leveling.is_no_xp_message(
                        snapshot,
                        channel.id,
                        getattr(channel, "category_id", None),
                        (r.id for r in member.roles),
                    )
                )

                afk_channel = guild.afk_channel
                eligible = leveling.is_voice_xp_eligible(
                    enabled=True,
                    in_voice=True,
                    human_count=sum(1 for m in channel.members if not m.bot),
                    is_afk_channel=(
                        afk_channel is not None and channel.id == afk_channel.id
                    ),
                    self_deaf=voice.self_deaf,
                    self_mute=voice.self_mute,
                    is_no_xp=is_no_xp,
                )
                # XP multipliers (L4): applied to the per-minute RATE, once,
                # before voice_credit multiplies it by the whole-minute count
                # (see tools.leveling.apply_multiplier's docstring for why
                # rounding happens here and not on the aggregated total). The
                # common case (no boosts/event configured) is a single
                # ``is_trivial`` check - zero extra allocation, zero rate
                # change.
                if guild_id not in multiplier_snapshots:
                    multiplier_snapshots[
                        guild_id
                    ] = await leveling_cog.ensure_multiplier_snapshot(guild_id)
                multiplier_snapshot = multiplier_snapshots[guild_id]
                rate = config.voice_xp_per_minute
                if not multiplier_snapshot.is_trivial:
                    role_ids = (
                        (r.id for r in member.roles)
                        if multiplier_snapshot.roles
                        else ()
                    )
                    multiplier = leveling.compute_multiplier(
                        multiplier_snapshot,
                        channel.id,
                        getattr(channel, "category_id", None),
                        role_ids,
                        wall_now,
                    )
                    rate = leveling.apply_multiplier(rate, multiplier)

                gain, consumed = leveling.voice_credit(
                    now - session.last_credit,
                    rate,
                    VOICE_SWEEP_INTERVAL,
                    eligible=eligible,
                )
                session.last_credit += consumed
                if gain > 0:
                    credits.append((guild_id, user_id, gain))
                    pending[key] = (member, channel, config, gain)
            except Exception:
                # One bad session must never abort the whole sweep (or block the
                # batch write for everyone else). Skip it; it is retried next tick.
                log.exception(
                    "voice-xp: failed to evaluate session %s", key
                )

        self._stats["swept"] += len(self._sessions)
        self._stats["evicted"] += evicted
        if not credits:
            return

        guild_ids, user_ids, gains = leveling.build_voice_grant_payload(credits)
        week_key, month_key = leveling.current_period_keys(wall_now)
        rows = await self.bot.db_pool.fetch(
            _BATCH_GRANT_QUERY, guild_ids, user_ids, gains, week_key, month_key
        )
        self._stats["credited"] += len(credits)
        self._stats["writes"] += 1
        log.debug(
            "voice-xp sweep: credited %d, evicted %d dead session(s)",
            len(credits),
            evicted,
        )

        # L6 lazy retention: piggyback the prune-decision on this tick, once
        # per DISTINCT guild actually credited (not once per session) - the
        # common case is a cache hit (a tuple compare, zero DB) on every one
        # of them, so this never adds a real round trip except on the rare
        # tick where a guild's week or month just rolled over.
        for credited_guild_id in {c[0] for c in credits}:
            await leveling_cog.maybe_prune_expired_periods(
                credited_guild_id, wall_now
            )

        # Level-up handful: the ONLY per-user awaits in the sweep, gated by the
        # PURE level_up_between test so a member who merely gained XP without
        # crossing a threshold (the common case) costs no await at all - only
        # actual level-ups route through the announce + reward seams.
        for row in rows:
            key = (row["guild_id"], row["user_id"])
            ctx = pending.get(key)
            if ctx is None:
                continue
            member, channel, config, gain = ctx
            new_xp = row["xp"]
            old_xp = new_xp - gain
            if leveling.level_up_between(old_xp, new_xp) is None:
                continue
            await leveling_cog.credit_voice_levelup(
                guild=member.guild,
                member=member,
                channel=channel,
                config=config,
                old_xp=old_xp,
                new_xp=new_xp,
            )

    # ------------------------------------------------------------------
    # Startup seeding (soften the restart-loses-sessions gap)
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        """Seed sessions from the CURRENT voice states, exactly once.

        DECISION: seed on on_ready, NOT cog_load. cog_load runs inside
        setup_hook, BEFORE the gateway delivers any guild - ``bot.guilds`` is
        empty there, so a cog_load scan would find nothing. By on_ready the guild
        and voice-state caches are populated AND the Leveling cog's config cache
        is loaded (its cog_load ran during setup_hook), so the read-through in
        _config_for is valid. on_ready can fire repeatedly on reconnects, hence
        the once-flag. A failure here only skips seeding (members' sessions still
        start on their next join/move) and never blocks startup.
        """
        if self._seeded:
            return
        self._seeded = True
        try:
            self._seed_sessions()
        except Exception:
            log.exception("Failed to seed voice XP sessions on ready")

    def _seed_sessions(self):
        """Scan every voice-XP guild's occupied voice channels once and open a
        session per non-bot member, marked from NOW (so pre-restart time is never
        retroactively credited - no banking). Synchronous: all reads hit the
        gateway caches, no awaits, no DB."""
        now = time.monotonic()
        seeded = 0
        for guild in self.bot.guilds:
            config = self._config_for(guild.id)
            if config is None or not config.voice_xp_enabled:
                continue
            for channel in guild.voice_channels:
                for member in channel.members:
                    if member.bot:
                        continue
                    self._start_session(guild.id, member.id, channel.id, now)
                    seeded += 1
        if seeded:
            log.info(
                "Seeded %d voice XP session(s) from current voice states", seeded
            )


async def setup(bot):
    await bot.add_cog(VoiceXP(bot))
