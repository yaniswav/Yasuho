"""Interactive announcement builder.

`/announce` opens an author-restricted panel that reuses the shared
``tools.embed_creator`` editor to compose a rich embed, pick a target channel,
optionally ping a role, preview it, and send it. The draft lives only in the
open panel (in memory) - announcements are one-shot, so nothing is persisted.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import logging

import discord
from discord.ext import commands

from tools import embed_creator
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)

# Tokens an announcement body may interpolate, shown in the placeholder guide.
PLACEHOLDERS = [
    ("{server}", "The server name"),
    ("{members}", "The current member count"),
    ("{channel}", "A mention of the target channel"),
]
PLACEHOLDER_HINT = "{server} {members} {channel}"


def build_substitution(guild, channel):
    """Return the token resolver passed to embed_creator.render.

    Pure and side-effect free (takes plain values), so it is unit-testable.
    """
    members = f"{guild.member_count:,}" if guild and guild.member_count else "0"
    replacements = {
        "{server}": guild.name if guild else "",
        "{members}": members,
        "{channel}": channel.mention if channel else "",
    }

    def substitute(text):
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    return substitute


class _TargetChannelSelect(discord.ui.ChannelSelect):
    """Pick the channel the announcement will be posted to."""

    def __init__(self, panel):
        self._owner = panel
        defaults = []
        cid = panel.draft.get("channel_id")
        if cid:
            channel = panel.guild.get_channel(cid)
            if channel is not None:
                defaults = [channel]
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=_("Target channel..."),
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=0,
        )

    async def callback(self, interaction):
        try:
            self._owner.draft["channel_id"] = self.values[0].id
            await self._owner._rerender(interaction)
        except Exception:
            log.exception("Announce channel select failed")
            await self._owner._error(interaction)


class _PingRoleSelect(discord.ui.RoleSelect):
    """Optionally pick a role to ping with the announcement (0 = none)."""

    def __init__(self, panel):
        self._owner = panel
        defaults = []
        rid = panel.draft.get("role_id")
        if rid:
            role = panel.guild.get_role(rid)
            if role is not None:
                defaults = [role]
        super().__init__(
            placeholder=_("Ping a role (optional)..."),
            min_values=0,
            max_values=1,
            default_values=defaults,
            row=1,
        )

    async def callback(self, interaction):
        try:
            self._owner.draft["role_id"] = self.values[0].id if self.values else None
            await self._owner._rerender(interaction)
        except Exception:
            log.exception("Announce ping select failed")
            await self._owner._error(interaction)


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Preview"), style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction):
        try:
            embed = self._owner.render_announcement()
            if not embed_creator.embed_has_content(embed):
                return await interaction.response.send_message(
                    _("Add some content to the embed first."), ephemeral=True
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            log.exception("Announce preview failed")
            await embed_creator.notify_failure(
                interaction, _("Could not render the preview.")
            )


class _SendButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Send"), style=discord.ButtonStyle.success, row=3)

    async def callback(self, interaction):
        try:
            await self._owner.send(interaction)
        except Exception:
            log.exception("Announce send failed")
            await embed_creator.notify_failure(
                interaction, _("Could not send the announcement.")
            )


class AnnouncePanel(AuthorView):
    """Author-restricted announcement builder (an embed_creator.EmbedEditorHost).

    Exposes ``embed_config`` (the draft embed the shared modals mutate) and
    ``on_embed_changed`` (re-render in place, no persistence).
    """

    def __init__(self, cog, guild, author_id, draft, timeout=600):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.cog = cog
        self.guild = guild
        self.draft = draft
        self.placeholder_hint = PLACEHOLDER_HINT
        self.asset_hint = "https://..."

        self.add_item(_TargetChannelSelect(self))
        self.add_item(_PingRoleSelect(self))
        self.add_item(
            embed_creator.make_edit_select(
                self, placeholder=_("Edit the announcement embed..."), row=2
            )
        )
        self.add_item(_PreviewButton(self))
        self.add_item(_SendButton(self))
        self.add_item(
            embed_creator.PlaceholderGuideButton(PLACEHOLDERS, label=_("Placeholders"), row=3)
        )

    # -- embed_creator.EmbedEditorHost contract -------------------------
    @property
    def embed_config(self):
        return self.draft["embed"]

    async def on_embed_changed(self, interaction):
        await self._rerender(interaction)

    # -- rendering ------------------------------------------------------
    def _target_channel(self):
        cid = self.draft.get("channel_id")
        return self.guild.get_channel(cid) if cid else None

    def render_announcement(self):
        """Render the draft embed with placeholders resolved (preview + send)."""
        substitute = build_substitution(self.guild, self._target_channel())
        return embed_creator.render(self.draft.get("embed") or {}, substitute=substitute)

    def build_embed(self):
        colour = (self.draft.get("embed") or {}).get("color")
        embed = discord.Embed(
            title=_("Announcement builder"),
            description=_(
                "Compose the embed with the menu, pick a channel (and optionally "
                "a role to ping), then **Send**. Preview shows it only to you."
            ),
            colour=colour if isinstance(colour, int) else random_colour(),
        )
        cid = self.draft.get("channel_id")
        rid = self.draft.get("role_id")
        embed.add_field(
            name=_("Channel"),
            value=f"<#{cid}>" if cid else _("*Not set.*"),
            inline=True,
        )
        embed.add_field(
            name=_("Ping"),
            value=f"<@&{rid}>" if rid else _("None"),
            inline=True,
        )
        embed.add_field(
            name=_("Embed"),
            value=embed_creator.summarise(self.draft.get("embed") or {}),
            inline=False,
        )
        embed.set_footer(
            text=_("Only you can use this. Placeholders: {placeholders}").format(
                placeholders=PLACEHOLDER_HINT
            )
        )
        return embed

    async def _rerender(self, interaction):
        new = AnnouncePanel(self.cog, self.guild, self.author_id, self.draft)
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def _error(self, interaction):
        await embed_creator.notify_failure(interaction, _("Something went wrong."))

    # -- send -----------------------------------------------------------
    async def send(self, interaction):
        channel = self._target_channel()
        if channel is None:
            return await interaction.response.send_message(
                _("Pick a target channel first."), ephemeral=True
            )
        if not channel.permissions_for(self.guild.me).send_messages:
            return await interaction.response.send_message(
                _("I can't send messages in {channel}.").format(channel=channel.mention),
                ephemeral=True,
            )

        embed = self.render_announcement()
        if not embed_creator.embed_has_content(embed):
            return await interaction.response.send_message(
                _("Add some content to the embed first."), ephemeral=True
            )

        content = None
        allowed = discord.AllowedMentions.none()
        rid = self.draft.get("role_id")
        role = self.guild.get_role(rid) if rid else None
        if role is not None:
            content = role.mention
            allowed = discord.AllowedMentions(roles=[role])

        try:
            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException:
            log.exception("Announce channel send failed")
            return await interaction.response.send_message(
                _("Sending failed, please try again."), ephemeral=True
            )

        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            _("Announcement sent to {channel}.").format(channel=channel.mention),
            ephemeral=True,
        )


class Announcements(commands.Cog):
    """Build and send rich announcement embeds interactively."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="announce", aliases=["announcement"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(embed_links=True)
    async def announce(self, ctx):
        """Open the announcement builder."""
        draft = {"embed": embed_creator.default_embed(), "channel_id": None, "role_id": None}
        view = AnnouncePanel(self, ctx.guild, ctx.author.id, draft)
        view.message = await ctx.send(embed=view.build_embed(), view=view)


async def setup(bot):
    await bot.add_cog(Announcements(bot))
