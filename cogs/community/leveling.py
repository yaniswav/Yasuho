import io
import logging
import os

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from tools import leveling, leveling_gate, settings
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _
from tools.lru_cache import BoundedLRU

log = logging.getLogger(__name__)

# Bundled TTF used for the rank card; falls back to Pillow's default if missing.
_FONT_PATH = os.path.join("ressources", "fonts", "impact.ttf")

# Neutral Discord avatar used when a top-ranked member has left the guild and no
# real avatar is available for the Section thumbnail accessory.
_DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"

# Components V2 budget: how many ranks get their own avatar Section (podium) and
# how many total ranks the single-page layout shows (the rest go in a text list).
_PODIUM_SLOTS = 5
_LEADERBOARD_CAP = 15

# Medal glyphs for the top three; lower ranks fall back to a plain number.
_MEDALS = {1: "\N{FIRST PLACE MEDAL}", 2: "\N{SECOND PLACE MEDAL}", 3: "\N{THIRD PLACE MEDAL}"}

# No-xp snapshot cache ceiling (tools.lru_cache.BoundedLRU): comfortably above
# any plausible number of guilds with leveling enabled AND no-xp zones
# configured, so eviction is a rare, harmless extra DB read rather than a
# steady-state cost - see NoXpSnapshot's cog-level cache, self._no_xp below.
_NO_XP_CACHE_CAP = 2048

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


class LeaderboardView(discord.ui.LayoutView):
    """Single-page Components V2 podium for the guild XP leaderboard.

    The top ranks each become a :class:`discord.ui.Section` with the member's
    avatar as a :class:`discord.ui.Thumbnail` accessory; the remaining ranks are
    collapsed into one :class:`discord.ui.TextDisplay` list to respect the V2
    component budget. It is purely presentational (no interactive components), so
    it carries no author gating.
    """

    def __init__(self, title, entries, *, timeout=180):
        # entries: list of dicts with rank, name, level, xp, avatar_url.
        super().__init__(timeout=timeout)
        self.message = None
        self._build(title, entries)

    def _build(self, title, entries):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay("## {title}".format(title=title)))
        container.add_item(discord.ui.Separator())

        podium = entries[:_PODIUM_SLOTS]
        remainder = entries[_PODIUM_SLOTS:]

        for entry in podium:
            marker = _MEDALS.get(entry["rank"], "**#{rank}**".format(rank=entry["rank"]))
            text = _("{marker} **{name}**\nLevel **{level}** - {xp} XP").format(
                marker=marker,
                name=entry["name"],
                level=entry["level"],
                xp=entry["xp"],
            )
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(text),
                    accessory=discord.ui.Thumbnail(entry["avatar_url"]),
                )
            )

        if remainder:
            container.add_item(discord.ui.Separator())
            lines = [
                _("**#{rank}** {name} - level **{level}** ({xp} XP)").format(
                    rank=entry["rank"],
                    name=entry["name"],
                    level=entry["level"],
                    xp=entry["xp"],
                )
                for entry in remainder
            ]
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        self.add_item(container)


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
        LevelRewards.levelrewards_mode uses), so a guild whose leveling is
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
        # place (tools/leveling.py); rank / levels and the tests call this off the
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

        try:
            query = """
                INSERT INTO levels (guild_id, user_id, xp)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET xp = levels.xp + $3
                RETURNING xp;
                """

            new_xp = await self.bot.db_pool.fetchval(
                query, message.guild.id, message.author.id, gain
            )
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

    @commands.hybrid_command()
    @commands.guild_only()
    async def rank(self, ctx, member: discord.Member = None):
        """Shows your level and XP rank card, or another member's."""

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

    @commands.hybrid_command(aliases=["leaderboard", "top"])
    @commands.guild_only()
    async def levels(self, ctx):
        """Shows the ranked members of the guild."""

        query = """
            SELECT user_id, xp FROM levels
            WHERE guild_id = $1
            ORDER BY xp DESC
            LIMIT 50;
            """

        rows = await self.bot.db_pool.fetch(query, ctx.guild.id)

        if not rows:
            embed = discord.Embed(
                title=_("Leaderboard | {guild}").format(guild=ctx.guild.name),
                description=_("No one has earned any XP yet!"),
                colour=random_colour(),
            )
            return await ctx.send(embed=embed)

        entries = []
        for index, row in enumerate(rows[:_LEADERBOARD_CAP], start=1):
            uid = row["user_id"]
            xp = row["xp"]
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else _("User {uid}").format(uid=uid)
            avatar_url = member.display_avatar.url if member else _DEFAULT_AVATAR_URL
            entries.append(
                {
                    "rank": index,
                    "name": name,
                    "level": self.level_for_xp(xp),
                    "xp": xp,
                    "avatar_url": avatar_url,
                }
            )

        # A LayoutView carries its own content: send it with no embed/content, and
        # suppress mentions since TextDisplay resolves them (unlike an embed).
        view = LeaderboardView(
            _("Leaderboard | {guild}").format(guild=ctx.guild.name), entries
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(Leveling(bot))
