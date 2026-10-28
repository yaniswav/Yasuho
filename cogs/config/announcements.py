"""Interactive announcement builder.

`/announce` opens an author-restricted panel that reuses the shared
``tools.embed_creator`` editor to compose a rich embed, pick a target channel,
optionally ping a role, preview it, and send it. The draft lives only in the
open panel (in memory) - announcements are one-shot, so nothing is persisted.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import datetime
import json
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands

from cogs.community.announcements_store import (
    AnnouncementArchived,
    AnnouncementLimitReached,
    max_for_guild,
)
from tools import embed_creator, interactions, premium, premium_upsell
from tools.formats import random_colour
from tools.i18n import N_, _
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


# Kept under its old name: the builder preview, the legacy timer listener and
# the scheduled-announcement engine all resolve the same tokens.
build_substitution = embed_creator.server_substitution


async def _refuse_over_limit(interaction, bot, guild, text, *, responded):
    """An ephemeral limit refusal with the house upsell note (and slash button)."""
    upsell = await premium_upsell.for_guild_refusal(
        bot,
        limit_key="scheduled_announcements",
        guild_id=guild.id,
        person_id=interaction.user.id,
        is_admin=premium_upsell.invoker_is_admin(interaction.user),
        already_top_tier=premium_upsell.is_guild_already_top_tier(bot, guild.id),
        benefit=str(premium.GUILD_PREMIUM.max_scheduled_announcements),
    )
    kwargs = {"ephemeral": True}
    if upsell is not None:
        text += "\n" + upsell.line
        view = upsell.view()
        if view is not None:
            kwargs["view"] = view
    send = interaction.followup.send if responded else interaction.response.send_message
    await send(text, **kwargs)


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


REPEAT_CHOICES = (
    ("none", N_("Once")),
    ("daily", N_("Every day")),
    ("weekly", N_("Every week")),
    ("monthly", N_("Every month")),
)


class ScheduleModal(LocaleModal):
    """Ask when (and how often) to post the announcement, then hand it to the
    scheduled-announcement engine, which posts it exactly once per occurrence
    even across a restart."""

    def __init__(self, panel):
        super().__init__(title=_("Schedule the announcement"))
        self.panel = panel
        self.when_field = discord.ui.TextInput(
            label=_("When"),
            placeholder=_("e.g. 2h, tomorrow at 9am, or a <t:...> tag"),
            max_length=100,
            required=True,
        )
        self.repeat_select = discord.ui.Select(
            options=[
                discord.SelectOption(label=_(label), value=value, default=value == "none")
                for value, label in REPEAT_CHOICES
            ],
            min_values=1,
            max_values=1,
        )
        self.tz_field = discord.ui.TextInput(
            label=_("Time zone"),
            placeholder=_("e.g. Europe/Paris, America/New_York"),
            default="UTC",
            max_length=64,
            required=True,
        )
        self.add_item(self.when_field)
        self.add_item(discord.ui.Label(text=_("Repeat"), component=self.repeat_select))
        self.add_item(self.tz_field)

    async def on_submit(self, interaction):
        raw = (self.when_field.value or "").strip()
        tz_name = (self.tz_field.value or "").strip() or "UTC"
        try:
            tzinfo = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            return await interaction.response.send_message(
                _(
                    "I don't know the time zone `{tz}`. Use a name like "
                    "`Europe/Paris` or `UTC`."
                ).format(tz=discord.utils.escape_markdown(tz_name[:64])),
                ephemeral=True,
            )
        now = interaction.created_at.astimezone(tzinfo)
        # A pasted Discord timestamp token wins outright; otherwise fall back to
        # the ShortTime -> FutureTime natural-language parsing, in that zone.
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
        dt = dt.astimezone(tzinfo)
        # Instants, not wall times: two datetimes sharing a ZoneInfo compare
        # by wall clock, which is wrong inside a DST fold.
        if dt.astimezone(datetime.timezone.utc) <= interaction.created_at:
            return await interaction.response.send_message(
                _("That time is in the past. Give me a moment in the future."),
                ephemeral=True,
            )
        repeat = (self.repeat_select.values or ["none"])[0]
        await self.panel.schedule(interaction, dt, repeat, tz_name)


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

    async def schedule(self, interaction, when, repeat="none", tz_name="UTC"):
        channel = await self._validated(interaction)
        if channel is None:
            return
        engine = self.cog.bot.get_cog("ScheduledAnnouncements")
        if engine is None:
            return await interaction.response.send_message(
                _("Scheduling is unavailable right now."), ephemeral=True
            )
        # Acknowledge before the database write: a slow pool must not expire
        # the modal after the schedule committed (a retry would double it).
        await interactions.defer(interaction, ephemeral=True, surface="announce schedule")
        # The embed config is stored raw and rendered at fire time, so
        # {members} and friends reflect the moment it actually posts.
        try:
            await engine.store.create(
                self.guild.id,
                channel.id,
                interaction.user.id,
                local_time=when.replace(tzinfo=None),
                timezone=tz_name,
                recurrence=repeat,
                embed=self.draft.get("embed"),
                role_id=self.draft.get("role_id"),
            )
        except AnnouncementLimitReached:
            return await _refuse_over_limit(
                interaction, self.cog.bot, self.guild,
                _(
                    "This server has reached its limit of {limit} scheduled "
                    "announcements. Delete one with `/announcements` first."
                ).format(limit=max_for_guild(self.cog.bot, self.guild.id)),
                responded=True,
            )
        except ValueError:
            log.info("Announcement schedule refused", exc_info=True)
            return await interaction.followup.send(
                _("I couldn't schedule that: check the time and try again."),
                ephemeral=True,
            )
        lines = [
            _("Scheduled for {when} in {channel}.").format(
                when=discord.utils.format_dt(when, "F"), channel=channel.mention
            )
        ]
        if repeat != "none":
            lines.append(
                _("Repeats: {repeat}. Manage it with `/announcements`.").format(
                    repeat=_(dict(REPEAT_CHOICES)[repeat])
                )
            )
        role = self.guild.get_role(self.draft.get("role_id") or 0)
        if role is not None and not role.mentionable and not channel.permissions_for(
            self.guild.me
        ).mention_everyone:
            lines.append(
                _(
                    "{role} is not mentionable and I can't mention every role "
                    "here, so it won't be pinged."
                ).format(role=role.mention)
            )
        await interaction.followup.send(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
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


def _summary(row, archived=False):
    """One line per scheduled announcement for the manager panel."""
    embed = row["embed"]
    if isinstance(embed, str):
        embed = json.loads(embed)
    embed = embed if isinstance(embed, dict) else {}
    title = embed.get("title") or embed.get("description") or ""
    title = (title if isinstance(title, str) else "")[:40]
    repeat = _(dict(REPEAT_CHOICES).get(row["recurrence"], REPEAT_CHOICES[0][1]))
    if archived and row["enabled"]:
        when = _("archived, over the limit")
    elif row["enabled"] and row["next_run_at"] is not None:
        when = discord.utils.format_dt(row["next_run_at"], "R")
    elif row["failures"] >= 3:
        when = _("stopped after repeated failures")
    else:
        when = _("paused")
    return "`#{id}` <#{channel}> - {repeat}, {when}{title}".format(
        id=row["id"],
        channel=row["channel_id"],
        repeat=repeat,
        when=when,
        title=" - " + discord.utils.escape_markdown(title) if title else "",
    )


class _ScheduledSelect(discord.ui.Select):
    def __init__(self, panel, start=0):
        self._owner = panel
        options = [
            discord.SelectOption(
                label="#{0}".format(row["id"]),
                value=str(row["id"]),
                description=_summary_plain(row),
                default=row["id"] == panel.selected,
            )
            for row in panel.rows[start:start + 25]
        ]
        # A select holds 25 options and a server up to 50 schedules: a second
        # select carries the rest.
        super().__init__(
            placeholder=_("Pick a scheduled announcement..."),
            options=options,
            row=start // 25,
        )

    async def callback(self, interaction):
        self._owner.selected = int(self.values[0])
        await self._owner.rerender(interaction)


def _summary_plain(row):
    repeat = _(dict(REPEAT_CHOICES).get(row["recurrence"], REPEAT_CHOICES[0][1]))
    state = _("active") if row["enabled"] else _("paused")
    return "{repeat} - {state}".format(repeat=repeat, state=state)[:100]


class ScheduledPanel(AuthorView):
    """List this server's scheduled announcements; pause, resume or delete one."""

    def __init__(self, cog, guild, author_id, rows, selected=None):
        super().__init__(author_id, deny_message="This panel isn't for you.")
        self.cog = cog
        self.guild = guild
        self.rows = list(rows)
        self.selected = selected
        if self.rows:
            for start in range(0, min(len(self.rows), 50), 25):
                self.add_item(_ScheduledSelect(self, start))
            row = self._selected_row()
            toggle = discord.ui.Button(
                label=_("Resume") if row is not None and not row["enabled"] else _("Pause"),
                style=discord.ButtonStyle.secondary,
                disabled=row is None,
                row=2,
            )
            toggle.callback = self._toggle
            delete = discord.ui.Button(
                label=_("Delete"),
                style=discord.ButtonStyle.danger,
                disabled=row is None,
                row=2,
            )
            delete.callback = self._delete
            self.add_item(toggle)
            self.add_item(delete)

    def _selected_row(self):
        return next((r for r in self.rows if r["id"] == self.selected), None)

    def build_embed(self):
        embed = discord.Embed(title=_("Scheduled announcements"), colour=random_colour())
        if not self.rows:
            embed.description = _(
                "Nothing scheduled. Build one with `/announce`, then press **Schedule**."
            )
        else:
            # Rows past the limit (newest first) are archived: see the store.
            limit = max_for_guild(self.cog.bot, self.guild.id)
            text = "\n".join(
                _summary(r, archived=i >= limit) for i, r in enumerate(self.rows[:50])
            )
            embed.description = text if len(text) <= 4000 else text[:3997] + "..."
        return embed

    async def interaction_check(self, interaction):
        # Authority is re-checked on every click, not only when /announcements
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

    @property
    def store(self):
        engine = self.cog.bot.get_cog("ScheduledAnnouncements")
        return engine.store if engine is not None else None

    async def rerender(self, interaction):
        store = self.store
        rows = await store.list_for_guild(self.guild.id) if store is not None else []
        new = ScheduledPanel(self.cog, self.guild, self.author_id, rows, self.selected)
        new.message = self.message
        self.stop()
        await embed_creator.refresh_in_place(
            interaction, self.message, embed=new.build_embed(), view=new
        )

    async def _toggle(self, interaction):
        row, store = self._selected_row(), self.store
        if row is None or store is None:
            return await self.rerender(interaction)
        try:
            await store.set_enabled(self.guild.id, row["id"], not row["enabled"])
        except AnnouncementArchived:
            return await _refuse_over_limit(
                interaction, self.cog.bot, self.guild,
                _(
                    "This server is over its limit of {limit} scheduled "
                    "announcements, so this one is archived. Delete another to "
                    "free a slot."
                ).format(limit=max_for_guild(self.cog.bot, self.guild.id)),
                responded=False,
            )
        except ValueError:
            return await interaction.response.send_message(
                _(
                    "That one-time announcement is already past. Delete it and "
                    "schedule a new one."
                ),
                ephemeral=True,
            )
        await self.rerender(interaction)

    async def _delete(self, interaction):
        row, store = self._selected_row(), self.store
        if row is not None and store is not None:
            await store.delete(self.guild.id, row["id"])
            self.selected = None
        await self.rerender(interaction)


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

    @commands.hybrid_command(name="announcements")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def announcements(self, ctx):
        """List, pause, resume or delete this server's scheduled announcements."""
        engine = self.bot.get_cog("ScheduledAnnouncements")
        if engine is None:
            return await ctx.send(_("Scheduling is unavailable right now."))
        rows = await engine.store.list_for_guild(ctx.guild.id)
        view = ScheduledPanel(self, ctx.guild, ctx.author.id, rows)
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
