import logging

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)


class UserInfoView(discord.ui.LayoutView):
    """Member profile rendered as a Components V2 layout.

    A coloured container holds a Section whose thumbnail accessory is the
    member's display avatar alongside the identity lines, then (when present)
    a MediaGallery showing the member's banner. This view carries its own
    content, so it is sent with ``view=`` only (no embed, no content) and with
    ``allowed_mentions`` suppressed because the TextDisplay resolves mentions.
    """

    def __init__(self, member: discord.Member, *, banner_url: str = None):
        super().__init__(timeout=None)

        container = discord.ui.Container(accent_colour=random_colour())

        created = "{full} ({rel})".format(
            full=discord.utils.format_dt(member.created_at, "F"),
            rel=discord.utils.format_dt(member.created_at, "R"),
        )
        if member.joined_at is not None:
            joined = discord.utils.format_dt(member.joined_at, "F")
        else:
            joined = _("Unknown")

        lines = [
            _("## {member}").format(member=member),
            _("**Display name:** {name}").format(name=member.display_name),
            _("**ID:** `{id}`").format(id=member.id),
            _("**Mention:** {mention}").format(mention=member.mention),
            _("**Account created:** {created}").format(created=created),
            _("**Joined server:** {joined}").format(joined=joined),
            _("**Top role:** {role}").format(role=member.top_role.mention),
            _("**Role count:** {count}").format(count=len(member.roles) - 1),
            _("**Is bot:** {value}").format(
                value=_("Yes") if member.bot else _("No")
            ),
        ]

        section = discord.ui.Section(
            discord.ui.TextDisplay("\n".join(lines)),
            accessory=discord.ui.Thumbnail(member.display_avatar.url),
        )
        container.add_item(section)

        if banner_url:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(banner_url)
                )
            )

        self.add_item(container)


class Info(commands.Cog):
    """Informational commands about users, the server and the bot."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="userinfo", aliases=["whois", "ui"])
    @commands.guild_only()
    async def userinfo(self, ctx, member: discord.Member = None):
        """Displays information about a member of the guild."""

        member = member or ctx.author

        # Banners require a REST fetch; show it and opportunistically archive it.
        banner_url = None
        try:
            full = await self.bot.fetch_user(member.id)
            if full.banner:
                banner_url = full.banner.url
            ah = self.bot.get_cog("AvatarHistory")
            if ah:
                await ah.capture_banner(member)
        except Exception:
            log.exception("failed to fetch/capture banner for %s", member.id)

        view = UserInfoView(member, banner_url=banner_url)
        # A LayoutView carries its own content; send it with no embed and no
        # content, and suppress pings since the TextDisplay resolves mentions.
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="serverinfo", aliases=["guildinfo", "si"])
    @commands.guild_only()
    async def serverinfo(self, ctx):
        """Displays information about the current guild."""

        guild = ctx.guild

        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)

        embed = discord.Embed(
            title=_("Server info - {guild}").format(guild=guild.name),
            colour=random_colour(),
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        embed.add_field(name=_("Name"), value=guild.name)
        embed.add_field(name=_("ID"), value=guild.id)
        embed.add_field(
            name=_("Owner"),
            value=guild.owner.mention if guild.owner else _("Unknown"),
        )
        embed.add_field(
            name=_("Created"),
            value=discord.utils.format_dt(guild.created_at, "F"),
            inline=False,
        )
        embed.add_field(name=_("Members"), value=guild.member_count)
        embed.add_field(name=_("Text channels"), value=text_channels)
        embed.add_field(name=_("Voice channels"), value=voice_channels)
        embed.add_field(name=_("Roles"), value=len(guild.roles))
        embed.add_field(
            name=_("Boost tier"),
            value=_("Tier {tier}").format(tier=guild.premium_tier),
        )
        embed.add_field(name=_("Boosts"), value=guild.premium_subscription_count)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="avatar", aliases=["av", "pfp"])
    async def avatar(self, ctx, member: discord.Member = None):
        """Displays the avatar of a member."""

        member = member or ctx.author

        embed = discord.Embed(
            title=_("Avatar - {member}").format(member=member),
            colour=random_colour(),
        )
        embed.set_image(url=member.display_avatar.url)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="ping")
    async def ping(self, ctx):
        """Shows the bot's websocket latency."""

        embed = discord.Embed(
            title=_("Pong!"),
            description=_("Latency: **{ms} ms**").format(
                ms=round(self.bot.latency * 1000)
            ),
            colour=random_colour(),
        )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="botinfo", aliases=["about", "info"])
    async def botinfo(self, ctx):
        """Displays information about the bot."""

        total_users = sum(
            g.member_count for g in self.bot.guilds if g.member_count is not None
        )

        embed = discord.Embed(
            title=_("Bot info"),
            colour=random_colour(),
        )
        embed.add_field(name=_("Servers"), value=len(self.bot.guilds))
        embed.add_field(name=_("Users"), value=total_users)
        embed.add_field(name="discord.py", value=discord.__version__)
        embed.add_field(
            name=_("Websocket latency"),
            value=_("{ms} ms").format(ms=round(self.bot.latency * 1000)),
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Info(bot))
