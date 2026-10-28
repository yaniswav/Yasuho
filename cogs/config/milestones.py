"""Member milestones: celebrate the server reaching a member count.

When a join takes the server past a milestone (50, 100, 250 ... or, with
Yasuho+, up to ten custom values), Yasuho posts one message in the configured
channel. ``/milestones`` opens the manager panel.

STORAGE. The config (enabled, channel, Yasuho+ custom list and message) is the
``milestones`` key of the guild_settings JSONB blob, so it rides the cached
tools.settings path the dashboard already syncs. The only extra row is the
per-guild HIGH-WATER mark in ``milestone_state``: announcing is ONE conditional
UPDATE (``last_milestone < $m``), so concurrent joins post once, a member count
that drops and rises again never re-announces, and several milestones crossed
at once post only the highest.

HOT PATH. ``on_member_join`` runs for every join bot-wide. A guild that did not
enable the feature costs one settings-cache read (the guild blob other cogs
already keep warm) and no query. An enabled guild skips the database entirely
until a milestone above ``_floor`` (the highest milestone this process knows is
already announced) is reached. ``_floor`` only ever saves queries, the UPDATE
stays the source of truth.

DOWNGRADE. Nothing is deleted: a lapsed Yasuho+ simply stops honouring the
custom list and message (the fixed list and default message apply again) and
they come back the moment the subscription does.

Typography rule: ASCII '-' and '...' only.
"""

import bisect
import logging
import re
import types

import discord
from discord.ext import commands

from tools import embed_creator, i18n, premium, premium_upsell, settings
from tools.formats import random_colour
from tools.i18n import _
from tools.snowflake import coerce_id
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

SETTINGS_KEY = "milestones"
FREE_MILESTONES = (50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000)
MAX_CUSTOM_MILESTONES = 10
MAX_MILESTONE = 10_000_000
MAX_MESSAGE_LENGTH = 500
PLACEHOLDER_HINT = "{server} {members} {user}"

_ARM_SQL = (
    "INSERT INTO milestone_state (guild_id, last_milestone) VALUES ($1, $2) "
    "ON CONFLICT (guild_id) DO UPDATE SET "
    "last_milestone = GREATEST(milestone_state.last_milestone, EXCLUDED.last_milestone) "
    "RETURNING last_milestone"
)
# Cold start: adopt the stored mark untouched, or create it from a baseline.
# Never advances an existing mark, so a concurrent join's claim is not swallowed.
_LOAD_SQL = (
    "INSERT INTO milestone_state (guild_id, last_milestone) VALUES ($1, $2) "
    "ON CONFLICT (guild_id) DO UPDATE SET last_milestone = milestone_state.last_milestone "
    "RETURNING last_milestone"
)
_CLAIM_SQL = (
    "UPDATE milestone_state SET last_milestone = $2 "
    "WHERE guild_id = $1 AND last_milestone < $2 RETURNING last_milestone"
)


def normalise(blob):
    """A fresh, fully-typed config from the stored (or dashboard-written) blob."""
    blob = blob if isinstance(blob, dict) else {}
    values = blob.get("milestones")
    message = blob.get("message")
    return {
        "enabled": blob.get("enabled") is True,
        "channel_id": coerce_id(blob.get("channel_id")),
        "milestones": sorted(
            {
                v
                for v in (values if isinstance(values, list) else [])
                if type(v) is int and 0 < v <= MAX_MILESTONE
            }
        )[:MAX_CUSTOM_MILESTONES],
        "message": message[:MAX_MESSAGE_LENGTH] if isinstance(message, str) and message.strip() else None,
    }


def parse_milestones(text):
    """Parse ``"75, 150 300"`` into a sorted, unique list, or raise ValueError."""
    values = set()
    for token in re.split(r"[,\s]+", text or ""):
        if not token:
            continue
        if re.fullmatch(r"[0-9]{1,8}", token) is None or not 0 < int(token) <= MAX_MILESTONE:
            raise ValueError("milestone out of range")
        values.add(int(token))
    if len(values) > MAX_CUSTOM_MILESTONES:
        raise ValueError("too many milestones")
    return sorted(values)


def effective_milestones(config, custom_allowed):
    """The list in force: the custom one only while the guild has Yasuho+."""
    if custom_allowed and config["milestones"]:
        return tuple(config["milestones"])
    return FREE_MILESTONES


def highest_passed(milestones, count):
    """The highest milestone ``<= count``, or 0."""
    index = bisect.bisect_right(milestones, count)
    return milestones[index - 1] if index else 0


def render_message(guild, member, milestone, template=None):
    """Fill a message with the shared {server}/{members} substitution.

    ``{members}`` is the milestone itself (a join burst can move the live
    count past it); ``{user}`` is the member whose join crossed it.
    """
    text = template or _(
        "{server} just reached {members} members! Thank you all, and "
        "welcome {user}!"
    )
    shown = types.SimpleNamespace(name=guild.name, member_count=milestone)
    text = embed_creator.server_substitution(shown, None)(text)
    text = text.replace("{user}", discord.utils.escape_markdown(member.display_name))
    return text[:2000]  # a 500-char template can repeat {server}


class MilestoneModal(LocaleModal):
    """Yasuho+: the custom milestone list and message template."""

    def __init__(self, panel):
        super().__init__(title=_("Custom milestones"))
        self.panel = panel
        config = panel.config
        self.values_field = discord.ui.TextInput(
            label=_("Milestones (up to 10, empty = default)"),
            placeholder="75, 150, 300, 1000",
            default=", ".join(str(v) for v in config["milestones"]) or None,
            required=False,
            max_length=120,
        )
        self.message_field = discord.ui.TextInput(
            label=_("Message (empty = default)"),
            placeholder=PLACEHOLDER_HINT,
            default=config["message"],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_MESSAGE_LENGTH,
        )
        self.add_item(self.values_field)
        self.add_item(self.message_field)

    async def on_submit(self, interaction):
        try:
            values = parse_milestones(self.values_field.value)
        except ValueError:
            return await interaction.response.send_message(
                _(
                    "Give up to {count} whole numbers between 1 and {maximum}, "
                    "separated by commas."
                ).format(count=MAX_CUSTOM_MILESTONES, maximum=f"{MAX_MILESTONE:,}"),
                ephemeral=True,
            )
        self.panel.config["milestones"] = values
        self.panel.config["message"] = (self.message_field.value or "").strip() or None
        await self.panel.save_and_rerender(interaction)


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel, current):
        self._owner = panel
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=_("Celebration channel..."),
            min_values=1,
            max_values=1,
            default_values=[current] if current is not None else [],
            row=0,
        )

    async def callback(self, interaction):
        await self._owner.pick_channel(interaction, self.values[0].id)


class MilestonePanel(AuthorView):
    """The ephemeral manager: channel, on/off, preview, Yasuho+ customisation."""

    def __init__(self, cog, guild, author_id, config):
        super().__init__(author_id, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.config = config

        select = _ChannelSelect(self, guild.get_channel(config["channel_id"] or 0))
        toggle = discord.ui.Button(
            label=_("Disable") if config["enabled"] else _("Enable"),
            style=discord.ButtonStyle.danger
            if config["enabled"]
            else discord.ButtonStyle.success,
            row=1,
        )
        toggle.callback = self._toggle
        preview = discord.ui.Button(
            label=_("Preview"), style=discord.ButtonStyle.secondary, row=1
        )
        preview.callback = self._preview
        custom = discord.ui.Button(
            label=_("Customize"), style=discord.ButtonStyle.primary, row=1
        )
        custom.callback = self._customize
        for item in (select, toggle, preview, custom):
            self.add_item(item)

    @property
    def custom_allowed(self):
        return premium.resolve_guild_limits(self.cog.bot, self.guild.id).milestones_custom

    async def interaction_check(self, interaction):
        # Authority is re-checked on every click, not only when /milestones
        # opened the panel: a manager who lost Manage Server loses the panel.
        if not await super().interaction_check(interaction):
            return False
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.manage_guild:
            await interaction.response.send_message(
                _("You need the Manage Server permission for this."), ephemeral=True
            )
            return False
        return True

    def build_embed(self):
        config = self.config
        embed = discord.Embed(
            title=_("Member milestones"),
            description=_(
                "Yasuho celebrates when your server reaches a member milestone."
            ),
            colour=random_colour(),
        )
        embed.add_field(
            name=_("Status"),
            value=_("Enabled") if config["enabled"] else _("Disabled"),
        )
        channel_id = config["channel_id"]
        embed.add_field(
            name=_("Channel"),
            value=f"<#{channel_id}>" if channel_id else _("*Not set.*"),
        )
        milestones = effective_milestones(config, self.custom_allowed)
        embed.add_field(
            name=_("Milestones"),
            value=", ".join(f"{m:,}" for m in milestones),
            inline=False,
        )
        template = config["message"] if self.custom_allowed else None
        embed.add_field(
            name=_("Message"),
            value=(template or _("Default message"))[:1000],
            inline=False,
        )
        if not self.custom_allowed:
            embed.set_footer(
                text=_("Yasuho+ unlocks your own milestones and message.")
            )
        return embed

    async def save_and_rerender(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()  # the writes below must not eat the 3 s
        await settings.set_guild(
            self.cog.bot.db_pool, self.guild.id, SETTINGS_KEY, dict(self.config)
        )
        if self.config["enabled"] and self.config["channel_id"]:
            # Enabling (or re-targeting) never replays milestones already passed.
            await self.cog.arm(self.guild)
        new = MilestonePanel(self.cog, self.guild, self.author_id, self.config)
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def pick_channel(self, interaction, channel_id):
        channel = self.guild.get_channel(channel_id)
        perms = channel.permissions_for(self.guild.me) if channel and self.guild.me else None
        if perms is None or not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(
                _(
                    "I can't send messages in that channel. Give me View Channel "
                    "and Send Messages there first."
                ),
                ephemeral=True,
            )
        self.config["channel_id"] = channel.id
        await self.save_and_rerender(interaction)

    async def _toggle(self, interaction):
        if not self.config["enabled"] and not self.config["channel_id"]:
            return await interaction.response.send_message(
                _("Pick a celebration channel first."), ephemeral=True
            )
        self.config["enabled"] = not self.config["enabled"]
        await self.save_and_rerender(interaction)

    async def _preview(self, interaction):
        milestones = effective_milestones(self.config, self.custom_allowed)
        shown = highest_passed(milestones, self.guild.member_count or 0) or milestones[0]
        template = self.config["message"] if self.custom_allowed else None
        await interaction.response.send_message(
            render_message(self.guild, interaction.user, shown, template),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _customize(self, interaction):
        if self.custom_allowed:
            return await interaction.response.send_modal(MilestoneModal(self))
        upsell = await premium_upsell.for_guild_refusal(
            self.cog.bot,
            limit_key="milestones_custom",
            guild_id=self.guild.id,
            person_id=interaction.user.id,
            is_admin=premium_upsell.invoker_is_admin(interaction.user),
            already_top_tier=premium_upsell.is_guild_already_top_tier(
                self.cog.bot, self.guild.id
            ),
            kind="unlock",
        )
        text = _("Custom milestones and messages are a Yasuho+ feature.")
        kwargs = {"ephemeral": True}
        if upsell is not None:
            text += "\n" + upsell.line
            view = upsell.view()
            if view is not None:
                kwargs["view"] = view
        await interaction.response.send_message(text, **kwargs)


class Milestones(commands.Cog):
    """Celebrate member-count milestones in a configured channel."""

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> highest milestone known announced (see the module docstring).
        self._floor = {}

    async def arm(self, guild, *, below=0, keep=False):
        """Raise the high-water mark to the highest milestone already passed.

        Idempotent and never lowers it. ``below`` lets a join arm against the
        count BEFORE itself, so the join that crosses a milestone still counts.
        ``keep`` (cold start) only creates a missing mark and adopts an existing
        one as is.
        """
        config = normalise(
            await settings.get_guild(self.bot.db_pool, guild.id, SETTINGS_KEY, None)
        )
        custom = premium.resolve_guild_limits(self.bot, guild.id).milestones_custom
        passed = highest_passed(
            effective_milestones(config, custom), (guild.member_count or 0) - below
        )
        stored = await self.bot.db_pool.fetchval(_LOAD_SQL if keep else _ARM_SQL, guild.id, passed)
        self._floor[guild.id] = max(self._floor.get(guild.id, 0), stored)
        return stored

    @commands.hybrid_command(name="milestones")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def milestones(self, ctx):
        """Celebrate when your server reaches a member milestone."""
        config = normalise(
            await settings.get_guild(self.bot.db_pool, ctx.guild.id, SETTINGS_KEY, None)
        )
        view = MilestonePanel(self, ctx.guild, ctx.author.id, config)
        view.message = await ctx.send(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            guild = member.guild
            blob = await settings.get_guild(
                self.bot.db_pool, guild.id, SETTINGS_KEY, None
            )
            if not isinstance(blob, dict) or blob.get("enabled") is not True:
                return
            config = normalise(blob)
            if not config["channel_id"]:
                return
            count = guild.member_count or 0
            custom = premium.resolve_guild_limits(self.bot, guild.id).milestones_custom
            reached = highest_passed(effective_milestones(config, custom), count)
            if not reached:
                return
            floor = self._floor.get(guild.id)
            if floor is None:
                # Cold (restart, or enabled from the dashboard): swallow every
                # milestone passed BEFORE this join, keep this join's own.
                floor = await self.arm(guild, below=1, keep=True)
            if reached <= floor:
                return
            won = await self.bot.db_pool.fetchval(_CLAIM_SQL, guild.id, reached)
            # Won or lost, the stored mark is now at least `reached`.
            self._floor[guild.id] = max(self._floor.get(guild.id, 0), reached)
            if won is not None:
                await self._announce(member, config, reached, custom)
        except Exception:
            log.exception("Milestones on_member_join failed")

    async def _announce(self, member, config, milestone, custom):
        guild = member.guild
        channel = guild.get_channel(config["channel_id"])
        permissions = (
            channel.permissions_for(guild.me) if channel and guild.me else None
        )
        if channel is None:
            reason = "channel_missing"
        elif not (permissions.view_channel and permissions.send_messages):
            reason = "missing_permissions"
        else:
            locale = await i18n.resolve_guild_locale(self.bot, guild)
            with i18n.locale(locale):
                text = render_message(
                    guild, member, milestone, config["message"] if custom else None
                )
            try:
                await channel.send(
                    text, allowed_mentions=discord.AllowedMentions.none()
                )
                log.info("MILESTONE-SENT guild=%s milestone=%s", guild.id, milestone)
                return
            except discord.HTTPException as exc:
                reason = "http_" + type(exc).__name__.lower()
        # Persist-before-deliver: the mark already moved, so a failed send is
        # logged and never retried (no spam on a broken channel).
        log.warning("MILESTONE-FAIL guild=%s reason=%s", guild.id, reason)


async def setup(bot):
    await bot.add_cog(Milestones(bot))
