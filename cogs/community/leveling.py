import io
import logging
import os
from typing import Literal, Optional

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from tools import leveling, leveling_gate, settings
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
# tools.leveling.LEADERBOARD_PAGE_SIZE (the pager's home).
_PODIUM_SLOTS = 5

# Medal glyphs for the top three; lower ranks fall back to a plain number.
_MEDALS = {1: "\N{FIRST PLACE MEDAL}", 2: "\N{SECOND PLACE MEDAL}", 3: "\N{THIRD PLACE MEDAL}"}

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
# and maybe_prune_expired_periods. An evicted guild simply re-prunes on its
# next grant (a rare, harmless extra DELETE), never a correctness issue.
_PERIOD_MARKER_CACHE_CAP = 2048

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
    thumbnails. Prev/Next walk pages of :data:`~tools.leveling.LEADERBOARD_PAGE_SIZE`
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
        # Per-guild no-xp-zone snapshot (tools.leveling.NoXpSnapshot: two
        # frozensets of channel/category ids and role ids), loaded from
        # level_no_xp on a guild's first grant-eligible message and kept live by
        # refresh_no_xp_snapshot (called on every level_no_xp write, from
        # cogs/community/level_config_ui.py). Bounded, unlike self._configs:
        # every ENABLED guild eventually gets an entry here (even an empty one,
        # once it earns its first XP), so this is genuinely unbounded by guild
        # count and needs the same size-cap tools.settings uses for user blobs.
        # An evicted guild simply re-reads its (usually tiny) row set on its
        # next grant-eligible message - a rare, harmless extra query, never a
        # per-message cost (SCALE STORY).
        self._no_xp: BoundedLRU = BoundedLRU(_NO_XP_CACHE_CAP)
        # Per-guild XP-multiplier snapshot (tools.leveling.MultiplierSnapshot:
        # global/channel/role factors plus the active timed event, see that
        # class's docstring), the L4 sibling of self._no_xp above - same
        # cached-or-load contract (ensure_multiplier_snapshot), same
        # write-path refresh hook (refresh_multiplier_snapshot, called by
        # cogs/community/level_config_ui.py after every boost/event write),
        # same BoundedLRU sizing rationale.
        self._multipliers: BoundedLRU = BoundedLRU(_MULTIPLIER_CACHE_CAP)
        # Per-guild "last seen period" marker (L6): the (week_key, month_key)
        # pair this guild's most recent grant/credit already observed. Read
        # by maybe_prune_expired_periods to decide, in O(1) with zero DB on
        # the common case, whether a period just rolled over for this guild
        # and its xp_period rows are due for a lazy prune. Bounded like
        # self._no_xp / self._multipliers above (same rationale).
        self._period_markers: BoundedLRU = BoundedLRU(_PERIOD_MARKER_CACHE_CAP)

    async def cog_load(self):
        """Load every enabled guild's leveling config once, at startup.

        Runs during load_extension (setup_hook), before the gateway delivers any
        message, so the hot path sees a populated map from the very first event.
        Two reads, both over small tables: the level_config rows (the new source of
        truth), then the legacy guild_settings.leveling_enabled bool as a
        READ-THROUGH fallback for guilds that turned leveling on before level_config
        existed and have not re-toggled since. A level_config row always wins, so a
        guild that later switched leveling OFF via the new table is never
        resurrected by its stale JSONB value. A failure here only leaves leveling
        dormant until the next toggle - it is logged and never blocks startup.
        """
        try:
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
        except Exception:
            log.exception("Failed to load leveling config")

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

    def _cache_config_row(self, guild_id, row):
        """Resolve a level_config RETURNING row into the hot-path config map.

        Shared by every writer of level_config (set_enabled, set_announce_mode,
        set_announce_template): a row that leaves the guild enabled refreshes
        its cached :class:`~tools.leveling.LevelConfig`, a disabled one (or a
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
        leveling ``enabled`` flag. Called by cogs/community/level_config_ui.py
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
        """The cached :class:`~tools.leveling.LevelConfig` for a guild, or None.

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

        Two callers: cogs/community/level_config_ui.py invokes this after EVERY
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
        cogs/community/level_config_ui.py invokes this after EVERY
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
        """Lazily drop a guild's stale xp_period rows (L6 retention).

        Fires a DELETE ONLY on the first grant/credit of a NEW period for
        this guild (week or month rolled over since the marker was last set)
        - never a background timer, never on every grant. The common case
        (nothing rolled over since the last check) is a single BoundedLRU
        read plus a tuple compare via tools.leveling.period_marker_changed:
        zero DB, so this is safe to await from both hot paths (on_message
        and the voice sweep, once per credited guild - see their call
        sites). Never raises: a failed prune only leaves a few extra
        periods' worth of rows until the NEXT rollover retries it, and the
        marker is updated regardless so a persistently-failing guild does
        not retry the DELETE on every single message.
        """
        now = now or discord.utils.utcnow()
        current = leveling.current_period_keys(now)
        previous = self._period_markers.get(guild_id)
        if not leveling.period_marker_changed(previous, current):
            return
        self._period_markers[guild_id] = current
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
                leveling.monthly_prune_cutoff_key(now),
            )
        except Exception:
            log.exception(
                "Failed to prune expired xp_period rows for guild %s", guild_id
            )

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
        # place (tools/leveling.py); rank / leaderboard and the tests call this off the
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
        # membership (tools.leveling.is_no_xp_message) - zero DB, zero
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
        # test - the pure set lookups in tools.leveling.is_no_xp_message.
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

        Called by cogs/community/voice_xp.py once per credited member who crossed
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
    ):
        """Blocking Pillow render of a member's rank card. Returns a BytesIO PNG."""
        width, height = 880, 240
        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)

        # Rounded dark panel.
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1), radius=30, fill=(28, 30, 38, 255)
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
                    )

                buf = await self.bot.loop.run_in_executor(None, _render)
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
