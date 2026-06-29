import logging

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)

# Self-assignable role buttons are public by design: anyone may click them to
# toggle a role. The author-restriction / on_timeout conventions therefore apply
# only to the admin-facing creation menu below (CreatePanelView), not to the
# persistent ButtonRoleView.


def build_panel_embed(title, roles):
    """The public-facing embed that sits above the role buttons."""

    embed = discord.Embed(title=title or "Self-assignable roles", colour=random_colour())
    if roles:
        embed.description = (
            "Click a button below to give yourself a role, or click it again "
            "to remove it.\n\n" + "\n".join(f"- {r.mention}" for r in roles)
        )
    else:
        embed.description = "Click a button below to toggle a role."
    return embed


class ButtonRoleButton(discord.ui.Button):
    """A single self-assignable role button with a stable, persistent custom_id."""

    def __init__(self, role_id, label, emoji=None):
        self.role_id = role_id
        super().__init__(
            label=(label or "Role")[:80],
            emoji=emoji or None,
            style=discord.ButtonStyle.secondary,
            custom_id=f"br:{role_id}",
        )

    async def callback(self, interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Roles can only be toggled inside a server.", ephemeral=True
            )
            return

        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(
                "That role no longer exists.", ephemeral=True
            )
            return

        none = discord.AllowedMentions.none()
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Button role")
                await interaction.response.send_message(
                    f"Removed {role.mention} from you.",
                    ephemeral=True,
                    allowed_mentions=none,
                )
            else:
                await member.add_roles(role, reason="Button role")
                await interaction.response.send_message(
                    f"Gave you {role.mention}.",
                    ephemeral=True,
                    allowed_mentions=none,
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to manage that role. It may be above "
                "my highest role.",
                ephemeral=True,
            )
        except discord.HTTPException:
            log.exception("Failed to toggle button role %s", self.role_id)
            await interaction.response.send_message(
                "Something went wrong toggling that role.", ephemeral=True
            )


class ButtonRoleView(discord.ui.View):
    """Persistent (timeout=None) view holding one button per self-assignable role."""

    def __init__(self, rows):
        super().__init__(timeout=None)
        # rows: iterable of (role_id, label, emoji)
        for role_id, label, emoji in rows:
            self.add_item(ButtonRoleButton(role_id, label, emoji))


class _CreatePanelRoleSelect(discord.ui.RoleSelect):
    """Lets the admin pick up to 5 roles to drop on a new panel."""

    def __init__(self):
        super().__init__(
            placeholder="Pick up to 5 roles for the panel...",
            min_values=1,
            max_values=5,
            row=0,
        )

    async def callback(self, interaction):
        await self.view.on_roles_selected(interaction, self.values)


class CreatePanelView(discord.ui.View):
    """Author-restricted menu that builds and posts a button-role panel."""

    def __init__(self, cog, author_id, channel, title, *, timeout=180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.channel = channel
        self.title = title
        self.roles = []
        self.message = None
        self.add_item(_CreatePanelRoleSelect())

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    @staticmethod
    def _can_assign(role):
        guild = role.guild
        me = guild.me
        return (
            not role.is_default()
            and not role.managed
            and me is not None
            and role < me.top_role
        )

    async def on_roles_selected(self, interaction, roles):
        self.roles = list(roles)
        self.confirm_button.disabled = False
        embed = discord.Embed(
            title=self.title,
            description=(
                "Selected roles:\n"
                + "\n".join(f"- {r.mention}" for r in self.roles)
                + "\n\nPress Post panel to publish, or pick again to change."
            ),
            colour=random_colour(),
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Post panel", style=discord.ButtonStyle.success, disabled=True, row=1
    )
    async def confirm_button(self, interaction, button):
        assignable = [r for r in self.roles if self._can_assign(r)]
        if not assignable:
            await interaction.response.send_message(
                "None of those roles can be assigned by me - they're either "
                "managed by an integration or above my highest role.",
                ephemeral=True,
            )
            return

        skipped = [r for r in self.roles if r not in assignable]
        rows = [(r.id, r.name[:80], None) for r in assignable]

        try:
            panel = await self.channel.send(
                embed=build_panel_embed(self.title, assignable),
                view=ButtonRoleView(rows),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I can't send messages in {self.channel.mention}.", ephemeral=True
            )
            return
        except discord.HTTPException:
            log.exception("Failed to post button-role panel")
            await interaction.response.send_message(
                "Something went wrong posting the panel.", ephemeral=True
            )
            return

        query = """
            INSERT INTO button_roles
            (message_id, guild_id, channel_id, role_id, label, emoji)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (message_id, role_id) DO UPDATE
            SET label = EXCLUDED.label, emoji = EXCLUDED.emoji;
            """
        try:
            await self.cog.bot.db_pool.executemany(
                query,
                [
                    (
                        panel.id,
                        self.channel.guild.id,
                        self.channel.id,
                        r.id,
                        r.name[:80],
                        None,
                    )
                    for r in assignable
                ],
            )
        except Exception:
            log.exception("Failed to persist button roles for message %s", panel.id)

        # Register the persistent view so the buttons keep working after a restart.
        try:
            self.cog.bot.add_view(ButtonRoleView(rows), message_id=panel.id)
        except Exception:
            log.exception("Failed to register button-role view for message %s", panel.id)

        for child in self.children:
            child.disabled = True
        self.stop()

        done = discord.Embed(
            title="Panel posted",
            description=f"Your button-role panel is live in {self.channel.mention}.",
            colour=random_colour(),
        )
        if skipped:
            done.add_field(
                name="Skipped (not assignable)",
                value=", ".join(r.mention for r in skipped),
                inline=False,
            )
        await interaction.response.edit_message(embed=done, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_button(self, interaction, button):
        for child in self.children:
            child.disabled = True
        self.stop()
        embed = discord.Embed(
            title="Cancelled",
            description="Panel creation was cancelled.",
            colour=random_colour(),
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ButtonRoles(commands.Cog):
    """Self-assignable roles via buttons - a modern take on reaction roles."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register every stored panel as a persistent view so the buttons
        # survive bot restarts.
        query = """
            SELECT message_id, role_id, label, emoji
            FROM button_roles
            ORDER BY message_id;
            """
        rows = await self.bot.db_pool.fetch(query)

        grouped = {}
        for row in rows:
            grouped.setdefault(row["message_id"], []).append(
                (row["role_id"], row["label"], row["emoji"])
            )

        for mid, items in grouped.items():
            try:
                self.bot.add_view(ButtonRoleView(items), message_id=mid)
            except Exception:
                log.exception(
                    "Failed to register button-role view for message %s", mid
                )

    @commands.hybrid_group(aliases=["br"])
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def buttonrole(self, ctx):
        """Self-assignable role buttons."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @buttonrole.command(name="create")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def buttonrole_create(self, ctx, *, title: str = "Self-assignable roles"):
        """Interactively build a button-role panel in this channel."""

        embed = discord.Embed(
            title=title,
            description="Pick up to 5 roles below, then press Post panel.",
            colour=random_colour(),
        )
        view = CreatePanelView(self, ctx.author.id, ctx.channel, title)
        view.message = await ctx.send(embed=embed, view=view)

    @buttonrole.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def buttonrole_list(self, ctx):
        """List every button-role panel set up in this guild."""

        query = """
            SELECT message_id, channel_id, role_id
            FROM button_roles
            WHERE guild_id = $1
            ORDER BY message_id;
            """
        rows = await self.bot.db_pool.fetch(query, ctx.guild.id)

        if not rows:
            embed = discord.Embed(
                title="Button roles",
                description="No button-role panels have been set up for this guild.",
                colour=random_colour(),
            )
            await ctx.send(embed=embed)
            return

        grouped = {}
        for row in rows:
            grouped.setdefault(
                (row["message_id"], row["channel_id"]), []
            ).append(row["role_id"])

        lines = []
        for (mid, cid), role_ids in grouped.items():
            roles = " ".join(f"<@&{rid}>" for rid in role_ids)
            link = f"https://discord.com/channels/{ctx.guild.id}/{cid}/{mid}"
            lines.append(f"[`{mid}`]({link}) - {roles}")

        await Paginator(
            paginate_lines(lines, title="Button roles"), author_id=ctx.author.id
        ).start(ctx)

    @buttonrole.command(name="delete")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def buttonrole_delete(self, ctx, message_id: str):
        """Delete a button-role panel by its message ID."""

        try:
            mid = int(message_id)
        except ValueError:
            await ctx.send("That doesn't look like a valid message ID.")
            return

        query = """
            DELETE FROM button_roles
            WHERE message_id = $1 AND guild_id = $2
            RETURNING channel_id;
            """
        rows = await self.bot.db_pool.fetch(query, mid, ctx.guild.id)

        if not rows:
            await ctx.send("No button-role panel found with that message ID.")
            return

        # Best-effort: also remove the panel message itself.
        channel = ctx.guild.get_channel(rows[0]["channel_id"])
        if channel is not None:
            try:
                msg = await channel.fetch_message(mid)
                await msg.delete()
            except discord.HTTPException:
                pass

        embed = discord.Embed(
            title="Button-role panel deleted",
            description=f"Removed `{len(rows)}` role button(s) for message `{mid}`.",
            colour=random_colour(),
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ButtonRoles(bot))
