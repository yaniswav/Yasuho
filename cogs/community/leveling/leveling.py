import asyncio
import datetime
import io
import logging
import os
from typing import Literal, Optional

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from . import engine as leveling
from . import gate as leveling_gate
from . import rank_card
from tools import rendering, settings
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _, ngettext
from tools.lru_cache import BoundedLRU
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# Bundled TTF used for the rank card; falls back to Pillow's default if missing.
_FONT_PATH = os.path.join("ressources", "fonts", "impact.ttf")

# Neutral Discord avatar used when a top-ranked member has left the guild and no
# real avatar is available for the Section thumbnail accessory.
_DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"

# Components V2 budget: how many ranks get their own avatar Section (podium) on
# page 0. The remaining ranks on the page (and every rank on later pages) render
# as a plain text list. The per-page rank count itself lives in
# cogs.community.leveling.engine.LEADERBOARD_PAGE_SIZE (the pager's home).
_PODIUM_SLOTS = 5

# Medal glyphs for the top three; lower ranks fall back to a plain number.
# Shared with the season announce and the hall-of-fame card through
# cogs.community.leveling.engine.PODIUM_MEDALS so all three podium surfaces mark the same ranks
# with the same glyphs; aliased locally so every call site below reads unchanged.
_MEDALS = leveling.PODIUM_MEDALS

# No-xp snapshot cache ceiling (tools.lru_cache.BoundedLRU): comfortably above
# any plausible number of guilds with leveling enabled AND no-xp zones
# configured, so eviction is a rare, harmless extra DB read rather than a
# steady-state cost - see NoXpSnapshot's cog-level cache, self._no_xp below.
_NO_XP_CACHE_CAP = 2048

# XP-multiplier snapshot cache ceiling (L4). Same sizing rationale as
# _NO_XP_CACHE_CAP: comfortably above any plausible number of guilds with
# leveling enabled AND boosts/an event configured - see self._multipliers.
_MULTIPLIER_CACHE_CAP = 2048

# Per-guild "last seen period" marker cache ceiling (L6). Same sizing
# rationale as _NO_XP_CACHE_CAP/_MULTIPLIER_CACHE_CAP: comfortably above any
# plausible number of guilds with leveling enabled - see self._period_markers
# and maybe_prune_expired_periods. An evicted guild looks cold on its next
# grant and pays that method's cold branch once: the extra DELETE, the
# closed-month lookup that clamps it, and an idempotent season snapshot that
# almost always stops on its exists probe. Rare and bounded, never a
# correctness issue - the same branch every guild takes once after a restart.
_PERIOD_MARKER_CACHE_CAP = 2048

# Per-guild rank-card STYLE cache ceiling (RC1). Much smaller than its siblings
# above on purpose: this one is not on any hot path - it is read once per /rank
# invocation, a rare, human-paced, already-rate-limited command - so its job is
# only to spare the DB a repeated lookup during a burst of /rank in the same
# guild. 512 entries of (accent tuple | None, bool) is a few tens of KiB; the
# background BYTES are deliberately NOT cached (see _rank_cards below).
_RANK_CARD_CACHE_CAP = 512

# What a guild with no rank_cards row looks like, cached verbatim so a stock
# guild's repeat /rank calls do not re-query. Distinct from a cache MISS (which
# BoundedLRU reports as None), hence a real tuple rather than None.
_STOCK_RANK_CARD = (None, False)

# Scrim painted over a custom background before any text is drawn. The stock
# card's text colours were picked against the near-black panel below, so a
# bright photo would make them unreadable; this keeps the contrast budget the
# layout was designed for while leaving the image clearly visible.
_BACKGROUND_SCRIM = (18, 19, 26)
_BACKGROUND_SCRIM_ALPHA = 150

# The full set of level_config columns the hot-path LevelConfig mirror is built
# from. EVERY read that refreshes a cached config - cog_load's bulk SELECT and
# each writer's RETURNING - must project ALL of them, or a writer that omits one
# would silently reset that knob in the cache (LevelConfig.from_row defaults an
# absent column) until the next restart. Kept in one place so a new column added
# to the cached config (voice_xp_* here) lands in every query at once.
_CONFIG_COLUMNS = (
    "enabled, cooldown_seconds, xp_min, xp_max, "
    "announce_mode, announce_channel_id, announce_template, "
    "voice_xp_enabled, voice_xp_per_minute"
)


class _PagerButton(discord.ui.Button):
    """A leaderboard pager button whose click delegates to a bound handler.

    Components V2 layouts cannot use the ``@discord.ui.button`` decorator
    (buttons live inside :class:`discord.ui.ActionRow` children), so Prev/Next
    are plain instances that forward their click to a coroutine on the owning
    view - the same shape as the music cog's ``_ControllerButton``.
    """

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction):
        await self._handler(interaction)


class LeaderboardView(AuthorLayoutView):
    """Paginated Components V2 podium for the guild XP leaderboard.

    Page 0 keeps the podium: the top :data:`_PODIUM_SLOTS` ranks each become a
    :class:`discord.ui.Section` with the member's avatar as a
    :class:`discord.ui.Thumbnail` accessory, and the rest of the page collapses
    into one :class:`discord.ui.TextDisplay` ranked list (the V2 component
    budget). Page 1+ drops the avatars entirely for a single plain ranked list -
    a member scrolling past the top 15 wants the numbers, not fifteen more
    thumbnails. Prev/Next walk pages of :data:`~cogs.community.leveling.engine.LEADERBOARD_PAGE_SIZE`
    and are author-gated through :class:`~tools.views.AuthorLayoutView` (only the
    member who ran /leaderboard drives them), so a busy channel never has strangers
    flipping each other's boards. The pager row only appears when there is more
    than one page, so a board of 15 or fewer renders exactly as it did before L5.
    """

    def __init__(self, author_id, title, entries, *, timeout=180):
        # entries: list of dicts with rank, name, xp, avatar_url - the FULL
        # ranked list (up to the query's LIMIT), sliced per page here. The
        # all-time view also carries a "level" key; the period views
        # (weekly/monthly) omit it - the render branches on ``"level" in entry``
        # and shows XP only when it is absent, so never index entry["level"]
        # unconditionally.
        super().__init__(author_id, timeout=timeout)
        self.title = title
        self.entries = entries
        self.page = 0
        self._build()

    def _entry_line(self, entry):
        """One plain ranked line (used by the page-0 remainder AND page 1+)."""
        if "level" in entry:
            return _("**#{rank}** {name} - level **{level}** ({xp} XP)").format(
                rank=entry["rank"],
                name=entry["name"],
                level=entry["level"],
                xp=entry["xp"],
            )
        return _("**#{rank}** {name} - {xp} XP").format(
            rank=entry["rank"], name=entry["name"], xp=entry["xp"]
        )

    def _podium_text(self, entry):
        """The Section text for a top-ranked member on page 0."""
        marker = _MEDALS.get(entry["rank"], "**#{rank}**".format(rank=entry["rank"]))
        if "level" in entry:
            # All-time view: levels are lifetime-only, so this is the ONLY branch
            # that ever shows one - byte-for-byte the original text.
            return _("{marker} **{name}**\nLevel **{level}** - {xp} XP").format(
                marker=marker,
                name=entry["name"],
                level=entry["level"],
                xp=entry["xp"],
            )
        # Period view (weekly/monthly): no lifetime level, just the period XP.
        return _("{marker} **{name}**\n{xp} XP").format(
            marker=marker, name=entry["name"], xp=entry["xp"]
        )

    def _build(self):
        self.clear_items()
        total = len(self.entries)
        self.page, total_pages, start, end = leveling.leaderboard_page(
            total, self.page
        )
        page_entries = self.entries[start:end]

        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay("## {title}".format(title=self.title))
        )
        container.add_item(discord.ui.Separator())

        if self.page == 0:
            # Page 0 keeps the podium: avatars for the top ranks, the rest as a
            # plain list - byte-for-byte the pre-L5 single-page layout.
            podium = page_entries[:_PODIUM_SLOTS]
            remainder = page_entries[_PODIUM_SLOTS:]
            for entry in podium:
                container.add_item(
                    discord.ui.Section(
                        discord.ui.TextDisplay(self._podium_text(entry)),
                        accessory=discord.ui.Thumbnail(entry["avatar_url"]),
                    )
                )
            if remainder:
                container.add_item(discord.ui.Separator())
                container.add_item(
                    discord.ui.TextDisplay(
                        "\n".join(self._entry_line(e) for e in remainder)
                    )
                )
        else:
            # Page 1+: a single plain ranked list, no avatars.
            container.add_item(
                discord.ui.TextDisplay(
                    "\n".join(self._entry_line(e) for e in page_entries)
                )
            )

        if total_pages > 1:
            container.add_item(discord.ui.Separator())
            members = ngettext(
                "{count} member", "{count} members", total
            ).format(count=total)
            container.add_item(
                discord.ui.TextDisplay(
                    _("-# Page {page}/{pages} - {members}").format(
                        page=self.page + 1, pages=total_pages, members=members
                    )
                )
            )
            container.add_item(
                discord.ui.ActionRow(
                    _PagerButton(
                        self._prev,
                        label=_("Prev"),
                        emoji="◀️",
                        style=discord.ButtonStyle.secondary,
                        disabled=self.page <= 0,
                    ),
                    _PagerButton(
                        self._next,
                        label=_("Next"),
                        emoji="▶️",
                        style=discord.ButtonStyle.secondary,
                        disabled=self.page >= total_pages - 1,
                    ),
                )
            )

        self.add_item(container)

    async def _prev(self, interaction):
        try:
            self.page -= 1
            self._build()
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("Leaderboard prev failed")

    async def _next(self, interaction):
        try:
            self.page += 1
            self._build()
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("Leaderboard next failed")


class Leveling(commands.Cog):
    """XP and leveling commands."""

    def __init__(self, bot):
        self.bot = bot
        # The sweep baseline for the debounce map is the default cooldown; the
        # ACTUAL window is per-guild and passed to is_active() on each check.
        self._cooldowns = Cooldowns(leveling.DEFAULT_COOLDOWN_SECONDS)
        # Per-guild leveling config for guilds with leveling ON, mirrored in memory
        # so the on_message hot path answers "can this guild earn XP, and with what
        # knobs?" with a single dict.get (zero awaits, zero allocations) instead of
        # a per-message settings read. Membership == enabled: a guild absent from
        # the map earns no XP, and a present guild hands its whole config
        # (cooldown, xp band) back in that same lookup. Most guilds leave leveling
        # off (the default), so the miss branch short-circuits the overwhelming
        # majority of messages bot-wide. Loaded once in cog_load and kept live by
        # set_enabled (the config toggle). level_config (the DB) is the source of
        # truth; this map is a hot-path mirror, rebuilt on every restart. Bounded by
        # the number of guilds that ENABLE leveling, so it needs no eviction.
        self._configs: dict[int, leveling.LevelConfig] = {}
        # The two bot-mention command prefixes, cached once. bot.user is only
        # known after login, so this is filled lazily on first use (on_message
        # never fires before the bot is ready).
        self._mention_prefixes: tuple[str, ...] | None = None
        # Per-guild no-xp-zone snapshot (cogs.community.leveling.engine.NoXpSnapshot: two
        # frozensets of channel/category ids and role ids), loaded from
        # level_no_xp on a guild's first grant-eligible message and kept live by
        # refresh_no_xp_snapshot (called on every level_no_xp write, from
        # cogs/community/leveling/level_config_ui.py). Bounded, unlike self._configs:
        # every ENABLED guild eventually gets an entry here (even an empty one,
        # once it earns its first XP), so this is genuinely unbounded by guild
        # count and needs the same size-cap tools.settings uses for user blobs.
        # An evicted guild simply re-reads its (usually tiny) row set on its
        # next grant-eligible message - a rare, harmless extra query, never a
        # per-message cost (SCALE STORY).
        self._no_xp: BoundedLRU = BoundedLRU(_NO_XP_CACHE_CAP)
        # Per-guild XP-multiplier snapshot (cogs.community.leveling.engine.MultiplierSnapshot:
        # global/channel/role factors plus the active timed event, see that
        # class's docstring), the L4 sibling of self._no_xp above - same
        # cached-or-load contract (ensure_multiplier_snapshot), same
        # write-path refresh hook (refresh_multiplier_snapshot, called by
        # cogs/community/leveling/level_config_ui.py after every boost/event write),
        # same BoundedLRU sizing rationale.
        self._multipliers: BoundedLRU = BoundedLRU(_MULTIPLIER_CACHE_CAP)
        # Per-guild "last seen period" marker (L6): the (week_key, month_key)
        # pair this guild's most recent grant/credit already observed. Read
        # by maybe_prune_expired_periods to decide, in O(1) with zero DB on
        # the common case, whether a period just rolled over for this guild
        # and its xp_period rows are due for a lazy prune. Bounded like
        # self._no_xp / self._multipliers above (same rationale).
        self._period_markers: BoundedLRU = BoundedLRU(_PERIOD_MARKER_CACHE_CAP)
        # Per-guild rank-card STYLE (RC1): ``(accent_rgb | None, has_background)``
        # mirrored from the rank_cards table, or _STOCK_RANK_CARD for a guild
        # that never customised its card. Kept live by invalidate_rank_card,
        # which RC2's panel and cogs/system/dashboard_sync.py (kind
        # 'rank_card') both call after a write. SCALE STORY: this caches
        # METADATA ONLY - the background image bytes (up to ~512 KiB each) are
        # re-read from Postgres by the render itself, so 512 cached guilds cost
        # tens of KiB rather than a quarter of a gigabyte, and the extra
        # primary-key lookup rides inside a render already gated by the shared
        # 2-slot image semaphore.
        self._rank_cards: BoundedLRU = BoundedLRU(_RANK_CARD_CACHE_CAP)
        # Top.gg vote boost (V1): user_id -> the UTC datetime that voter's XP
        # boost runs out. A PLAIN DICT, not a BoundedLRU like its siblings
        # above, because eviction here would silently cancel a boost somebody
        # earned - and it does not need one: entries are self-expiring and only
        # a real vote ever creates one.
        # SCALE STORY. Size is bounded by "people who voted for the bot and are
        # still inside their boost window", plus at most the ones who have voted
        # since the last sweep - a handful even at thousands of guilds, and each
        # entry is an int key and a datetime. Three things keep it there: the
        # boot read below loads ONLY unexpired rows, every read on the two XP
        # hot paths deletes the entry it finds expired (apply_vote_boost), and
        # every vote sweeps the whole map before adding its own (note_vote_boost
        # - the only writer, and rare). No timer, no task, nothing to schedule.
        self._vote_boosts: dict[int, datetime.datetime] = {}
        # Strong refs to the in-flight season-rollover tasks (S1), so the event
        # loop cannot garbage-collect one mid-flight (core.py's
        # _schedule_startup_backup pattern). Self-bounding: a guild schedules at
        # most ONE per month and each entry is discarded by its own done
        # callback, so this holds only the handful of guilds whose month is
        # rolling over right now.
        self._season_tasks: set[asyncio.Task] = set()

    def cog_unload(self):
        """Cancel any in-flight season rollover on reload.

        A snapshot task outliving its cog would keep a stale bot/cog reference
        alive and could post an announce after a reload replaced the cog. The
        podium row is committed inside a single statement, so a cancellation
        either lands the snapshot or leaves the season untouched for the next
        trigger - never a half-frozen podium.
        """
        for task in list(self._season_tasks):
            task.cancel()

    async def cog_load(self):
        """Load every enabled guild's leveling config once, at startup.

        The read failure is swallowed HERE and only here: a failure at load
        leaves leveling dormant until the next toggle, which must never block
        the extension from loading. At runtime the same failure means something
        else entirely, so ``reload_configs`` raises and lets its caller decide.
        """
        try:
            await self.reload_configs()
        except Exception:
            log.exception("Failed to load leveling config")
        # Its OWN try: a vote-boost read that fails must not cost the whole
        # guild config map (and vice versa). Leveling works perfectly with an
        # empty boost map - every voter simply earns the normal amount until
        # their next vote re-arms them.
        try:
            await self.reload_vote_boosts()
        except Exception:
            log.exception("Failed to load top.gg vote boosts")

    async def reload_vote_boosts(self):
        """Prime the vote-boost map with every boost still running (V1).

        A boost outlives a restart because ``topgg_votes.boost_expires_at`` is
        stored, not derived: whoever was boosted when the process died is
        boosted again the moment it comes back, for exactly the remaining time.

        Reads ONLY unexpired rows, so the map starts at its true size rather
        than at "every voter we ever had". Runs during load_extension, i.e.
        before the gateway delivers a single message, so the hot path never sees
        a half-filled map; and like reload_configs it builds aside and swaps in
        one assignment.
        """
        rows = await self.bot.db_pool.fetch(
            "SELECT user_id, boost_expires_at FROM topgg_votes "
            "WHERE boost_expires_at > now();"
        )
        self._vote_boosts = {
            row["user_id"]: row["boost_expires_at"] for row in rows
        }
        log.info("Vote XP boost live for %d voter(s)", len(self._vote_boosts))

    def note_vote_boost(self, user_id, expires_at):
        """Arm (or extend) a voter's XP boost. The vote cog's ONLY way in.

        Called by cogs/community/votes.py right after the vote is banked, so the
        boost is live for the voter's very next message - no restart, no poll.

        This is also where the map is swept. It is the only writer and it runs
        at most once per vote (a person may vote every 12h), so an O(n) pass
        over a map of at most a few hundred entries is free here and saves both
        XP hot paths from ever needing more than one dict.get. The alternative -
        a timer - would be a scheduled task doing nothing 99.99% of the time.
        """
        now = discord.utils.utcnow()
        for uid in [
            uid for uid, expiry in self._vote_boosts.items() if expiry <= now
        ]:
            del self._vote_boosts[uid]
        self._vote_boosts[user_id] = expires_at

    def forget_vote_boost(self, user_id):
        """Drop a user's live boost; the erasure seam (V1).

        Reached from cogs/community/votes.forget_vote_boost, which the two
        profile-erasure paths call after privacy.delete_user_profile has removed
        the topgg_votes row. Returns whether anything was actually armed, and is
        idempotent: forgetting a user with no boost is a no-op, not an error.
        """
        return self._vote_boosts.pop(user_id, None) is not None

    def apply_vote_boost(self, user_id, amount, now):
        """Multiply an XP amount by this voter's boost, if one is live (V1).

        THE read shared by both XP paths - the on_message grant and the voice
        sweep - so the two can never disagree about what a vote is worth. The
        whole thing is ONE dict.get and a comparison against a ``now`` the
        caller already had in hand: no await, no DB, no clock read of its own,
        which is what lets it sit on a path that runs for every message that
        clears the cooldown.

        Deletes on read when it finds an expired entry: the map is only ever
        touched from the event loop, so this is safe without a lock, and it
        means a voter who stops voting stops costing memory the next time they
        speak rather than at some sweep in the future.

        Returns ``amount`` UNCHANGED for the overwhelming majority (everyone who
        has not voted), and never returns zero for a positive amount - a vote
        can only ever help.
        """
        expires_at = self._vote_boosts.get(user_id)
        if expires_at is None:
            return amount
        if expires_at <= now:
            del self._vote_boosts[user_id]
            return amount
        return leveling.apply_multiplier(amount, leveling.VOTE_BOOST_FACTOR)

    async def reload_configs(self):
        """Load every enabled guild's leveling config into the hot-path map.

        Runs during load_extension (setup_hook), before the gateway delivers any
        message, so the hot path sees a populated map from the very first event.
        Two reads, both over small tables: the level_config rows (the new source of
        truth), then the legacy guild_settings.leveling_enabled bool as a
        READ-THROUGH fallback for guilds that turned leveling on before level_config
        existed and have not re-toggled since. A level_config row always wins, so a
        guild that later switched leveling OFF via the new table is never
        resurrected by its stale JSONB value.

        RAISES on a failed read rather than logging and moving on: self._configs
        is only rebound after BOTH fetches returned, so a failure leaves the live
        map exactly as it was, and the caller has to be told - cog_load logs and
        starts dormant, resync_all logs and reports the step as NOT done instead
        of naming it on the "resynced" line.

        Split out of cog_load as a REUSABLE seam because self._configs is an
        eagerly-primed map whose ABSENCE means "leveling is off for this guild"
        (get_config is a plain synchronous dict read, no DB, no read-through):
        it can only ever be rebuilt, never cleared, or leveling would go silently
        dead bot-wide. cogs/system/dashboard_sync.py re-runs it after its LISTEN
        connection came back, where per-guild refresh_guild_config cannot help
        because the missed guild ids are exactly what a dropped NOTIFY loses. The
        map is built aside and swapped in one assignment, so the hot path never
        observes a partially loaded map.
        """
        configs: dict[int, leveling.LevelConfig] = {}
        rows = await self.bot.db_pool.fetch(
            f"SELECT guild_id, {_CONFIG_COLUMNS} FROM level_config;"
        )
        configured = set()
        for row in rows:
            gid = row["guild_id"]
            configured.add(gid)  # a row exists -> legacy fallback must skip it
            config = leveling.resolve_config(row, False)
            if config is not None:
                configs[gid] = config
        legacy = await self.bot.db_pool.fetch(
            "SELECT guild_id FROM guild_settings "
            "WHERE settings @> '{\"leveling_enabled\": true}'::jsonb;"
        )
        for row in legacy:
            gid = row["guild_id"]
            if gid not in configured:
                configs[gid] = leveling.resolve_config(None, True)
        self._configs = configs
        log.info("Leveling enabled in %d guild(s)", len(self._configs))

    async def set_enabled(self, guild_id, enabled):
        """Persist a leveling on/off toggle and refresh the hot-path config cache.

        Writes the level_config row (the source of truth) and updates the in-memory
        map so the change takes effect on the very next message, no restart. Only
        ``enabled`` is written, so any per-guild knobs a later lot may have set are
        preserved by the upsert; RETURNING the whole row keeps the cached config in
        step with what the DB now holds. This is the ONLY writer of
        level_config.enabled - the legacy JSONB bool is deliberately no longer
        written (read-through in cog_load handles guilds that predate this table).
        Called by the Settings cog through bot.get_cog (the house cross-cog seam).
        """
        row = await self.bot.db_pool.fetchrow(
            f"""
            INSERT INTO level_config (guild_id, enabled)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET enabled = $2
            RETURNING {_CONFIG_COLUMNS};
            """,
            guild_id,
            bool(enabled),
        )
        self._cache_config_row(guild_id, row)

    async def refresh_guild_config(self, guild_id):
        """Reload one guild after a retention-grace rejoin."""
        row = await self.bot.db_pool.fetchrow(
            f"SELECT {_CONFIG_COLUMNS} FROM level_config WHERE guild_id = $1;",
            guild_id,
        )
        if row is not None:
            self._cache_config_row(guild_id, row)
            return
        legacy_enabled = await settings.get_guild(
            self.bot.db_pool, guild_id, "leveling_enabled", False
        )
        config = leveling.resolve_config(None, legacy_enabled)
        if config is None:
            self._configs.pop(guild_id, None)
        else:
            self._configs[guild_id] = config

    def _cache_config_row(self, guild_id, row):
        """Resolve a level_config RETURNING row into the hot-path config map.

        Shared by every writer of level_config (set_enabled, set_announce_mode,
        set_announce_template): a row that leaves the guild enabled refreshes
        its cached :class:`~cogs.community.leveling.engine.LevelConfig`, a disabled one (or a
        somehow-missing row) drops the guild from the map entirely - mirroring
        cog_load's own read-through resolution so the cache never disagrees
        with what resolve_config would compute from the same row.
        """
        config = leveling.resolve_config(row, False)
        if config is not None:
            self._configs[guild_id] = config
        else:
            self._configs.pop(guild_id, None)

    async def set_announce_mode(self, guild_id, mode, channel_id=None):
        """Persist announce_mode (+ optional fixed-mode channel), refresh cache.

        Mirrors set_enabled's upsert shape but only ever touches the announce
        columns: the INSERT seeds ``enabled`` from the legacy
        guild_settings.leveling_enabled JSONB flag (the same seed
        LevelRewards.cmd_mode uses), so a guild whose leveling is
        currently ON only through that legacy bool is never masked by a fresh
        row defaulting to FALSE; the UPDATE branch never writes ``enabled`` at
        all, so this can never itself turn leveling on or off.
        """
        row = await self.bot.db_pool.fetchrow(
            f"""
            INSERT INTO level_config (guild_id, enabled, announce_mode, announce_channel_id)
            VALUES (
                $1,
                COALESCE(
                    (SELECT (settings->>'leveling_enabled')::boolean
                     FROM guild_settings WHERE guild_id = $1),
                    FALSE
                ),
                $2,
                $3
            )
            ON CONFLICT (guild_id) DO UPDATE
                SET announce_mode = $2, announce_channel_id = $3
            RETURNING {_CONFIG_COLUMNS};
            """,
            guild_id,
            mode,
            channel_id,
        )
        self._cache_config_row(guild_id, row)

    async def set_announce_template(self, guild_id, template):
        """Persist a custom announce_template (``None`` resets to the default).

        Same upsert shape and ``enabled``-preserving seed as set_announce_mode;
        this is the only other announce column set_announce_mode does not
        touch, kept separate so `/levelconfig announce template` never has to
        also pass a mode.
        """
        row = await self.bot.db_pool.fetchrow(
            f"""
            INSERT INTO level_config (guild_id, enabled, announce_template)
            VALUES (
                $1,
                COALESCE(
                    (SELECT (settings->>'leveling_enabled')::boolean
                     FROM guild_settings WHERE guild_id = $1),
                    FALSE
                ),
                $2
            )
            ON CONFLICT (guild_id) DO UPDATE SET announce_template = $2
            RETURNING {_CONFIG_COLUMNS};
            """,
            guild_id,
            template,
        )
        self._cache_config_row(guild_id, row)

    async def set_voice_xp_enabled(self, guild_id, enabled):
        """Persist the voice-XP on/off flag and refresh the hot-path config cache.

        Same upsert shape and ``enabled``-preserving legacy-JSONB seed as
        set_announce_mode (so toggling voice XP for a guild that turned leveling
        on only through the legacy bool never masks that flag with a fresh
        FALSE row); the UPDATE branch touches ONLY voice_xp_enabled, never the
        leveling ``enabled`` flag. Called by cogs/community/leveling/level_config_ui.py
        through bot.get_cog("Leveling"), the house cross-cog seam, so the
        VoiceXP cog reads the change through this same cached config on its very
        next sweep - no restart.
        """
        row = await self.bot.db_pool.fetchrow(
            f"""
            INSERT INTO level_config (guild_id, enabled, voice_xp_enabled)
            VALUES (
                $1,
                COALESCE(
                    (SELECT (settings->>'leveling_enabled')::boolean
                     FROM guild_settings WHERE guild_id = $1),
                    FALSE
                ),
                $2
            )
            ON CONFLICT (guild_id) DO UPDATE SET voice_xp_enabled = $2
            RETURNING {_CONFIG_COLUMNS};
            """,
            guild_id,
            bool(enabled),
        )
        self._cache_config_row(guild_id, row)

    async def set_voice_xp_rate(self, guild_id, rate):
        """Persist the per-minute voice-XP rate (validated 1..60 by the caller).

        Mirrors set_voice_xp_enabled's upsert; only voice_xp_per_minute is
        written, so it never turns leveling or voice XP on or off by itself.
        """
        row = await self.bot.db_pool.fetchrow(
            f"""
            INSERT INTO level_config (guild_id, enabled, voice_xp_per_minute)
            VALUES (
                $1,
                COALESCE(
                    (SELECT (settings->>'leveling_enabled')::boolean
                     FROM guild_settings WHERE guild_id = $1),
                    FALSE
                ),
                $2
            )
            ON CONFLICT (guild_id) DO UPDATE SET voice_xp_per_minute = $2
            RETURNING {_CONFIG_COLUMNS};
            """,
            guild_id,
            int(rate),
        )
        self._cache_config_row(guild_id, row)

    def get_config(self, guild_id):
        """The cached :class:`~cogs.community.leveling.engine.LevelConfig` for a guild, or None.

        The public O(1) read-through the VoiceXP cog leans on: it hands back the
        SAME frozen config the on_message hot path uses (leveling on/off folded
        into presence, plus the voice_xp knobs), with zero DB and zero awaits, so
        the voice listener's non-matching path stays allocation-free. None means
        leveling is off for the guild (absent from the enabled-config map).
        """
        return self._configs.get(guild_id)

    async def ensure_no_xp_snapshot(self, guild_id):
        """Return a guild's no-xp snapshot, loading it once on a cold miss.

        The cached-or-load accessor the VoiceXP sweep reuses so a voice member in
        a muted channel/category or holding a muted role earns no XP either - the
        SAME L3 snapshot the message path enforces. A hit is a plain BoundedLRU
        read (no DB); only a guild's first use (or one right after a cold
        eviction) pays the single DB read refresh_no_xp_snapshot does.
        """
        snapshot = self._no_xp.get(guild_id)
        if snapshot is None:
            snapshot = await self.refresh_no_xp_snapshot(guild_id)
        return snapshot

    async def refresh_no_xp_snapshot(self, guild_id):
        """Reload a guild's no-xp rows from the DB and refresh the hot-path cache.

        Two callers: cogs/community/leveling/level_config_ui.py invokes this after EVERY
        level_no_xp write (add/remove), so the very next message in that guild
        sees the change immediately - no restart, no reliance on cache
        eviction or a TTL. The on_message hot path below also calls this
        itself, exactly once, on a cold cache miss (a guild's first
        grant-eligible message, or one that follows this guild's snapshot
        being evicted under cache pressure).
        """
        rows = await self.bot.db_pool.fetch(
            "SELECT kind, target_id FROM level_no_xp WHERE guild_id = $1;",
            guild_id,
        )
        snapshot = (
            leveling.NoXpSnapshot.from_rows(rows)
            if rows
            else leveling.EMPTY_NO_XP_SNAPSHOT
        )
        self._no_xp[guild_id] = snapshot
        return snapshot

    async def ensure_multiplier_snapshot(self, guild_id):
        """Return a guild's XP-multiplier snapshot, loading it once on a cold
        miss. The L4 sibling of ensure_no_xp_snapshot: reused by the VoiceXP
        sweep (credit_voice_levelup's caller) so a boosted/reduced voice
        channel or role applies the SAME multiplier a message grant would. A
        hit is a plain BoundedLRU read (no DB); only a guild's first use (or
        one right after a cold eviction) pays the refresh's DB reads.
        """
        snapshot = self._multipliers.get(guild_id)
        if snapshot is None:
            snapshot = await self.refresh_multiplier_snapshot(guild_id)
        return snapshot

    async def refresh_multiplier_snapshot(self, guild_id):
        """Reload a guild's xp_multipliers rows AND its level_config event
        columns from the DB, and refresh the hot-path cache. Two callers:
        cogs/community/leveling/level_config_ui.py invokes this after EVERY
        xp_multipliers write (boost add/remove) and every event write
        (set/off), so the very next message/sweep tick sees the change
        immediately - no restart. The on_message hot path and the VoiceXP
        sweep also call this themselves, exactly once, on a cold cache miss.

        If the stored event has already expired (``event_ends_at`` in the
        past), it is lazily NULLED here (one best-effort UPDATE, never
        blocking or raising into the caller) so a stale expired event does not
        linger forever in level_config without a background timer - see
        schema.sql's ``event_ends_at`` comment. The cached snapshot always
        reflects the ALREADY-expired state (event_factor/event_ends_at both
        None), matching what compute_multiplier's own ``now`` check would
        have decided anyway.
        """
        rows = await self.bot.db_pool.fetch(
            "SELECT kind, target_id, factor FROM xp_multipliers "
            "WHERE guild_id = $1;",
            guild_id,
        )
        event_row = await self.bot.db_pool.fetchrow(
            "SELECT event_factor, event_ends_at FROM level_config "
            "WHERE guild_id = $1;",
            guild_id,
        )
        event_factor = event_row["event_factor"] if event_row else None
        event_ends_at = event_row["event_ends_at"] if event_row else None
        if event_ends_at is not None and event_ends_at <= discord.utils.utcnow():
            await self._clear_expired_event(guild_id)
            event_factor, event_ends_at = None, None

        snapshot = (
            leveling.MultiplierSnapshot.from_rows(rows, event_factor, event_ends_at)
            if (rows or event_factor is not None)
            else leveling.EMPTY_MULTIPLIER_SNAPSHOT
        )
        self._multipliers[guild_id] = snapshot
        return snapshot

    async def _clear_expired_event(self, guild_id):
        """Best-effort lazy null of an expired timed event (see
        refresh_multiplier_snapshot). Never raises into the caller - a failure
        here only means the stale row is retried on the next refresh; the
        cached snapshot is corrected regardless, so no message ever earns the
        expired event's factor even if this write itself fails.
        """
        try:
            await self.bot.db_pool.execute(
                "UPDATE level_config SET event_factor = NULL, "
                "event_ends_at = NULL WHERE guild_id = $1;",
                guild_id,
            )
        except Exception:
            log.exception(
                "Failed to lazily clear expired XP event for guild %s", guild_id
            )

    async def maybe_prune_expired_periods(self, guild_id, now=None):
        """Lazily drop a guild's stale xp_period rows (L6 retention) AND close
        its leveling season when a month rolled over (S1).

        Fires ONLY on the first grant/credit of a NEW period for this guild
        (week or month rolled over since the marker was last set) - never a
        background timer, never on every grant. The common case (nothing
        rolled over since the last check) is a single BoundedLRU read plus a
        tuple compare via cogs.community.leveling.engine.period_marker_changed: zero DB and
        zero awaits, so this is safe to await from both hot paths (on_message
        and the voice sweep, once per credited guild - see their call sites).

        Two effects ride that one marker test, in this order:

        1. the xp_period retention DELETE (cheap, deterministic, always first
           so a slow season rollover can never delay it) - with its monthly
           cutoff CLAMPED to the month awaiting a season snapshot, so it can
           never delete the very rows that snapshot is about to read;
        2. when the MONTH component changed (cogs.community.leveling.engine.month_rolled_over -
           a cold marker counts as changed), that same month is handed to the
           Seasons cog for its exactly-once podium snapshot.

        Which month that is comes from one of two places, and NEVER from the
        wall clock ("the month before now" is empty for a guild silent through
        a whole month, whose real podium sits in an OLDER one):

        * marker WARM - the month it names
          (cogs.community.leveling.engine.season_rollover_period_key), free, no DB;
        * marker COLD (``None``: every restart, since the BoundedLRU starts
          empty and the deploy is continuous, plus the rare eviction) - ONE
          lookup in the data, :meth:`_resolve_cold_closed_month`, run BEFORE
          the DELETE. It has to be before: the cold branch is exactly where the
          clamp would otherwise be ``None``, and an unclamped cutoff can delete
          the last active month of a guild dormant for PRUNE_PERIODS_BACK
          months or more - the rows the snapshot needs - a beat before the
          background task gets to read them. Once per guild per PROCESS (the
          marker is warm from here on), and only on a month rollover.

        Never raises AND never awaits the season work: the snapshot runs as a
        tracked background task (see :meth:`_dispatch_season_rollover`), so
        neither the level-up announce right after a grant nor the voice sweep's
        per-guild loop ever waits on its role/HTTP calls. A failed prune only
        leaves a few extra periods' worth of rows until the NEXT rollover
        retries it, and the marker is updated regardless so a persistently
        failing guild does not retry either on every single message.
        """
        now = now or discord.utils.utcnow()
        current = leveling.current_period_keys(now)
        previous = self._period_markers.get(guild_id)
        if not leveling.period_marker_changed(previous, current):
            return
        self._period_markers[guild_id] = current
        month_rolled = leveling.month_rolled_over(previous, current)
        # The month to protect from the prune below AND to hand to the season
        # snapshot - the same one, which is the whole point of resolving it
        # here rather than letting the DELETE and the snapshot disagree.
        rolled_season = (
            leveling.season_rollover_period_key(previous) if month_rolled else None
        )
        if month_rolled and rolled_season is None:
            rolled_season = await self._resolve_cold_closed_month(guild_id, now)
        try:
            await self.bot.db_pool.execute(
                """
                DELETE FROM xp_period
                WHERE guild_id = $1
                  AND (
                      (period_key LIKE 'W%' AND period_key < $2)
                      OR (period_key LIKE 'M%' AND period_key < $3)
                  );
                """,
                guild_id,
                leveling.weekly_prune_cutoff_key(now),
                leveling.monthly_prune_cutoff_key(now, keep_month=rolled_season),
            )
        except Exception:
            log.exception(
                "Failed to prune expired xp_period rows for guild %s", guild_id
            )
        if month_rolled:
            self._dispatch_season_rollover(guild_id, rolled_season, now)

    async def _resolve_cold_closed_month(self, guild_id, now):
        """The month a COLD-marker rollover closes, read from the data.

        The cold branch of :meth:`maybe_prune_expired_periods`: with no marker
        there is nothing in memory to name the month, so we ask xp_period for
        the guild's latest monthly period strictly before the current one -
        cogs.community.leveling.engine.LATEST_CLOSED_MONTH_SQL, the very query the Seasons cog
        uses for the same question, shared verbatim so the month this clamps
        the prune to and the month the snapshot freezes are always the same one.

        Returns ``None`` when the guild has no closed month at all (a brand new
        guild) or when the lookup fails - both mean "nothing to protect", and
        the failure case is additionally covered by handing that ``None`` to
        the dispatch, which makes the engine resolve it again for itself.

        Scale: ONE extra index lookup per guild per PROCESS, on the first month
        rollover it sees after a restart - not per rollover, not per message.
        At 1000+ guilds that is at most 1000 single-row lookups spread over
        whenever each guild first speaks in a new month.
        """
        try:
            return await self.bot.db_pool.fetchval(
                leveling.LATEST_CLOSED_MONTH_SQL,
                guild_id,
                leveling.month_period_key(now),
            )
        except Exception:
            log.exception(
                "Failed to resolve the closed season month for guild %s", guild_id
            )
            return None

    def _dispatch_season_rollover(self, guild_id, period_key, now):
        """Hand a just-closed month to the Seasons cog (S1), best effort.

        Cross-cog seam in the house shape (mirrors :meth:`_apply_level_rewards`'s
        ``bot.get_cog("LevelRewards")``): the Seasons cog owns the podium
        snapshot, the champion role and the announce; the leveling cog only
        knows WHICH month rolled over for a guild, from its period marker or
        (cold) from the same lookup the engine itself would run. The cog lookup
        comes FIRST so a bot without the Seasons extension loaded pays nothing
        beyond one dict read, and a guild that is no longer cached (left,
        outage) is simply skipped - the snapshot is idempotent and the next
        rollover, or an on-demand ``ensure_season_snapshot`` from a read
        surface, picks it up.

        ``period_key`` may still be ``None`` - a guild with no closed month at
        all, or a resolution that failed - and that is deliberate: the engine
        then resolves it itself (and short-circuits on a guild that has none),
        so a transient DB error on the cold path still closes the season.

        SYNCHRONOUS on purpose: it only SCHEDULES the snapshot as a tracked
        task (the house strong-ref pattern, core.py's _schedule_startup_backup)
        instead of awaiting it. The snapshot can fire several rate-limited role
        moves and a channel.send, and both callers are latency-sensitive in a
        way that would be visible at scale: the message path would delay the
        level-up announce of the first message of the month, and the voice
        sweep - which loops over EVERY credited guild in one tick - would
        serialize every guild's rollover into the same tick right after
        midnight UTC on the 1st. One task per guild per month, so the strong-ref
        set holds at most as many entries as guilds rolling over at once.
        """
        seasons_cog = self.bot.get_cog("Seasons")
        if seasons_cog is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        async def _close():
            try:
                await seasons_cog.ensure_season_snapshot(
                    guild, period_key, now=now
                )
            except Exception:
                log.exception(
                    "Failed to close the leveling season for guild %s", guild_id
                )

        task = asyncio.ensure_future(_close())
        self._season_tasks.add(task)
        task.add_done_callback(self._season_tasks.discard)

    def is_enabled(self, guild_id):
        """Whether leveling is currently ON for a guild (in-memory, no DB).

        The authoritative read-through answer: the map already reflects level_config
        with the JSONB fallback resolved at load, so config panels and help can show
        the true state without a query. A guild is enabled iff it is in the map.
        """
        return guild_id in self._configs

    def _command_prefixes(self, guild_id):
        """Prefixes that mark a message as a command in this guild.

        Mirrors core.get_prefix (when_mentioned_or): the guild's text prefix (or
        the bot default) plus the two bot-mention forms. Only built for messages
        in leveling-enabled guilds (a minority), so the small tuple allocation
        stays off the bulk of the hot path.
        """
        if self._mention_prefixes is None and self.bot.user is not None:
            uid = self.bot.user.id
            self._mention_prefixes = (f"<@{uid}>", f"<@!{uid}>")
        text_prefix = self.bot.prefixes.get(guild_id) or self.bot.default_prefix
        return (text_prefix, *(self._mention_prefixes or ()))

    @staticmethod
    def level_for_xp(xp):
        # Thin delegate to the pure service so the XP curve lives in exactly one
        # place (cogs/community/leveling/engine.py); rank / leaderboard and the tests call this off the
        # class, so the staticmethod contract is kept.
        return leveling.level_for_xp(xp)

    @commands.Cog.listener()
    async def on_message(self, message):
        # Cheap, synchronous gate first: on_message runs for every message on
        # every guild, and the vast majority are in guilds with leveling OFF, so
        # they must cost ZERO awaits and ZERO allocations here.
        if message.author.bot or message.guild is None:
            return
        # One dict.get gates the message AND hands back the per-guild config
        # (cooldown, xp band) in the same lookup: None means leveling is off here.
        config = self._configs.get(message.guild.id)
        if config is None:
            return

        # A message that invokes (or merely looks like) a prefix command earns no
        # XP. Slash commands are interactions and never reach on_message, so only
        # the text-prefix / mention forms are checked here.
        if leveling_gate.is_command_invocation(
            message.content, self._command_prefixes(message.guild.id)
        ):
            return

        # No-xp zones (L3): a guild's snapshot is loaded once (a DB read) and
        # then lives in self._no_xp for every later message, so this is a plain
        # cache read except on a guild's very first grant-eligible message (or
        # right after a cold eviction). The check itself is pure set
        # membership (cogs.community.leveling.engine.is_no_xp_message) - zero DB, zero
        # allocation beyond the tiny role-id generator below.
        no_xp = self._no_xp.get(message.guild.id)
        if no_xp is None:
            no_xp = await self.refresh_no_xp_snapshot(message.guild.id)
        # The common case (a guild that configured NO zones) is a single
        # truthiness check on two empty frozensets: `and` short-circuits before
        # the role-id generator is built and before is_no_xp_message is even
        # called, so a no-zone guild pays ZERO allocations here (and never
        # touches the fresh-list-building Member.roles property). Only a guild
        # that actually muted a channel/category/role pays for the membership
        # test - the pure set lookups in cogs.community.leveling.engine.is_no_xp_message.
        if (no_xp.channels or no_xp.roles) and leveling.is_no_xp_message(
            no_xp,
            message.channel.id,
            getattr(message.channel, "category_id", None),
            (role.id for role in getattr(message.author, "roles", ())),
        ):
            return

        key = (message.guild.id, message.author.id)
        if self._cooldowns.is_active(key, seconds=config.cooldown_seconds):
            return

        self._cooldowns.touch(key)
        gain = leveling.grant_amount(config.xp_min, config.xp_max)

        # XP multipliers (L4): a per-guild snapshot lives in self._multipliers,
        # loaded once and refreshed on every admin write - the SAME cached-or-
        # load contract as the no-xp snapshot just above. The common case (no
        # boosts and no event configured anywhere in this guild) is a single
        # ``is_trivial`` attribute check: the role-id generator is never built
        # and compute_multiplier is never even called, so a guild with no
        # multiplier configuration pays ZERO extra allocation here.
        # Wall-clock "now", shared by the multiplier event check AND the L6
        # period-key maths below - one clock read per message, not two.
        now = discord.utils.utcnow()

        multiplier_snapshot = self._multipliers.get(message.guild.id)
        if multiplier_snapshot is None:
            multiplier_snapshot = await self.refresh_multiplier_snapshot(
                message.guild.id
            )
        if not multiplier_snapshot.is_trivial:
            role_ids = (
                (role.id for role in getattr(message.author, "roles", ()))
                if multiplier_snapshot.roles
                else ()
            )
            multiplier = leveling.compute_multiplier(
                multiplier_snapshot,
                message.channel.id,
                getattr(message.channel, "category_id", None),
                role_ids,
                now,
            )
            gain = leveling.apply_multiplier(gain, multiplier)
            if gain <= 0:
                # A multiplier that rounds the grant down to zero (e.g. a 0.0
                # boost) earns literally nothing THIS message - skip the write
                # entirely (it would be a no-op INSERT anyway: xp = xp + 0
                # never crosses a level threshold). The cooldown was already
                # touched above, so this message still counts against it.
                return

        # Top.gg vote boost (V1): the ONE user-scoped factor in a system that is
        # otherwise entirely guild-scoped. Deliberately applied HERE, after the
        # guild maths and after the zero-gain early return above, which fixes
        # the stacking rule in code: the boost multiplies WHATEVER THE GUILD
        # PRODUCED, so a guild that muted this channel/role with a 0.0 factor
        # stays muted for voters too (0 x 1.5 is still 0, and we never even get
        # here). It can only ever raise a positive grant, never zero one.
        # Cost on the common path: one dict.get on a map that is empty on a bot
        # with no live voters. Zero awaits, zero DB - see apply_vote_boost.
        gain = self.apply_vote_boost(message.author.id, gain, now)

        try:
            # L6: a grant credits the lifetime `levels` total AND both period
            # rollups (xp_period, weekly + monthly) in ONE round trip. This is
            # a SINGLE parameterized SQL command (a WITH query whose CTEs are
            # themselves the three upserts) rather than three separate
            # statements joined by ';': asyncpg's extended query protocol
            # (used whenever arguments are passed) prepares exactly ONE
            # command, so a multi-statement string WOULD raise
            # "cannot insert multiple commands into a prepared statement".
            # PostgreSQL guarantees every data-modifying CTE in a WITH clause
            # executes exactly once, in full, even when the primary SELECT
            # never reads its output (see "Data-Modifying Statements in
            # WITH" in the Postgres docs) - so `week`/`month` below run
            # unconditionally even though only `xp_grant` is selected from.
            # NOTE: the CTE is named `xp_grant`, not `grant` - GRANT is a
            # reserved SQL keyword and Postgres rejects it unquoted as a CTE
            # name ("syntax error at or near 'grant'"), confirmed live.
            week_key, month_key = leveling.current_period_keys(now)
            query = """
                WITH xp_grant AS (
                    INSERT INTO levels (guild_id, user_id, xp)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET xp = levels.xp + $3
                    RETURNING xp
                ), week AS (
                    INSERT INTO xp_period (guild_id, user_id, period_key, xp)
                    VALUES ($1, $2, $4, $3)
                    ON CONFLICT (guild_id, user_id, period_key)
                    DO UPDATE SET xp = xp_period.xp + $3
                ), month AS (
                    INSERT INTO xp_period (guild_id, user_id, period_key, xp)
                    VALUES ($1, $2, $5, $3)
                    ON CONFLICT (guild_id, user_id, period_key)
                    DO UPDATE SET xp = xp_period.xp + $3
                )
                SELECT xp FROM xp_grant;
                """

            new_xp = await self.bot.db_pool.fetchval(
                query,
                message.guild.id,
                message.author.id,
                gain,
                week_key,
                month_key,
            )
            await self.maybe_prune_expired_periods(message.guild.id, now)
            new_level = leveling.level_up_between(new_xp - gain, new_xp)

            if new_level is not None:
                # Reward roles are granted regardless of the announce opt-out
                # below - that setting controls only the announce MESSAGE, never
                # whether earned roles are handed out.
                old_level = leveling.level_for_xp(new_xp - gain)
                granted = await self._apply_level_rewards(
                    message.guild, message.author, old_level, new_level
                )
                await self._announce_levelup(
                    member=message.author,
                    channel=message.channel,
                    guild=message.guild,
                    config=config,
                    new_level=new_level,
                    granted=granted,
                )

        except Exception:
            log.exception("Failed to update XP")

    async def _apply_level_rewards(self, guild, member, old_level, new_level):
        """Grant (and in replace mode remove) reward roles for a level-up.

        Returns the roles actually ADDED (``list``), for the announce suffix.
        Cross-cog seam (mirrors rolemenus.py's get_cog("Reminder")) shared by the
        message path (on_message) and the voice path (credit_voice_levelup): a
        missing or failing LevelRewards cog must never break the level-up itself,
        so this always returns a list and swallows errors (the reward cog also
        guards internally).
        """
        rewards_cog = self.bot.get_cog("LevelRewards")
        if rewards_cog is None:
            return []
        try:
            return await rewards_cog.grant_for_levelup(
                guild, member, old_level, new_level
            )
        except Exception:
            log.exception("Failed to grant level rewards for %s", member.id)
            return []

    async def credit_voice_levelup(
        self, *, guild, member, channel, config, old_xp, new_xp
    ):
        """Route a voice-earned level-up through the SAME reward + announce seams.

        Called by cogs/community/leveling/voice_xp.py once per credited member who crossed
        a level in a sweep, so a voice level-up behaves exactly like a message
        one: reward roles are granted regardless of the announce opt-out, and the
        announce follows the guild's announce_mode - with "channel" mode targeting
        the VOICE channel's own text chat (the ``channel`` passed here). Never
        raises (reused inside the cog's already-guarded sweep, and every awaited
        step has its own narrower handling).
        """
        new_level = leveling.level_up_between(old_xp, new_xp)
        if new_level is None:
            return
        old_level = leveling.level_for_xp(old_xp)
        granted = await self._apply_level_rewards(guild, member, old_level, new_level)
        await self._announce_levelup(
            member=member,
            channel=channel,
            guild=guild,
            config=config,
            new_level=new_level,
            granted=granted,
        )

    async def apply_admin_xp_change(self, *, guild, member, channel, old_xp, new_xp):
        """Route an admin XP edit (/levelconfig xp give|take|set|reset) through
        the reward + announce seams, the L5 sibling of
        :meth:`credit_voice_levelup`.

        The admin's action is message-independent, so ``channel`` is where a
        "channel"-mode announce lands (the command's own channel). Behaviour by
        direction:

        * level UP: behaves exactly like a message/voice level-up - reward roles
          are granted (:meth:`_apply_level_rewards`) and, when leveling is
          enabled for the guild, the level-up is announced per its announce_mode
          and the member's own opt-out. Rewards are granted even if leveling is
          currently OFF (rewards are a separate opt-in); only the announce is
          skipped in that case (no cached config to route it).
        * level DOWN: roles are RECONCILED instead (:meth:`_reconcile_level_down`)
          - in replace mode the tier is recomputed (roles above the new level are
          removed), while in stack mode nothing is removed (earned roles are kept
          on XP loss, the documented convention). A downward move is never
          announced.
        * no threshold crossed: nothing to do.

        Admin edits deliberately do NOT touch xp_period (periods track organic
        activity only - see schema.sql's xp_period), so this seam concerns only
        the lifetime level. Never raises into the caller: each awaited step has
        its own guard (grant/announce are already swallowing seams, and the
        reconcile below is wrapped), so a reward/announce hiccup never undoes the
        XP write the admin command already committed.
        """
        up_level = leveling.level_up_between(old_xp, new_xp)
        if up_level is not None:
            old_level = leveling.level_for_xp(old_xp)
            granted = await self._apply_level_rewards(
                guild, member, old_level, up_level
            )
            config = self.get_config(guild.id)
            if config is not None:
                await self._announce_levelup(
                    member=member,
                    channel=channel,
                    guild=guild,
                    config=config,
                    new_level=up_level,
                    granted=granted,
                )
            return

        down_level = leveling.level_down_between(old_xp, new_xp)
        if down_level is not None:
            await self._reconcile_level_down(guild, member, down_level)

    async def _reconcile_level_down(self, guild, member, new_level):
        """Reconcile a member's reward roles after an admin XP edit dropped them
        below a tier (see :meth:`apply_admin_xp_change`). Cross-cog seam mirroring
        :meth:`_apply_level_rewards`: a missing or failing LevelRewards cog must
        never break the admin command, so this always returns quietly and
        swallows errors (the reward cog also guards internally, and stack mode is
        a no-op there anyway).
        """
        rewards_cog = self.bot.get_cog("LevelRewards")
        if rewards_cog is None:
            return
        try:
            await rewards_cog.reconcile_for_level(guild, member, new_level)
        except Exception:
            log.exception(
                "Failed to reconcile level-down rewards for %s", member.id
            )

    async def _announce_levelup(
        self, *, member, channel, guild, config, new_level, granted
    ):
        """Tell the member (or not) about a level-up, per the guild's and the
        member's own settings. Never raises - called from on_message's already
        try/except-wrapped block (and the voice sweep's), but every awaited step
        here has its own narrower handling so one bad destination (a closed DM, a
        deleted fixed channel) never masks another.

        ``member`` is the leveler, ``channel`` the origin channel a "channel"-mode
        announce lands in (a text channel for a message level-up, the voice
        channel's own text chat for a voice one), and ``guild`` their guild.

        Gate order: the per-user ``levelup_announce`` opt-out is checked FIRST
        and applies in EVERY mode (an opted-out member gets no message
        anywhere - reward roles were already granted by the caller, regardless).
        Only then does the guild's announce_mode decide WHERE, and the
        per-user ``levelup_ping`` preference decides whether the member is
        pinged or just named in the text.
        """
        if not await settings.get_user(
            self.bot.db_pool, member.id, "levelup_announce", True
        ):
            return

        route, target_channel_id = leveling.resolve_announce_target(
            config.announce_mode, channel.id, config.announce_channel_id
        )
        if route == "off":
            return

        ping = await settings.get_user(
            self.bot.db_pool, member.id, "levelup_ping", True
        )
        user_text = member.mention if ping else member.display_name

        if config.announce_template:
            # A custom template replaces the whole sentence, so the granted-
            # roles suffix (translatable on its own) is appended afterwards
            # rather than folded into one combined msgid - the default,
            # no-custom-template branch below keeps the original single
            # sentences verbatim for translators.
            base_text = leveling.render_announce_template(
                config.announce_template,
                user_text=user_text,
                level=new_level,
                guild_name=guild.name,
            )
            if granted:
                text = _("{base} ... and earned {roles}").format(
                    base=base_text,
                    roles=", ".join(r.mention for r in granted),
                )
            else:
                text = base_text
        elif granted:
            text = _(
                "{user} reached level **{level}**! ... and earned {roles}"
            ).format(
                user=user_text,
                level=new_level,
                roles=", ".join(r.mention for r in granted),
            )
        else:
            text = _("{user} reached level **{level}**!").format(
                user=user_text, level=new_level
            )

        # Ping only the member who leveled up (or no one, per levelup_ping).
        # The granted-roles suffix embeds role mentions (<@&id>); with the
        # bot's mention permissions those would notify EVERY holder of a
        # reward role (a mass ping) on each level-up, so roles/@everyone stay
        # suppressed regardless of destination.
        allowed_mentions = discord.AllowedMentions(
            everyone=False, roles=False, users=True
        )

        try:
            if route == "channel":
                await channel.send(text, allowed_mentions=allowed_mentions)
            elif route == "fixed":
                target = guild.get_channel(target_channel_id)
                if target is not None:
                    await target.send(text, allowed_mentions=allowed_mentions)
                else:
                    # The configured fixed channel was deleted (or the bot lost
                    # sight of it). DECIDED behaviour: drop the announce quietly
                    # rather than fall back to the origin channel - "fixed" exists
                    # precisely to keep level-ups OUT of arbitrary channels, so
                    # spraying them into the origin channel on a deletion would be
                    # the more surprising outcome. Roles were already granted; an
                    # admin re-points the channel to resume announces. Logged for
                    # observability.
                    log.debug(
                        "Level-up fixed announce channel %s missing in guild %s",
                        target_channel_id,
                        guild.id,
                    )
            elif route == "dm":
                await member.send(text, allowed_mentions=allowed_mentions)
        except discord.Forbidden:
            # Closed DMs, or the bot lost access to the fixed channel: quiet -
            # roles were already granted regardless, and this is routine
            # enough (any member can close their DMs) to not warrant a log.
            pass
        except discord.HTTPException:
            log.debug("Failed to send level-up announce (route=%s)", route)

    @staticmethod
    def _load_font(size):
        """Load the bundled TTF at a size, falling back to Pillow's default."""
        try:
            return ImageFont.truetype(_FONT_PATH, size=size)
        except Exception:
            return ImageFont.load_default()

    # -- rank-card customisation (RC1) ----------------------------------
    async def ensure_rank_card_style(self, guild_id):
        """Return ``(accent_rgb | None, has_background)`` for a guild's card.

        The cached-or-load accessor the /rank path uses, mirroring
        ensure_no_xp_snapshot's contract: a hit is a plain BoundedLRU read (no
        DB), a miss pays ONE primary-key lookup on rank_cards that deliberately
        does not select the image blob (cogs/community/leveling/rank_card.CONFIG_QUERY).

        Never raises and never degrades /rank: a DB failure here logs and
        returns the stock style WITHOUT caching it, so the card still renders
        (in its default look) and the next call retries. That is why this is not
        simply inlined in the command's try block - a hiccup reading an optional
        cosmetic must not push the whole command onto its plain-embed fallback.
        """
        cached = self._rank_cards.get(guild_id)
        if cached is not None:
            return cached
        try:
            row = await rank_card.fetch_config(self.bot.db_pool, guild_id)
        except Exception:
            log.exception("Failed to read rank card config for guild %s", guild_id)
            return _STOCK_RANK_CARD
        style = (
            _STOCK_RANK_CARD
            if row is None
            else (
                rank_card.accent_to_rgb(row["accent"]),
                bool(row["has_background"]),
            )
        )
        self._rank_cards[guild_id] = style
        return style

    def invalidate_rank_card(self, guild_id):
        """Drop a guild's cached card style so the next /rank re-reads it.

        A plain eviction rather than the eager re-read the other dashboard
        invalidators do: /rank is rare and human-paced, so paying the lookup on
        the next actual render is strictly cheaper than paying it on every
        notification - and it also covers a background-bytes-only change, which
        no cached metadata would reflect anyway. Called by RC2's panel after a
        bot-side write and by cogs/system/dashboard_sync.py (kind 'rank_card')
        after a dashboard one.
        """
        self._rank_cards.discard(guild_id)

    # -- rank-card customisation writes (RC2 contract) -------------------
    # TODO-CONTRACT fulfilled: every bot-side write below validates, persists
    # THROUGH cogs.community.leveling.rank_card, then invalidates this cog's own cache in the
    # SAME call - so from the caller's point of view (RC2's panel and its
    # /levelconfig card background command) a write is atomic: there is no
    # window where the DB has the new value but the next /rank still renders
    # the stale cached style. The dashboard path is exempt from this seam by
    # construction (it writes the row directly, over a different process) and
    # is instead covered by cogs/system/dashboard_sync.py's own invalidation
    # (kind 'rank_card' -> invalidate_rank_card), see that module's docstring.
    async def set_rank_background(self, guild_id, data, content_type=None):
        """Validate, store and invalidate one guild's rank-card background.

        ``data`` is the raw uploaded bytes; ``content_type`` is the OPTIONAL
        client-declared type (an Attachment's own, when the caller has one).
        Raises whichever :class:`cogs.community.leveling.rank_card.RankCardError` subclass the
        upload failed on (SourceTooLarge, ImageTooLarge, UnsupportedFormat,
        DecodeFailed, EncodedTooLarge) - the caller maps each to its own short
        user-facing message; nothing is written and the cache is left untouched
        on a rejection. Validation is Pillow work (decode, cover-crop, WebP
        encode), so it runs through tools.rendering.run_image_job like every
        other image job - never blocking the event loop directly.
        """
        encoded, background_format = await rendering.run_image_job(
            self.bot, rank_card.validate_and_downscale, data, content_type
        )
        await rank_card.set_background(
            self.bot.db_pool, guild_id, encoded, background_format
        )
        self.invalidate_rank_card(guild_id)
        return background_format

    async def set_rank_accent(self, guild_id, value):
        """Validate, store and invalidate one guild's rank-card accent colour.

        ``value`` is whatever the caller collected (an int or a hex string in
        any shape :func:`cogs.community.leveling.rank_card.validate_accent` accepts). Raises
        :class:`cogs.community.leveling.rank_card.InvalidAccent` on bad input; nothing is written
        and the cache is left untouched on a rejection. Returns the packed
        0xRRGGBB int that was stored, for the caller's confirmation message.
        """
        accent = rank_card.validate_accent(value)
        await rank_card.set_accent(self.bot.db_pool, guild_id, accent)
        self.invalidate_rank_card(guild_id)
        return accent

    async def clear_rank_card(self, guild_id, *, target=None):
        """Reset one guild's rank-card customisation and invalidate the cache.

        ``target`` picks what to drop: ``'background'`` clears only the
        background (keeping any accent), ``'accent'`` clears only the accent
        (keeping any background), and ``None`` (the default) resets the whole
        row - a guild back to the stock card. Always invalidates, even when
        there was nothing to clear (idempotent, matching the storage layer's
        own no-op-without-a-row contract).
        """
        pool = self.bot.db_pool
        if target == "background":
            await rank_card.clear_background(pool, guild_id)
        elif target == "accent":
            await rank_card.clear_accent(pool, guild_id)
        else:
            await rank_card.clear(pool, guild_id)
        self.invalidate_rank_card(guild_id)

    @staticmethod
    def _paint_background(card, data):
        """Paint a stored background under the card, returning success.

        The blob is written by cogs/community/leveling/rank_card.validate_and_downscale, so it is
        already a card-sized WebP; the defensive resize only covers a blob
        stored before a future card resize. A corrupt or undecodable row is
        logged and reported as a failure so the caller falls back to the stock
        panel - a broken background must never cost a member their /rank.
        """
        width, height = rank_card.CARD_SIZE
        try:
            with Image.open(io.BytesIO(data)) as source:
                source.load()
                image = source.convert("RGB")
            if image.size != rank_card.CARD_SIZE:
                image = image.resize(rank_card.CARD_SIZE, Image.Resampling.LANCZOS)
        except Exception:
            log.warning("Unusable stored rank card background", exc_info=True)
            return False
        # Rounded mask so the background respects the card's corners exactly as
        # the stock panel does.
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, width - 1, height - 1), radius=rank_card.CARD_RADIUS, fill=255
        )
        card.paste(image, (0, 0), mask)
        card.paste(
            Image.new("RGB", (width, height), _BACKGROUND_SCRIM),
            (0, 0),
            mask.point(lambda value: value * _BACKGROUND_SCRIM_ALPHA // 255),
        )
        return True

    @classmethod
    def _render_rank_card(
        cls,
        avatar_bytes,
        name,
        level,
        rank_pos,
        xp,
        cur_threshold,
        next_threshold,
        accent,
        background=None,
    ):
        """Blocking Pillow render of a member's rank card. Returns a BytesIO PNG.

        ``accent`` is the (r, g, b) the ring, the LEVEL label and the progress
        bar are drawn in - the member's colour by default, the guild's
        configured accent when it set one (RC1). ``background`` is the guild's
        stored background WebP, or None for the stock card. When it is None NOT
        A SINGLE drawing call below changes, so the default card stays
        byte-for-byte what it was before RC1 (guarded by a hash test).

        DECIDED: the accent does NOT recolour the display name. It is drawn on
        the dark panel at a fixed high-contrast light tone, and a guild is free
        to pick a near-black accent; tying the name to it would let one setting
        make the card's most important text unreadable. Accent-coloured surfaces
        are the ones that stay legible at any hue: the avatar ring, the LEVEL
        label and the bar fill.
        """
        width, height = rank_card.CARD_SIZE
        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)

        # Rounded dark panel (or the guild's background under a contrast scrim,
        # falling back to the panel if the stored blob cannot be decoded).
        if background is None or not cls._paint_background(card, background):
            draw.rounded_rectangle(
                (0, 0, width - 1, height - 1),
                radius=rank_card.CARD_RADIUS,
                fill=(28, 30, 38, 255),
            )

        # Circular avatar with an accent ring on the left.
        av_size = 150
        av_x, av_y = 45, 45
        avatar = (
            Image.open(io.BytesIO(avatar_bytes))
            .convert("RGBA")
            .resize((av_size, av_size))
        )
        mask = Image.new("L", (av_size, av_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
        card.paste(avatar, (av_x, av_y), mask)
        draw.ellipse(
            (av_x - 4, av_y - 4, av_x + av_size + 4, av_y + av_size + 4),
            outline=accent,
            width=6,
        )

        text_x = av_x + av_size + 40

        # Member name, truncated to fit the available width.
        name_font = cls._load_font(40)
        name_max = width - text_x - 45
        display = name
        if draw.textlength(display, font=name_font) > name_max:
            while display and draw.textlength(
                display + "...", font=name_font
            ) > name_max:
                display = display[:-1]
            display = display + "..."
        draw.text((text_x, 48), display, font=name_font, fill=(240, 242, 248))

        # Rank + level, right-aligned on their own row.
        stat_font = cls._load_font(30)
        level_text = f"LEVEL {level}"
        rank_text = f"RANK #{rank_pos}"
        level_w = draw.textlength(level_text, font=stat_font)
        draw.text(
            (width - 45 - level_w, 108), level_text, font=stat_font, fill=accent
        )
        rank_w = draw.textlength(rank_text, font=stat_font)
        draw.text(
            (width - 45 - level_w - 28 - rank_w, 108),
            rank_text,
            font=stat_font,
            fill=(176, 182, 200),
        )

        # XP progress toward the next level.
        span = max(next_threshold - cur_threshold, 1)
        into_level = max(min(xp - cur_threshold, span), 0)
        pct = into_level / span

        bar_x, bar_y = text_x, 185
        bar_w, bar_h = width - bar_x - 45, 30
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            radius=bar_h // 2,
            fill=(58, 61, 74, 255),
        )
        fill_w = int(bar_w * pct)
        if fill_w > 0:
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + max(fill_w, bar_h), bar_y + bar_h),
                radius=bar_h // 2,
                fill=accent,
            )

        # XP figures above the bar's right edge.
        xp_font = cls._load_font(22)
        xp_text = f"{into_level} / {span} XP"
        xp_w = draw.textlength(xp_text, font=xp_font)
        draw.text(
            (bar_x + bar_w - xp_w, bar_y - 30),
            xp_text,
            font=xp_font,
            fill=(176, 182, 200),
        )

        buf = io.BytesIO()
        card.save(buf, "PNG")
        buf.seek(0)
        return buf

    @commands.hybrid_command(aliases=["level", "lvl"])
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.guild_only()
    @discord.app_commands.describe(member="Whose rank to show (defaults to you).")
    async def rank(self, ctx, member: discord.Member = None):
        """Show your level and XP rank card, or another member's."""

        member = member or ctx.author

        xp = (
            await self.bot.db_pool.fetchval(
                "SELECT xp FROM levels WHERE guild_id = $1 AND user_id = $2;",
                ctx.guild.id,
                member.id,
            )
            or 0
        )
        level = leveling.level_for_xp(xp)
        cur_threshold = leveling.xp_for_level(level)
        next_threshold = leveling.xp_for_level(level + 1)
        needed = next_threshold - xp

        # Rank position within the guild (uses levels_guild_xp_idx).
        rank_pos = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) + 1 FROM levels WHERE guild_id = $1 AND xp > $2;",
            ctx.guild.id,
            xp,
        )

        async with ctx.typing():
            try:
                avatar_bytes = await member.display_avatar.replace(size=128).read()
                name = member.display_name
                accent = (
                    member.colour.to_rgb()
                    if member.colour.value
                    else (88, 101, 242)
                )
                # Per-guild look (RC1). A configured accent is the guild's card
                # branding and therefore outranks the member's role colour; the
                # background bytes are fetched only when the cached style says
                # there is one, and a row that vanished meanwhile just renders
                # the stock panel.
                guild_accent, has_background = await self.ensure_rank_card_style(
                    ctx.guild.id
                )
                if guild_accent is not None:
                    accent = guild_accent
                background = None
                if has_background:
                    try:
                        background = await rank_card.fetch_background(
                            self.bot.db_pool, ctx.guild.id
                        )
                    except Exception:
                        log.exception(
                            "Failed to read rank card background for guild %s",
                            ctx.guild.id,
                        )

                def _render():
                    return self._render_rank_card(
                        avatar_bytes,
                        name,
                        level,
                        rank_pos,
                        xp,
                        cur_threshold,
                        next_threshold,
                        accent,
                        background,
                    )

                buf = await rendering.run_image_job(self.bot, _render)
                await ctx.send(file=discord.File(buf, filename="rank.png"))
            except Exception:
                log.exception("Failed to render rank card")
                embed = discord.Embed(
                    title=_("Rank | {name}").format(name=member.display_name),
                    colour=random_colour(),
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.add_field(name=_("Rank"), value=f"**#{rank_pos}**")
                embed.add_field(name=_("Level"), value=f"**{level}**")
                embed.add_field(name=_("XP"), value=f"**{xp}**")
                embed.add_field(
                    name=_("XP for next level"),
                    value=f"**{needed}** ({xp}/{next_threshold})",
                    inline=False,
                )
                await ctx.send(embed=embed)

    @commands.hybrid_command(name="leaderboard", aliases=["levels", "top"])
    @commands.guild_only()
    @discord.app_commands.describe(
        period="Leave empty for the all-time leaderboard, or pick weekly/monthly."
    )
    async def leaderboard(
        self, ctx, period: Optional[Literal["weekly", "monthly"]] = None
    ):
        """Show the ranked members of the guild (add weekly/monthly for a
        rolling period leaderboard instead of the all-time one)."""

        if period is None:
            # UNCHANGED byte-for-byte from before the L6 period leaderboards:
            # the bare invocation's query, title and entry shape (level shown)
            # are exactly what they always were.
            query = """
                SELECT user_id, xp FROM levels
                WHERE guild_id = $1
                ORDER BY xp DESC
                LIMIT 50;
                """
            rows = await self.bot.db_pool.fetch(query, ctx.guild.id)
            title = _("Leaderboard | {guild}").format(guild=ctx.guild.name)

            if not rows:
                embed = discord.Embed(
                    title=title,
                    description=_("No one has earned any XP yet!"),
                    colour=random_colour(),
                )
                return await ctx.send(embed=embed)

            entries = []
            # Build EVERY fetched row into an entry (the view pages them 15 at a
            # time); the pre-L5 code sliced to the first page here, which the
            # pager now owns - see LeaderboardView.
            for index, row in enumerate(rows, start=1):
                uid = row["user_id"]
                xp = row["xp"]
                member = ctx.guild.get_member(uid)
                name = (
                    member.display_name if member else _("User {uid}").format(uid=uid)
                )
                avatar_url = (
                    member.display_avatar.url if member else _DEFAULT_AVATAR_URL
                )
                entries.append(
                    {
                        "rank": index,
                        "name": name,
                        "level": self.level_for_xp(xp),
                        "xp": xp,
                        "avatar_url": avatar_url,
                    }
                )
        else:
            # L6 period view: reads xp_period for the CURRENT period key
            # (guild_id, period_key) -> the covering index
            # xp_period_guild_period_xp_idx serves this as a pure range scan,
            # no sort. Levels are lifetime-only, so entries here carry XP but
            # no "level" key - LeaderboardView renders that shape without it.
            now = discord.utils.utcnow()
            week_key, month_key = leveling.current_period_keys(now)
            period_key = (
                week_key if period == leveling.PERIOD_WEEKLY else month_key
            )
            query = """
                SELECT user_id, xp FROM xp_period
                WHERE guild_id = $1 AND period_key = $2
                ORDER BY xp DESC
                LIMIT 50;
                """
            rows = await self.bot.db_pool.fetch(query, ctx.guild.id, period_key)

            if period == leveling.PERIOD_WEEKLY:
                title = _("Weekly leaderboard | {guild}").format(guild=ctx.guild.name)
                empty_text = _("No one has earned any XP this week yet!")
            else:
                title = _("Monthly leaderboard | {guild}").format(
                    guild=ctx.guild.name
                )
                empty_text = _("No one has earned any XP this month yet!")

            if not rows:
                embed = discord.Embed(
                    title=title, description=empty_text, colour=random_colour()
                )
                return await ctx.send(embed=embed)

            entries = []
            # Same as the lifetime branch: build every fetched row, the view pages.
            for index, row in enumerate(rows, start=1):
                uid = row["user_id"]
                xp = row["xp"]
                member = ctx.guild.get_member(uid)
                name = (
                    member.display_name if member else _("User {uid}").format(uid=uid)
                )
                avatar_url = (
                    member.display_avatar.url if member else _DEFAULT_AVATAR_URL
                )
                entries.append(
                    {"rank": index, "name": name, "xp": xp, "avatar_url": avatar_url}
                )

        # A LayoutView carries its own content: send it with no embed/content, and
        # suppress mentions since TextDisplay resolves them (unlike an embed). The
        # pager is author-gated, so it is bound to whoever invoked /leaderboard.
        view = LeaderboardView(ctx.author.id, title, entries)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(Leveling(bot))
