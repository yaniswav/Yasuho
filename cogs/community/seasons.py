"""Leveling seasons (S1 engine + S2 surfaces) - closes a month, freezes its
podium, and lets the guild browse and configure it.

A SEASON is one calendar month, and it rides the monthly ``xp_period`` rollup
the leveling cog already writes on every grant (tools.leveling.month_period_key).
Nothing is ever reset or destroyed when a season ends: lifetime ``levels``
totals and member levels are untouched, the month simply stops accumulating and
its top 3 are FROZEN into ``season_podiums`` so they survive the lazy xp_period
prune. That snapshot is what a later lot's hall-of-fame surface reads.

No new timer. Rollover detection is LAZY and rides the leveling cog's existing
per-guild "last seen period" marker (a BoundedLRU tested in memory BEFORE any
await): the first XP grant of a new month for a guild dispatches here through
``bot.get_cog("Seasons")``. That marker is in MEMORY only, so it is cold for
every guild after a restart - the leveling cog's cold branch resolves the
closed month from xp_period before its own retention DELETE can touch it (see
tools.leveling.LATEST_CLOSED_MONTH_SQL, shared with this cog) and hands it over
like a warm one. A guild with no activity at all in the new month is never
detected that way, so :meth:`Seasons.ensure_season_snapshot` is also the public
on-demand entry point a read surface calls when it opens (S2's hall of fame) -
all paths converge on the SAME idempotent INSERT.

Exactly once, twice over: the INSERT is ``ON CONFLICT DO NOTHING`` on the
``(guild_id, period_key, rank)`` PK, and its ``RETURNING`` is what ELECTS the
single caller allowed to run the one-shot side effects (the champion role and
the announce). A caller that inserted nothing - because the podium was already
there, or because a concurrent trigger won the race - stays silent. The podium
row is the source of truth and is committed BEFORE any side effect, so a role
or announce failure can never cost a guild its season history.

DECLARED LIMITATION - the announce is AT MOST ONCE, and deliberately so. That
same election is the only replay guard: once the podium row is committed the
season counts as closed, so if the process dies (or the send is rejected) in
the window between the COMMIT and the ``channel.send``, that month's announce
and champion-role move are simply never retried - a later trigger short-circuits
on the exists probe. This is the intended trade: the HISTORY (the podium, the
only thing that cannot be reconstructed) is never at risk, while a lost
announce costs one message. Making the announce at-least-once would need a
separate ``announced_at`` claim on the rank-1 row (an UPDATE ... WHERE
announced_at IS NULL RETURNING) so a replay can re-elect an announcer WITHOUT
re-freezing the podium; deliberately not built until a guild asks for it.

S2 adds two read/admin surfaces, both thin: ``/halloffame`` (a browsable
Components V2 card over ``season_podiums``, opening on the most recent season
and walking older/newer ones one indexed hop at a time - see the "hall of fame
browsing queries" below) and ``/levelconfig seasons`` (a panel for the two
level_config knobs the S1 engine already reads: the champion role and the
announce toggle, delegated from LevelConfigUI the same way ``rewards``/``xp``
already are). Neither surface touches the engine's exactly-once contract
above; ``/halloffame`` only ever calls :meth:`ensure_season_snapshot` the way
any other on-demand caller would. The Views themselves live in the sibling
``seasons_views.py`` (the presentation concern, mirroring the automod.py /
automod_panel.py split) - this module owns the queries and the command bodies,
that one owns the Components V2 layout and its interactive callbacks.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.community import seasons_views
from tools import i18n, leveling
from tools.i18n import _
from tools.modchecks import bot_can_assign_role as _assignable

log = logging.getLogger(__name__)

# Medal glyphs for the three podium places, shared with the leaderboard card and
# the hall of fame (tools.leveling.PODIUM_MEDALS - one home, so the three podium
# surfaces can never drift apart). A rank with no glyph (impossible while
# SEASON_PODIUM_SIZE is 3, but cheap to survive) falls back to a plain number.
_MEDALS = leveling.PODIUM_MEDALS

# Audit-log reasons for the champion role moves. Plain English literals, never
# _(): a reason is written to the guild's audit log at call time, outside any
# per-guild locale scope, exactly like level_rewards' own "Level reward".
_REASON_GRANT = "Season champion (leveling)"
_REASON_REPLACE = "Season champion (replaced by the new season's winner)"

# The exactly-once probe: a PK-prefix lookup on season_podiums, so a guild whose
# season is already frozen pays ONE index-only scan and nothing else.
_SEASON_EXISTS_SQL = (
    "SELECT 1 FROM season_podiums WHERE guild_id = $1 AND period_key = $2 LIMIT 1;"
)

# The snapshot itself, ONE statement: the top N of the closed month (an index
# range scan on xp_period_guild_period_xp_idx plus a top-N heapsort for the
# user_id tie-break, run at most once per guild per month) ranked and inserted
# in the same command. Ties are broken by the LOWER user_id so a re-run can
# never reshuffle a stored podium. RETURNING yields ONLY the rows this call
# actually inserted, which is precisely the exactly-once election for the side
# effects below - a concurrent trigger that lost the race gets an empty result.
_SEASON_SNAPSHOT_SQL = """
    WITH top AS (
        SELECT user_id, xp
        FROM xp_period
        WHERE guild_id = $1 AND period_key = $2
        ORDER BY xp DESC, user_id ASC
        LIMIT $3
    )
    INSERT INTO season_podiums (guild_id, period_key, rank, user_id, xp)
    SELECT $1, $2, ROW_NUMBER() OVER (ORDER BY xp DESC, user_id ASC), user_id, xp
    FROM top
    ON CONFLICT (guild_id, period_key, rank) DO NOTHING
    RETURNING rank, user_id, xp;
    """

# The outgoing champion, for the REPLACE below: rank 1 of the most recent
# season this guild ever froze BEFORE the one being closed. The PK
# (guild_id, period_key, rank) serves it as a backward index scan stopping at
# the first matching row. This is the AUTHORITATIVE previous holder - unlike
# Role.members, it does not depend on the member cache being populated.
_PREVIOUS_CHAMPION_SQL = """
    SELECT user_id
    FROM season_podiums
    WHERE guild_id = $1 AND period_key < $2 AND rank = 1
    ORDER BY period_key DESC
    LIMIT 1;
    """

# The season knobs, read straight from level_config once per closed season per
# guild rather than through the leveling cog's hot-path config mirror: this runs
# at most once a month, and reading the table directly also works for a guild
# whose leveling is currently OFF (an on-demand snapshot from a read surface
# still has a season to show).
_SEASON_CONFIG_SQL = """
    SELECT season_champion_role_id, season_announce,
           announce_mode, announce_channel_id
    FROM level_config
    WHERE guild_id = $1;
    """

# ---------------------------------------------------------------------------
# S2 read surface: the hall of fame's browsing queries. Every one of them is
# served by the season_podiums PK (guild_id, period_key, rank) as an index
# range scan - there is deliberately no "list every season this guild has"
# query anywhere: the card walks one hop at a time (see seasons_views.py's
# HallOfFameCard), so a guild with 2 closed seasons or 200 costs the same per
# page flip.
# ---------------------------------------------------------------------------

# The most recent season this guild ever froze, or NULL for a guild with none
# at all - the hall of fame's landing page.
_LATEST_SEASON_KEY_SQL = """
    SELECT period_key FROM season_podiums
    WHERE guild_id = $1
    ORDER BY period_key DESC
    LIMIT 1;
    """

# The closest season STRICTLY OLDER than ``period_key`` ("Prev" on the card),
# or NULL when ``period_key`` is already the guild's oldest frozen season.
_OLDER_SEASON_KEY_SQL = """
    SELECT period_key FROM season_podiums
    WHERE guild_id = $1 AND period_key < $2
    ORDER BY period_key DESC
    LIMIT 1;
    """

# The closest season STRICTLY NEWER than ``period_key`` ("Next" on the card),
# or NULL when ``period_key`` is already the guild's latest frozen season.
_NEWER_SEASON_KEY_SQL = """
    SELECT period_key FROM season_podiums
    WHERE guild_id = $1 AND period_key > $2
    ORDER BY period_key ASC
    LIMIT 1;
    """

# One season's frozen podium, rank-ordered. Bounded at SEASON_PODIUM_SIZE rows
# by construction (the snapshot never inserts more), so this is always a tiny,
# PK-served read - never a sort over the guild's whole season history.
_SEASON_POD_ROWS_SQL = """
    SELECT rank, user_id, xp FROM season_podiums
    WHERE guild_id = $1 AND period_key = $2
    ORDER BY rank ASC;
    """

# S2 admin writes: the seasons panel's two level_config knobs, each a
# targeted UPDATE of exactly one column via the house upsert shape (mirrors
# cogs/community/leveling.py's set_announce_mode/set_voice_xp_enabled) so a
# guild that has never written a level_config row yet still gets its very first
# season setting persisted, instead of a plain UPDATE silently touching zero
# rows. Neither knob is part of the Leveling cog's hot-path config mirror (see
# the class docstring), so neither write refreshes it.
#
# The INSERT branch seeds ``enabled`` from the legacy
# guild_settings.leveling_enabled JSONB bool, EXACTLY like every other
# level_config writer in the house. That is not cosmetic: a legacy guild whose
# leveling is ON only through that JSONB flag has no level_config row at all,
# so a bare INSERT here would create one with enabled defaulting to FALSE - and
# tools.leveling.resolve_config, which prefers the row over the legacy bool,
# would then answer "leveling off" on the next restart and that guild would
# silently stop earning XP just because an admin picked a champion role. The
# ON CONFLICT branch keeps touching ONLY its own column, so neither statement
# can ever turn leveling on or off by itself.
_SET_CHAMPION_ROLE_SQL = """
    INSERT INTO level_config (guild_id, enabled, season_champion_role_id)
    VALUES (
        $1,
        COALESCE(
            (SELECT (settings->>'leveling_enabled')::boolean
             FROM guild_settings WHERE guild_id = $1),
            FALSE
        ),
        $2
    )
    ON CONFLICT (guild_id) DO UPDATE SET season_champion_role_id = $2;
    """

_SET_SEASON_ANNOUNCE_SQL = """
    INSERT INTO level_config (guild_id, enabled, season_announce)
    VALUES (
        $1,
        COALESCE(
            (SELECT (settings->>'leveling_enabled')::boolean
             FROM guild_settings WHERE guild_id = $1),
            FALSE
        ),
        $2
    )
    ON CONFLICT (guild_id) DO UPDATE SET season_announce = $2;
    """


class Seasons(commands.Cog):
    """Closes leveling seasons: podium snapshot, champion role, announce.

    Owns exactly one table (``season_podiums``) and reacts only when called -
    no listener, no task, no persistent view. The leveling cog dispatches here
    on a month rollover (``bot.get_cog("Seasons")``, the house cross-cog seam),
    and any read surface can call :meth:`ensure_season_snapshot` directly to
    materialize a month that closed while the guild was quiet.

    Two visible commands (S2): ``/halloffame`` (browse past podiums) and, via
    LevelConfigUI's ``/levelconfig seasons`` delegation, :meth:`cmd_seasons_panel`
    (configure the champion role and the announce toggle).

    Scale: the work is bounded at ONE snapshot per ACTIVE guild per MONTH. The
    detection that gates it is a single in-memory marker test on the leveling
    cog's already-existing BoundedLRU, so a guild that has not rolled a month
    costs nothing at all, and 1000+ guilds cost 1000 cheap statements spread
    across a whole month rather than any synchronized sweep.
    """

    def __init__(self, bot):
        self.bot = bot

    # -- public engine -------------------------------------------------
    async def ensure_season_snapshot(self, guild, period_key=None, *, now=None):
        """Freeze the podium of a CLOSED month for ``guild``, exactly once.

        ``guild`` is a :class:`discord.Guild` (the champion role and the
        announce both need it; a ``None`` guild is a quiet no-op).
        ``period_key`` names the CLOSED month to freeze. The activity hook
        always passes one: the month its period marker knows this guild last
        earned XP in (tools.leveling.season_rollover_period_key) or, when that
        marker was cold, the month it resolved from the data before pruning.
        A caller that genuinely cannot say - a read surface opening the hall of
        fame - omits it and gets it RESOLVED here instead
        (:meth:`_resolve_closed_month`, over the shared
        tools.leveling.LATEST_CLOSED_MONTH_SQL). "The month before now" is NOT
        the default, because a guild that stayed silent for a whole month
        closed an OLDER month and would otherwise have its only podium skipped
        forever.

        Returns the podium rows this call INSERTED as a rank-ordered list of
        ``(rank, user_id, xp)`` tuples, or an empty list when there was nothing
        to do (already snapshotted, no closed month left, no XP at all that
        month, a lost race, or a failure). NEVER raises - it is called from the
        leveling grant path.

        Side effects (champion role, announce) run only for the caller that
        actually inserted the podium, and only AFTER it is committed, so they
        can never cost a guild its season history.

        CONTRACT for later lots (S2's hall of fame and any backfill tool):

        * only ever name a month that is REALLY closed. ``period_key`` is
          trusted verbatim - nothing here re-checks that it is in the past, so
          passing the CURRENT month would freeze a podium mid-month and the
          ``ON CONFLICT DO NOTHING`` would then refuse to ever correct it.
        * the side effects belong to the LATEST closed month only. A caller
          that names an OLDER month (a deliberate backfill of a season the bot
          missed) still gets its podium frozen, but silently: no champion role
          move, no announce - see :meth:`_run_side_effects`. Crowning the winner
          of a months-old season would strip the CURRENT champion and ping a
          channel about a month nobody is playing any more.
        """
        if guild is None:
            return []
        now = now or discord.utils.utcnow()
        # Whether the side effects still have to prove this is the latest
        # closed month: a key we resolved ourselves IS that month by
        # construction, so only a caller-supplied one is ever re-checked (and
        # even then only once the podium was actually frozen).
        verify_latest = bool(period_key)
        try:
            if not period_key:
                period_key = await self._resolve_closed_month(guild.id, now)
                if period_key is None:
                    return []
            # Cheap re-verification before any expensive work: the overwhelmingly
            # common case for a repeat trigger (a restart, an LRU eviction, a
            # read surface opening twice) is "already frozen", answered by one
            # index-only scan instead of a sort over the month's rows.
            already = await self.bot.db_pool.fetchval(
                _SEASON_EXISTS_SQL, guild.id, period_key
            )
            if already:
                return []

            rows = await self.bot.db_pool.fetch(
                _SEASON_SNAPSHOT_SQL,
                guild.id,
                period_key,
                leveling.SEASON_PODIUM_SIZE,
            )
            if not rows:
                # Either nobody earned XP in that month (nothing to freeze) or a
                # concurrent trigger won the race and is running the side
                # effects itself. Both mean: stay silent.
                return []

            podium = sorted(
                (int(row["rank"]), row["user_id"], row["xp"]) for row in rows
            )
        except Exception:
            log.exception(
                "Failed to snapshot the season podium for guild %s season %s",
                guild.id,
                period_key,
            )
            return []

        log.info(
            "Closed leveling season %s for guild %s (%d podium place(s))",
            period_key,
            guild.id,
            len(podium),
        )
        try:
            await self._run_side_effects(
                guild, period_key, podium, now=now, verify_latest=verify_latest
            )
        except Exception:
            # The podium is already committed; a champion/announce hiccup is
            # never allowed to make this look like a failed snapshot.
            log.exception(
                "Season side effects failed for guild %s season %s",
                guild.id,
                period_key,
            )
        return podium

    # -- closed-month resolution -----------------------------------------
    async def _resolve_closed_month(self, guild_id, now):
        """The month this guild last earned XP in, before the current one.

        The data-driven answer for a caller that cannot name the closed season
        (see :meth:`ensure_season_snapshot`). Returns ``None`` when the guild
        has no closed month left to freeze at all - a brand new guild, or one
        whose old rows the retention prune already dropped.

        Bounded by design: it can only ever reach back as far as xp_period
        retention keeps rows (tools.leveling.PRUNE_PERIODS_BACK months), which
        is the standing contract of the whole period rollup - a season that
        went unfrozen for longer than that is genuinely gone. The prune is what
        keeps that from happening on the LAST active month: it clamps its
        monthly cutoff to the month awaiting a snapshot, on BOTH of its
        branches - the one whose in-memory marker names that month, and the
        cold-marker one, which resolves it with THIS very query (the shared
        tools.leveling.LATEST_CLOSED_MONTH_SQL) before deleting anything.
        """
        return await self.bot.db_pool.fetchval(
            leveling.LATEST_CLOSED_MONTH_SQL,
            guild_id,
            leveling.month_period_key(now),
        )

    async def _is_latest_closed_month(self, guild_id, period_key, now):
        """Whether ``period_key`` is the guild's LAST closed month (see
        :meth:`_run_side_effects`'s backfill guard).

        Fails OPEN on a DB hiccup: the caller-supplied key is overwhelmingly
        the live rollover (the activity path always names the month its marker
        just closed), so a failed verification must not cost that guild its
        champion role and announce - the rare, deliberate backfill is what this
        guard is aimed at, not a transient error.
        """
        try:
            latest = await self._resolve_closed_month(guild_id, now)
        except Exception:
            log.exception(
                "Failed to verify the latest closed month for guild %s", guild_id
            )
            return True
        return latest is None or latest == period_key

    # -- one-shot side effects ------------------------------------------
    async def _run_side_effects(
        self, guild, period_key, podium, *, now, verify_latest
    ):
        """Champion role then announce, for the caller that won the snapshot.

        Both are opt-in per guild and both are best effort: the champion role
        is skipped (with a log) when it is unset, deleted, unmanageable or its
        winner has left, and the announce is skipped when the guild never opted
        in or its announce_mode gives no channel to post in.

        Two structural guards run BEFORE either of them, because both crown
        ``podium[0]``:

        * the RETURNING must actually contain rank 1. It normally does (the
          INSERT writes the whole podium in one statement), but a partial
          insert is representable - a concurrent trigger that wrote rank 1 a
          moment earlier leaves us the ranks 2..3 it did not have. Crowning the
          runner-up is worse than crowning nobody, so a podium that does not
          start at rank 1 is logged and skipped.
        * ``period_key`` must be the LATEST closed month when the CALLER named
          it (``verify_latest``; a key we resolved ourselves already is that
          month). This is the backfill guard of the contract in
          :meth:`ensure_season_snapshot`: freezing an old season is welcome,
          crowning its winner over the current champion is not. The extra
          lookup is paid at most once per guild per MONTH - only after a podium
          was really frozen, and only for a guild that opted into an effect.
        """
        if podium[0][0] != 1:
            log.warning(
                "Season %s for guild %s was frozen without rank 1 (ranks %s); "
                "skipping the champion role and the announce",
                period_key,
                guild.id,
                [rank for rank, _user_id, _xp in podium],
            )
            return

        row = await self.bot.db_pool.fetchrow(_SEASON_CONFIG_SQL, guild.id)
        if row is None:
            # No level_config row at all: both season knobs are at their
            # inert defaults (no champion role, announce off).
            return

        role_id = row["season_champion_role_id"]
        if role_id is None and not row["season_announce"]:
            return  # nothing opted in: no guard to pay, nothing to do

        if verify_latest and not await self._is_latest_closed_month(
            guild.id, period_key, now
        ):
            log.info(
                "Season %s for guild %s is a backfill (an older month than the "
                "last closed one); podium frozen, no champion role, no announce",
                period_key,
                guild.id,
            )
            return

        champion_role = None
        if role_id is not None:
            champion_role = await self._apply_champion_role(
                guild, role_id, period_key, podium[0][1]
            )

        if not row["season_announce"]:
            return
        channel_id = leveling.resolve_season_announce_channel(
            row["announce_mode"], row["announce_channel_id"]
        )
        if channel_id is None:
            # WARNING, not debug: this is an admin MISCONFIGURATION, not a
            # normal skip. The guild explicitly turned the season announce ON,
            # so a silent no-op would be indistinguishable from a bug. Only
            # "fixed" mode with a real channel can receive a guild-wide season
            # announce (there is no origin channel and no single member to DM),
            # and this fires at most once per guild per MONTH, so it can never
            # flood the log.
            log.warning(
                "Season announce is ON for guild %s but announce_mode %r "
                "resolves to no channel - set the announce mode to a fixed "
                "channel for the season podium to be posted",
                guild.id,
                row["announce_mode"],
            )
            return
        await self._announce_season(
            guild, channel_id, period_key, podium, champion_role
        )

    async def _resolve_member(self, guild, member_id):
        """A guild member by id, cache first then ONE HTTP fetch.

        The house pattern (cogs/config/reactionroles.py): the bot runs with
        ``chunk_guilds_at_startup=False``, and - verified in discord.py -
        MESSAGE_CREATE does NOT populate the member cache (the Member is built
        off the message and never added to ``guild._members``), while
        GUILD_CREATE only ships members up to ``large_threshold``. So on any
        guild of real size ``guild.get_member`` returns ``None`` for most
        members, INCLUDING the one who just won the season (the rollover is
        triggered by somebody ELSE's message). A cache-only lookup would
        silently never grant the champion role there.

        One fetch per member per MONTH is a rounding error against that, and a
        genuinely departed member simply resolves to ``None`` (NotFound is an
        HTTPException subclass, so the one handler covers both).
        """
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except discord.HTTPException:
            return None

    async def _previous_champion_id(self, guild_id, period_key):
        """The user id that won the season BEFORE ``period_key``, or ``None``.

        Read from ``season_podiums`` (the table this cog owns) rather than
        inferred from the member cache - see :meth:`_apply_champion_role`.
        Best effort: a DB hiccup here must not cost the winner their role, so a
        failure degrades to "no known previous champion".
        """
        try:
            return await self.bot.db_pool.fetchval(
                _PREVIOUS_CHAMPION_SQL, guild_id, period_key
            )
        except Exception:
            log.exception(
                "Failed to read the previous season champion for guild %s",
                guild_id,
            )
            return None

    async def _apply_champion_role(self, guild, role_id, period_key, winner_id):
        """Move the champion role to the closed season's #1, REPLACE style.

        The previous holder(s) lose it first, then the winner gains it - so the
        role always names exactly one champion, the current one. This does NOT
        go through the LevelRewards cog: that machinery is rules-driven (a
        ``level_rewards`` row per "reach level N, get role R", reconciled by
        tools.level_rewards.decide_role_changes), and a champion role is not a
        level tier - it has no level, no stack/replace mode of its own and only
        ever one holder. What IS reused is that cog's hierarchy guard
        (``_assignable``: not @everyone, not integration-managed, strictly below
        the bot's top role), so a champion role obeys exactly the same rules as
        a reward role.

        ``period_key`` is the season being CLOSED; it is what locates the
        OUTGOING champion (the rank 1 of the most recent season strictly before
        it, :meth:`_previous_champion_id`) without depending on the member
        cache being populated.

        Best effort end to end: a deleted role, a role above the bot, a winner
        who left, or a per-member HTTP failure is logged and skipped, never
        raised - the podium row stands regardless. Returns the role when the
        winner ends up holding it, else ``None``.
        """
        role = guild.get_role(role_id)
        if role is None:
            log.info(
                "Season champion role %s no longer exists in guild %s",
                role_id,
                guild.id,
            )
            return None
        if not _assignable(role, guild):
            log.info(
                "Cannot manage season champion role %s in guild %s "
                "(above my top role, managed, or @everyone)",
                role_id,
                guild.id,
            )
            return None

        # REPLACE first: the outgoing champion loses the role even if the new
        # winner turns out to be unreachable, so a stale champion is never left
        # wearing it. The AUTHORITATIVE outgoing champion is last season's rank
        # 1, read from season_podiums - NOT Role.members, which (verified in
        # discord.py: it filters guild._members) only ever sees the member
        # CACHE and is typically empty under chunk_guilds_at_startup=False.
        # Role.members is still folded in as a best-effort net for holders the
        # bot happens to know about (a role handed out by hand, a champion from
        # before this cog existed) - O(cached members) once per guild per MONTH.
        holder_ids = {holder.id for holder in getattr(role, "members", ())}
        previous_champion_id = await self._previous_champion_id(guild.id, period_key)
        if previous_champion_id is not None:
            holder_ids.add(previous_champion_id)
        holder_ids.discard(winner_id)
        for holder_id in sorted(holder_ids):
            holder = await self._resolve_member(guild, holder_id)
            if holder is None:
                continue
            if not any(r.id == role.id for r in getattr(holder, "roles", ())):
                continue  # already stripped (or never actually held it)
            try:
                await holder.remove_roles(role, reason=_REASON_REPLACE)
            except discord.HTTPException:
                log.debug(
                    "Failed to strip the season champion role from %s in guild %s",
                    holder_id,
                    guild.id,
                )

        winner = await self._resolve_member(guild, winner_id)
        if winner is None:
            log.info(
                "Season champion %s is unreachable in guild %s (left, or the "
                "member fetch failed); role not granted",
                winner_id,
                guild.id,
            )
            return None
        if any(r.id == role.id for r in getattr(winner, "roles", ())):
            return role  # already wearing it (a back-to-back win)
        try:
            await winner.add_roles(role, reason=_REASON_GRANT)
        except discord.HTTPException:
            log.debug(
                "Failed to grant the season champion role to %s in guild %s",
                winner_id,
                guild.id,
            )
            return None
        return role

    async def _announce_season(
        self, guild, channel_id, period_key, podium, champion_role
    ):
        """Post the closed season's podium in the guild's leveling channel.

        Rendered in the GUILD's locale (a rollover has no invoking user, so the
        per-guild resolution is the right one - same shape as the AniList feed's
        channel deliveries). Mentions are users-only: the podium members are
        pinged (that is the point of naming them), while the champion role
        mention renders as a highlight WITHOUT notifying every past champion.
        """
        channel = guild.get_channel(channel_id)
        if channel is None:
            log.debug(
                "Season announce channel %s is missing in guild %s",
                channel_id,
                guild.id,
            )
            return
        loc = await i18n.resolve_guild_locale(self.bot, guild)
        allowed_mentions = discord.AllowedMentions(
            everyone=False, roles=False, users=True
        )
        try:
            # Render AND send inside the locale block: the context manager
            # resets in a finally, so one guild's locale can never leak into
            # the next guild handled by the same task.
            with i18n.locale(loc):
                await channel.send(
                    self._render_announce(period_key, podium, champion_role),
                    allowed_mentions=allowed_mentions,
                )
        except discord.Forbidden:
            log.debug(
                "No permission to post the season announce in channel %s "
                "(guild %s)",
                channel_id,
                guild.id,
            )
        except discord.HTTPException:
            log.debug(
                "Failed to post the season announce in channel %s (guild %s)",
                channel_id,
                guild.id,
            )

    @staticmethod
    def _render_announce(period_key, podium, champion_role):
        """The announce text: a header, up to three podium lines, and (only
        when the role actually moved) one champion line. Deliberately sober -
        no embed, no card: this lands in a channel members already read.
        """
        lines = [
            _("**Season {month} is over!** Here is the final podium:").format(
                month=leveling.format_month_period_label(period_key)
            )
        ]
        for rank, user_id, xp in podium:
            lines.append(
                _("{medal} {user} - {xp} XP").format(
                    medal=_MEDALS.get(rank, "#{rank}".format(rank=rank)),
                    user="<@{user_id}>".format(user_id=user_id),
                    xp=xp,
                )
            )
        if champion_role is not None:
            lines.append(
                _("{user} is the new season champion and receives {role}.").format(
                    user="<@{user_id}>".format(user_id=podium[0][1]),
                    role=champion_role.mention,
                )
            )
        return "\n".join(lines)

    # -- S2 read surface: hall-of-fame browsing queries -------------------
    async def latest_season_key(self, guild_id):
        """The most recent season this guild ever froze, or ``None``."""
        return await self.bot.db_pool.fetchval(_LATEST_SEASON_KEY_SQL, guild_id)

    async def older_season_key(self, guild_id, period_key):
        """The closest frozen season strictly OLDER than ``period_key``, or
        ``None`` when ``period_key`` is already the oldest one on record."""
        return await self.bot.db_pool.fetchval(
            _OLDER_SEASON_KEY_SQL, guild_id, period_key
        )

    async def newer_season_key(self, guild_id, period_key):
        """The closest frozen season strictly NEWER than ``period_key``, or
        ``None`` when ``period_key`` is already the latest one on record."""
        return await self.bot.db_pool.fetchval(
            _NEWER_SEASON_KEY_SQL, guild_id, period_key
        )

    async def season_podium_rows(self, guild_id, period_key):
        """``(rank, user_id, xp)`` tuples for one frozen season, rank-ordered."""
        rows = await self.bot.db_pool.fetch(
            _SEASON_POD_ROWS_SQL, guild_id, period_key
        )
        return [(row["rank"], row["user_id"], row["xp"]) for row in rows]

    # -- S2 admin writes: the seasons panel --------------------------------
    async def set_champion_role(self, guild_id, role_id):
        """Persist (or clear, with ``role_id=None``) the season champion role."""
        await self.bot.db_pool.execute(_SET_CHAMPION_ROLE_SQL, guild_id, role_id)

    async def set_season_announce(self, guild_id, enabled):
        """Persist the season-rollover announce toggle."""
        await self.bot.db_pool.execute(
            _SET_SEASON_ANNOUNCE_SQL, guild_id, bool(enabled)
        )

    # -- S2 commands --------------------------------------------------------
    @commands.hybrid_command(name="halloffame")
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def halloffame(self, ctx):
        """Browse this server's leveling season podiums, most recent first."""
        await self.cmd_halloffame(ctx)

    async def cmd_halloffame(self, ctx):
        """The ``/halloffame`` body, also the ``get_cog`` seam for a future
        caller (matches the house delegation shape, see LevelConfigUI's
        ``cmd_*`` wrappers around LevelRewards/LevelAdmin)."""
        guild = ctx.guild
        # ACKNOWLEDGE FIRST. Opening the hall of fame is also the on-demand
        # entry point that can CLOSE a season, and that path is not a read: on
        # the one invocation per guild per month that actually wins the
        # snapshot it fetches members over HTTP, moves the champion role and
        # posts the announce - comfortably past the 3s an un-deferred slash
        # interaction gets, which would show "the application did not respond"
        # and then lose the card to an expired token. Deferring costs nothing on
        # every other invocation, and is a no-op for a prefix call (verified in
        # discord.py: Context.defer only acts when self.interaction is set).
        await ctx.defer()
        # Materialize a closed month the guild's own activity never triggered
        # (see the module docstring's contract): a read surface is exactly the
        # on-demand entry point ensure_season_snapshot documents for this.
        await self.ensure_season_snapshot(guild)

        period_key = await self.latest_season_key(guild.id)
        if period_key is None:
            await ctx.send(
                _("No leveling season has closed here yet - check back next month.")
            )
            return

        podium = await self.season_podium_rows(guild.id, period_key)
        # We just asked for the LATEST season, so by definition nothing is
        # newer - no query needed to know that "Next" starts disabled.
        has_older = await self.older_season_key(guild.id, period_key) is not None
        view = seasons_views.HallOfFameCard(
            self, guild, ctx.author.id, period_key, podium, has_older, False
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def cmd_seasons_panel(self, ctx):
        """Open the ``/levelconfig seasons`` admin panel (delegated from
        LevelConfigUI, the house cross-cog shape)."""
        row = await self.bot.db_pool.fetchrow(_SEASON_CONFIG_SQL, ctx.guild.id)
        state = seasons_views.season_panel_state(row)
        view = seasons_views.SeasonsPanel(self, ctx.guild, ctx.author.id, state)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(Seasons(bot))
