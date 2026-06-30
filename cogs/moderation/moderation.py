import datetime
import logging

import discord
from discord.ext import commands
from discord.ext.commands import MemberConverter

from tools import modactions
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)

E_VERIF = config_loader.getstr("Emojis", "verif")


def trim_reason(reason):
    """Truncate a moderation reason to 100 characters, appending an ellipsis when clipped."""
    return reason if len(reason) <= 100 else f"{reason[:100]}..."


class ConfirmView(discord.ui.View):
    """Author-restricted Confirm/Cancel prompt for dangerous moderation actions.

    The invoker presses Confirm or Cancel; the caller waits on the view and reads
    ``self.value`` (``True`` confirmed, ``False``/``None`` aborted).
    """

    def __init__(self, author_id, *, timeout=30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value = None
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

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

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class WarningsView(discord.ui.View):
    """Author-restricted, paginated list of a member's warn-cases.

    A dropdown selects a warn on the current page and the danger button removes
    it (deletes the case row and decrements the member's ``warns_count``).
    """

    def __init__(self, cog, guild, member, warns, author_id, *, per_page=10, timeout=120):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.member = member
        self.warns = list(warns)  # asyncpg Records, newest first
        self.author_id = author_id
        self.per_page = per_page
        self.index = 0
        self.selected = None
        self.message = None

        self.select = discord.ui.Select(
            placeholder="Select a warn to remove...", row=0
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
            title=f"Warnings - {self.member}",
            colour=modactions.action_colour("warn"),
        )
        embed.set_thumbnail(url=self.member.display_avatar.url)

        page = self._page_slice()
        if not page:
            embed.description = "No warnings on record."
        else:
            lines = []
            for warn in page:
                reason = warn["reason"] or "*No reason provided*"
                when = discord.utils.format_dt(warn["created_at"], "R")
                lines.append(
                    f"**Case #{warn['case_number']}** - {reason}\n"
                    f"by {self._mod_text(warn['moderator_id'])} - {when}"
                )
            embed.description = "\n\n".join(lines)

        embed.set_footer(
            text=f"Page {self.index + 1}/{self.page_count} - {len(self.warns)} warn(s)"
        )
        return embed

    def _rebuild(self):
        """Refresh the select options and button states for the current page."""
        page = self._page_slice()
        options = []
        for warn in page:
            reason = warn["reason"] or "No reason"
            options.append(
                discord.SelectOption(
                    label=f"Case #{warn['case_number']}",
                    description=reason[:100],
                    value=str(warn["case_number"]),
                )
            )

        if options:
            self.select.options = options
            self.select.disabled = False
        else:
            self.select.options = [
                discord.SelectOption(label="No warnings", value="none")
            ]
            self.select.disabled = True

        self.selected = None
        self.remove_warn.disabled = True
        self.prev_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= self.page_count - 1

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

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
                        "Couldn't select that warn, please try again.",
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
                        "Couldn't turn the page, please try again.", ephemeral=True
                    )
                except Exception:
                    log.exception("Warnings pagination failed")

    @discord.ui.button(label="Remove warn", style=discord.ButtonStyle.danger, row=1)
    async def remove_warn(self, interaction, button):
        if self.selected is None:
            return await interaction.response.send_message(
                "Pick a warn from the dropdown first.", ephemeral=True
            )

        try:
            await self.cog.bot.db_pool.execute(
                "DELETE FROM cases WHERE guild_id = $1 AND user_id = $2 "
                "AND action = 'warn' AND case_number = $3;",
                self.guild.id,
                self.member.id,
                self.selected,
            )
            await self.cog.bot.db_pool.execute(
                "UPDATE warns SET warns_count = GREATEST(warns_count - 1, 0) "
                "WHERE guild_id = $1 AND user_id = $2;",
                self.guild.id,
                self.member.id,
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
                        "Couldn't remove that warn, please try again.",
                        ephemeral=True,
                    )
                except Exception:
                    log.exception("Failed to remove warn case")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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
        ml = self.bot.get_cog("ModLog")
        if ml is None:
            return
        try:
            await ml.post_action(guild, embed)
        except Exception:
            log.exception("Failed to funnel mod action to mod-log")

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

    def _case_record_embed(self, guild, row):
        """Render a stored case row (DB record) as a consistent case embed.

        Resolves the target/moderator from cache when possible and degrades to a
        bare mention when they are no longer reachable.
        """
        action = row["action"]
        verb = modactions.ACTION_VERBS.get(action, action.title())
        embed = discord.Embed(
            title=f"Case #{row['case_number']} - {verb}",
            colour=modactions.action_colour(action),
            timestamp=row["created_at"],
        )

        target = guild.get_member(row["user_id"]) or self.bot.get_user(
            row["user_id"]
        )
        if target is not None:
            embed.set_thumbnail(url=target.display_avatar.url)
            user_value = f"{target.mention} (`{target.id}`)"
        else:
            user_value = f"<@{row['user_id']}> (`{row['user_id']}`)"

        moderator = guild.get_member(row["moderator_id"]) or self.bot.get_user(
            row["moderator_id"]
        )
        mod_value = (
            moderator.mention if moderator else f"<@{row['moderator_id']}>"
        )

        embed.add_field(name="User", value=user_value)
        embed.add_field(name="Moderator", value=mod_value)
        embed.add_field(
            name="Reason",
            value=row["reason"] or "*No reason provided*",
            inline=False,
        )
        if row["expires"] is not None:
            embed.add_field(
                name="Expires",
                value=discord.utils.format_dt(row["expires"], "R"),
            )
        embed.set_footer(text=f"User ID: {row['user_id']}")
        return embed

    @commands.hybrid_command(aliases=["newmembers"])
    @commands.guild_only()
    async def newusers(self, ctx, *, count=5):
        """Tells you the newest members of the server.
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

                e = discord.Embed(
                    title="New Members", colour=random_colour()
                )

                for member in members:
                    body = f"joined {discord.utils.format_dt(member.joined_at, 'R')}, created {discord.utils.format_dt(member.created_at, 'R')}"
                    e.add_field(
                        name=f"{member} (ID: {member.id})", value=body, inline=False
                    )

                await ctx.send(embed=e)

        except Exception:
            log.exception("Failed to send new members embed")

    @commands.hybrid_command(name="kick", aliases=["k"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    async def _kick(self, ctx, target: discord.User, *, reason: str = None):
        """Kicks an annoying user. Requires kick members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        # Suppress the ModLog leave listener so this bot kick is logged once
        # (the case embed below), not twice.
        ml = self.bot.get_cog("ModLog")
        if ml:
            ml.suppress(ctx.guild.id, target.id, "remove")

        try:
            await ctx.guild.kick(
                target,
                reason=f"{ctx.author}: {trim_reason(reason)}",
            )
        except Exception:
            log.exception("Failed to kick member")
            return await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

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
    async def _voicekick(self, ctx, user: discord.Member, *, reason: str = None):
        """Kicks an annoying user. Requires kick members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        embedkick = discord.Embed(
            color=random_colour(),
            timestamp=ctx.message.created_at,
            title=f"Kick | {ctx.author.name} has kicked {user.name}",
        )
        embedkick.set_thumbnail(url=user.display_avatar.url)
        embedkick.add_field(
            name="**🔴 Voice Kick Info**",
            value=f"Moderator: **{ctx.author.mention}**\nReason: **{trim_reason(reason)}**\nTime: **{ctx.message.created_at}**",
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
            await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

    @commands.hybrid_command(name="move")
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    async def _move(self, ctx, user: discord.Member, room: str):
        """Moves an annoying user to a channel."""

        channel = discord.utils.get(ctx.guild.voice_channels, name=room)
        try:
            await user.move_to(channel, reason=None)
            await ctx.send(f"{user.name} has been moved to {channel}")
        except Exception:
            await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )
            log.exception("Failed to move member to channel")

    @commands.hybrid_command(name="ban", aliases=["b"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(ban_members=True)
    async def _ban(self, ctx, target: discord.User, *, reason: str = None):
        """Bans an annoying user. Requires ban members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        confirm = discord.Embed(
            title="Confirm ban",
            description=(
                f"Are you sure you want to ban {target.mention} "
                f"(`{target.id}`)?"
            ),
            colour=modactions.action_colour("ban"),
        )
        confirm.add_field(name="Reason", value=trim_reason(reason), inline=False)
        confirm.set_thumbnail(url=target.display_avatar.url)

        view = ConfirmView(ctx.author.id)
        view.message = await ctx.send(embed=confirm, view=view)
        await view.wait()

        if not view.value:
            aborted = discord.Embed(
                title="Ban cancelled",
                description=f"No action taken against {target.mention}.",
                colour=modactions.action_colour("note"),
            )
            try:
                await view.message.edit(embed=aborted, view=None)
            except discord.HTTPException:
                pass
            return

        # Suppress the ModLog ban listener so this bot ban is logged once
        # (the case embed below), not twice.
        ml = self.bot.get_cog("ModLog")
        if ml:
            ml.suppress(ctx.guild.id, target.id, "ban")

        try:
            await ctx.guild.ban(
                target,
                reason=f"{ctx.author}: {trim_reason(reason)}",
            )
        except Exception:
            log.exception("Failed to ban member")
            return await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

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
            await view.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.hybrid_command(name="unban", aliases=["ub"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(ban_members=True)
    async def _unban(self, ctx, target: discord.User, *, reason: str = None):
        """Unbans a user. Requires ban members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        # Suppress the ModLog unban listener so this bot unban is logged once
        # (the case embed below), not twice.
        ml = self.bot.get_cog("ModLog")
        if ml:
            ml.suppress(ctx.guild.id, target.id, "unban")

        try:
            await ctx.guild.unban(
                target,
                reason=f"{ctx.author}: {trim_reason(reason)}",
            )
        except Exception:
            log.exception("Failed to unban member")
            return await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

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
                "Give me at least one user id to ban.\n"
                "Usage: `massban <id1> <id2> ... [reason]`"
            )
        if len(users) > 200:
            return await ctx.send("I can ban at most 200 users in one go.")

        if reason is None:
            reason = "No reason specified"

        confirm = discord.Embed(
            title="Confirm mass ban",
            description=f"Are you sure you want to ban **{len(users)}** user(s) by id?",
            colour=modactions.action_colour("ban"),
        )
        confirm.add_field(name="Reason", value=trim_reason(reason), inline=False)
        view = ConfirmView(ctx.author.id)
        view.message = await ctx.send(embed=confirm, view=view)
        await view.wait()

        if not view.value:
            aborted = discord.Embed(
                title="Mass ban cancelled",
                description="No action taken.",
                colour=modactions.action_colour("note"),
            )
            try:
                await view.message.edit(embed=aborted, view=None)
            except discord.HTTPException:
                pass
            return

        # Log each ban once (the summary below), not twice via the ModLog listener.
        ml = self.bot.get_cog("ModLog")
        if ml:
            for obj in users:
                ml.suppress(ctx.guild.id, obj.id, "ban")

        try:
            result = await ctx.guild.bulk_ban(
                users,
                reason=f"{ctx.author}: {trim_reason(reason)}",
                delete_message_seconds=0,
            )
        except Exception:
            log.exception("Failed to bulk ban")
            return await ctx.send(
                "**:x: Sorry, I could not ban those users (missing permissions?).**",
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
            title="Mass ban complete",
            colour=modactions.action_colour("ban"),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Banned", value=str(len(result.banned)))
        embed.add_field(name="Failed", value=str(len(result.failed)))
        embed.add_field(name="Reason", value=trim_reason(reason), inline=False)
        embed.set_footer(
            text=f"By {ctx.author}", icon_url=ctx.author.display_avatar.url
        )
        try:
            await view.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.hybrid_command(
        name="purge", aliases=["pg", "massclean", "massdelete", "prune"]
    )
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    async def _purge(self, ctx, count: int):
        """Purges messages. Requires manage messages permission"""

        if ctx.interaction:
            await ctx.interaction.response.defer()

        if count > 999 or count < 1:
            return await ctx.send(
                ":warning: | **Count can't be lesser than 0 and greater than 999**",
                delete_after=3,
            )

        else:
            try:
                await ctx.channel.purge(limit=count + 1)
            except Exception:
                log.exception("Failed to purge messages")
                return await ctx.send(
                    "**:x: Sorry, I am missing permissions to do this**", delete_after=5
                )

        return await ctx.send(f"{E_VERIF} **Deleted successfully!**", delete_after=3)

    @commands.hybrid_command(description="Clears X messages.")
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    async def clean(self, ctx, num: int, target: discord.Member):
        """Clears X messages of a member"""

        if num > 500 or num < 0:
            return await ctx.send("Invalid amount. Maximum is 500.")

        def msgcheck(amsg):
            if target:
                return amsg.author.id == target.id
            return True

        if ctx.interaction:
            await ctx.interaction.response.defer()

        deleted = await ctx.channel.purge(limit=num, check=msgcheck)
        await ctx.send(
            f"{E_VERIF} Deleted **{len(deleted)}/{num}** possible messages for you.",
            delete_after=3,
        )

    async def create_mute_role(self, ctx):
        perms = discord.Permissions(
            send_messages=False,
            read_messages=True,
            add_reactions=False,
            send_tts_messages=False,
            read_message_history=True,
            speak=False,
        )
        role = "Muted"
        await ctx.guild.create_role(name=role, permissions=perms)
        await ctx.send(f"{ctx.guild.id}, {role}")

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mute(self, ctx, user: discord.Member, *, reason: str = None):
        """Mutes the specified member."""

        if reason is None:
            reason = "No reason specified"

        role = await self._get_mute_role_id(ctx.guild.id)

        try:
            if role is None:
                try:
                    await ctx.send("Mute role is not defined", delete_after=3)
                    await ctx.send("Creating role...", delete_after=1)
                    perms = discord.Permissions(
                        send_messages=False,
                        add_reactions=False,
                        send_tts_messages=False,
                        speak=False,
                    )
                    role = "Muted"
                    mrole = await ctx.guild.create_role(name=role, permissions=perms)
                    await ctx.send(content="Mute role created!", delete_after=5)
                    query = """INSERT INTO muterole (guild_id, role_id) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET role_id = $3;"""
                    await self.bot.db_pool.execute(query, ctx.guild.id, mrole.id, mrole.id)
                    self.bot.muteroles[ctx.guild.id] = mrole.id

                    for channel in ctx.guild.text_channels:
                        await channel.set_permissions(
                            mrole,
                            overwrite=discord.PermissionOverwrite(
                                send_messages=False,
                                add_reactions=False,
                                send_tts_messages=False,
                            ),
                        )
                    for channel in ctx.guild.voice_channels:
                        await channel.set_permissions(
                            mrole, overwrite=discord.PermissionOverwrite(speak=False)
                        )
                    for channel in ctx.guild.categories:
                        await channel.set_permissions(
                            mrole,
                            overwrite=discord.PermissionOverwrite(
                                send_messages=False,
                                add_reactions=False,
                                send_tts_messages=False,
                                speak=False,
                            ),
                        )

                    await user.add_roles(
                        mrole, reason=f"""Muted By: {ctx.author} for: {reason} """
                    )

                    query = """INSERT INTO mutedmembers (mguild_id, member_id) VALUES ($1, $2)"""
                    await self.bot.db_pool.execute(query, ctx.guild.id, user.id)

                    num = await modactions.create_case(
                        self.bot.db_pool,
                        ctx.guild.id,
                        user.id,
                        ctx.author.id,
                        "mute",
                        reason,
                    )
                    embed = modactions.case_embed(
                        num, "mute", user, ctx.author, reason
                    )
                    await ctx.send(embed=embed)
                    await self._post_modlog(ctx.guild, embed)
                    return

                except Exception:
                    log.exception("Failed to create mute role")

            mutedrole = discord.utils.get(ctx.guild.roles, id=role)
            await user.add_roles(
                mutedrole, reason=f"""Muted By: {ctx.author} for: {reason} """
            )

            query = (
                """INSERT INTO mutedmembers (mguild_id, member_id) VALUES ($1, $2)"""
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
            log.exception("Failed to mute member")
            embed = discord.Embed(
                title="Already Muted",
                colour=random_colour(),
                description=f":red_circle: {user} is already muted!",
                timestamp=datetime.datetime.utcnow(),
            )
            await ctx.send(embed=embed)
            return

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(self, ctx, user: discord.Member):
        """Un-mutes the specified member."""

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

        except Exception as e:
            await ctx.send(e, delete_after=3)
            embed = discord.Embed(
                title="Not Muted",
                colour=random_colour(),
                description=f""":red_circle: {user} was never muted!""",
                timestamp=datetime.datetime.utcnow(),
            )
            await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def addrole(self, ctx, member, role: discord.Role):
        """Set a role to a specified member."""

        if member == "-all":
            confirm = discord.Embed(
                title="Confirm mass role add",
                description=(
                    f"Add the **{role.name}** role to **all** members of this "
                    "server? This can take a while."
                ),
                colour=modactions.action_colour("note"),
            )
            view = ConfirmView(ctx.author.id)
            view.message = await ctx.send(embed=confirm, view=view)
            await view.wait()

            if not view.value:
                try:
                    await view.message.edit(
                        content="Cancelled.", embed=None, view=None
                    )
                except discord.HTTPException:
                    pass
                return

            async with ctx.typing():
                for m in ctx.guild.members:
                    if role not in m.roles:
                        await m.add_roles(role)

            return await ctx.send(
                f"Added to all guilds members **`{role.name}`** role."
            )

        converter = MemberConverter()
        m = await converter.convert(ctx, member)
        await m.add_roles(role)
        return await ctx.send(
            f"{E_VERIF} **`{role.name}`** role has been added to **{m.name}**"
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def removerole(self, ctx, member, role: discord.Role):
        """Remove a role to a specified member."""

        if member == "-all":
            confirm = discord.Embed(
                title="Confirm mass role remove",
                description=(
                    f"Remove the **{role.name}** role from **all** members of "
                    "this server? This can take a while."
                ),
                colour=modactions.action_colour("note"),
            )
            view = ConfirmView(ctx.author.id)
            view.message = await ctx.send(embed=confirm, view=view)
            await view.wait()

            if not view.value:
                try:
                    await view.message.edit(
                        content="Cancelled.", embed=None, view=None
                    )
                except discord.HTTPException:
                    pass
                return

            async with ctx.typing():
                for m in ctx.guild.members:
                    if role in m.roles:
                        await m.remove_roles(role)

            return await ctx.send(
                f"Removed to all guilds members **`{role.name}`** role."
            )

        converter = MemberConverter()
        m = await converter.convert(ctx, member)
        await m.remove_roles(role)
        return await ctx.send(
            f"{E_VERIF} **`{role.name}`** role has been removed to **{m.name}**"
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def moverole(self, ctx, role: discord.Role, pos: int):
        """Move a role to the given position in the hierarchy."""

        try:
            await role.edit(position=pos)
            await ctx.send(f"{role} moved.")
        except discord.Forbidden:
            await ctx.send("You do not have permission to do that")
        except discord.HTTPException:
            await ctx.send("Failed to move role")
        except (TypeError, ValueError):
            await ctx.send("Invalid argument")

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
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
            return await ctx.send(f"{member.mention} has no warns.")

        await ctx.send(f"{member.mention} has {fetch} warn(s)")

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def warn(self, ctx, member: discord.Member = None, *, reason: str = None):
        """Warn a member of the guild (auto-kick at 3 warns)"""

        if member is None:
            return await ctx.send_help(ctx.command)

        query = """

        SELECT warns_count FROM warns
        WHERE guild_id = $1 AND user_id = $2;

        """

        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id) or 0
        new_count = fetch + 1

        # Every warn is recorded as its own case for history/auditing, while the
        # warns_count row stays the source of truth for the auto-kick threshold.
        num = await modactions.create_case(
            self.bot.db_pool,
            ctx.guild.id,
            member.id,
            ctx.author.id,
            "warn",
            reason,
        )

        if new_count >= 3:
            query = """ INSERT INTO warns
                        (guild_id, user_id, warns_count)
                        VALUES
                        ($1, $2, 0) ON CONFLICT (guild_id, user_id) DO UPDATE SET warns_count = 0;
                        """
            await self.bot.db_pool.execute(query, ctx.guild.id, member.id)

            embed = modactions.case_embed(num, "warn", member, ctx.author, reason)
            embed.add_field(
                name="Auto-action", value="Reached 3 warns - kicked", inline=False
            )

            # Suppress the ModLog leave listener so this auto-kick is logged once
            # (the case embed above), not twice.
            ml = self.bot.get_cog("ModLog")
            if ml:
                ml.suppress(ctx.guild.id, member.id, "remove")

            try:
                await member.kick(reason="Auto-kick: reached 3 warns")
            except Exception:
                log.exception("Failed to kick member at 3 warns")
                await ctx.send(embed=embed)
                await self._post_modlog(ctx.guild, embed)
                return await ctx.send(
                    f"{member.mention} has 3 warns but I don't have permissions "
                    "to kick them from the guild."
                )

            try:
                await member.send("You have been kicked from the server!")
            except Exception:
                log.exception("Failed to DM kicked member")

            await ctx.send(embed=embed)
            await self._post_modlog(ctx.guild, embed)
            return

        query = """ INSERT INTO warns (guild_id, user_id, warns_count) VALUES ($1, $2, $3) ON CONFLICT (guild_id, user_id) DO UPDATE SET warns_count = $3;"""
        await self.bot.db_pool.execute(query, ctx.guild.id, member.id, new_count)

        embed = modactions.case_embed(num, "warn", member, ctx.author, reason)
        embed.add_field(name="Warns", value=f"{new_count}/3", inline=False)
        await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

    @commands.hybrid_command(aliases=["rmwarn", "removewarn"])
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def delwarn(self, ctx, member: discord.Member = None, num: int = 1):
        """Remove a warn from a member of the guild."""

        if member is None:
            return await ctx.send_help(ctx.command)

        query = (
            """SELECT warns_count FROM warns WHERE guild_id = $1 AND user_id = $2;"""
        )
        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id)

        if not fetch:
            return await ctx.send(f"{member.mention} has no warns!")

        if fetch - num < 0:
            query = """ UPDATE warns SET warns_count = 0 WHERE guild_id = $1 AND user_id = $2;"""
            await self.bot.db_pool.execute(query, ctx.guild.id, member.id)
            return await ctx.send(f"Removed all warns for {member.mention}.")

        query = f""" UPDATE warns SET warns_count = warns_count - {int(num)} WHERE guild_id = $1 AND user_id = $2;"""
        await self.bot.db_pool.execute(query, ctx.guild.id, member.id)
        await ctx.send(
            f"Removed {num} warn(s) for {member.mention}. [{fetch - num} warns]"
        )

    @commands.hybrid_command(name="warnings", aliases=["warns"])
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
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
    async def case(self, ctx, number: int):
        """Show a single moderation case by its number."""

        row = await self.bot.db_pool.fetchrow(
            "SELECT * FROM cases WHERE guild_id = $1 AND case_number = $2;",
            ctx.guild.id,
            number,
        )
        if row is None:
            return await ctx.send(f"No case #{number} found in this server.")

        await ctx.send(embed=self._case_record_embed(ctx.guild, row))

    @commands.hybrid_command(name="cases", aliases=["history"])
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cases(self, ctx, member: discord.Member = None):
        """Paginated moderation case history (optionally filtered to a member)."""

        if member is None:
            rows = await self.bot.db_pool.fetch(
                "SELECT case_number, user_id, action, reason, created_at FROM cases "
                "WHERE guild_id = $1 ORDER BY case_number DESC;",
                ctx.guild.id,
            )
            title = f"Case history - {ctx.guild.name}"
        else:
            rows = await self.bot.db_pool.fetch(
                "SELECT case_number, user_id, action, reason, created_at FROM cases "
                "WHERE guild_id = $1 AND user_id = $2 ORDER BY case_number DESC;",
                ctx.guild.id,
                member.id,
            )
            title = f"Case history - {member}"

        if not rows:
            return await ctx.send("No cases on record.")

        lines = []
        for row in rows:
            verb = modactions.ACTION_VERBS.get(
                row["action"], row["action"].title()
            )
            reason = row["reason"] or "No reason"
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
    async def reason(self, ctx, case_number: int, *, new_reason: str):
        """Update the reason recorded on an existing case."""

        row = await self.bot.db_pool.fetchrow(
            "UPDATE cases SET reason = $3 "
            "WHERE guild_id = $1 AND case_number = $2 RETURNING *;",
            ctx.guild.id,
            case_number,
            new_reason,
        )
        if row is None:
            return await ctx.send(f"No case #{case_number} found in this server.")

        embed = self._case_record_embed(ctx.guild, row)
        embed.add_field(name="Updated by", value=ctx.author.mention, inline=False)
        await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
