import logging
import re

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.i18n import _
from tools.interactions import notify_failure
from tools.paginator import Paginator, paginate_lines
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# https://discord.com/channels/<guild>/<channel>/<message>
_LINK_RE = re.compile(
    r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def _parse_message_ref(text, default_channel_id):
    """Resolve a message ID or jump link to (guild_id, channel_id, message_id).

    A jump link yields all three. A bare numeric ID yields (None, the supplied
    default channel id, the message id). Returns None when nothing parses.
    """

    if not text:
        return None
    text = text.strip()
    match = _LINK_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    if text.isdigit():
        return None, default_channel_id, int(text)
    return None


# ----------------------------------------------------------------------
# Guided add: a modal (message ref + emoji + role picker) and, for the
# prefix path where no interaction exists yet, a one-button opener view.
# ----------------------------------------------------------------------
class AddReactionRoleModal(LocaleModal):
    """Collect a message ref, an emoji and a role, then persist the mapping.

    Mirrors the buttonroles AddButtonModal / AttachModal patterns: two plain
    TextInputs plus a Label-wrapped RoleSelect (a Components V2 select inside a
    modal). Resolution and persistence run through the cog's shared add logic.
    """

    def __init__(self, cog, guild, default_channel_id):
        super().__init__(title=_("Add reaction role"))
        self.cog = cog
        self.guild = guild
        self.default_channel_id = default_channel_id

        self.ref_field = discord.ui.TextInput(
            label=_("Message ID or link"),
            required=True,
            max_length=200,
            placeholder="123456789012345678 or https://discord.com/channels/...",
        )
        self.emoji_field = discord.ui.TextInput(
            label=_("Emoji"),
            required=True,
            max_length=64,
            placeholder=_("Paste the emoji to react with"),
        )
        self.role_select = discord.ui.RoleSelect(
            placeholder=_("Pick the role to grant..."),
            min_values=1,
            max_values=1,
        )
        self.add_item(self.ref_field)
        self.add_item(self.emoji_field)
        self.add_item(discord.ui.Label(text=_("Role"), component=self.role_select))

    async def on_submit(self, interaction):
        role = self.role_select.values[0] if self.role_select.values else None
        if role is None:
            await interaction.response.send_message(
                _("Pick a role to grant."), ephemeral=True
            )
            return

        emoji = (self.emoji_field.value or "").strip()
        if not emoji:
            await interaction.response.send_message(
                _("Give me an emoji to react with."), ephemeral=True
            )
            return

        parsed = _parse_message_ref(self.ref_field.value, self.default_channel_id)
        if parsed is None:
            await interaction.response.send_message(
                _("That doesn't look like a message ID or a Discord message link."),
                ephemeral=True,
            )
            return
        guild_id, channel_id, message_id = parsed
        if guild_id is not None and guild_id != self.guild.id:
            await interaction.response.send_message(
                _("That message link points to a different server."),
                ephemeral=True,
            )
            return

        channel = self.guild.get_channel_or_thread(channel_id)
        if channel is None:
            await interaction.response.send_message(
                _("I can't find that channel in this server."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            embed = await self.cog._persist_reaction_role(
                self.guild, channel, message_id, emoji, role
            )
        except Exception:
            log.exception("Reaction-role add modal failed")
            await notify_failure(interaction)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)


class AddReactionRoleView(AuthorView):
    """One-button opener for the guided add modal (used on the prefix path)."""

    def __init__(self, cog, guild, default_channel_id, author_id, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog
        self.guild = guild
        self.default_channel_id = default_channel_id

    @discord.ui.button(label="Open builder", style=discord.ButtonStyle.primary)
    async def open_button(self, interaction, button):
        try:
            await interaction.response.send_modal(
                AddReactionRoleModal(self.cog, self.guild, self.default_channel_id)
            )
        except Exception:
            log.exception("Reaction-role builder open failed")
            await notify_failure(interaction)


class ReactionRoles(commands.Cog):
    """Assign roles to members when they react to a message."""

    def __init__(self, bot):
        self.bot = bot
        self.cache = {}

    async def cog_load(self):
        query = "SELECT message_id, emoji, role_id FROM reaction_roles;"
        rows = await self.bot.db_pool.fetch(query)
        self.cache = {
            (row["message_id"], row["emoji"]): row["role_id"] for row in rows
        }

    @commands.hybrid_group(aliases=["rr"])
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole(self, ctx):
        """Reaction-role related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    async def _persist_reaction_role(self, guild, channel, mid, emoji, role):
        """Best-effort react + upsert the mapping and refresh the cache.

        Shared by the classic text command and the guided modal so both paths
        behave identically. Returns the confirmation embed to send.
        """

        try:
            msg = await channel.fetch_message(mid)
            await msg.add_reaction(emoji)
        except Exception:
            log.exception("Failed to pre-add reaction")

        stored_emoji = emoji.replace("\uFE0F", "")

        query = """
            INSERT INTO reaction_roles
            (message_id, emoji, role_id, guild_id)
            VALUES
            ($1, $2, $3, $4)
            ON CONFLICT (message_id, emoji) DO UPDATE SET role_id = $3;
            """

        await self.bot.db_pool.execute(
            query, mid, stored_emoji, role.id, guild.id
        )

        self.cache[(mid, stored_emoji)] = role.id

        embed = discord.Embed(
            title=_("Reaction role added"),
            colour=random_colour(),
        )
        embed.add_field(name=_("Message"), value=f"`{mid}`")
        embed.add_field(name=_("Emoji"), value=emoji)
        embed.add_field(name=_("Role"), value=f"<@&{role.id}>")
        return embed

    async def _prompt_add(self, ctx):
        """Open the guided add modal (slash) or a builder button (prefix)."""

        interaction = ctx.interaction
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.send_modal(
                AddReactionRoleModal(self, ctx.guild, ctx.channel.id)
            )
            return

        view = AddReactionRoleView(self, ctx.guild, ctx.channel.id, ctx.author.id)
        embed = discord.Embed(
            title=_("Add a reaction role"),
            description=_(
                "Open the builder to map an emoji on a message to a role."
            ),
            colour=random_colour(),
        )
        view.message = await ctx.send(embed=embed, view=view)

    @reactionrole.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_add(
        self,
        ctx,
        message_id: str = None,
        emoji: str = None,
        role: discord.Role = None,
    ):
        """Map an emoji on a message to a role.

        Give ``<message_id> <emoji> <role>`` for the classic one-shot, or run it
        with no arguments to open a guided builder.
        """

        # Guided path: anything missing opens the modal builder instead.
        if message_id is None or emoji is None or role is None:
            await self._prompt_add(ctx)
            return

        try:
            mid = int(message_id)
        except ValueError:
            await ctx.send(_("That doesn't look like a valid message ID."))
            return

        # Show a typing indicator (and let the slash interaction resolve) so the
        # message fetch, reaction add and DB write can't blow the 3s window.
        async with ctx.typing():
            embed = await self._persist_reaction_role(
                ctx.guild, ctx.channel, mid, emoji, role
            )

        await ctx.send(embed=embed)

    @reactionrole.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_remove(self, ctx, message_id: str, emoji: str):
        """Remove an emoji-to-role mapping from a message."""

        try:
            mid = int(message_id)
        except ValueError:
            await ctx.send(_("That doesn't look like a valid message ID."))
            return

        stored_emoji = emoji.replace("\uFE0F", "")

        query = """
            DELETE FROM reaction_roles
            WHERE message_id = $1 AND emoji = $2;
            """

        await self.bot.db_pool.execute(query, mid, stored_emoji)

        self.cache.pop((mid, stored_emoji), None)

        embed = discord.Embed(
            title=_("Reaction role removed"),
            colour=random_colour(),
        )
        embed.add_field(name=_("Message"), value=f"`{mid}`")
        embed.add_field(name=_("Emoji"), value=emoji)
        await ctx.send(embed=embed)

    @reactionrole.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_list(self, ctx):
        """List all reaction-role mappings for this guild."""

        query = """
            SELECT message_id, emoji, role_id FROM reaction_roles
            WHERE guild_id = $1;
            """

        rows = await self.bot.db_pool.fetch(query, ctx.guild.id)

        if not rows:
            embed = discord.Embed(
                title=_("Reaction roles"),
                description=_("No reaction roles have been set up for this guild."),
                colour=random_colour(),
            )
            await ctx.send(embed=embed)
            return

        lines = [
            _("Message `{mid}` - {emoji} -> <@&{role}>").format(
                mid=row["message_id"], emoji=row["emoji"], role=row["role_id"]
            )
            for row in rows
        ]
        await Paginator(
            paginate_lines(lines, title=_("Reaction roles")), author_id=ctx.author.id
        ).start(ctx)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return

        key = (payload.message_id, str(payload.emoji).replace("\uFE0F", ""))
        rid = self.cache.get(key)

        if not rid:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(rid)

        if role:
            try:
                await payload.member.add_roles(role, reason="Reaction role")
            except Exception:
                log.exception("Failed to add role")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if payload.guild_id is None:
            return

        key = (payload.message_id, str(payload.emoji).replace("\uFE0F", ""))
        rid = self.cache.get(key)

        if not rid:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            member = None
        role = guild.get_role(rid)

        if member and role:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except Exception:
                log.exception("Failed to remove role")


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
