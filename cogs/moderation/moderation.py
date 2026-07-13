import logging

import discord
from discord.ext import commands
from discord.ext.commands import MemberConverter

from tools import db, modactions, modchecks
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.i18n import _
from tools.interactions import notify_failure
from tools.paginator import Paginator, paginate_lines
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

E_VERIF = config_loader.getstr("Emojis", "verif")


def trim_reason(reason):
    """Truncate a moderation reason to 100 characters, appending an ellipsis when clipped."""
    return reason if len(reason) <= 100 else f"{reason[:100]}..."


class _MentionFallback:
    """Minimal stand-in for a user who is no longer reachable.

    Lets ``modactions.case_embed`` render a bare mention + id for someone who
    has left the guild, exactly as the old hand-rolled embed did. It exposes
    only ``id`` and ``mention`` (and deliberately no ``display_avatar``), so the
    embed simply omits the thumbnail.
    """

    __slots__ = ("id",)

    def __init__(self, user_id):
        self.id = user_id

    @property
    def mention(self):
        return f"<@{self.id}>"


class ConfirmView(AuthorView):
    """Author-restricted Confirm/Cancel prompt for dangerous moderation actions.

    The invoker presses Confirm or Cancel; the caller waits on the view and reads
    ``self.value`` (``True`` confirmed, ``False``/``None`` aborted).
    """

    def __init__(self, author_id, *, timeout=30):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.value = None

    async def _finish(self, interaction, value):
        self.value = value
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            log.exception("Confirm view failed to update")
        finally:
            self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await self._finish(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await self._finish(interaction, False)


class WarningsView(AuthorView):
    """Author-restricted, paginated list of a member's warn-cases.

    A dropdown selects a warn on the current page and the danger button removes
    it (deletes the case row and decrements the member's ``warns_count``).
    """

    def __init__(self, cog, guild, member, warns, author_id, *, per_page=10, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog
        self.guild = guild
        self.member = member
        self.warns = list(warns)  # asyncpg Records, newest first
        self.per_page = per_page
        self.index = 0
        self.selected = None

        self.select = discord.ui.Select(
            placeholder=_("Select a warn to remove..."), row=0
        )
        self.select.callback = self._on_select
        self.add_item(self.select)
        self._rebuild()

    @property
    def page_count(self):
        if not self.warns:
            return 1
        return (len(self.warns) + self.per_page - 1) // self.per_page

    def _page_slice(self):
        start = self.index * self.per_page
        return self.warns[start : start + self.per_page]

    def _mod_text(self, moderator_id):
        mod = self.guild.get_member(moderator_id)
        return mod.mention if mod else f"<@{moderator_id}>"

    def embed(self):
        embed = discord.Embed(
            title=_("Warnings - {member}").format(member=self.member),
            colour=modactions.action_colour("warn"),
        )
        embed.set_thumbnail(url=self.member.display_avatar.url)

        page = self._page_slice()
        if not page:
            embed.description = _("No warnings on record.")
        else:
            lines = []
            for warn in page:
                reason = warn["reason"] or _("*No reason provided*")
                when = discord.utils.format_dt(warn["created_at"], "R")
                lines.append(
                    _("**Case #{case}** - {reason}\nby {mod} - {when}").format(
                        case=warn["case_number"],
                        reason=reason,
                        mod=self._mod_text(warn["moderator_id"]),
                        when=when,
                    )
                )
            embed.description = "\n\n".join(lines)

        embed.set_footer(
            text=_("Page {current}/{total} - {count} warn(s)").format(
                current=self.index + 1,
                total=self.page_count,
                count=len(self.warns),
            )
        )
        return embed

    def _rebuild(self):
        """Refresh the select options and button states for the current page."""
        page = self._page_slice()
        options = []
        for warn in page:
            reason = warn["reason"] or _("No reason")
            options.append(
                discord.SelectOption(
                    label=_("Case #{case}").format(case=warn["case_number"]),
                    description=reason[:100],
                    value=str(warn["case_number"]),
                )
            )

        if options:
            self.select.options = options
            self.select.disabled = False
        else:
            self.select.options = [
                discord.SelectOption(label=_("No warnings"), value="none")
            ]
            self.select.disabled = True

        self.selected = None
        self.remove_warn.disabled = True
        self.prev_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= self.page_count - 1

    async def _on_select(self, interaction):
        try:
            self.selected = int(self.select.values[0])
            self.remove_warn.disabled = False
            for option in self.select.options:
                option.default = option.value == self.select.values[0]
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("Warnings select failed")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        _("Couldn't select that warn, please try again."),
                        ephemeral=True,
                    )
                except Exception:
                    log.exception("Warnings select failed")

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page(self, interaction, button):
        await self._turn(interaction, self.index - 1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction, button):
        await self._turn(interaction, self.index + 1)

    async def _turn(self, interaction, index):
        try:
            self.index = max(0, min(index, self.page_count - 1))
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        except Exception:
            log.exception("Warnings pagination failed")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        _("Couldn't turn the page, please try again."), ephemeral=True
                    )
                except Exception:
                    log.exception("Warnings pagination failed")

    @discord.ui.button(label="Remove warn", style=discord.ButtonStyle.danger, row=1)
    async def remove_warn(self, interaction, button):
        if self.selected is None:
            return await interaction.response.send_message(
                _("Pick a warn from the dropdown first."), ephemeral=True
            )

        try:
            await self.cog.remove_warn_case(
                self.guild.id, self.member.id, self.selected
            )

            removed = self.selected
            self.warns = [
                w for w in self.warns if w["case_number"] != removed
            ]
            if self.index >= self.page_count:
                self.index = self.page_count - 1
            self._rebuild()
            await interaction.response.edit_message(
                embed=self.embed(), view=self
            )
        except Exception:
            log.exception("Failed to remove warn case")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        _("Couldn't remove that warn, please try again."),
                        ephemeral=True,
                    )
                except Exception:
                    log.exception("Failed to remove warn case")


class ReasonEditModal(LocaleModal):
    """Edit a case's reason via a paragraph input prefilled with its current value.

    Opened from the interactive (slash) ``reason`` path once the case row has
    been resolved. On submit it reuses the cog's UPDATE + case-embed + mod-log
    plumbing so both the text and interactive paths persist identically.
    """

    def __init__(self, cog, guild, row):
        super().__init__(
            title=_("Edit reason - case #{number}").format(
                number=row["case_number"]
            )[:45]
        )
        self.cog = cog
        self.guild = guild
        self.row = row

        self.reason_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
            default=row["reason"] or "",
        )
        self.add_item(
            discord.ui.Label(
                text=_("New reason"), component=self.reason_input
            )
        )

    async def on_submit(self, interaction):
        new_reason = (self.reason_input.value or "").strip()
        if not new_reason:
            return await notify_failure(
                interaction, _("The reason can't be empty.")
            )

        try:
            row = await self.cog.bot.db_pool.fetchrow(
                "UPDATE cases SET reason = $3 "
                "WHERE guild_id = $1 AND case_number = $2 RETURNING *;",
                self.guild.id,
                self.row["case_number"],
                new_reason,
            )
            if row is None:
                return await notify_failure(
                    interaction,
                    _("No case #{number} found in this server.").format(
                        number=self.row["case_number"]
                    ),
                )

            embed = self.cog._case_record_embed(self.guild, row)
            embed.add_field(
                name=_("Updated by"),
                value=interaction.user.mention,
                inline=False,
            )
            await interaction.response.send_message(embed=embed)
            await self.cog._post_modlog(self.guild, embed)
        except Exception:
            log.exception("Failed to edit case reason via modal")
            await notify_failure(
                interaction, _("Sorry, I couldn't update that reason.")
            )


class NewUsersView(discord.ui.LayoutView):
    """Newest members rendered as a Components V2 layout.

    A single container holds one Section per member (their avatar as a Thumbnail
    accessory beside a TextDisplay of join/create relative timestamps), with
    Separators between. The member list is capped so the component budget holds.
    """

    MAX_MEMBERS = 8

    def __init__(self, members, *, timeout=180):
        super().__init__(timeout=timeout)

        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay(_("## New Members")))

        for member in members[: self.MAX_MEMBERS]:
            container.add_item(discord.ui.Separator())
            body = _(
                "**{member}** (ID: {id})\njoined {joined}, created {created}"
            ).format(
                member=member.mention,
                id=member.id,
                joined=discord.utils.format_dt(member.joined_at, "R"),
                created=discord.utils.format_dt(member.created_at, "R"),
            )
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(body),
                    accessory=discord.ui.Thumbnail(member.display_avatar.url),
                )
            )

        self.add_item(container)


class Moderation(commands.Cog):
    """Ultracool moderator commands"""

    def __init__(self, bot):
        self.bot = bot
        self.units = {
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2592000,
        }

    async def _post_modlog(self, guild, embed):
        """Funnel a mod-action embed to the guild's configured mod-log channel."""
        await modactions.funnel_action(self.bot, guild, embed)

    async def _no_perms(self, ctx):
        """Standard 'missing permissions' notice, auto-deleted after 10s."""
        await ctx.send(
            _("**:x: Sorry, I am missing permissions to do this!**"), delete_after=10
        )

    async def _edit_confirm(self, ctx, **kwargs):
        """Edit the stored confirm-prompt message in place, ignoring HTTP errors."""
        try:
            await ctx.confirm_message.edit(**kwargs)
        except discord.HTTPException:
            pass

    async def _confirm(self, ctx, embed, *, timeout=30):
        """Send a danger-action confirm prompt and wait for the author's choice.

        Builds a ConfirmView locked to the invoker, sends ``embed`` with it, then
        waits and returns True only if Confirm was pressed. The prompt message is
        stored on ``ctx`` as ``confirm_message`` so callers can edit it in place
        to show the cancelled/result embed exactly as before.
        """
        view = ConfirmView(ctx.author.id, timeout=timeout)
        view.message = await ctx.send(embed=embed, view=view)
        ctx.confirm_message = view.message
        await view.wait()
        return bool(view.value)

    async def _get_mute_role_id(self, guild_id):
        """Return the guild's mute-role id from the bot cache, DB-filling on a miss."""
        role_id = self.bot.muteroles.get(guild_id)
        if role_id is not None:
            return role_id
        role_id = await self.bot.db_pool.fetchval(
            "SELECT role_id FROM muterole WHERE guild_id = $1;", guild_id
        )
        if role_id is not None:
            self.bot.muteroles[guild_id] = role_id
        return role_id

    async def remove_warn_case(self, guild_id, user_id, case_number):
        """Delete a member's warn-case row and clamp their warns_count at 0.

        Owns the persistence the warnings UI used to run inline: removes the
        single ``warn`` case and decrements the running ``warns_count`` (floored
        at 0 via ``GREATEST(..., 0)``).
        """
        await self.bot.db_pool.execute(
            "DELETE FROM cases WHERE guild_id = $1 AND user_id = $2 "
            "AND action = 'warn' AND case_number = $3;",
            guild_id,
            user_id,
            case_number,
        )
        await self.bot.db_pool.execute(
            "UPDATE warns SET warns_count = GREATEST(warns_count - 1, 0) "
            "WHERE guild_id = $1 AND user_id = $2;",
            guild_id,
            user_id,
        )

    def _case_record_embed(self, guild, row):
        """Render a stored case row (DB record) as a consistent case embed.

        Thin wrapper over ``modactions.case_embed``: resolves the
        target/moderator from cache when possible and degrades to a bare-mention
        shim when they are no longer reachable (e.g. the user left the guild),
        then restamps the embed with the case's stored creation time.
        """
        target = (
            guild.get_member(row["user_id"])
            or self.bot.get_user(row["user_id"])
            or _MentionFallback(row["user_id"])
        )
        moderator = (
            guild.get_member(row["moderator_id"])
            or self.bot.get_user(row["moderator_id"])
            or _MentionFallback(row["moderator_id"])
        )

        embed = modactions.case_embed(
            row["case_number"],
            row["action"],
            target,
            moderator,
            row["reason"],
            row["expires"],
        )
        # case_embed stamps "now" for live actions; a stored record must reflect
        # when the case was actually created.
        embed.timestamp = row["created_at"]
        return embed

    @commands.hybrid_command(aliases=["newmembers"])
    @commands.guild_only()
    @discord.app_commands.describe(count="How many members to show (max 25, default 5).")
    async def newusers(self, ctx, *, count=5):
        """Show the newest members of the server.
        This is useful to check if any suspicious members have
        joined.
        The count parameter can only be up to 25.
        """
        try:
            count = max(min(count, 25), 5)

            async with ctx.typing():
                if not ctx.guild.chunked:
                    await self.bot.request_offline_members(ctx.guild)

                members = sorted(
                    ctx.guild.members, key=lambda m: m.joined_at, reverse=True
                )[:count]

                # A LayoutView carries its own content, so it is sent with no
                # embed and no content. It renders member mentions via
                # TextDisplay, so suppress pings on send.
                view = NewUsersView(members)
                await ctx.send(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        except Exception:
            log.exception("Failed to send new members view")

    @commands.hybrid_command(name="kick", aliases=["k"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(target="The member to kick.", reason="Why they're being kicked.")
    async def _kick(self, ctx, target: discord.User, *, reason: str = None):
        """Kick a member from the server."""

        if reason is None:
            reason = "No reason specified"

        err = modchecks.hierarchy_error(ctx, target)
        if err:
            return await ctx.send(err, delete_after=10)

        # Suppress the ModLog leave listener so this bot kick is logged once
        # (the case embed below), not twice.
        modactions.funnel_suppress(self.bot, ctx.guild.id, target.id, "remove")

        try:
            await ctx.guild.kick(
                target,
                reason=f"{ctx.author}: {trim_reason(reason)}",
            )
        except Exception:
            log.exception("Failed to kick member")
            return await self._no_perms(ctx)

        num = await modactions.create_case(
            self.bot.db_pool,
            ctx.guild.id,
            target.id,
            ctx.author.id,
            "kick",
            reason,
        )
        embed = modactions.case_embed(num, "kick", target, ctx.author, reason)
        await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.hybrid_command(name="voicekick", aliases=["vkick", "voicek"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(
        user="The member to disconnect from voice.", reason="Why they're being removed."
    )
    async def _voicekick(self, ctx, user: discord.Member, *, reason: str = None):
        """Disconnect a member from their voice channel."""

        if reason is None:
            reason = "No reason specified"

        err = modchecks.hierarchy_error(ctx, user)
        if err:
            return await ctx.send(err, delete_after=10)

        embedkick = discord.Embed(
            color=random_colour(),
            timestamp=ctx.message.created_at,
            title=_("Kick | {mod} has kicked {target}").format(
                mod=ctx.author.name, target=user.name
            ),
        )
        embedkick.set_thumbnail(url=user.display_avatar.url)
        embedkick.add_field(
            name=_("**🔴 Voice Kick Info**"),
            value=_(
                "Moderator: **{mod}**\nReason: **{reason}**\nTime: **{time}**"
            ).format(
                mod=ctx.author.mention,
                reason=trim_reason(reason),
                time=ctx.message.created_at,
            ),
        )
        embedkick.set_footer(text=ctx.guild, icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

        try:
            await user.move_to(
                None,
                reason=f"{ctx.author}: {trim_reason(reason)}",
            )
            await ctx.send(embed=embedkick)
        except Exception:
            log.exception("Failed to voice kick member")
            await self._no_perms(ctx)

    @commands.hybrid_command(name="move")
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(
        user="The member to move.", room="The name of the voice channel to move them to."
    )
    async def _move(self, ctx, user: discord.Member, room: str):
        """Move a member to a different voice channel."""

        channel = discord.utils.get(ctx.guild.voice_channels, name=room)
        try:
            await user.move_to(channel, reason=None)
            await ctx.send(
                _("{user} has been moved to {channel}").format(
                    user=user.name, channel=channel
                )
            )
        except Exception:
            await self._no_perms(ctx)
            log.exception("Failed to move member to channel")

    @commands.hybrid_command(name="ban", aliases=["b"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(ban_members=True)
    @discord.app_commands.describe(target="The member to ban.", reason="Why they're being banned.")
    async def _ban(self, ctx, target: discord.User, *, reason: str = None):
        """Ban a member from the server."""

        if reason is None:
            reason = "No reason specified"

        err = modchecks.hierarchy_error(ctx, target)
        if err:
            return await ctx.send(err, delete_after=10)

        confirm = discord.Embed(
            title=_("Confirm ban"),
            description=_("Are you sure you want to ban {target} (`{id}`)?").format(
                target=target.mention, id=target.id
            ),
            colour=modactions.action_colour("ban"),
        )
        confirm.add_field(name=_("Reason"), value=trim_reason(reason), inline=False)
        confirm.set_thumbnail(url=target.display_avatar.url)

        if not await self._confirm(ctx, confirm):
            aborted = discord.Embed(
                title=_("Ban cancelled"),
                description=_("No action taken against {target}.").format(
                    target=target.mention
                ),
                colour=modactions.action_colour("note"),
            )
            await self._edit_confirm(ctx, embed=aborted, view=None)
            return

        # Suppress the ModLog ban listener so this bot ban is logged once
        # (the case embed below), not twice.
        modactions.funnel_suppress(self.bot, ctx.guild.id, target.id, "ban")

        try:
            await ctx.guild.ban(
                target,
                reason=f"{ctx.author}: {trim_reason(reason)}",
            )
        except Exception:
            log.exception("Failed to ban member")
            return await self._no_perms(ctx)

        num = await modactions.create_case(
            self.bot.db_pool,
            ctx.guild.id,
            target.id,
            ctx.author.id,
            "ban",
            reason,
        )
        embed = modactions.case_embed(num, "ban", target, ctx.author, reason)
        try:
            await ctx.confirm_message.edit(embed=embed, view=None)
        except discord.HTTPException:
            await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.hybrid_command(name="unban", aliases=["ub"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(ban_members=True)
    @discord.app_commands.describe(target="The user to unban.", reason="Why they're being unbanned.")
    async def _unban(self, ctx, target: discord.User, *, reason: str = None):
        """Unban a user from the server."""

        if reason is None:
            reason = "No reason specified"

        # Suppress the ModLog unban listener so this bot unban is logged once
        # (the case embed below), not twice.
        modactions.funnel_suppress(self.bot, ctx.guild.id, target.id, "unban")

        try:
            await ctx.guild.unban(
                target,
                reason=f"{ctx.author}: {trim_reason(reason)}",
            )
        except Exception:
            log.exception("Failed to unban member")
            return await self._no_perms(ctx)

        num = await modactions.create_case(
            self.bot.db_pool,
            ctx.guild.id,
            target.id,
            ctx.author.id,
            "unban",
            reason,
        )
        embed = modactions.case_embed(num, "unban", target, ctx.author, reason)
        await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.command(name="massban", aliases=["bulkban", "hackban"])
    @commands.guild_only()
    @commands.cooldown(1.0, 10.0, commands.BucketType.guild)
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def massban(self, ctx, users: commands.Greedy[discord.Object], *, reason: str = None):
        """Ban many users at once by id, for raid cleanup.

        Usage: massban <id1> <id2> <id3> ... [reason]
        Bans up to 200 ids in one go and does NOT delete their messages.
        Requires the Ban Members permission.
        """
        if not users:
            return await ctx.send(
                _(
                    "Give me at least one user id to ban.\n"
                    "Usage: `massban <id1> <id2> ... [reason]`"
                )
            )
        if len(users) > 200:
            return await ctx.send(_("I can ban at most 200 users in one go."))

        if reason is None:
            reason = "No reason specified"

        confirm = discord.Embed(
            title=_("Confirm mass ban"),
            description=_(
                "Are you sure you want to ban **{count}** user(s) by id?"
            ).format(count=len(users)),
            colour=modactions.action_colour("ban"),
        )
        confirm.add_field(name=_("Reason"), value=trim_reason(reason), inline=False)
        if not await self._confirm(ctx, confirm):
            aborted = discord.Embed(
                title=_("Mass ban cancelled"),
                description=_("No action taken."),
                colour=modactions.action_colour("note"),
            )
            await self._edit_confirm(ctx, embed=aborted, view=None)
            return

        # Log each ban once (the summary below), not twice via the ModLog listener.
        for obj in users:
            modactions.funnel_suppress(self.bot, ctx.guild.id, obj.id, "ban")

        try:
            result = await ctx.guild.bulk_ban(
                users,
                reason=f"{ctx.author}: {trim_reason(reason)}",
                delete_message_seconds=0,
            )
        except Exception:
            log.exception("Failed to bulk ban")
            return await ctx.send(
                _("**:x: Sorry, I could not ban those users (missing permissions?).**"),
                delete_after=10,
            )

        for obj in result.banned:
            try:
                await modactions.create_case(
                    self.bot.db_pool,
                    ctx.guild.id,
                    obj.id,
                    ctx.author.id,
                    "ban",
                    reason,
                )
            except Exception:
                log.exception("Failed to record mass-ban case for %s", obj.id)

        embed = discord.Embed(
            title=_("Mass ban complete"),
            colour=modactions.action_colour("ban"),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name=_("Banned"), value=str(len(result.banned)))
        embed.add_field(name=_("Failed"), value=str(len(result.failed)))
        embed.add_field(name=_("Reason"), value=trim_reason(reason), inline=False)
        embed.set_footer(
            text=_("By {author}").format(author=ctx.author),
            icon_url=ctx.author.display_avatar.url,
        )
        try:
            await ctx.confirm_message.edit(embed=embed, view=None)
        except discord.HTTPException:
            await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.hybrid_command(
        name="purge", aliases=["pg", "massclean", "massdelete", "prune"]
    )
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    @discord.app_commands.describe(count="How many messages to delete (1 to 999).")
    async def _purge(self, ctx, count: int):
        """Delete a number of recent messages in this channel."""

        if ctx.interaction:
            await ctx.interaction.response.defer()

        if count > 999 or count < 1:
            return await ctx.send(
                _(":warning: | **Count can't be lesser than 0 and greater than 999**"),
                delete_after=3,
            )

        else:
            try:
                await ctx.channel.purge(limit=count + 1)
            except Exception:
                log.exception("Failed to purge messages")
                return await ctx.send(
                    _("**:x: Sorry, I am missing permissions to do this**"),
                    delete_after=5,
                )

        return await ctx.send(
            _("{emoji} **Deleted successfully!**").format(emoji=E_VERIF),
            delete_after=3,
        )

    @commands.hybrid_command(description="Delete a number of a member's recent messages.")
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    @discord.app_commands.describe(
        num="How many of the member's messages to delete (max 500).",
        target="The member whose messages to delete.",
    )
    async def clean(self, ctx, num: int, target: discord.Member):
        """Delete a number of a member's recent messages."""

        if num > 500 or num < 0:
            return await ctx.send(_("Invalid amount. Maximum is 500."))

        def msgcheck(amsg):
            if target:
                return amsg.author.id == target.id
            return True

        if ctx.interaction:
            await ctx.interaction.response.defer()

        deleted = await ctx.channel.purge(limit=num, check=msgcheck)
        await ctx.send(
            _("{emoji} Deleted **{deleted}/{num}** possible messages for you.").format(
                emoji=E_VERIF, deleted=len(deleted), num=num
            ),
            delete_after=3,
        )

    async def _ensure_mute_role(self, guild):
        """Create the guild's "Muted" role, persist it, and return it.

        Provisions the role with deny overwrites across every text channel,
        voice channel and category, persists the role id via the muterole
        upsert, and primes the in-memory ``bot.muteroles`` cache so subsequent
        mutes resolve it without recreating it.
        """
        perms = discord.Permissions(
            send_messages=False,
            add_reactions=False,
            send_tts_messages=False,
            speak=False,
        )
        mrole = await guild.create_role(name="Muted", permissions=perms)
        await db.upsert_guild_value(
            self.bot.db_pool, "muterole", "role_id", guild.id, mrole.id
        )
        self.bot.muteroles[guild.id] = mrole.id

        for channel in guild.text_channels:
            await channel.set_permissions(
                mrole,
                overwrite=discord.PermissionOverwrite(
                    send_messages=False,
                    add_reactions=False,
                    send_tts_messages=False,
                ),
            )
        for channel in guild.voice_channels:
            await channel.set_permissions(
                mrole, overwrite=discord.PermissionOverwrite(speak=False)
            )
        for channel in guild.categories:
            await channel.set_permissions(
                mrole,
                overwrite=discord.PermissionOverwrite(
                    send_messages=False,
                    add_reactions=False,
                    send_tts_messages=False,
                    speak=False,
                ),
            )
        return mrole

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(user="The member to mute.", reason="Why they're being muted.")
    async def mute(self, ctx, user: discord.Member, *, reason: str = None):
        """Mute a member."""

        if reason is None:
            reason = "No reason specified"

        err = modchecks.hierarchy_error(ctx, user)
        if err:
            return await ctx.send(err, delete_after=10)

        role_id = await self._get_mute_role_id(ctx.guild.id)

        try:
            if role_id is None:
                await ctx.send(_("Mute role is not defined"), delete_after=3)
                await ctx.send(_("Creating role..."), delete_after=1)
                mutedrole = await self._ensure_mute_role(ctx.guild)
                await ctx.send(content=_("Mute role created!"), delete_after=5)
            else:
                mutedrole = discord.utils.get(ctx.guild.roles, id=role_id)

            await user.add_roles(
                mutedrole, reason=f"""Muted By: {ctx.author} for: {reason} """
            )

            query = (
                "INSERT INTO mutedmembers (mguild_id, member_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING"
            )
            await self.bot.db_pool.execute(query, ctx.guild.id, user.id)

            num = await modactions.create_case(
                self.bot.db_pool,
                ctx.guild.id,
                user.id,
                ctx.author.id,
                "mute",
                reason,
            )
            embed = modactions.case_embed(num, "mute", user, ctx.author, reason)
            await ctx.send(embed=embed)
            await self._post_modlog(ctx.guild, embed)

        except Exception:
            # "Already muted" is the only benign outcome here; treat anything
            # else as a real failure (missing permissions, DB error, deleted
            # role) and log it instead of mislabelling it as "already muted".
            mute_role_id = self.bot.muteroles.get(ctx.guild.id)
            mute_role = (
                discord.utils.get(ctx.guild.roles, id=mute_role_id)
                if mute_role_id is not None
                else None
            )
            if mute_role is not None and mute_role in user.roles:
                embed = discord.Embed(
                    title=_("Already Muted"),
                    colour=random_colour(),
                    description=_(":red_circle: {user} is already muted!").format(
                        user=user
                    ),
                    timestamp=discord.utils.utcnow(),
                )
                await ctx.send(embed=embed)
                return

            log.exception("Failed to mute member")
            embed = discord.Embed(
                title=_("Mute failed"),
                colour=random_colour(),
                description=_(
                    ":red_circle: Could not mute {user}, please try again."
                ).format(user=user),
                timestamp=discord.utils.utcnow(),
            )
            await ctx.send(embed=embed)
            return

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(user="The member to unmute.")
    async def unmute(self, ctx, user: discord.Member):
        """Unmute a member."""

        role = await self._get_mute_role_id(ctx.guild.id)

        try:
            mutedrole = discord.utils.get(ctx.guild.roles, id=role)
            await user.remove_roles(mutedrole, reason=f"""Unmuted by {ctx.author}""")

            query = (
                """DELETE FROM mutedmembers WHERE mguild_id = $1 AND member_id = $2;"""
            )
            await self.bot.db_pool.execute(query, ctx.guild.id, user.id)

            num = await modactions.create_case(
                self.bot.db_pool,
                ctx.guild.id,
                user.id,
                ctx.author.id,
                "unmute",
                None,
            )
            embed = modactions.case_embed(num, "unmute", user, ctx.author, None)
            await ctx.send(embed=embed)
            await self._post_modlog(ctx.guild, embed)

        except Exception:
            log.exception("Failed to unmute member")
            embed = discord.Embed(
                title=_("Unmute failed"),
                colour=random_colour(),
                description=_(
                    ":red_circle: Could not unmute {user}, please try again."
                ).format(user=user),
                timestamp=discord.utils.utcnow(),
            )
            await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        member="The member to give the role to (or -all for everyone).",
        role="The role to give.",
    )
    async def addrole(self, ctx, member, role: discord.Role):
        """Give a role to a member."""

        if member == "-all":
            confirm = discord.Embed(
                title=_("Confirm mass role add"),
                description=_(
                    "Add the **{role}** role to **all** members of this "
                    "server? This can take a while."
                ).format(role=role.name),
                colour=modactions.action_colour("note"),
            )
            if not await self._confirm(ctx, confirm):
                await self._edit_confirm(
                    ctx, content=_("Cancelled."), embed=None, view=None
                )
                return

            # Chunk first so we see every member (guilds are not chunked at
            # startup), and never let one failure abort the whole sweep.
            if not ctx.guild.chunked:
                await ctx.guild.chunk()
            done, failed = 0, 0
            async with ctx.typing():
                for m in ctx.guild.members:
                    if role in m.roles:
                        continue
                    try:
                        await m.add_roles(
                            role, reason=f"addrole -all by {ctx.author}"
                        )
                        done += 1
                    except discord.HTTPException:
                        failed += 1

            return await ctx.send(
                _("Added **{role}** to {done} member(s) ({failed} failed).").format(
                    role=role.name, done=done, failed=failed
                )
            )

        converter = MemberConverter()
        m = await converter.convert(ctx, member)
        await m.add_roles(role)
        return await ctx.send(
            _("{emoji} **`{role}`** role has been added to **{member}**").format(
                emoji=E_VERIF, role=role.name, member=m.name
            )
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        member="The member to remove the role from (or -all for everyone).",
        role="The role to remove.",
    )
    async def removerole(self, ctx, member, role: discord.Role):
        """Remove a role from a member."""

        if member == "-all":
            confirm = discord.Embed(
                title=_("Confirm mass role remove"),
                description=_(
                    "Remove the **{role}** role from **all** members of "
                    "this server? This can take a while."
                ).format(role=role.name),
                colour=modactions.action_colour("note"),
            )
            if not await self._confirm(ctx, confirm):
                await self._edit_confirm(
                    ctx, content=_("Cancelled."), embed=None, view=None
                )
                return

            # Chunk first so we see every member (guilds are not chunked at
            # startup), and never let one failure abort the whole sweep.
            if not ctx.guild.chunked:
                await ctx.guild.chunk()
            done, failed = 0, 0
            async with ctx.typing():
                for m in ctx.guild.members:
                    if role not in m.roles:
                        continue
                    try:
                        await m.remove_roles(
                            role, reason=f"removerole -all by {ctx.author}"
                        )
                        done += 1
                    except discord.HTTPException:
                        failed += 1

            return await ctx.send(
                _("Removed **{role}** from {done} member(s) ({failed} failed).").format(
                    role=role.name, done=done, failed=failed
                )
            )

        converter = MemberConverter()
        m = await converter.convert(ctx, member)
        await m.remove_roles(role)
        return await ctx.send(
            _("{emoji} **`{role}`** role has been removed to **{member}**").format(
                emoji=E_VERIF, role=role.name, member=m.name
            )
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        role="The role to move.", pos="The position to move it to in the role hierarchy."
    )
    async def moverole(self, ctx, role: discord.Role, pos: int):
        """Move a role to the given position in the hierarchy."""

        try:
            await role.edit(position=pos)
            await ctx.send(_("{role} moved.").format(role=role))
        except discord.Forbidden:
            await ctx.send(_("You do not have permission to do that"))
        except discord.HTTPException:
            await ctx.send(_("Failed to move role"))
        except (TypeError, ValueError):
            await ctx.send(_("Invalid argument"))

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(member="The member to check.")
    async def warninfo(self, ctx, member: discord.Member = None):
        """Show how many warns a member currently has."""

        if member is None:
            return await ctx.send_help(ctx.command)

        query = """

        SELECT warns_count FROM warns
        WHERE guild_id = $1 AND user_id = $2;

        """

        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id)

        if not fetch:
            return await ctx.send(
                _("{member} has no warns.").format(member=member.mention)
            )

        await ctx.send(
            _("{member} has {count} warn(s)").format(
                member=member.mention, count=fetch
            )
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(member="The member to warn.", reason="Why they're being warned.")
    async def warn(self, ctx, member: discord.Member = None, *, reason: str = None):
        """Warn a member (auto-kicks at 3 warns)."""

        if member is None:
            return await ctx.send_help(ctx.command)

        # Every warn is recorded as its own case for history/auditing, while the
        # warns_count row (bumped here) stays the source of truth for the
        # 3-strike auto-kick threshold. bump_warn is shared with AutoMod.
        num = await modactions.create_case(
            self.bot.db_pool,
            ctx.guild.id,
            member.id,
            ctx.author.id,
            "warn",
            reason,
        )
        new_count = await modactions.bump_warn(
            self.bot.db_pool, ctx.guild.id, member.id
        )

        if new_count >= 3:
            embed = modactions.case_embed(num, "warn", member, ctx.author, reason)
            embed.add_field(
                name=_("Auto-action"),
                value=_("Reached 3 warns - kicked"),
                inline=False,
            )

            # Suppress the ModLog leave listener so this auto-kick is logged once
            # (the case embed above), not twice.
            modactions.funnel_suppress(self.bot, ctx.guild.id, member.id, "remove")

            try:
                await member.kick(reason="Auto-kick: reached 3 warns")
            except Exception:
                log.exception("Failed to kick member at 3 warns")
                await ctx.send(embed=embed)
                await self._post_modlog(ctx.guild, embed)
                return await ctx.send(
                    _(
                        "{member} has 3 warns but I don't have permissions "
                        "to kick them from the guild."
                    ).format(member=member.mention)
                )

            try:
                await member.send(_("You have been kicked from the server!"))
            except Exception:
                log.exception("Failed to DM kicked member")

            await ctx.send(embed=embed)
            await self._post_modlog(ctx.guild, embed)
            return

        embed = modactions.case_embed(num, "warn", member, ctx.author, reason)
        embed.add_field(name=_("Warns"), value=f"{new_count}/3", inline=False)
        await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.hybrid_command(aliases=["rmwarn", "removewarn"])
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(
        member="The member to remove warns from.", num="How many warns to remove (default 1)."
    )
    async def delwarn(self, ctx, member: discord.Member = None, num: int = 1):
        """Remove a warn from a member."""

        if member is None:
            return await ctx.send_help(ctx.command)

        query = (
            """SELECT warns_count FROM warns WHERE guild_id = $1 AND user_id = $2;"""
        )
        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id)

        if not fetch:
            return await ctx.send(
                _("{member} has no warns!").format(member=member.mention)
            )

        if fetch - num < 0:
            query = """ UPDATE warns SET warns_count = 0 WHERE guild_id = $1 AND user_id = $2;"""
            await self.bot.db_pool.execute(query, ctx.guild.id, member.id)
            return await ctx.send(
                _("Removed all warns for {member}.").format(member=member.mention)
            )

        query = """UPDATE warns SET warns_count = warns_count - $3 WHERE guild_id = $1 AND user_id = $2;"""
        await self.bot.db_pool.execute(query, ctx.guild.id, member.id, num)
        await ctx.send(
            _("Removed {num} warn(s) for {member}. [{remaining} warns]").format(
                num=num, member=member.mention, remaining=fetch - num
            )
        )

    @commands.hybrid_command(name="warnings", aliases=["warns"])
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(member="Whose warnings to browse (defaults to you).")
    async def warnings(self, ctx, member: discord.Member = None):
        """Interactively browse and remove a member's warnings."""

        member = member or ctx.author

        rows = await self.bot.db_pool.fetch(
            "SELECT case_number, reason, moderator_id, created_at FROM cases "
            "WHERE guild_id = $1 AND user_id = $2 AND action = 'warn' "
            "ORDER BY case_number DESC;",
            ctx.guild.id,
            member.id,
        )

        view = WarningsView(self, ctx.guild, member, rows, ctx.author.id)
        view.message = await ctx.send(embed=view.embed(), view=view)

    @commands.hybrid_command(name="case")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @discord.app_commands.describe(number="The case number to look up.")
    async def case(self, ctx, number: int):
        """Show a single moderation case by its number."""

        row = await self.bot.db_pool.fetchrow(
            "SELECT * FROM cases WHERE guild_id = $1 AND case_number = $2;",
            ctx.guild.id,
            number,
        )
        if row is None:
            return await ctx.send(
                _("No case #{number} found in this server.").format(number=number)
            )

        await ctx.send(embed=self._case_record_embed(ctx.guild, row))

    @commands.hybrid_command(name="cases", aliases=["history"])
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @discord.app_commands.describe(member="Only show cases for this member (optional).")
    async def cases(self, ctx, member: discord.Member = None):
        """Paginated moderation case history (optionally filtered to a member)."""

        if member is None:
            rows = await self.bot.db_pool.fetch(
                "SELECT case_number, user_id, action, reason, created_at FROM cases "
                "WHERE guild_id = $1 ORDER BY case_number DESC;",
                ctx.guild.id,
            )
            title = _("Case history - {guild}").format(guild=ctx.guild.name)
        else:
            rows = await self.bot.db_pool.fetch(
                "SELECT case_number, user_id, action, reason, created_at FROM cases "
                "WHERE guild_id = $1 AND user_id = $2 ORDER BY case_number DESC;",
                ctx.guild.id,
                member.id,
            )
            title = _("Case history - {member}").format(member=member)

        if not rows:
            return await ctx.send(_("No cases on record."))

        lines = []
        for row in rows:
            verb = modactions.ACTION_VERBS.get(
                row["action"], row["action"].title()
            )
            reason = row["reason"] or _("No reason")
            if len(reason) > 60:
                reason = f"{reason[:57]}..."
            when = discord.utils.format_dt(row["created_at"], "d")
            lines.append(
                f"`#{row['case_number']}` **{verb}** <@{row['user_id']}> "
                f"- {reason} ({when})"
            )

        embeds = paginate_lines(
            lines, title=title, colour=modactions.action_colour("note")
        )
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)

    @commands.hybrid_command(name="reason")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @discord.app_commands.describe(
        case_number="The case to update.", new_reason="The new reason to record."
    )
    async def reason(self, ctx, case_number: int, *, new_reason: str = None):
        """Update the reason recorded on an existing case."""

        # Interactive path: no reason supplied via the slash command. Resolve the
        # case row first, then open a modal prefilled with its current reason so
        # the moderator can edit it in place (persisted on submit).
        if new_reason is None and ctx.interaction is not None:
            row = await self.bot.db_pool.fetchrow(
                "SELECT * FROM cases WHERE guild_id = $1 AND case_number = $2;",
                ctx.guild.id,
                case_number,
            )
            if row is None:
                return await ctx.send(
                    _("No case #{number} found in this server.").format(
                        number=case_number
                    )
                )
            await ctx.interaction.response.send_modal(
                ReasonEditModal(self, ctx.guild, row)
            )
            return

        # Text path with no reason: nothing to update, show usage.
        if new_reason is None:
            return await ctx.send_help(ctx.command)

        row = await self.bot.db_pool.fetchrow(
            "UPDATE cases SET reason = $3 "
            "WHERE guild_id = $1 AND case_number = $2 RETURNING *;",
            ctx.guild.id,
            case_number,
            new_reason,
        )
        if row is None:
            return await ctx.send(
                _("No case #{number} found in this server.").format(
                    number=case_number
                )
            )

        embed = self._case_record_embed(ctx.guild, row)
        embed.add_field(name=_("Updated by"), value=ctx.author.mention, inline=False)
        await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
