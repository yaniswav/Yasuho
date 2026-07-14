"""A simple verification gate: a persistent "Verify" button that grants a role.

Unlike the button-role / self-role menus (which toggle roles), this is one-way -
a member clicks Verify once to gain access. Config is a single per-guild
`verify_role` setting; one global persistent view (custom_id "verify_button")
handles every guild's button and reads that guild's role at click time, so
nothing needs re-registering per message.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

import logging

import discord
from discord.ext import commands

from tools import i18n, settings
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)


class VerifyButton(discord.ui.Button):
    """The public, persistent Verify button (grants the guild's verify role)."""

    def __init__(self):
        super().__init__(
            label="Verify",
            style=discord.ButtonStyle.success,
            emoji="\U00002705",
            custom_id="verify_button",
        )

    async def callback(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                _("Verification only works inside a server."), ephemeral=True
            )
        role_id = await settings.get_guild(
            interaction.client.db_pool, guild.id, "verify_role", None
        )
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            return await interaction.response.send_message(
                _("Verification is not set up here."), ephemeral=True
            )
        if role in member.roles:
            return await interaction.response.send_message(
                _("You are already verified."), ephemeral=True
            )
        if role >= guild.me.top_role or role.managed:
            return await interaction.response.send_message(
                _("I can't assign that role - it may be above my highest role."),
                ephemeral=True,
            )
        try:
            await member.add_roles(role, reason="Verification")
        except discord.HTTPException:
            log.exception("Verification role grant failed")
            return await interaction.response.send_message(
                _("Something went wrong, please try again."), ephemeral=True
            )
        await interaction.response.send_message(
            _("You are verified. Welcome!"), ephemeral=True
        )


class VerifyView(discord.ui.View):
    """Persistent (timeout=None) wrapper around the single Verify button."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyButton())


class VerifyStatusView(discord.ui.LayoutView):
    """Single-page Components V2 card: the current verification configuration
    for a guild (read-only, no controls)."""

    def __init__(self, guild, role_id, *, timeout=180):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, role_id)

    def _build(self, guild, role_id):
        role = guild.get_role(role_id) if role_id else None
        enabled = role is not None
        status_value = (
            ("\U0001F7E2 " + _("Enabled"))
            if enabled
            else ("\U0001F534 " + _("Disabled"))
        )
        if role_id and role is None:
            role_value = f"`{role_id}` " + _("(deleted)")
        else:
            role_value = role.mention if role is not None else _("*Not set.*")

        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("Verification | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**Status:** {status}\n**Role granted:** {role}").format(
                    status=status_value, role=role_value
                )
            )
        )
        if not enabled:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "-# " + _("Use `/verify setup` to turn on verification.")
                )
            )
        self.add_item(container)


class Verification(commands.Cog):
    """A one-click verification gate that grants a role."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # One global persistent view handles every guild's Verify button.
        try:
            self.bot.add_view(VerifyView())
        except Exception:
            log.exception("Failed to register the verification view")

    @commands.hybrid_group(name="verify")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def verify(self, ctx):
        """Manage the verification gate: set it up or disable it."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @verify.command(name="setup")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    @discord.app_commands.describe(
        role="The role granted on verification.",
        channel="Where to post the Verify button (defaults to here).",
        message="A custom message on the verify embed.",
    )
    async def verify_setup(
        self,
        ctx,
        role: discord.Role,
        channel: discord.TextChannel = None,
        *,
        message: str = None,
    ):
        """Post a Verify button that grants a role (one click, one way)."""
        if role >= ctx.guild.me.top_role or role.managed:
            return await ctx.send(
                _("I can't assign {role} - it must be below my highest role and not managed.").format(
                    role=role.mention
                )
            )
        channel = channel or ctx.channel
        if not channel.permissions_for(ctx.guild.me).send_messages:
            return await ctx.send(
                _("I can't send messages in {channel}.").format(channel=channel.mention)
            )

        await settings.set_guild(self.bot.db_pool, ctx.guild.id, "verify_role", role.id)

        embed = discord.Embed(
            title=_("Verification"),
            description=(
                message
                or _("Click the button below to verify and unlock the server.")
            ),
            colour=random_colour(),
        )
        await channel.send(embed=embed, view=VerifyView())
        await ctx.send(
            _("Verification is set up - clicking grants {role} in {channel}.").format(
                role=role.mention, channel=channel.mention
            )
        )

    @verify.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def verify_disable(self, ctx):
        """Turn off verification (existing buttons will report it is off)."""
        await settings.set_guild(self.bot.db_pool, ctx.guild.id, "verify_role", None)
        await ctx.send(_("Verification disabled."))

    @verify.command(name="status")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def verify_status(self, ctx):
        """Show the current verification configuration."""
        role_id = await settings.get_guild(
            self.bot.db_pool, ctx.guild.id, "verify_role", None
        )
        view = VerifyStatusView(ctx.guild, role_id)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(Verification(bot))
