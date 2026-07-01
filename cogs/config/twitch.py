import logging
import types
import typing

import discord
from discord.ext import commands

from tools import embed_creator, settings
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.paginator import Paginator, paginate_lines
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Twitch brand purple, used as the default embed colour.
TWITCH_PURPLE = 0x9146FF

# Legacy "Live" role name (invisible-emote style) kept for backward compat when
# no role_id is configured. The trailing char is a red circle (U+1F534).
LEGACY_ROLE_NAME = "Live \U0001F534"

# Hint shown in modals so admins know what they can interpolate.
PLACEHOLDER_HINT = "{streamer} {mention} {url} {game} {title} {server}"
ASSET_HINT = "https://... or {avatar}"

# Twitch's own colour palette (brand purple, aliased as "twitch" and "purple")
# lives on TwitchPanel.colour_names below, so the shared embed_creator palette
# stays cog-agnostic (we never mutate the shared global).

# Edit-menu option for the classic-text style. The embed style reuses
# embed_creator's default edit options through make_edit_select. The label is
# N_-marked for extraction and translated at the use site via _(label).
TEXT_EDIT_OPTIONS = [
    ("message", N_("Message"), "\U0001F4AC"),
]

# Placeholder tokens shown by the guide button, as (token, description) pairs.
# Descriptions are N_-marked for extraction and translated at the use site.
PLACEHOLDER_ENTRIES = [
    ("{streamer}", N_("The streamer's display name.")),
    ("{mention}", N_("Pings the streamer, e.g. @name.")),
    ("{url}", N_("A clickable link to the Twitch stream.")),
    ("{game}", N_("What they are playing (may be blank).")),
    ("{title}", N_("The stream's title.")),
    ("{server}", N_("Your server's name.")),
    (
        "{avatar}",
        N_("The streamer's avatar URL. Perfect for the Thumbnail or Image field."),
    ),
]

# Intro blurb for the placeholder guide (carries the old example + tip lines).
# N_-marked for extraction; translated at the use site via _(PLACEHOLDER_INTRO).
PLACEHOLDER_INTRO = N_(
    "Drop any of these into your message, or into the embed's title, "
    "description, fields, author, or footer. They are filled in automatically "
    "the moment a watched member goes live.\n\n"
    "Example: `{mention} is now live playing {game}! Watch: {url}`\n"
    "Tip: pop {avatar} into Thumbnail for a clean look."
)


def _default_config():
    """A fresh, fully-populated Twitch alert config blob (no shared refs)."""

    return {
        "enabled": False,
        "channel_id": None,
        "role_id": None,
        "style": "embed",
        "text": "{mention} just went live on Twitch! Come watch: {url}",
        "embed": {
            "title": "{streamer} is now live on Twitch!",
            "description": (
                "{mention} just went live - come hang out!\n\n"
                "[Watch the stream]({url})"
            ),
            "color": TWITCH_PURPLE,
            "author": {"name": "", "icon": ""},
            "footer": {"text": "Streaming in {server}", "icon": ""},
            "thumbnail": "{avatar}",
            "image": "",
            "fields": [
                {"name": "Playing", "value": "{game}", "inline": True},
                {"name": "Title", "value": "{title}", "inline": True},
            ],
        },
    }


def _merge_defaults(blob):
    """Return a fresh config merged over the defaults (fills missing keys).

    Every nested container is rebuilt so the result never aliases the settings
    cache; the panel can mutate it freely and persist with one set_guild call.
    The embed sub-blob is rebuilt by embed_creator.merge_embed.
    """

    config = _default_config()
    if not isinstance(blob, dict):
        return config

    for key in ("enabled", "channel_id", "role_id", "style", "text"):
        if key in blob:
            config[key] = blob[key]
    if config["style"] not in ("embed", "text"):
        config["style"] = "embed"

    config["embed"] = embed_creator.merge_embed(blob.get("embed"))
    return config


# ----------------------------------------------------------------------
# Text-style modal (the cog's own concern; embed parts come from embed_creator)
# ----------------------------------------------------------------------
class MessageModal(LocaleModal):
    """Edit the classic-text alert message (used when style == 'text')."""

    def __init__(self, panel):
        super().__init__(title=_("Edit message"))
        self.panel = panel
        self.field = discord.ui.TextInput(
            label=_("Alert message"),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=2000,
            default=panel.config.get("text") or None,
            placeholder=PLACEHOLDER_HINT,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        try:
            self.panel.config["text"] = self.field.value.strip()
            await self.panel.on_embed_changed(interaction)
        except Exception:
            log.exception("Twitch message modal failed")
            await embed_creator.notify_failure(interaction)


# ----------------------------------------------------------------------
# Panel components
# ----------------------------------------------------------------------
class TwitchChannelSelect(discord.ui.ChannelSelect):
    """Pick the channel live alerts are posted to."""

    def __init__(self, panel):
        self.panel = panel
        defaults = []
        cid = panel.config.get("channel_id")
        if cid:
            channel = panel.guild.get_channel(cid)
            if channel is not None:
                defaults = [channel]
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder=_("Select the alert channel..."),
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=0,
        )

    async def callback(self, interaction):
        try:
            self.panel.config["channel_id"] = self.values[0].id
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch channel select failed")
            await self.panel._error(interaction)


class TwitchRoleSelect(discord.ui.RoleSelect):
    """Pick the role assigned to a member while they are live (optional)."""

    def __init__(self, panel):
        self.panel = panel
        defaults = []
        rid = panel.config.get("role_id")
        if rid:
            role = panel.guild.get_role(rid)
            if role is not None:
                defaults = [role]
        super().__init__(
            placeholder=_("Select the Live role (optional)..."),
            min_values=0,
            max_values=1,
            default_values=defaults,
            row=1,
        )

    async def callback(self, interaction):
        try:
            self.panel.config["role_id"] = (
                self.values[0].id if self.values else None
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch role select failed")
            await self.panel._error(interaction)


class _MessageSelect(discord.ui.Select):
    """Single 'Message' option that edits the classic-text alert (text style).

    The embed style uses embed_creator.make_edit_select instead; only the
    text-style message editing stays the cog's own concern.
    """

    def __init__(self, panel):
        self.panel = panel
        options = [
            discord.SelectOption(label=_(label), value=value, emoji=emoji)
            for value, label, emoji in TEXT_EDIT_OPTIONS
        ]
        super().__init__(
            placeholder=_("Edit the alert message..."),
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction):
        try:
            if self.values[0] == "message":
                await interaction.response.send_modal(MessageModal(self.panel))
        except Exception:
            log.exception("Twitch message select failed")
            await self.panel._error(interaction)


class _StyleButton(discord.ui.Button):
    """Switch the notification between an embed and a classic message."""

    def __init__(self, panel):
        self.panel = panel
        style = panel.config.get("style", "embed")
        label = _("Style: Embed") if style == "embed" else _("Style: Classic")
        super().__init__(
            label=label, style=discord.ButtonStyle.secondary, row=3
        )

    async def callback(self, interaction):
        try:
            current = self.panel.config.get("style", "embed")
            self.panel.config["style"] = (
                "text" if current == "embed" else "embed"
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch style button failed")
            await self.panel._error(interaction)


class _PlaceholdersButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label=_("Placeholders"),
            style=discord.ButtonStyle.secondary,
            row=3,
        )

    async def callback(self, interaction):
        try:
            entries = [
                (token, _(desc)) for token, desc in PLACEHOLDER_ENTRIES
            ]
            await interaction.response.send_message(
                embed=embed_creator.placeholder_guide(
                    entries,
                    title=_("Twitch alert placeholders"),
                    intro=_(PLACEHOLDER_INTRO),
                    colour=TWITCH_PURPLE,
                ),
                ephemeral=True,
            )
        except Exception:
            log.exception("Twitch placeholders button failed")
            await self.panel._error(interaction)


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            label=_("Preview"), style=discord.ButtonStyle.primary, row=3
        )

    async def callback(self, interaction):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.panel.cog.send_preview(interaction, self.panel.config)
        except Exception:
            log.exception("Twitch preview failed")
            await embed_creator.notify_failure(
                interaction, _("Could not render the preview.")
            )


class _EnableButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        enabled = bool(panel.config.get("enabled"))
        super().__init__(
            label=_("Disable") if enabled else _("Enable"),
            style=(
                discord.ButtonStyle.danger
                if enabled
                else discord.ButtonStyle.success
            ),
            row=4,
        )

    async def callback(self, interaction):
        try:
            self.panel.config["enabled"] = not bool(
                self.panel.config.get("enabled")
            )
            await self.panel.cog.save(self.panel.guild.id, self.panel.config)
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Twitch enable button failed")
            await self.panel._error(interaction)


# ----------------------------------------------------------------------
# Main control panel
# ----------------------------------------------------------------------
class TwitchPanel(AuthorView):
    """Author-restricted Twitch live-alert builder (the single entry point).

    Satisfies the embed_creator.EmbedEditorHost protocol: it exposes the
    ``embed_config`` sub-blob the shared modals/edit-select mutate, plus an async
    ``on_embed_changed`` that persists and refreshes. ``placeholder_hint`` and
    ``asset_hint`` are read by the shared modals via getattr.
    """

    # Surfaced to the shared embed_creator modals (read via getattr).
    placeholder_hint = PLACEHOLDER_HINT
    asset_hint = ASSET_HINT
    # Per-cog colour palette: a copy of the shared names plus the Twitch brand
    # purple ("twitch"/"purple"). The shared embed_creator.COLOUR_NAMES is never
    # mutated, so other cogs keep their own vocabulary.
    colour_names = {**embed_creator.COLOUR_NAMES, "twitch": TWITCH_PURPLE, "purple": TWITCH_PURPLE}

    def __init__(self, cog, guild, author_id, config, timeout=180):
        super().__init__(
            author_id,
            timeout=timeout,
            deny_message="This panel isn't for you.",
        )
        self.cog = cog
        self.guild = guild
        self.config = config

        self.add_item(TwitchChannelSelect(self))
        self.add_item(TwitchRoleSelect(self))
        if config.get("style") == "text":
            self.add_item(_MessageSelect(self))
        else:
            self.add_item(
                embed_creator.make_edit_select(
                    self, placeholder=_("Edit the alert embed..."), row=2
                )
            )
        self.add_item(_StyleButton(self))
        self.add_item(_PlaceholdersButton(self))
        self.add_item(_PreviewButton(self))
        self.add_item(_EnableButton(self))

    # -- EmbedEditorHost contract ---------------------------------------
    @property
    def embed_config(self):
        """The embed sub-blob the shared embed_creator modals mutate."""

        return self.config["embed"]

    async def on_embed_changed(self, interaction):
        """Persist the whole config blob and refresh the panel in place.

        This is the EmbedEditorHost hook the shared modals and edit-select call
        after mutating embed_config. MessageModal reuses it too, since persist +
        refresh is identical for the classic-text style.
        """

        await self.cog.save(self.guild.id, self.config)
        await self._refresh(interaction)

    def build_embed(self):
        config = self.config
        embed_cfg = config.get("embed") or {}
        enabled = bool(config.get("enabled"))
        style = config.get("style", "embed")
        colour = embed_cfg.get("color")

        embed = discord.Embed(
            title=_("Twitch live alerts"),
            description=_(
                "Design the alert that fires when a watched member goes live "
                "on Twitch. Every change saves instantly - hit **Preview** to "
                "see it, and add streamers with `/twitch watch`."
            ),
            colour=colour if isinstance(colour, int) else TWITCH_PURPLE,
        )

        cid = config.get("channel_id")
        channel_value = f"<#{cid}>" if cid else _("*Not set.*")
        rid = config.get("role_id")
        role_value = f"<@&{rid}>" if rid else _("*None (legacy lookup).*")

        embed.add_field(
            name=_("Status"),
            value=(
                ("\U0001F7E2 " + _("Enabled"))
                if enabled
                else ("\U0001F534 " + _("Disabled"))
            ),
            inline=True,
        )
        embed.add_field(name=_("Channel"), value=channel_value, inline=True)
        embed.add_field(
            name=_("Style"),
            value=_("Embed") if style == "embed" else _("Classic message"),
            inline=True,
        )
        embed.add_field(name=_("Live role"), value=role_value, inline=False)

        if style == "text":
            text = config.get("text") or _("*none*")
            if len(text) > 200:
                text = text[:197] + "..."
            embed.add_field(name=_("Message"), value=text, inline=False)
        else:
            summary = embed_creator.summarise(embed_cfg)
            content_line = config.get("text")
            if content_line:
                preview = content_line
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                summary += "\n" + _("**Content line:** {preview}").format(
                    preview=preview
                )
            embed.add_field(name=_("Embed"), value=summary, inline=False)

        embed.set_footer(
            text=_("Only you can use these controls. Placeholders: {placeholders}").format(
                placeholders=PLACEHOLDER_HINT
            )
        )
        return embed

    async def _refresh(self, interaction):
        """Rebuild a fresh panel from current config and show it in place."""

        new = TwitchPanel(self.cog, self.guild, self.author_id, self.config)
        new.message = self.message
        self.stop()
        embed = new.build_embed()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=embed, view=new
        )

    async def _error(self, interaction):
        await embed_creator.notify_failure(interaction)


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class Twitch(commands.Cog):
    """Announce when watched members go live on Twitch, with a Live role."""

    def __init__(self, bot):
        self.bot = bot

    # -- config storage (single JSONB blob per guild) -------------------
    async def get_config(self, guild_id):
        """Load the Twitch alert blob, merged over the defaults."""

        blob = await settings.get_guild(self.bot.db_pool, guild_id, "twitch", None)
        return _merge_defaults(blob)

    async def save(self, guild_id, config):
        await settings.set_guild(self.bot.db_pool, guild_id, "twitch", config)

    # -- placeholder + embed building -----------------------------------
    def _apply(self, text, member, activity=None):
        if not text:
            return text
        guild = member.guild if member else None
        replacements = {
            "{streamer}": member.display_name if member else "",
            "{mention}": member.mention if member else "",
            "{url}": (getattr(activity, "url", "") or "") if activity else "",
            "{game}": (getattr(activity, "game", "") or "") if activity else "",
            "{title}": (getattr(activity, "name", "") or "") if activity else "",
            "{server}": guild.name if guild else "",
            "{avatar}": member.display_avatar.url if member else "",
        }
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    def _compose(self, config, member, activity=None):
        """Build (content, embed) exactly as a real go-live would render."""

        style = config.get("style", "embed")
        text = self._apply(config.get("text"), member, activity)
        if style == "text":
            return text or None, None

        embed = embed_creator.render(
            config.get("embed") or {},
            substitute=lambda value: self._apply(value, member, activity),
        )
        if not embed_creator.embed_has_content(embed):
            embed.description = self._apply(
                _("{mention} is now live! {url}"), member, activity
            )
        return (text or None), embed

    async def send_preview(self, interaction, config):
        """Render the alert as a real go-live would, shown to the admin."""

        member = interaction.user
        activity = types.SimpleNamespace(
            url="https://twitch.tv/yourchannel",
            game="Just Chatting",
            name="Live now: come hang out!",
            platform="Twitch",
        )
        content, embed = self._compose(config, member, activity)
        kwargs = {"ephemeral": True}
        if content:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if not content and embed is None:
            kwargs["content"] = _(
                "Nothing to preview yet - add a message or some embed content."
            )
        await interaction.followup.send(**kwargs)

    # -- live role helpers ----------------------------------------------
    def _resolve_role(self, guild, config):
        """The configured Live role, falling back to the legacy name."""

        rid = config.get("role_id")
        if rid:
            return guild.get_role(rid)
        return discord.utils.get(guild.roles, name=LEGACY_ROLE_NAME)

    async def _assign_role(self, member, config):
        try:
            role = self._resolve_role(member.guild, config)
            if role is not None and role not in member.roles:
                await member.add_roles(role, reason="Twitch live alert")
        except Exception:
            log.exception("Twitch role assign failed")

    async def _remove_role(self, member, config):
        try:
            role = self._resolve_role(member.guild, config)
            if role is not None and role in member.roles:
                await member.remove_roles(role, reason="Twitch live ended")
        except Exception:
            log.exception("Twitch role removal failed")

    # -- streaming listener ---------------------------------------------
    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ):
        try:
            was_live = any(
                isinstance(a, discord.Streaming) for a in before.activities
            )
            now_activity = next(
                (a for a in after.activities if isinstance(a, discord.Streaming)),
                None,
            )

            if now_activity is not None and not was_live:
                await self._on_go_live(after, now_activity)
            elif now_activity is None and was_live:
                config = await self.get_config(after.guild.id)
                await self._remove_role(after, config)
        except Exception:
            log.exception("Twitch on_member_update failed")

    async def _on_go_live(self, member, activity):
        """Post the alert and assign the Live role when a watched member goes live."""

        guild = member.guild
        try:
            row = await self.bot.db_pool.fetchrow(
                "SELECT channel_id FROM twitch_alert "
                "WHERE guild_id = $1 AND user_id = $2;",
                guild.id,
                member.id,
            )
        except Exception:
            log.exception("Twitch watchlist lookup failed")
            return
        if row is None:
            return

        config = await self.get_config(guild.id)
        if not config.get("enabled"):
            return

        # Per-member override (0 == no override) else the guild channel.
        channel_id = row["channel_id"] or config.get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            try:
                content, embed = self._compose(config, member, activity)
                kwargs = {}
                if content:
                    kwargs["content"] = content
                if embed is not None:
                    kwargs["embed"] = embed
                if kwargs:
                    await channel.send(**kwargs)
            except Exception:
                log.exception("Twitch alert send failed")

        await self._assign_role(member, config)

    # -- commands -------------------------------------------------------
    @commands.hybrid_group(name="twitch", aliases=["stream"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch(self, ctx: commands.Context):
        """Open the Twitch live-alert builder."""

        if ctx.invoked_subcommand is not None:
            return

        config = await self.get_config(ctx.guild.id)
        view = TwitchPanel(self, ctx.guild, ctx.author.id, config)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @twitch.command(name="watch", aliases=["add"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch_watch(
        self,
        ctx: commands.Context,
        member: discord.Member,
        channel: typing.Optional[discord.TextChannel] = None,
    ):
        """Add a member to the live-alert watchlist (channel is an optional override)."""

        override = channel.id if channel else 0
        try:
            await self.bot.db_pool.execute(
                "DELETE FROM twitch_alert WHERE guild_id = $1 AND user_id = $2;",
                ctx.guild.id,
                member.id,
            )
            await self.bot.db_pool.execute(
                "INSERT INTO twitch_alert(guild_id, user_id, channel_id, message) "
                "VALUES($1, $2, $3, NULL);",
                ctx.guild.id,
                member.id,
                override,
            )
        except Exception:
            log.exception("Twitch watch failed")
            return await ctx.send(
                _("Could not add that member to the watchlist.")
            )

        where = (
            channel.mention if channel else _("the configured alert channel")
        )
        embed = discord.Embed(title=_("Twitch watchlist"), colour=random_colour())
        embed.add_field(name=_("Now watching"), value=member.mention, inline=True)
        embed.add_field(name=_("Alerts in"), value=where, inline=True)
        embed.set_footer(text=_("Use /twitch to open the builder."))
        await ctx.send(embed=embed)

    @twitch.command(name="unwatch", aliases=["remove", "del"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch_unwatch(
        self, ctx: commands.Context, member: discord.Member
    ):
        """Remove a member from the live-alert watchlist."""

        try:
            await self.bot.db_pool.execute(
                "DELETE FROM twitch_alert WHERE guild_id = $1 AND user_id = $2;",
                ctx.guild.id,
                member.id,
            )
        except Exception:
            log.exception("Twitch unwatch failed")
            return await ctx.send(_("Could not remove that member."))

        embed = discord.Embed(title=_("Twitch watchlist"), colour=random_colour())
        embed.add_field(name=_("Removed"), value=member.mention, inline=False)
        await ctx.send(embed=embed)

    @twitch.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def twitch_list(self, ctx: commands.Context):
        """Show every member on the live-alert watchlist."""

        try:
            rows = await self.bot.db_pool.fetch(
                "SELECT user_id, channel_id FROM twitch_alert "
                "WHERE guild_id = $1 ORDER BY user_id;",
                ctx.guild.id,
            )
        except Exception:
            log.exception("Twitch list failed")
            return await ctx.send(_("Could not load the watchlist."))

        lines = []
        for row in rows:
            cid = row["channel_id"]
            target = f"<#{cid}>" if cid else _("default alert channel")
            lines.append(f"<@{row['user_id']}> -> {target}")

        embeds = paginate_lines(
            lines, title=_("Twitch watchlist"), colour=TWITCH_PURPLE, per_page=10
        )
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)

    @twitch.command(name="createrole", aliases=["setup-role"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def twitch_createrole(self, ctx: commands.Context):
        """Create a Live streamer role and link it to the alert config."""

        existing = discord.utils.get(ctx.guild.roles, name=LEGACY_ROLE_NAME)
        if existing is not None:
            config = await self.get_config(ctx.guild.id)
            if not config.get("role_id"):
                config["role_id"] = existing.id
                await self.save(ctx.guild.id, config)
            return await ctx.send(
                _(
                    "Your guild already has a Live streamer role - it is now "
                    "linked."
                )
            )

        try:
            role = await ctx.guild.create_role(
                name=LEGACY_ROLE_NAME, hoist=True, reason="Twitch live role"
            )
        except discord.HTTPException as e:
            return await ctx.send(
                _("Could not create the Live streamer role.") + f"\n\n{e}"
            )

        config = await self.get_config(ctx.guild.id)
        config["role_id"] = role.id
        await self.save(ctx.guild.id, config)
        await ctx.send(
            _(
                "Live streamer role created and linked. Move it to your preferred "
                "position in the role list."
            )
        )

    @twitch.command(name="removerole", aliases=["delete-role", "del-role"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def twitch_removerole(self, ctx: commands.Context):
        """Delete the Live streamer role and unlink it from the config."""

        config = await self.get_config(ctx.guild.id)
        role = self._resolve_role(ctx.guild, config)
        if role is None:
            return await ctx.send(_("No Live streamer role is set up."))

        try:
            await role.delete(reason="Twitch live role removed")
        except discord.HTTPException as e:
            return await ctx.send(
                _("Could not delete the Live streamer role.") + f"\n\n{e}"
            )

        if config.get("role_id"):
            config["role_id"] = None
            await self.save(ctx.guild.id, config)
        await ctx.send(_("Live streamer role removed."))


async def setup(bot):
    await bot.add_cog(Twitch(bot))
