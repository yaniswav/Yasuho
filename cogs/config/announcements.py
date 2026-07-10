"""Interactive announcement builder.

`/announce` opens an author-restricted panel that reuses the shared
``tools.embed_creator`` editor to compose a rich embed, pick a target channel,
optionally ping a role, preview it, and send it. The draft lives only in the
open panel (in memory) - announcements are one-shot, so nothing is persisted.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import datetime
import logging

import discord
from discord.ext import commands

from tools import embed_creator
from tools.formats import random_colour
from tools.i18n import _
from tools.time import FutureTime, ShortTime, parse_timestamp_token
from tools.views import AuthorView, LocaleModal

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


class ScheduleModal(LocaleModal):
    """Ask when to post the announcement, then queue it as a timer."""

    def __init__(self, panel):
        super().__init__(title=_("Schedule the announcement"))
        self.panel = panel
        self.when_field = discord.ui.TextInput(
            label=_("When"),
            placeholder=_("e.g. 2h, tomorrow at 9am, or a <t:...> tag"),
            max_length=100,
            required=True,
        )
        self.add_item(self.when_field)

    async def on_submit(self, interaction):
        raw = (self.when_field.value or "").strip()
        reminder = self.panel.cog.bot.get_cog("Reminder")
        tzinfo = (
            await reminder.get_tzinfo(interaction.user.id)
            if reminder is not None
            else datetime.timezone.utc
        )
        now = interaction.created_at.astimezone(tzinfo)
        # A pasted Discord timestamp token wins outright (UTC); otherwise fall
        # back to the existing ShortTime -> FutureTime natural-language parsing.
        dt = parse_timestamp_token(raw)
        if dt is None:
            try:
                dt = ShortTime(raw, now=now, tzinfo=tzinfo).dt
            except commands.BadArgument:
                try:
                    dt = FutureTime(raw, now=now, tzinfo=tzinfo).dt
                except commands.BadArgument:
                    return await interaction.response.send_message(
                        _(
                            "I couldn't understand that time. Try something like "
                            "`2h`, `tomorrow at 9am`, or `in 3 days`."
                        ),
                        ephemeral=True,
                    )
        else:
            dt = dt.astimezone(tzinfo)
        if dt <= now:
            return await interaction.response.send_message(
                _("That time is in the past. Give me a moment in the future."),
                ephemeral=True,
            )
        await self.panel.schedule(interaction, dt)


class _ScheduleButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Schedule"), style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction):
        await interaction.response.send_modal(ScheduleModal(self._owner))


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
        self.add_item(_ScheduleButton(self))
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
    async def _validated(self, interaction):
        """Return the target channel if ready to send/schedule, else None.

        Sends the relevant ephemeral error on failure (channel unset, no send
        permission, or an empty embed).
        """
        channel = self._target_channel()
        if channel is None:
            await interaction.response.send_message(
                _("Pick a target channel first."), ephemeral=True
            )
            return None
        if not channel.permissions_for(self.guild.me).send_messages:
            await interaction.response.send_message(
                _("I can't send messages in {channel}.").format(channel=channel.mention),
                ephemeral=True,
            )
            return None
        if not embed_creator.embed_has_content(self.render_announcement()):
            await interaction.response.send_message(
                _("Add some content to the embed first."), ephemeral=True
            )
            return None
        return channel

    def _ping(self):
        """Return (content, allowed_mentions) for the optional role ping."""
        rid = self.draft.get("role_id")
        role = self.guild.get_role(rid) if rid else None
        if role is None:
            return None, discord.AllowedMentions.none()
        return role.mention, discord.AllowedMentions(roles=[role])

    async def send(self, interaction):
        channel = await self._validated(interaction)
        if channel is None:
            return
        content, allowed = self._ping()
        try:
            await channel.send(
                content=content,
                embed=self.render_announcement(),
                allowed_mentions=allowed,
            )
        except discord.HTTPException:
            log.exception("Announce channel send failed")
            return await interaction.response.send_message(
                _("Sending failed, please try again."), ephemeral=True
            )
        await self._finish(interaction, _("Announcement sent to {channel}.").format(
            channel=channel.mention
        ))

    async def schedule(self, interaction, when):
        channel = await self._validated(interaction)
        if channel is None:
            return
        reminder = self.cog.bot.get_cog("Reminder")
        if reminder is None:
            return await interaction.response.send_message(
                _("Scheduling is unavailable right now."), ephemeral=True
            )
        # The embed blob is stored raw and re-rendered at fire time, so
        # {members} and friends reflect the moment it actually posts.
        await reminder.create_timer(
            when,
            "announcement",
            guild_id=self.guild.id,
            channel_id=channel.id,
            role_id=self.draft.get("role_id"),
            embed=self.draft.get("embed"),
        )
        await interaction.response.send_message(
            _("Scheduled for {when} in {channel}.").format(
                when=discord.utils.format_dt(when, "F"), channel=channel.mention
            ),
            ephemeral=True,
        )
        # Freeze the builder so Send / Schedule can't double-fire the same draft.
        for child in self.children:
            child.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _finish(self, interaction, message):
        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(message, ephemeral=True)


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

    @commands.Cog.listener()
    async def on_announcement_timer_complete(self, extra):
        """Post a scheduled announcement when its timer fires (from the Reminder
        cog's generic timer dispatch). The embed is rendered here, so its
        placeholders reflect the moment it posts."""
        try:
            guild = self.bot.get_guild(extra.get("guild_id"))
            if guild is None:
                return
            channel = guild.get_channel(extra.get("channel_id"))
            if channel is None or not channel.permissions_for(guild.me).send_messages:
                return
            substitute = build_substitution(guild, channel)
            embed = embed_creator.render(extra.get("embed") or {}, substitute=substitute)
            if not embed_creator.embed_has_content(embed):
                return
            content = None
            allowed = discord.AllowedMentions.none()
            rid = extra.get("role_id")
            role = guild.get_role(rid) if rid else None
            if role is not None:
                content = role.mention
                allowed = discord.AllowedMentions(roles=[role])
            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except Exception:
            log.exception("Scheduled announcement send failed")


async def setup(bot):
    await bot.add_cog(Announcements(bot))
